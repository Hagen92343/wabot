"""Media-downloader port — fetch a WhatsApp media blob by Meta media_id.

Phase-7 C7.1 introduces the port so :class:`whatsbot.application.media_service`
can be unit-tested with an in-memory fake. The real adapter
(:mod:`whatsbot.adapters.meta_media_downloader`) is a two-step dance
against Meta Graph:

1. ``GET https://graph.facebook.com/<ver>/<media_id>`` → JSON with a
   short-lived ``url`` field.
2. ``GET <url>`` → raw bytes + ``Content-Type``.

Both calls carry the long-lived Keychain ``meta-access-token`` as a
Bearer. The adapter handles retries and timeouts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class MediaDownloadError(RuntimeError):
    """Raised when the download could not be completed.

    Carries a ``reason`` that's safe to log but — unlike
    :class:`whatsbot.domain.media.MediaValidationError` — not meant
    to be shown directly to the user. The webhook layer renders a
    generic "Download fehlgeschlagen" reply so upstream detail doesn't
    leak.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    """Raw media blob plus Meta-reported metadata.

    ``sha256`` is computed by the adapter so the application layer
    doesn't need to read the payload twice. Useful for de-duplication
    and audit logs.
    """

    payload: bytes
    mime: str
    sha256: str


class MediaDownloader(Protocol):
    """Fetch a media blob by Meta ``media_id``."""

    def download(self, media_id: str) -> DownloadedMedia:
        """Return the downloaded bytes + MIME + SHA-256.

        Raises :class:`MediaDownloadError` on any failure (network,
        HTTP >= 400, missing ``url`` field, empty body).
        """
