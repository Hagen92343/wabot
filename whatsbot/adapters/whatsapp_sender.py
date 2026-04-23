"""Outbound WhatsApp adapters.

``LoggingMessageSender``
    Used in dev/test and as a fallback when Meta credentials are missing.
    Writes a structured log line and does not contact the network. Lets us
    iterate on the bot before WhatsApp Business approval is in place.

``WhatsAppCloudSender`` (skeleton)
    Calls Meta Graph ``/PHONE_NUMBER_ID/messages`` with a Bearer token. The
    full implementation (httpx + tenacity retry + redaction) lands in C2.x
    once project sessions actually need to reply. For now it raises if you
    try to use it without credentials.
"""

from __future__ import annotations

from whatsbot.adapters.resilience import resilient
from whatsbot.logging_setup import get_logger
from whatsbot.ports.message_sender import MessageSender

# Service-name registered in the circuit-breaker registry.
# Kept in module scope so tests and /status can introspect via
# :func:`resilience.get_breaker`.
META_SEND_SERVICE: str = "meta_send"


class LoggingMessageSender:
    """``MessageSender`` that logs the would-be send instead of calling Meta.

    Mirrors the interface of the real adapter so swapping it in
    ``create_app()`` is a single dependency change.
    """

    def __init__(self) -> None:
        self._log = get_logger("whatsbot.sender.logging")

    def send_text(self, *, to: str, body: str) -> None:
        self._log.info(
            "outbound_message_dev",
            to=to,
            body_preview=body[:200],
            body_len=len(body),
        )


class WhatsAppCloudSender:
    """Skeleton — real implementation deferred to C2.x.

    Constructed once at startup with the long-lived Meta access token and
    phone-number-id from Keychain. The actual ``send_text`` will use httpx
    with tenacity-backed retry and the spec-§10 redaction pipeline.
    """

    def __init__(self, *, access_token: str, phone_number_id: str) -> None:
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self._log = get_logger("whatsbot.sender.whatsapp")

    @resilient(META_SEND_SERVICE)
    def send_text(self, *, to: str, body: str) -> None:
        # Intentional: this MUST raise so we don't silently 'succeed' while
        # nothing actually leaves the bot. C2.x replaces with httpx + retry.
        # The @resilient decorator is wired now so C2.x only needs to drop
        # in the httpx call — Spec §25 FMEA #1 protection is already here.
        raise NotImplementedError(
            "WhatsAppCloudSender.send_text is a Phase-1 skeleton. "
            "Use LoggingMessageSender until the real adapter lands in C2.x."
        )


# Re-export the protocol so callers don't need two imports.
__all__ = ["LoggingMessageSender", "MessageSender", "WhatsAppCloudSender"]
