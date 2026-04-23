"""MetricsMessageSender — wraps any :class:`MessageSender` and
increments the outbound counter per successful send.

Phase 8 C8.4. Kept as a thin adapter so the counter logic sits
next to the other observability helpers instead of polluting the
webhook or the RedactingSender.
"""

from __future__ import annotations

from whatsbot.http.metrics import MetricsRegistry
from whatsbot.ports.message_sender import MessageSender


class MetricsMessageSender:
    """Decorator that bumps ``whatsbot_messages_total{direction=out}``
    on every :meth:`send_text` that returns normally. Failures are
    *not* counted (a delivery we didn't achieve isn't a send).
    """

    def __init__(
        self, *, inner: MessageSender, registry: MetricsRegistry
    ) -> None:
        self._inner = inner
        self._registry = registry

    def send_text(self, *, to: str, body: str) -> None:
        self._inner.send_text(to=to, body=body)
        self._registry.increment(
            "whatsbot_messages_total",
            labels={"direction": "out", "kind": "text"},
            help_text="Inbound + outbound message counters",
        )
