"""Circuit-breaker + @resilient decorator — Spec §20 resilience.

Phase-8 C8.3 wiring: every external adapter whose downstream can
go flaky (Meta, Whisper, the WhatsApp Cloud API) wraps its
user-facing method with :func:`resilient`. The decorator holds
a per-``service_name`` :class:`CircuitBreaker` at module scope
so multiple instances of the same adapter share one breaker and
a burst of 5 failures in 60 s trips the circuit for everybody.

State machine (Spec §20 + §25 FMEA #1 Meta-API-Outage):

    CLOSED  ───(5 failures in 60 s window)──►  OPEN
    OPEN    ───(5 min cooldown elapsed)────►   HALF_OPEN
    HALF_OPEN ──(probe success)────────────►   CLOSED
    HALF_OPEN ──(probe failure)────────────►   OPEN (new cooldown)

``CLOSED`` passes every call through. ``OPEN`` raises
:class:`CircuitOpenError` *without* touching the wrapped call at
all. ``HALF_OPEN`` lets exactly one probe through per cooldown;
concurrent attempts while a probe is in flight are rejected with
``CircuitOpenError`` so we don't re-stampede the fragile backend.

Thread-safety: all state mutations sit behind ``threading.Lock``.
That covers the sync call path (httpx.Client, subprocess.run).
Async callers reach the decorator via ``asyncio.to_thread`` which
serialises through the thread pool, and the lock keeps the state
consistent across threads. If an async-native call path ever
lands, swap ``threading.Lock`` for ``asyncio.Lock`` in a
per-event-loop variant — but that's outside C8.3.

The decorator logs every state transition so ``/errors`` surfaces
``circuit_opened`` / ``circuit_half_open`` / ``circuit_closed``
events with the service name.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, TypeVar

from whatsbot.logging_setup import get_logger

# Defaults from Spec §20 + §25 FMEA #1.
DEFAULT_FAILURE_THRESHOLD: Final[int] = 5
DEFAULT_WINDOW_SECONDS: Final[float] = 60.0
DEFAULT_COOLDOWN_SECONDS: Final[float] = 5 * 60.0


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised by a ``@resilient`` call when the circuit is OPEN.

    Carries the service name and the timestamp at which the
    circuit will next try a probe, so the CommandHandler can
    render a reopen-countdown to WhatsApp.
    """

    def __init__(self, service_name: str, reopens_at: float) -> None:
        super().__init__(
            f"circuit open: service={service_name} reopens_at={reopens_at}"
        )
        self.service_name = service_name
        self.reopens_at = reopens_at


@dataclass(slots=True)
class _BreakerState:
    """Internal state — mutated only while holding the breaker lock."""

    state: CircuitState = CircuitState.CLOSED
    failure_timestamps: list[float] = field(default_factory=list)
    opened_at: float = 0.0
    reopens_at: float = 0.0
    # True once HALF_OPEN has handed out its single probe — prevents
    # a second concurrent probe from racing through the wrapped call.
    probe_in_flight: bool = False


