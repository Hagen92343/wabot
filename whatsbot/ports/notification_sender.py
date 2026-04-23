"""Notification port — push alerts to the local user's machine.

Phase 6 uses this for the ``/panic`` confirmation banner and (later)
watchdog kicks. macOS has ``osascript -e 'display notification ...'``
out of the box; Linux dev boxes get the no-op fallback so tests
and CI don't blow up.

Intentionally narrow: title + body + optional sound. No actions, no
images — that's a richer integration that doesn't earn its weight
for a single-user emergency notifier.
"""

from __future__ import annotations

from typing import Protocol


class NotificationSender(Protocol):
    """Local-machine notification interface."""

    def send(self, *, title: str, body: str, sound: bool = False) -> None:
        """Display ``title`` / ``body``. Failures are best-effort —
        the implementation should swallow + log instead of raising,
        so a broken notifier never aborts the panic flow."""
