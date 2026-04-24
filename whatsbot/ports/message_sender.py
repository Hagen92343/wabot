"""MessageSender port — outbound WhatsApp message abstraction.

Two concrete implementations live in :mod:`whatsbot.adapters.whatsapp_sender`:

* ``LoggingMessageSender`` — dev/test sender that writes a structured log
  line instead of contacting Meta. Never raises.
* ``WhatsAppCloudSender`` — production sender against Meta Graph
  ``/<phone_number_id>/messages``. May raise :class:`MessageSendError`
  on unrecoverable failures (4xx, exhausted retries after 3 tenacity
  attempts, or :class:`~whatsbot.adapters.resilience.CircuitOpenError`
  when the ``meta_send`` breaker is OPEN). Transient transport errors
  are retried inside the adapter; only final failures surface.
"""

from __future__ import annotations

from typing import Protocol


class MessageSendError(RuntimeError):
    """Raised when an outbound WhatsApp message could not be delivered.

    Carries a ``reason`` that is safe to log. Like
    :class:`whatsbot.ports.media_downloader.MediaDownloadError`, it is
    not meant to be shown directly to the user — the webhook layer
    renders generic replies so upstream detail does not leak.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MessageSender(Protocol):
    """Send a single text message to a WhatsApp recipient.

    Implementations should be safe to call from inside an async request
    handler (no blocking work beyond a short-lived HTTP POST). On
    unrecoverable failure they raise :class:`MessageSendError` or a
    subclass so outer decorators (metrics, circuit-breaker) can observe
    the failure rather than silently swallow it.
    """

    def send_text(self, *, to: str, body: str) -> None: ...