class CircuitBreaker:
    """Per-service breaker. Thread-safe."""

    def __init__(
        self,
        service_name: str,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if failure_threshold <= 0:
            raise ValueError("failure_threshold must be > 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be > 0")
        self._name = service_name
        self._threshold = failure_threshold
        self._window = window_seconds
        self._cooldown = cooldown_seconds
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._state = _BreakerState()
        self._log = get_logger("whatsbot.resilience")

    # ---- public API -------------------------------------------------

    @property
    def service_name(self) -> str:
        return self._name

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state.state

    @property
    def reopens_at(self) -> float:
        with self._lock:
            return self._state.reopens_at

    def before_call(self) -> None:
        """Pre-flight gate. Raises :class:`CircuitOpenError` when the
        breaker is OPEN (cooldown still running) or HALF_OPEN with a
        probe already in flight.
        """
        now = self._clock()
        transitioned_to: CircuitState | None = None
        with self._lock:
            state = self._state.state
            if state is CircuitState.OPEN:
                if now < self._state.reopens_at:
                    reopens = self._state.reopens_at
                    raise CircuitOpenError(self._name, reopens)
                # Cooldown elapsed — promote to HALF_OPEN + let this
                # call be the probe.
                self._state.state = CircuitState.HALF_OPEN
                self._state.probe_in_flight = True
                transitioned_to = CircuitState.HALF_OPEN
                self._log.warning(
                    "circuit_half_open",
                    service=self._name,
                )
            elif state is CircuitState.HALF_OPEN:
                if self._state.probe_in_flight:
                    raise CircuitOpenError(
                        self._name, self._state.reopens_at
                    )
                # No probe in flight yet — hand this call the role.
                self._state.probe_in_flight = True
            # CLOSED — just pass through.
        if transitioned_to is not None:
            _notify_state(self._name, transitioned_to)

    def record_success(self) -> None:
        """Close the circuit if the probe succeeded; clear counters."""
        with self._lock:
            was_half_open = self._state.state is CircuitState.HALF_OPEN
            # On any success the rolling-window counter resets so a
            # healthy stream of calls can't accumulate old errors.
            self._state.failure_timestamps.clear()
            self._state.state = CircuitState.CLOSED
            self._state.probe_in_flight = False
            self._state.opened_at = 0.0
            self._state.reopens_at = 0.0
            if was_half_open:
                self._log.info("circuit_closed", service=self._name)
        if was_half_open:
            _notify_state(self._name, CircuitState.CLOSED)

    def record_failure(self) -> None:
        """Count a failure. Trip if we crossed the threshold inside
        the sliding window, or re-open if we were HALF_OPEN."""
        now = self._clock()
        transitioned_to: CircuitState | None = None
        with self._lock:
            if self._state.state is CircuitState.HALF_OPEN:
                # Probe failed — re-open with a fresh cooldown.
                self._state.state = CircuitState.OPEN
                self._state.opened_at = now
                self._state.reopens_at = now + self._cooldown
                self._state.probe_in_flight = False
                self._state.failure_timestamps.clear()
                transitioned_to = CircuitState.OPEN
                self._log.warning(
                    "circuit_reopened_after_probe",
                    service=self._name,
                    reopens_at=self._state.reopens_at,
                )
            else:
                # CLOSED — slide the window and count.
                window_start = now - self._window
                self._state.failure_timestamps = [
                    ts for ts in self._state.failure_timestamps if ts >= window_start
                ]
                self._state.failure_timestamps.append(now)
                if len(self._state.failure_timestamps) >= self._threshold:
                    self._state.state = CircuitState.OPEN
                    self._state.opened_at = now
                    self._state.reopens_at = now + self._cooldown
                    self._state.failure_timestamps.clear()
                    transitioned_to = CircuitState.OPEN
                    self._log.warning(
                        "circuit_opened",
                        service=self._name,
                        reopens_at=self._state.reopens_at,
                        threshold=self._threshold,
                        window_s=self._window,
                    )
        if transitioned_to is not None:
            _notify_state(self._name, transitioned_to)


# ---- @resilient decorator ------------------------------------------

_REGISTRY: dict[str, CircuitBreaker] = {}
_REGISTRY_LOCK = threading.Lock()

# Module-level observer hook — set by main.py at startup to pipe
# state transitions into the Prometheus MetricsRegistry. Left as a
# plain function callable so resilience.py doesn't depend on the
# http layer.
_STATE_OBSERVER: Callable[[str, CircuitState], None] | None = None


def set_state_observer(
    observer: Callable[[str, CircuitState], None] | None,
) -> None:
    """Register a callback fired on every CircuitBreaker state
    transition. Pass ``None`` to unregister (used in tests).

    The callback receives ``(service_name, new_state)`` and must be
    crash-safe — the breaker swallows exceptions from it so an
    observer bug can't topple the call path.
    """
    global _STATE_OBSERVER
    _STATE_OBSERVER = observer


def _notify_state(service_name: str, new_state: CircuitState) -> None:
    observer = _STATE_OBSERVER
    if observer is None:
        return
    # Observer bugs must NEVER kill the breaker path.
    with contextlib.suppress(Exception):
        observer(service_name, new_state)

F = TypeVar("F", bound=Callable[..., Any])


def _get_breaker(service_name: str) -> CircuitBreaker:
    with _REGISTRY_LOCK:
        breaker = _REGISTRY.get(service_name)
        if breaker is None:
            breaker = CircuitBreaker(service_name)
            _REGISTRY[service_name] = breaker
        return breaker


def resilient(service_name: str) -> Callable[[F], F]:
    """Decorator factory — wraps ``func`` in the circuit-breaker
    registered under ``service_name``. Multiple decorated
    adapters with the same name share one breaker (that's the
    point — one Meta outage trips all Meta-bound callers).
    """

    def decorator(func: F) -> F:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            breaker = _get_breaker(service_name)
            breaker.before_call()
            try:
                result = func(*args, **kwargs)
            except BaseException:
                breaker.record_failure()
                raise
            breaker.record_success()
            return result

        wrapper.__wrapped__ = func  # type: ignore[attr-defined]
        wrapper.__name__ = getattr(func, "__name__", "resilient_wrapper")
        wrapper.__doc__ = func.__doc__
        return wrapper  # type: ignore[return-value]

    return decorator


def get_breaker(service_name: str) -> CircuitBreaker:
    """Public accessor — used by ``/status`` and tests to inspect state."""
    return _get_breaker(service_name)


def list_breakers() -> list[CircuitBreaker]:
    """Snapshot of all breakers registered so far — for ``/status`` and
    metrics later. Order is insertion order (stable in 3.7+)."""
    with _REGISTRY_LOCK:
        return list(_REGISTRY.values())


def _reset_registry_for_tests() -> None:
    """Test-only helper — drops the module-level registry so each
    test starts with fresh breakers. Not part of the public API.
    """
    global _STATE_OBSERVER
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
    _STATE_OBSERVER = None


__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_WINDOW_SECONDS",
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "get_breaker",
    "list_breakers",
    "resilient",
    "set_state_observer",
]
