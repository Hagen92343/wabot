"""MessageSender port — outbound WhatsApp message abstraction.

In Phase 1 the only implementation is the logger-only adapter
(``adapters/whatsapp_sender.LoggingMessageSender``) which is enough for the
echo bot. The real Meta-Cloud-API adapter (``WhatsAppCloudSender``) is also
defined as a skeleton so the wiring can be tested end-to-end once the user
has populated the seven Keychain secrets and Cloudflare tunnel is up.
"""

from __future__ import annotations

from typing import Protocol


class MessageSender(Protocol):
    """Send a single text message to a WhatsApp recipient.

    Implementations should be safe to call from inside an async request
    handler (no blocking work) and must NOT raise on transient transport
    errors — those should be retried/queued by the adapter, not surfaced to
    the webhook handler.
    """

    def send_text(self, *, to: str, body: str) -> None: ...
