"""LimitService — bookkeeping for the three Claude usage limits.

Phase-8 C8.1 wires up what Phase-4 only surfaced: each
``UsageLimitEvent`` from the transcript parser becomes a persisted
``max_limits`` row. The send-prompt path calls
:meth:`check_guard` so a WhatsApp prompt during an active reset
window gets an immediate "⏸ Max-Limit erreicht" reply instead of
being queued or silently forwarded (Spec §14 "Sofort ablehnen,
keine Queue").

The proactive <10 % warning runs on a lifespan task (see
:mod:`whatsbot.application.max_limit_sweeper`) — this service is
stateless beyond the DB + clock injection so tests can drive it
synchronously.
"""

from __future__ import annotations

from collections.abc import Callable

from whatsbot.domain.limits import (
    LimitKind,
    MaxLimit,
    format_reset_duration,
    is_active,
    shortest_active,
    should_warn,
)
from whatsbot.domain.limits import parse_reset_at as parse_reset_at_domain
from whatsbot.domain.transcript import UsageLimitEvent
from whatsbot.logging_setup import get_logger
from whatsbot.ports.max_limits_repository import MaxLimitsRepository
from whatsbot.ports.message_sender import MessageSender

# Fallback window when Claude surfaces a usage-limit event without a
# parseable ``reset_at``. One hour is the shortest meaningful pause
# we'd want on Spec §14's "keine Queue" principle — long enough to
# avoid a retry storm, short enough that a genuine reset lands the
# user back in business on their next ping.
_DEFAULT_WINDOW_SECONDS: int = 3600


class MaxLimitActiveError(RuntimeError):
    """Raised by :meth:`LimitService.check_guard` when the caller
    (typically :meth:`SessionService.send_prompt`) should abort with
    a user-facing "⏸ Max-Limit" reply instead of routing to Claude."""

    def __init__(self, limit: MaxLimit) -> None:
        super().__init__(
            f"max-limit active: {limit.kind.value} reset_at={limit.reset_at_ts}"
        )
        self.limit = limit


