"""Unit tests for CircuitBreaker + @resilient (Phase 8 C8.3)."""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from whatsbot.adapters.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    _reset_registry_for_tests,
    get_breaker,
    resilient,
)


class ManualClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    _reset_registry_for_tests()
    yield
    _reset_registry_for_tests()


# ---- CLOSED → OPEN transitions ---------------------------------------


def test_breaker_starts_closed() -> None:
    cb = CircuitBreaker("svc", clock=ManualClock())
    assert cb.state is CircuitState.CLOSED


def test_single_failure_does_not_trip() -> None:
    cb = CircuitBreaker("svc", clock=ManualClock())
    for _ in range(4):
        cb.record_failure()
    assert cb.state is CircuitState.CLOSED


def test_threshold_failures_in_window_trip_to_open() -> None:
    clock = ManualClock()
    cb = CircuitBreaker(
        "svc",
        clock=clock,
        failure_threshold=5,
        window_seconds=60,
        cooldown_seconds=300,
    )
    for _ in range(5):
        cb.record_failure()
        clock.advance(1)
    assert cb.state is CircuitState.OPEN


def test_failures_outside_window_do_not_count() -> None:
    clock = ManualClock()
    cb = CircuitBreaker(
        "svc", clock=clock, failure_threshold=3, window_seconds=10
    )
    cb.record_failure()
    cb.record_failure()
    # Advance beyond window before the third failure.
    clock.advance(11)
    cb.record_failure()
    cb.record_failure()
    # Only 2 failures now inside the sliding window → still closed.
    assert cb.state is CircuitState.CLOSED


