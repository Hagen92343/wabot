"""Media domain — pure, no I/O.

Phase-7 C7.1 groundwork: classifies incoming WhatsApp media messages
into supported kinds (image / audio / document) and unsupported kinds
(video / location / sticker / contact) per Spec §9, and defines the
size / MIME-type validation used by :class:`whatsbot.application.media_service`.

The split between *supported* and *unsupported* drives the HTTP webhook
dispatch: supported kinds go through the download → validate → cache →
forward-to-Claude pipeline, unsupported kinds get a friendly explain-
message reply (never a silent drop, never a crash).

All constants and functions here are pure — no network, no filesystem,
no clock access. Adapters in :mod:`whatsbot.adapters` handle the I/O
side.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class MediaKind(StrEnum):
    """Every WhatsApp media type we currently recognise.

    ``UNKNOWN`` is a catch-all for future Meta payload types so the
    webhook never crashes on a new message kind — it gets a generic
    reject reply until we add proper support.
    """

    IMAGE = "image"
    AUDIO = "audio"
    DOCUMENT = "document"
    VIDEO = "video"
    LOCATION = "location"
    STICKER = "sticker"
    CONTACT = "contact"
    UNKNOWN = "unknown"


SUPPORTED_KINDS: Final[frozenset[MediaKind]] = frozenset(
    {MediaKind.IMAGE, MediaKind.AUDIO, MediaKind.DOCUMENT}
)
"""Media kinds with a real inbound pipeline (Spec §16)."""


UNSUPPORTED_KINDS: Final[frozenset[MediaKind]] = frozenset(
    {
        MediaKind.VIDEO,
        MediaKind.LOCATION,
        MediaKind.STICKER,
        MediaKind.CONTACT,
    }
)
"""Media kinds we explicitly reject with a friendly message (Spec §9)."""


# Per-kind byte caps (Spec §16). Slightly generous on audio — WhatsApp
# voice-notes are typically < 500 KB at normal speech length, but
# forwarded voice messages can be chunked up to roughly the limit here
# and we want to transcribe them rather than reject for size alone.
MAX_BYTES_PER_KIND: Final[dict[MediaKind, int]] = {
    MediaKind.IMAGE: 10 * 1024 * 1024,  # 10 MB
    MediaKind.DOCUMENT: 20 * 1024 * 1024,  # 20 MB
    MediaKind.AUDIO: 25 * 1024 * 1024,  # 25 MB
}


# Permitted MIME types per kind. WhatsApp hands us the MIME from the
# sender's device — we trust it enough to use for routing, but the
# magic-bytes check in :mod:`whatsbot.domain.magic_bytes` is what
# actually gates content.
ALLOWED_MIMES_PER_KIND: Final[dict[MediaKind, frozenset[str]]] = {
    MediaKind.IMAGE: frozenset(
        {
            "image/jpeg",
            "image/png",
            "image/webp",
            "image/heic",
            "image/heif",
        }
    ),
    MediaKind.DOCUMENT: frozenset({"application/pdf"}),
    MediaKind.AUDIO: frozenset(
        {
            "audio/ogg",
            "audio/opus",
            "audio/mp4",
            "audio/mpeg",
            "audio/wav",
            "audio/x-wav",
            "audio/webm",
        }
    ),
}


class MediaValidationError(ValueError):
    """Raised when an inbound media payload fails a validation rule.

    Carries a ``reason`` string that is safe to show to the user in a
    WhatsApp reply — no sensitive detail, just the category.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def classify_meta_kind(meta_type: str | None) -> MediaKind:
    """Map Meta's webhook ``messages[].type`` field to our enum.

    Unknown / None values fall into ``MediaKind.UNKNOWN``; the webhook
    dispatch turns that into a generic reject rather than crashing.
    """
    if not isinstance(meta_type, str):
        return MediaKind.UNKNOWN
    match meta_type.strip().lower():
        case "image":
            return MediaKind.IMAGE
        case "audio" | "voice":
            return MediaKind.AUDIO
        case "document":
            return MediaKind.DOCUMENT
        case "video":
            return MediaKind.VIDEO
        case "location":
            return MediaKind.LOCATION
        case "sticker":
            return MediaKind.STICKER
        case "contacts" | "contact":
            return MediaKind.CONTACT
        case _:
            return MediaKind.UNKNOWN


def validate_size(kind: MediaKind, bytes_count: int) -> None:
    """Raise :class:`MediaValidationError` if ``bytes_count`` exceeds the
    per-kind limit.

    Non-supported kinds are rejected outright — :func:`validate_size`
    isn't meant to be called on them, the HTTP layer routes those to
    ``process_unsupported`` before any download happens.
    """
    if bytes_count < 0:
        raise MediaValidationError("negative Groesse")
    max_bytes = MAX_BYTES_PER_KIND.get(kind)
    if max_bytes is None:
        raise MediaValidationError(f"Kind {kind.value!r} hat kein Groessen-Budget")
    if bytes_count > max_bytes:
        raise MediaValidationError(
            f"{kind.value} zu gross: {bytes_count} Bytes > Limit {max_bytes} Bytes"
        )


def validate_mime(kind: MediaKind, mime: str | None) -> None:
    """Raise :class:`MediaValidationError` if ``mime`` is not on the
    allow-list for ``kind``.

    Matches case-insensitively and ignores any ``; charset=...`` or
    similar parameters Meta occasionally appends.
    """
    allowed = ALLOWED_MIMES_PER_KIND.get(kind)
    if allowed is None:
        raise MediaValidationError(f"Kind {kind.value!r} hat keine MIME-Allow-List")
    if not isinstance(mime, str) or not mime.strip():
        raise MediaValidationError(f"{kind.value}: MIME-Type fehlt")
    base = mime.split(";", 1)[0].strip().lower()
    if base not in allowed:
        raise MediaValidationError(f"{kind.value}: MIME {mime!r} nicht erlaubt")


def suffix_for_mime(kind: MediaKind, mime: str) -> str:
    """Pick a filesystem-safe suffix for the cache filename.

    Called by the adapter when persisting a downloaded media blob.
    Unknown MIME falls back to a per-kind default — this only matters
    for local filename readability, content is validated separately
    via magic bytes.
    """
    base = mime.split(";", 1)[0].strip().lower()
    match base:
        case "image/jpeg":
            return ".jpg"
        case "image/png":
            return ".png"
        case "image/webp":
            return ".webp"
        case "image/heic" | "image/heif":
            return ".heic"
        case "application/pdf":
            return ".pdf"
        case "audio/ogg" | "audio/opus":
            return ".ogg"
        case "audio/mp4":
            return ".m4a"
        case "audio/mpeg":
            return ".mp3"
        case "audio/wav" | "audio/x-wav":
            return ".wav"
        case "audio/webm":
            return ".webm"
        case _:
            return _FALLBACK_SUFFIX.get(kind, ".bin")


_FALLBACK_SUFFIX: Final[dict[MediaKind, str]] = {
    MediaKind.IMAGE: ".img",
    MediaKind.DOCUMENT: ".pdf",
    MediaKind.AUDIO: ".audio",
}
