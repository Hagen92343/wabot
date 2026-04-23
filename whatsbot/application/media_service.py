"""MediaService — orchestrates the inbound media pipeline (Phase 7 C7.1).

Flow for *supported* kinds (image / pdf / audio):

1. Download the blob via :class:`MediaDownloader`.
2. Validate size + MIME + magic-bytes (:mod:`whatsbot.domain.media`,
   :mod:`whatsbot.domain.magic_bytes`).
3. Persist to the file-cache.
4. Build a Claude prompt (``analysiere <path>: <caption>`` for images,
   ``lies /path: <caption>`` for PDFs, raw transcript for audio — audio
   lands in C7.4).
5. Hand off to :class:`SessionService.send_prompt` on the active project.

The webhook layer routes each incoming media message to the right
``process_*`` method; unsupported kinds (video, location, sticker,
contact) never reach this service — they're rejected with a friendly
reply by :func:`process_unsupported` called from the HTTP layer.

C7.1 scope: ``process_image`` + ``process_unsupported``. PDFs (C7.2)
and audio (C7.3/C7.4) reuse the same download-validate-cache skeleton.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from whatsbot.application.active_project_service import ActiveProjectService
from whatsbot.application.session_service import SessionService
from whatsbot.domain.magic_bytes import looks_like_image, looks_like_pdf
from whatsbot.domain.media import (
    MediaKind,
    MediaValidationError,
    suffix_for_mime,
    validate_mime,
    validate_size,
)
from whatsbot.logging_setup import get_logger
from whatsbot.ports.media_cache import MediaCache
from whatsbot.ports.media_downloader import MediaDownloader, MediaDownloadError


@dataclass(frozen=True, slots=True)
class MediaOutcome:
    """Structured result of processing one inbound media message.

    ``kind`` describes what happened:

    * ``"sent"`` — prompt forwarded to Claude.
    * ``"no_active_project"`` — no ``/p`` set, user must pick one.
    * ``"validation_failed"`` — size / MIME / magic-bytes reject.
    * ``"download_failed"`` — network or Meta-side error.
    * ``"unsupported"`` — kind is video/sticker/location/contact/unknown.

    ``reply`` is the user-facing WhatsApp text the webhook should send
    back. For ``sent`` outcomes it's a short acknowledgement; for
    failures it's the friendly explain-message.
    """

    kind: str
    reply: str
    cache_path: Path | None = None
    project: str | None = None


class MediaService:
    """Inbound media → Claude-prompt pipeline."""

    def __init__(
        self,
        *,
        downloader: MediaDownloader,
        cache: MediaCache,
        active_project: ActiveProjectService,
        session_service: SessionService,
    ) -> None:
        self._downloader = downloader
        self._cache = cache
        self._active = active_project
        self._sessions = session_service
        self._log = get_logger("whatsbot.media")

    # ---- public API ---------------------------------------------------

    def process_image(
        self, *, media_id: str, caption: str | None, sender: str
    ) -> MediaOutcome:
        return self._process_supported(
            kind=MediaKind.IMAGE,
            media_id=media_id,
            caption=caption,
            sender=sender,
            magic_check=looks_like_image,
            prompt_builder=_build_image_prompt,
        )

    def process_pdf(
        self, *, media_id: str, caption: str | None, sender: str
    ) -> MediaOutcome:
        # Placeholder — C7.2 wires this up; leaving it in C7.1 so the
        # dispatch table is complete and callers don't have to guard.
        return self._process_supported(
            kind=MediaKind.DOCUMENT,
            media_id=media_id,
            caption=caption,
            sender=sender,
            magic_check=looks_like_pdf,
            prompt_builder=_build_pdf_prompt,
        )

    def process_unsupported(self, kind: MediaKind) -> MediaOutcome:
        """Return a friendly reject reply for an unsupported kind.

        Never touches download / cache / session — strictly
        informational. Called by the webhook so the sender learns why
        the bot didn't act on their message.
        """
        reply = _REJECT_REPLIES.get(kind, _REJECT_REPLIES[MediaKind.UNKNOWN])
        self._log.info("media_unsupported", kind=kind.value)
        return MediaOutcome(kind="unsupported", reply=reply)

    # ---- internals ---------------------------------------------------

    def _process_supported(
        self,
        *,
        kind: MediaKind,
        media_id: str,
        caption: str | None,
        sender: str,
        magic_check: Callable[[bytes, str | None], bool],
        prompt_builder: Callable[[Path, str | None], str],
    ) -> MediaOutcome:
        # 1. Active project guard — without it we have nowhere to send
        # the prompt. The user sees a short hint instead.
        project = self._active.get_active()
        if project is None:
            self._log.warning(
                "media_no_active_project",
                kind=kind.value,
                media_id=media_id,
                sender=sender,
            )
            return MediaOutcome(
                kind="no_active_project",
                reply=(
                    "⚠️ Kein aktives Projekt. "
                    "Setze eins mit /p <name> und schick das Medium erneut."
                ),
            )

        # 2. Download.
        try:
            downloaded = self._downloader.download(media_id)
        except MediaDownloadError as exc:
            self._log.warning(
                "media_download_failed",
                kind=kind.value,
                media_id=media_id,
                reason=exc.reason,
            )
            return MediaOutcome(
                kind="download_failed",
                reply="⚠️ Download fehlgeschlagen. Versuch's nochmal.",
                project=project,
            )

        # 3. Validate.
        try:
            validate_mime(kind, downloaded.mime)
            validate_size(kind, len(downloaded.payload))
        except MediaValidationError as exc:
            self._log.warning(
                "media_validation_failed",
                kind=kind.value,
                media_id=media_id,
                reason=exc.reason,
            )
            return MediaOutcome(
                kind="validation_failed",
                reply=f"⚠️ {exc.reason}",
                project=project,
            )
        if not magic_check(downloaded.payload, downloaded.mime):
            self._log.warning(
                "media_magic_bytes_mismatch",
                kind=kind.value,
                media_id=media_id,
                mime=downloaded.mime,
            )
            return MediaOutcome(
                kind="validation_failed",
                reply=f"⚠️ {kind.value}: Inhalt passt nicht zum MIME-Type.",
                project=project,
            )

        # 4. Cache.
        suffix = suffix_for_mime(kind, downloaded.mime)
        cache_path = self._cache.store(media_id, downloaded.payload, suffix)

        # 5. Build prompt + hand off to SessionService.
        prompt = prompt_builder(cache_path, caption)
        try:
            self._sessions.send_prompt(project, prompt)
        except Exception as exc:  # pragma: no cover — defensive
            # send_prompt can raise LocalTerminalHoldsLockError etc.;
            # we log and return a failure outcome rather than crash
            # the webhook.
            self._log.warning(
                "media_send_prompt_failed",
                kind=kind.value,
                project=project,
                error=str(exc),
            )
            return MediaOutcome(
                kind="download_failed",
                reply=f"⚠️ Prompt an '{project}' fehlgeschlagen: {exc}",
                project=project,
                cache_path=cache_path,
            )

        self._log.info(
            "media_forwarded_to_claude",
            kind=kind.value,
            project=project,
            media_id=media_id,
            cache_path=str(cache_path),
            prompt_len=len(prompt),
        )
        short_kind = _KIND_LABEL[kind]
        return MediaOutcome(
            kind="sent",
            reply=f"📨 {short_kind} an '{project}' gesendet.",
            cache_path=cache_path,
            project=project,
        )


# --- reply-text factories -------------------------------------------------


def _build_image_prompt(path: Path, caption: str | None) -> str:
    base = f"analysiere {path}"
    if caption and caption.strip():
        return f"{base}: {caption.strip()}"
    return base


def _build_pdf_prompt(path: Path, caption: str | None) -> str:
    base = f"lies {path}"
    if caption and caption.strip():
        return f"{base}: {caption.strip()}"
    return base


_REJECT_REPLIES: Final[dict[MediaKind, str]] = {
    MediaKind.VIDEO: "🎬 Video wird nicht unterstützt, bitte Screenshot.",
    MediaKind.LOCATION: "📍 Location-Pins werden ignoriert.",
    MediaKind.STICKER: "😄 Nice sticker, aber ich brauche Text/Voice.",
    MediaKind.CONTACT: "📇 Kontaktkarten werden ignoriert.",
    MediaKind.UNKNOWN: "⚠️ Dieser Nachrichtentyp wird nicht unterstützt.",
}


_KIND_LABEL: Final[dict[MediaKind, str]] = {
    MediaKind.IMAGE: "Bild",
    MediaKind.AUDIO: "Voice",
    MediaKind.DOCUMENT: "PDF",
}
