"""RedactingMessageSender — outbound-side decorator for Spec §10 redaction.

Wraps any ``MessageSender`` and routes every body through
``domain.redaction.redact`` before delegating to the inner adapter.
Applied globally in ``main.py`` so all outgoing paths (command replies,
hook confirmation prompts, PIN-resolve acknowledgements, future kill
/stop notifications) get redaction for free.

Never raises on redaction failures: redaction is pure over strings and
has no external dependencies, but if the future brings a more ambitious
pipeline we still want the message to go out — the failure mode of
"bot went silent" is worse than "bot sent an unredacted line".
"""

from __future__ import annotations

from whatsbot.domain.redaction import redact
from whatsbot.logging_setup import get_logger
from whatsbot.ports.message_sender import MessageSender


class RedactingMessageSender:
    """Decorator that scrubs outgoing bodies through the redaction pipeline."""

    def __init__(self, inner: MessageSender) -> None:
        self._inner = inner
        self._log = get_logger("whatsbot.sender.redacting")

    def send_text(self, *, to: str, body: str) -> None:
        result = redact(body)
        if result.hits:
            labels = sorted({h.label for h in result.hits})
            self._log.info(
                "outbound_redacted",
                hit_count=len(result.hits),
                labels=labels,
                original_len=len(body),
                scrubbed_len=len(result.text),
            )
        self._inner.send_text(to=to, body=result.text)