def test_success_resets_failure_window() -> None:
    cb = CircuitBreaker("svc", clock=ManualClock(), failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    assert cb.state is CircuitState.CLOSED


# ---- OPEN → HALF_OPEN transitions ------------------------------------


def test_open_raises_without_calling_wrapped() -> None:
    clock = ManualClock()
    cb = CircuitBreaker(
        "svc",
        clock=clock,
        failure_threshold=2,
        cooldown_seconds=300,
    )
    cb.record_failure()
    cb.record_failure()
    assert cb.state is CircuitState.OPEN

    with pytest.raises(CircuitOpenError) as ei:
        cb.before_call()
    assert ei.value.service_name == "svc"
    assert ei.value.reopens_at == cb.reopens_at


def test_cooldown_elapsed_promotes_to_half_open_on_next_call() -> None:
    clock = ManualClock(start=0)
    cb = CircuitBreaker(
        "svc",
        clock=clock,
        failure_threshold=2,
        cooldown_seconds=300,
    )
    cb.record_failure()
    cb.record_failure()
    # Advance past cooldown.
    clock.advance(301)
    # before_call must accept the first call as probe and move to HALF_OPEN.
    cb.before_call()
    assert cb.state is CircuitState.HALF_OPEN


def test_half_open_second_concurrent_call_is_rejected() -> None:
    clock = ManualClock(start=0)
    cb = CircuitBreaker(
        "svc",
        clock=clock,
        failure_threshold=2,
        cooldown_seconds=300,
    )
    cb.record_failure()
    cb.record_failure()
    clock.advance(301)
    cb.before_call()  # probe accepted
    assert cb.state is CircuitState.HALF_OPEN

    # A second caller sees probe_in_flight → gets CircuitOpenError.
    with pytest.raises(CircuitOpenError):
        cb.before_call()


# ---- HALF_OPEN → CLOSED / OPEN transitions ---------------------------


def test_probe_success_closes_circuit_and_resets_counter() -> None:
    clock = ManualClock()
    cb = CircuitBreaker(
        "svc",
        clock=clock,
        failure_threshold=2,
        cooldown_seconds=60,
    )
    cb.record_failure()
    cb.record_failure()
    clock.advance(61)
    cb.before_call()
    cb.record_success()
    assert cb.state is CircuitState.CLOSED
    # Counter should be reset — a fresh failure shouldn't immediately
    # re-trip because the "2 failures before" don't carry over.
    cb.record_failure()
    assert cb.state is CircuitState.CLOSED


def test_probe_failure_reopens_with_fresh_cooldown() -> None:
    clock = ManualClock(start=0)
    cb = CircuitBreaker(
        "svc",
        clock=clock,
        failure_threshold=2,
        cooldown_seconds=60,
    )
    cb.record_failure()
    cb.record_failure()
    clock.advance(61)
    cb.before_call()  # HALF_OPEN probe
    cb.record_failure()
    assert cb.state is CircuitState.OPEN
    # A new cooldown starts from the probe-failure time, not the
    # original open-time.
    assert cb.reopens_at > 60


# ---- @resilient decorator -------------------------------------------


def test_resilient_decorator_passes_through_on_success() -> None:
    calls = {"n": 0}

    @resilient("echo")
    def echo(x: int) -> int:
        calls["n"] += 1
        return x * 2

    assert echo(3) == 6
    assert calls["n"] == 1
    assert get_breaker("echo").state is CircuitState.CLOSED


def test_resilient_counts_failures_and_trips() -> None:
    @resilient("flaky")
    def boom() -> None:
        raise ValueError("nope")

    for _ in range(5):
        with pytest.raises(ValueError):
            boom()

    breaker = get_breaker("flaky")
    assert breaker.state is CircuitState.OPEN

    # Next call short-circuits — the wrapped function must NOT run.
    with pytest.raises(CircuitOpenError):
        boom()


def test_resilient_same_name_shares_breaker() -> None:
    @resilient("shared")
    def a() -> None:
        raise ValueError()

    @resilient("shared")
    def b() -> None:
        raise ValueError()

    # 3 failures via `a` + 2 via `b` → 5 within the window, trips.
    for _ in range(3):
        with pytest.raises(ValueError):
            a()
    for _ in range(2):
        with pytest.raises(ValueError):
            b()
    assert get_breaker("shared").state is CircuitState.OPEN


def test_resilient_different_names_are_isolated() -> None:
    @resilient("alpha")
    def a() -> None:
        raise ValueError()

    @resilient("beta")
    def b() -> int:
        return 42

    for _ in range(5):
        with pytest.raises(ValueError):
            a()
    assert get_breaker("alpha").state is CircuitState.OPEN
    # beta has only seen successes.
    assert b() == 42
    assert get_breaker("beta").state is CircuitState.CLOSED


def test_resilient_preserves_dunder_name_and_doc() -> None:
    @resilient("svc")
    def do_work() -> None:
        """Does work."""

    assert do_work.__name__ == "do_work"
    assert do_work.__doc__ == "Does work."
    assert hasattr(do_work, "__wrapped__")


def test_resilient_records_base_exceptions_as_failures() -> None:
    # KeyboardInterrupt / SystemExit shouldn't silently skip counting;
    # if Meta hangs forever and the user aborts, the next call should
    # still see the failure record (user can always /status to check).
    @resilient("interrupted")
    def boom() -> None:
        raise KeyboardInterrupt()

    for _ in range(5):
        with pytest.raises(KeyboardInterrupt):
            boom()
    assert get_breaker("interrupted").state is CircuitState.OPEN


# ---- constructor guards ---------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"failure_threshold": 0},
        {"failure_threshold": -1},
        {"window_seconds": 0},
        {"window_seconds": -10},
        {"cooldown_seconds": 0},
    ],
)
def test_invalid_parameters_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        CircuitBreaker("svc", **kwargs)


# ---- thread safety smoke --------------------------------------------


def test_concurrent_failures_trip_exactly_once() -> None:
    """Regression guard — if two threads race through record_failure
    we want the breaker to settle in OPEN state without leaking a
    double-transition or crashing."""
    cb = CircuitBreaker(
        "race",
        failure_threshold=5,
        window_seconds=60,
        cooldown_seconds=300,
    )

    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(10):
                cb.record_failure()
        except BaseException as exc:  # pragma: no cover — diagnostic
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert cb.state is CircuitState.OPEN
