"""ConfirmationCoordinator — in-memory + DB coordination for Hook PIN flow.

Spec §7: when the Pre-Tool-Hook decides to ask the user (``AskUser``
verdict from ``evaluate_bash``), we need to:

1. persist a row in ``pending_confirmations`` (audit + timeout sweep),
2. send a WhatsApp prompt so the user knows what's pending,
3. block the hook HTTP response for up to 5 minutes, awaiting either a
   PIN reply (→ allow) or a ``nein`` reply (→ deny) or a timeout (→ deny).

Steps 1 and 2 are request-scoped, but step 3 requires cross-request
coordination: the hook endpoint (``/hook/bash`` on :8001) suspends on a
``Future``, and the ``/webhook`` handler on :8000 resolves it when the
user's reply arrives. Both apps run in the same process on the same
event loop (``asyncio.gather(meta_server, hook_server)`` in ``main.py``
for production), so a module-level ``Future`` registry works.

Thread safety: we rely on the FastAPI/uvicorn single-threaded event
loop. ``Future.set_result`` is safe from any coroutine on the same
loop, and dict operations are atomic enough for our single-writer
pattern.

Fail-closed: if any persistence call fails, we still return a ``Deny``
decision. The user sees "confirmation failed" instead of silent allow.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from ulid import ULID

from whatsbot.domain.hook_decisions import HookDecision, allow, deny
from whatsbot.domain.pending_confirmations import (
    CONFIRM_WINDOW_SECONDS,
    ConfirmationKind,
    PendingConfirmation,
    compute_deadline,
)
from whatsbot.logging_setup import get_logger
from whatsbot.ports.message_sender import MessageSender
from whatsbot.ports.pending_confirmation_repository import (
    PendingConfirmationRepository,
)

_REJECT_TOKENS: Final = frozenset({"nein", "no"})


@dataclass(frozen=True, slots=True)
class ResolveResult:
    """Output of ``try_resolve`` — the id of the resolved row + outcome."""

    confirmation_id: str
    accepted: bool


@dataclass
class _Pending:
    """Internal per-confirmation coordination slot."""

    confirmation: PendingConfirmation
    future: asyncio.Future[bool]


class ConfirmationCoordinator:
    """Bridges DB persistence and in-memory ``asyncio.Future`` signalling."""

    def __init__(
        self,
        *,
        repo: PendingConfirmationRepository,
        sender: MessageSender,
        default_recipient: str | None = None,
        window_seconds: int = CONFIRM_WINDOW_SECONDS,
    ) -> None:
        self._repo = repo
        self._sender = sender
        self._default_recipient = default_recipient
        self._window = window_seconds
        self._pending: dict[str, _Pending] = {}
        self._log = get_logger("whatsbot.coordinator")

    @property
    def open_count(self) -> int:
        """How many confirmations are awaiting a user answer."""
        return len(self._pending)

    async def ask_bash(
        self,
        *,
        command: str,
        project: str | None,
        reason: str,
        msg_id: str | None = None,
        recipient: str | None = None,
    ) -> HookDecision:
        """Open a Bash confirmation, notify the user, await the reply.

        Returns Allow if the user types the PIN within the window, Deny
        if they type ``nein`` / ``no``, or Deny on timeout.
        """
        to = recipient or self._default_recipient
        loop = asyncio.get_running_loop()
        confirmation_id = str(ULID())
        created_at = datetime.now(UTC)
        deadline_ts = compute_deadline(int(time.time()), self._window)

        confirmation = PendingConfirmation(
            id=confirmation_id,
            kind=ConfirmationKind.HOOK_BASH,
            payload=json.dumps({"command": command, "reason": reason}),
            deadline_ts=deadline_ts,
            created_at=created_at,
            project_name=project,
            msg_id=msg_id,
        )
        future: asyncio.Future[bool] = loop.create_future()

        # Persist first so even on WhatsApp-send failure the sweeper
        # still reaps the row after the window.
        try:
            self._repo.create(confirmation)
        except Exception as exc:
            self._log.exception("confirmation_persist_failed", id=confirmation_id)
            return deny(f"failed to open confirmation: {exc!r}")

        self._pending[confirmation_id] = _Pending(confirmation, future)
        self._log.info(
            "confirmation_opened",
            id=confirmation_id,
            project=project,
            kind=ConfirmationKind.HOOK_BASH.value,
            deadline_ts=deadline_ts,
        )

        # Best-effort WhatsApp notification. A failed send is logged but
        # not fatal — the user can still type the PIN proactively.
        if to is not None:
            try:
                self._sender.send_text(to=to, body=_format_bash_prompt(command, reason, self._window))
            except Exception:
                self._log.exception(
                    "confirmation_notify_failed", id=confirmation_id, to=to
                )
        else:
            self._log.warning("confirmation_no_recipient", id=confirmation_id)

        try:
            accepted = await asyncio.wait_for(future, timeout=self._window)
        except TimeoutError:
            self._log.warning("confirmation_timeout", id=confirmation_id)
            self._cleanup(confirmation_id)
            return deny("confirmation timed out (no answer within window)")
        except asyncio.CancelledError:
            self._log.warning("confirmation_cancelled", id=confirmation_id)
            self._cleanup(confirmation_id)
            raise

        self._cleanup(confirmation_id)
        self._log.info(
            "confirmation_resolved",
            id=confirmation_id,
            accepted=accepted,
        )
        if accepted:
            return allow("user confirmed via PIN")
        return deny("user rejected the request")

    def try_resolve(self, text: str, *, pin: str) -> ResolveResult | None:
        """Route a WhatsApp text to an open confirmation if it matches.

        FIFO: resolves the oldest open confirmation. Returns ``None`` if
        the text isn't a recognised answer or if there's nothing open.
        Safe to call from a sync context (no blocking I/O).
        """
        stripped = text.strip()
        if not stripped:
            return None

        # Constant-time PIN comparison — the PIN is a real secret.
        matches_pin = pin != "" and hmac.compare_digest(
            stripped.encode("utf-8"), pin.encode("utf-8")
        )
        matches_reject = stripped.casefold() in _REJECT_TOKENS

        if not matches_pin and not matches_reject:
            return None

        if not self._pending:
            return None

        # FIFO: oldest first (created_at ascending).
        oldest_id = min(
            self._pending,
            key=lambda cid: self._pending[cid].confirmation.created_at,
        )
        pending = self._pending[oldest_id]

        if pending.future.done():
            # Already resolved from another path (timeout, cancellation).
            # Drop our second answer silently to avoid surprising the user.
            return None

        accepted = matches_pin
        pending.future.set_result(accepted)
        return ResolveResult(confirmation_id=oldest_id, accepted=accepted)

    def _cleanup(self, confirmation_id: str) -> None:
        """Remove the in-memory slot + DB row. Safe to call multiple times."""
        self._pending.pop(confirmation_id, None)
        try:
            self._repo.resolve(confirmation_id)
        except Exception:
            self._log.exception("confirmation_cleanup_failed", id=confirmation_id)


def _format_bash_prompt(command: str, reason: str, window_seconds: int) -> str:
    """Render the WhatsApp prompt the user sees.

    Keeps the command preview short so a pathological 4 KB command
    doesn't blow the WhatsApp 4096-char limit.
    """
    window_min = max(1, window_seconds // 60)
    preview = command if len(command) <= 120 else command[:117] + "..."
    return (
        "⚠️ Claude will ausführen:\n"
        f"`{preview}`\n\n"
        f"Grund: {reason}\n\n"
        "Antworte mit deiner PIN zum Freigeben.\n"
        "Antworte mit 'nein' zum Ablehnen.\n"
        f"Timeout: {window_min} min."
    )
