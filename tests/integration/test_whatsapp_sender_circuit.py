"""C10.2 — Circuit-breaker integration against the real WhatsAppCloudSender.

Mirrors :mod:`tests.integration.test_resilience_circuit_integration` for the
MediaDownloader but uses the outbound path. Five consecutive retry-
exhausting failures must trip the module-level ``meta_send`` breaker so
the sixth call short-circuits without touching httpx again.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import cast

import httpx
import pytest

from whatsbot.adapters import resilience
from whatsbot.adapters.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    _reset_registry_for_tests,
)
from whatsbot.adapters.whatsapp_sender import (
    META_SEND_SERVICE,
    WhatsAppCloudSender,
)
from whatsbot.ports.message_sender import MessageSendError

pytestmark = [pytest.mark.integration]


class ManualClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class Counter503:
    """Always fails with 503. Counts HTTP hits so the test can assert
    the breaker is actually short-circuiting once OPEN."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(503)


@pytest.fixture
def manual_clock() -> ManualClock:
    return ManualClock(start=1_000_000)


@pytest.fixture
def patched_breaker(manual_clock: ManualClock) -> Iterator[CircuitBreaker]:
    _reset_registry_for_tests()
    breaker = CircuitBreaker(
        META_SEND_SERVICE,
        failure_threshold=5,
        window_seconds=60,
        cooldown_seconds=300,
        clock=manual_clock,
    )
    resilience._REGISTRY[META_SEND_SERVICE] = breaker
    yield breaker
    _reset_registry_for_tests()


def _build_sender(
    handler: Callable[[httpx.Request], httpx.Response],
) -> WhatsAppCloudSender:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return WhatsAppCloudSender(
        access_token="t",
        phone_number_id="PNID",
        client=client,
    )


def test_five_send_failures_trip_circuit_and_sixth_short_circuits(
    patched_breaker: CircuitBreaker, manual_clock: ManualClock
) -> None:
    counter = Counter503()
    sender = _build_sender(counter)

    # 5 failing sends. Each send() exhausts 3 tenacity attempts = 1
    # breaker failure.
    for _ in range(5):
        with pytest.raises(MessageSendError):
            sender.send_text(to="491716598519", body="x")
        manual_clock.advance(1)

    assert patched_breaker.state is CircuitState.OPEN
    hits_before_short_circuit = counter.calls
    # 3 tenacity attempts per call, 5 calls → 15 HTTP hits.
    assert hits_before_short_circuit == 15

    # 6th call must raise CircuitOpenError without any HTTP.
    with pytest.raises(CircuitOpenError) as exc_info:
        sender.send_text(to="491716598519", body="x")
    assert exc_info.value.service_name == META_SEND_SERVICE
    assert counter.calls == hits_before_short_circuit


def test_cooldown_allows_probe_and_success_closes_breaker(
    patched_breaker: CircuitBreaker, manual_clock: ManualClock
) -> None:
    counter = Counter503()
    failing_sender = _build_sender(counter)

    for _ in range(5):
        with pytest.raises(MessageSendError):
            failing_sender.send_text(to="491716598519", body="x")
        manual_clock.advance(1)
    # Using == (not is) so mypy does not narrow the property to
    # Literal[OPEN] — later assertions exercise other states after
    # send_text() transitions the breaker.
    assert patched_breaker.state == CircuitState.OPEN

    # Healthy backend behind a different transport — but the breaker
    # is module-scope so the same ``meta_send`` entry gates this one
    # too.
    def healthy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "messaging_product": "whatsapp",
                "messages": [{"id": "wamid.ok"}],
            },
        )

    healthy_sender = WhatsAppCloudSender(
        access_token="t",
        phone_number_id="PNID",
        client=httpx.Client(transport=httpx.MockTransport(healthy)),
    )

    # Still in cooldown — breaker short-circuits.
    with pytest.raises(CircuitOpenError):
        healthy_sender.send_text(to="491716598519", body="x")

    # Advance past cooldown → HALF_OPEN probe → success → CLOSED.
    manual_clock.advance(301)
    healthy_sender.send_text(to="491716598519", body="x")
    # send_text transitioned the breaker. cast() drops mypy's earlier
    # literal narrowing (from the OPEN assertion above) so we can
    # assert the new state.
    post_state = cast(CircuitState, patched_breaker.state)
    assert post_state == CircuitState.CLOSED

    # Next call is straight-through, no probe gating.
    healthy_sender.send_text(to="491716598519", body="x")
