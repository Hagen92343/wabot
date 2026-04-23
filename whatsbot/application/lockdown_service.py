"""LockdownService — persists the Spec §7 lockdown flag.

Two storage layers, both kept in sync:

1. ``app_state.lockdown`` — JSON-serialized ``LockdownState``. The bot
   reads this on startup and the CommandHandler reads it before each
   command. Survives bot crashes and reboots.
2. Touch-file at ``settings.panic_marker_path`` (default
   ``/tmp/whatsbot-PANIC``). The watchdog (a separate process, no
   DB handle) checks this file's existence to know whether to back
   off — see C6.4. The file is deliberately ephemeral (``/tmp`` gets
   wiped on reboot): if both the bot and the watchdog die hard, a
   fresh boot sees no marker and recovers normally.

Disengage is PIN-gated — the C6.6 ``/unlock`` command. The PIN check
itself stays in the CommandHandler layer (re-uses the
``DeleteService`` / ``ForceService`` pattern); ``LockdownService`` just
exposes a plain ``disengage()`` so the panic-flow + PIN-flow + future
watchdog-clear flow can all share it.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from whatsbot.domain.lockdown import (
    LockdownState,
    disengaged,
    engage,
)
from whatsbot.logging_setup import get_logger
from whatsbot.ports.app_state_repository import (
    KEY_LOCKDOWN,
    AppStateRepository,
)


def _serialize(state: LockdownState) -> str:
    """Serialize the lockdown state to a JSON blob for ``app_state``."""
    return json.dumps(
        {
            "engaged": state.engaged,
            "engaged_at": (
                state.engaged_at.isoformat()
                if state.engaged_at is not None
                else None
            ),
            "reason": state.reason,
            "engaged_by": state.engaged_by,
        },
        separators=(",", ":"),
    )


def _deserialize(raw: str) -> LockdownState:
    """Inverse of ``_serialize``. Tolerates missing fields by
    defaulting them — old bot versions might have written less."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return disengaged()
    if not isinstance(data, dict) or not data.get("engaged"):
        return disengaged()
    engaged_at_raw = data.get("engaged_at")
    engaged_at = None
    if isinstance(engaged_at_raw, str):
        with contextlib.suppress(ValueError):
            engaged_at = datetime.fromisoformat(engaged_at_raw)
    return LockdownState(
        engaged=True,
        engaged_at=engaged_at,
        reason=data.get("reason"),
        engaged_by=data.get("engaged_by"),
    )


class LockdownService:
    """High-level engage / disengage / current operations."""

    def __init__(
        self,
        *,
        app_state: AppStateRepository,
        panic_marker_path: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._app_state = app_state
        self._marker = panic_marker_path
        self._clock = clock
        self._log = get_logger("whatsbot.lockdown")

    # ---- read --------------------------------------------------------

    def current(self) -> LockdownState:
        """Return the persisted state. ``disengaged()`` if no row."""
        raw = self._app_state.get(KEY_LOCKDOWN)
        if raw is None:
            return disengaged()
        return _deserialize(raw)

    def is_engaged(self) -> bool:
        return self.current().engaged

    # ---- engage ------------------------------------------------------

    def engage(
        self, *, reason: str, engaged_by: str | None = None
    ) -> LockdownState:
        """Engage lockdown if not already. Writes both the DB row and
        the touch-file. Touch-file errors are logged but don't block
        the DB write — the DB is the source of truth, the touch-file
        is just the watchdog's mailbox.
        """
        new_state = engage(
            self.current(),
            now=self._clock(),
            reason=reason,
            engaged_by=engaged_by,
        )
        self._app_state.set(KEY_LOCKDOWN, _serialize(new_state))
        self._touch_marker()
        self._log.warning(
            "lockdown_engaged",
            reason=new_state.reason,
            engaged_by=new_state.engaged_by,
            engaged_at=(
                new_state.engaged_at.isoformat()
                if new_state.engaged_at is not None
                else None
            ),
        )
        return new_state

    # ---- disengage --------------------------------------------------

    def disengage(self) -> LockdownState:
        """Clear lockdown. Idempotent — calling on an already-clear
        state is a no-op + warning-free."""
        previous = self.current()
        if not previous.engaged:
            return previous
        cleared = disengaged()
        self._app_state.set(KEY_LOCKDOWN, _serialize(cleared))
        self._remove_marker()
        self._log.info(
            "lockdown_disengaged",
            previous_reason=previous.reason,
            previous_engaged_by=previous.engaged_by,
        )
        return cleared

    # ---- internals --------------------------------------------------

    def _touch_marker(self) -> None:
        try:
            self._marker.parent.mkdir(parents=True, exist_ok=True)
            self._marker.touch(exist_ok=True)
        except OSError as exc:
            # Watchdog falls back on the long-threshold mode; the DB
            # is still authoritative for in-process callers.
            self._log.warning(
                "lockdown_marker_write_failed",
                path=str(self._marker),
                error=str(exc),
            )

    def _remove_marker(self) -> None:
        try:
            self._marker.unlink(missing_ok=True)
        except OSError as exc:
            self._log.warning(
                "lockdown_marker_remove_failed",
                path=str(self._marker),
                error=str(exc),
            )
