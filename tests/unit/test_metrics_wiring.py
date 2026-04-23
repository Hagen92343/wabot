"""Unit tests for the C8.4 wiring — MetricsMessageSender decorator
and CircuitBreaker state-observer → MetricsRegistry integration."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from whatsbot.adapters.metrics_sender import MetricsMessageSender
from whatsbot.adapters.resilience import (
    CircuitBreaker,
    CircuitState,
    _reset_registry_for_tests,
    set_state_observer,
)
from whatsbot.http.metrics import MetricsRegistry


class _RecordingSender:
    def __init__(self, raise_on: str | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.raise_on = raise_on

    def send_text(self, *, to: str, body: str) -> None:
        if self.raise_on is not None and self.raise_on in body:
            raise RuntimeError("simulated downstream failure")
        self.sent.append((to, body))


@pytest.fixture(autouse=True)
def _fresh_breaker_registry() -> Iterator[None]:
    _reset_registry_for_tests()
    yield
    _reset_registry_for_tests()


# ---- MetricsMessageSender -------------------------------------------


def test_metrics_sender_increments_outbound_counter_on_success() -> None:
    registry = MetricsRegistry()
    inner = _RecordingSender()
    sender = MetricsMessageSender(inner=inner, registry=registry)

    sender.send_text(to="+491", body="hi")
    sender.send_text(to="+491", body="pong")

    assert (
        registry.counter_value(
            "whatsbot_messages_total",
            labels={"direction": "out", "kind": "text"},
        )
        == 2
    )
    assert len(inner.sent) == 2


def test_metrics_sender_does_not_count_failed_sends() -> None:
    registry = MetricsRegistry()
    inner = _RecordingSender(raise_on="boom")
    sender = MetricsMessageSender(inner=inner, registry=registry)

    with pytest.raises(RuntimeError):
        sender.send_text(to="+491", body="boom now")

    assert (
        registry.counter_value(
            "whatsbot_messages_total",
            labels={"direction": "out", "kind": "text"},
        )
        == 0
    )


# ---- circuit-state observer ------------------------------------------


def _observe_to(registry: MetricsRegistry):  # type: ignore[no-untyped-def]
    def _observer(service_name: str, new_state: CircuitState) -> None:
        for s in CircuitState:
            registry.set_gauge(
                "whatsbot_circuit_state",
                1 if s is new_state else 0,
                labels={"service": service_name, "state": s.value},
                help_text="circuit state",
            )

    return _observer


def test_circuit_state_observer_populates_gauge_on_open() -> None:
    registry = MetricsRegistry()
    set_state_observer(_observe_to(registry))
    breaker = CircuitBreaker(
        "meta_media", failure_threshold=2, cooldown_seconds=60
    )

    breaker.record_failure()
    breaker.record_failure()

    assert (
        registry.gauge_value(
            "whatsbot_circuit_state",
            labels={"service": "meta_media", "state": "open"},
        )
        == 1
    )
    assert (
        registry.gauge_value(
            "whatsbot_circuit_state",
            labels={"service": "meta_media", "state": "closed"},
        )
        == 0
    )


def test_circuit_state_observer_flips_on_close() -> None:
    registry = MetricsRegistry()
    set_state_observer(_observe_to(registry))

    clock = {"t": 0.0}

    def _clock() -> float:
        return clock["t"]

    breaker = CircuitBreaker(
        "w", failure_threshold=2, cooldown_seconds=10, clock=_clock
    )
    breaker.record_failure()
    breaker.record_failure()
    clock["t"] = 11.0
    breaker.before_call()  # HALF_OPEN probe
    breaker.record_success()  # → CLOSED

    assert (
        registry.gauge_value(
            "whatsbot_circuit_state",
            labels={"service": "w", "state": "closed"},
        )
        == 1
    )
    assert (
        registry.gauge_value(
            "whatsbot_circuit_state",
            labels={"service": "w", "state": "open"},
        )
        == 0
    )


def test_observer_failure_does_not_kill_breaker() -> None:
    calls = {"n": 0}

    def _broken_observer(service: str, new_state: CircuitState) -> None:
        calls["n"] += 1
        raise RuntimeError("observer is buggy")

    set_state_observer(_broken_observer)
    breaker = CircuitBreaker("x", failure_threshold=2)
    breaker.record_failure()
    breaker.record_failure()
    # Breaker must still be OPEN — observer exception didn't take it
    # down.
    assert breaker.state is CircuitState.OPEN
    assert calls["n"] >= 1