class LimitService:
    """Use-case glue between TranscriptIngest, SessionService, and
    the proactive-warning lifespan task."""

    def __init__(
        self,
        *,
        repo: MaxLimitsRepository,
        sender: MessageSender,
        default_recipient: str | None,
        clock: Callable[[], int] = lambda: _now_default(),
    ) -> None:
        self._repo = repo
        self._sender = sender
        self._default_recipient = default_recipient
        self._clock = clock
        self._log = get_logger("whatsbot.limits")

    # ---- record path (called from TranscriptIngest) ------------------

    def record(self, project_name: str, event: UsageLimitEvent) -> None:
        """Persist a fresh :class:`UsageLimitEvent` into ``max_limits``.

        Idempotent: a repeated event for the same kind just updates
        ``reset_at_ts`` + ``remaining_pct``. ``warned_at_ts`` survives
        upserts because Claude typically re-emits the event every
        few turns while the window is still open — we don't want to
        lose the "already warned" mark.
        """
        kind = _kind_from_event(event)
        reset_ts = parse_reset_at_domain(event.reset_at)
        if reset_ts is None:
            reset_ts = self._clock() + _DEFAULT_WINDOW_SECONDS
            self._log.warning(
                "limit_reset_missing_defaulted",
                project=project_name,
                kind=kind.value,
                default_window_seconds=_DEFAULT_WINDOW_SECONDS,
            )
        existing = self._repo.get(kind)
        warned_at = existing.warned_at_ts if existing is not None else None
        # The event doesn't carry ``remaining_pct`` today; we preserve
        # whatever prior value we had so a subsequent /metrics read
        # still reflects the last-known percentage.
        remaining = existing.remaining_pct if existing is not None else -1.0
        limit = MaxLimit(
            kind=kind,
            reset_at_ts=reset_ts,
            warned_at_ts=warned_at,
            remaining_pct=remaining,
        )
        self._repo.upsert(limit)
        self._log.info(
            "limit_recorded",
            project=project_name,
            kind=kind.value,
            reset_at_ts=reset_ts,
            remaining_pct=remaining,
        )

    def update_remaining(
        self, kind: LimitKind, remaining_pct: float
    ) -> None:
        """Set the ``remaining_pct`` on the row for ``kind`` (no-op
        if no row is present). Used when C8.4 metrics come on-line
        and the ingest starts surfacing percentages."""
        existing = self._repo.get(kind)
        if existing is None:
            return
        clamped = max(0.0, min(1.0, remaining_pct))
        self._repo.upsert(
            MaxLimit(
                kind=kind,
                reset_at_ts=existing.reset_at_ts,
                warned_at_ts=existing.warned_at_ts,
                remaining_pct=clamped,
            )
        )

    # ---- guard path (called from SessionService.send_prompt) ---------

    def check_guard(self, project_name: str) -> None:
        """Raise :class:`MaxLimitActiveError` iff any limit is still
        in its reset window. Returns normally otherwise.

        Uses :func:`shortest_active` so the reply names the closest
        reset — least surprising to the user (Spec §14 "Bei mehreren
        aktiv: kürzester Countdown in Antwort")."""
        limits = self._repo.list_all()
        now = self._clock()
        active = shortest_active(limits, now=now)
        if active is None:
            return
        self._log.info(
            "limit_guard_blocked",
            project=project_name,
            kind=active.kind.value,
            reset_at_ts=active.reset_at_ts,
            seconds_remaining=active.reset_at_ts - now,
        )
        raise MaxLimitActiveError(active)

    # ---- warning path (called from MaxLimitSweeper) ------------------

    def maybe_warn(self) -> int:
        """Fire one WhatsApp warning per low-remaining window.

        Returns the number of warnings actually sent (useful for
        tests + metrics). Misconfigured bot without a default
        recipient: silently skips — the warning loop keeps running
        so /status can still surface the state.
        """
        if not self._default_recipient:
            return 0
        now = self._clock()
        fired = 0
        for limit in self._repo.list_all():
            if not should_warn(limit, now=now):
                continue
            reset_str = format_reset_duration(
                limit.reset_at_ts, now=now
            )
            pct = int(limit.remaining_pct * 100)
            body = (
                f"⚠️ Max-Limit [{limit.kind.value}]: noch ~{pct}% · "
                f"Reset in {reset_str}"
            )
            try:
                self._sender.send_text(
                    to=self._default_recipient, body=body
                )
            except Exception as exc:
                self._log.warning(
                    "limit_warn_send_failed",
                    kind=limit.kind.value,
                    error=str(exc),
                )
                continue
            self._repo.mark_warned(limit.kind, now)
            fired += 1
            self._log.info(
                "limit_warn_sent",
                kind=limit.kind.value,
                remaining_pct=limit.remaining_pct,
                reset_at_ts=limit.reset_at_ts,
            )
        return fired

    def sweep_expired(self) -> int:
        """Delete rows whose ``reset_at_ts`` is in the past.

        Returns the number of deleted rows. Not strictly required —
        :meth:`check_guard` filters via :func:`is_active` — but it
        keeps ``/metrics`` and ``/status`` honest by pruning
        fossils."""
        now = self._clock()
        reaped = 0
        for limit in self._repo.list_all():
            if not is_active(limit, now=now):
                if self._repo.delete(limit.kind):
                    reaped += 1
        if reaped:
            self._log.info("limit_expired_reaped", count=reaped)
        return reaped

    # ---- /status helper ---------------------------------------------

    def snapshot(self) -> list[MaxLimit]:
        """All current rows, ordered by reset (earliest first). Used
        by the upcoming C8.2 ``/status`` and ``/ps``."""
        return self._repo.list_all()


def _kind_from_event(event: UsageLimitEvent) -> LimitKind:
    """Map the event's raw ``limit_kind`` string to the enum.

    Unknown / missing values default to :attr:`LimitKind.SESSION_5H`
    — that's the smallest / most-common window and the safest
    assumption when Claude didn't tell us more.
    """
    raw = (event.limit_kind or "").strip().lower()
    match raw:
        case "session_5h" | "session" | "5h" | "session-5h":
            return LimitKind.SESSION_5H
        case "weekly" | "week":
            return LimitKind.WEEKLY
        case "opus_sub" | "opus" | "opus-sub" | "opus_subscription":
            return LimitKind.OPUS_SUB
        case _:
            return LimitKind.SESSION_5H


def _now_default() -> int:
    # Defined as a module-level helper so tests can still pass an
    # explicit ``clock=`` in the constructor without paying the
    # ``time.time()`` import tax at module-load time.
    import time

    return int(time.time())
