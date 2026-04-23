"""MediaService — orchestrates the inbound media pipeline (Phase 7).

Flow for *supported* kinds (image / pdf / audio):

1. Download the blob via :class:`MediaDownloader`.
2. Validate size + MIME + magic-bytes (:mod:`whatsbot.domain.media`,
   :mod:`whatsbot.domain.magic_bytes`).
3. Persist to the file-cache.
4. Build a Claude prompt (``analysiere <path>: <caption>`` for images,
   ``lies <path>: <caption>`` for PDFs, raw transcript for audio —
   audio lands in C7.4).
5. Hand off to :class:`SessionService.send_prompt` on the active project.

The webhook layer routes each incoming media message to the right
``process_*`` method; unsupported kinds (video, location, sticker,
contact) never reach this service — they're rejected with a friendly
reply by :func:`process_unsupported` called from the HTTP layer.

Shipped: C7.1 (image + unsupported rejects), C7.2 (PDF). Audio lands
in C7.3 (ffmpeg convert) + C7.4 (whisper transcribe); the
``process_audio`` method will reuse the same download/validate/cache
skeleton as image and PDF.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from whatsbot.application.active_project_service import ActiveProjectService
from whatsbot.application.session_service import SessionService
from whatsbot.domain.magic_bytes import (
    looks_like_audio,
    looks_like_image,
    looks_like_pdf,
)
from whatsbot.domain.media import (
    MediaKind,
    MediaValidationError,
    suffix_for_mime,
    validate_mime,
    validate_size,
)
from whatsbot.logging_setup import get_logger
from whatsbot.ports.audio_converter import AudioConversionError, AudioConverter
from whatsbot.ports.media_cache import MediaCache
from whatsbot.ports.media_downloader import MediaDownloader, MediaDownloadError


@dataclass(frozen=True, slots=True)
class MediaOutcome:
    """Structured result of processing one inbound media message.

    ``kind`` describes what happened:

    * ``"sent"`` — prompt forwarded to Claude.
    * ``"audio_staged"`` — audio downloaded + converted to WAV;
      waiting on C7.4 whisper to turn it into a prompt.
    * ``"no_active_project"`` — no ``/p`` set, user must pick one.
    * ``"validation_failed"`` — size / MIME / magic-bytes reject.
    * ``"download_failed"`` — network or Meta-side error.
    * ``"conversion_failed"`` — ffmpeg couldn't produce a valid WAV.
    * ``"unsupported"`` — kind is video/sticker/location/contact/unknown.

    ``reply`` is the user-facing WhatsApp text the webhook should send
    back. For ``sent`` outcomes it's a short acknowledgement; for
    failures it's the friendly explain-message.

    ``wav_path`` is only set on ``audio_staged`` outcomes and points
    at the converted 16 kHz mono WAV file inside the media cache.
    """

    kind: str
    reply: str
    cache_path: Path | None = None
    project: str | None = None
    wav_path: Path | None = None


class MediaService:
    """Inbound media → Claude-prompt pipeline."""

    def __init__(
        self,
        *,
        downloader: MediaDownloader,
        cache: MediaCache,
        active_project: ActiveProjectService,
        session_service: SessionService,
        audio_converter: AudioConverter | None = None,
    ) -> None:
        self._downloader = downloader
        self._cache = cache
        self._active = active_project
        self._sessions = session_service
        self._audio_converter = audio_converter
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
        return self._process_supported(
            kind=MediaKind.DOCUMENT,
            media_id=media_id,
            caption=caption,
            sender=sender,
            magic_check=looks_like_pdf,
            prompt_builder=_build_pdf_prompt,
        )

    def process_audio_to_wav(
        self, *, media_id: str, mime: str | None, sender: str
    ) -> MediaOutcome:
        """Phase-7 C7.3 Stage 1 — download + validate + cache + convert.

        Whisper (C7.4) plugs onto this: it will read the returned
        ``wav_path`` and turn the transcript into a prompt. Until then
        the webhook doesn't route audio here — this method exists so
        C7.4 can layer on without refactoring C7.3's code paths.

        Pipeline:

        1. Guard an active project (so conversion isn't wasted).
        2. Download the blob via :class:`MediaDownloader`.
        3. Validate MIME (``audio/*`` allow-list), size (Spec §16
           25 MB cap), and magic bytes (OGG/MP3/MP4/WAV/WebM).
        4. Cache the source blob under its original suffix.
        5. Convert via the injected :class:`AudioConverter` to a
           16 kHz mono WAV at ``<cache>/<media_id>.wav``.
        6. Return ``MediaOutcome(kind="audio_staged", ...)``.

        Every failure produces a distinct ``kind`` so C7.4 / tests can
        branch cleanly. No exception escapes.
        """
        if self._audio_converter is None:
            # Defensive — normal wiring provides one; a misconfigured
            # bot hits this path and we reply with a neutral message
            # rather than crash.
            return MediaOutcome(
                kind="conversion_failed",
                reply="⚠️ Audio-Konverter ist gerade nicht konfiguriert.",
            )

        project = self._active.get_active()
        if project is None:
            self._log.warning(
                "media_no_active_project",
                kind=MediaKind.AUDIO.value,
                media_id=media_id,
                sender=sender,
            )
            return MediaOutcome(
                kind="no_active_project",
                reply=(
                    "⚠️ Kein aktives Projekt. "
                    "Setze eins mit /p <name> und schick die Voice erneut."
                ),
            )

        try:
            downloaded = self._downloader.download(media_id)
        except MediaDownloadError as exc:
            self._log.warning(
                "media_download_failed",
                kind=MediaKind.AUDIO.value,
                media_id=media_id,
                reason=exc.reason,
            )
            return MediaOutcome(
                kind="download_failed",
                reply="⚠️ Download fehlgeschlagen. Versuch's nochmal.",
                project=project,
            )

        # Prefer Meta's MIME; fall back to what the caller supplied if
        # Graph didn't return one. Final arbiter is the magic-bytes check.
        effective_mime = downloaded.mime or (mime or "")
        try:
            validate_mime(MediaKind.AUDIO, effective_mime)
            validate_size(MediaKind.AUDIO, len(downloaded.payload))
        except MediaValidationError as exc:
            self._log.warning(
                "media_validation_failed",
                kind=MediaKind.AUDIO.value,
                media_id=media_id,
                reason=exc.reason,
            )
            return MediaOutcome(
                kind="validation_failed",
                reply=f"⚠️ {exc.reason}",
                project=project,
            )
        if not looks_like_audio(downloaded.payload, effective_mime):
            self._log.warning(
                "media_magic_bytes_mismatch",
                kind=MediaKind.AUDIO.value,
                media_id=media_id,
                mime=effective_mime,
            )
            return MediaOutcome(
                kind="validation_failed",
                reply="⚠️ audio: Inhalt passt nicht zum MIME-Type.",
                project=project,
            )

        source_suffix = suffix_for_mime(MediaKind.AUDIO, effective_mime)
        source_path = self._cache.store(
            media_id, downloaded.payload, source_suffix
        )
        wav_path = self._cache.path_for(media_id, ".wav")

        try:
            self._audio_converter.to_wav_16k_mono(source_path, wav_path)
        except AudioConversionError as exc:
            self._log.warning(
                "audio_conversion_failed",
                media_id=media_id,
                project=project,
                reason=exc.reason,
            )
            return MediaOutcome(
                kind="conversion_failed",
                reply="⚠️ Konvertierung fehlgeschlagen. Versuch's nochmal.",
                project=project,
                cache_path=source_path,
            )

        self._log.info(
            "audio_staged",
            project=project,
            media_id=media_id,
            source_path=str(source_path),
            wav_path=str(wav_path),
        )
        return MediaOutcome(
            kind="audio_staged",
            reply="🎙 Transkribiere…",
            project=project,
            cache_path=source_path,
            wav_path=wav_path,
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
