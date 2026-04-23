"""C8.3 integration tests — Circuit-breaker + real MetaMediaDownloader.

Confirms that 5 consecutive HTTP 503s trip the module-level
``meta_media`` breaker and the 6th :meth:`download` call short-circuits
with :class:`CircuitOpenError` *without* touching httpx again.
Also confirms that advancing the clock past the cooldown promotes the
breaker to HALF_OPEN and lets exactly one probe call through.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest

from whatsbot.adapters import resilience
from whatsbot.adapters.meta_media_downloader import (
    META_MEDIA_SERVICE,
    MetaMediaDownloader,
)
from whatsbot.adapters.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    _reset_registry_for_tests,
)
from whatsbot.ports.media_downloader import MediaDownloadError

pytestmark = [pytest.mark.integration]


class ManualClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class Counter503:
    """Fails with 503 on every call — counts hits so we can assert the
    circuit is actually short-circuiting (httpx never invoked)."""

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
    """Install a custom-clock breaker under the ``meta_media`` key so
    the decorator picks it up for the module-level registry."""
    _reset_registry_for_tests()
    breaker = CircuitBreaker(
        META_MEDIA_SERVICE,
        failure_threshold=5,
        window_seconds=60,
        cooldown_seconds=300,
        clock=manual_clock,
    )
    resilience._REGISTRY[META_MEDIA_SERVICE] = breaker  # type: ignore[attr-defined]
    yield breaker
    _reset_registry_for_tests()


def _build_downloader(handler: Callable[[httpx.Request], httpx.Response]) -> MetaMediaDownloader:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return MetaMediaDownloader(access_token="t", client=client)


def test_five_failures_trip_circuit_and_sixth_call_short_circuits(
    patched_breaker: CircuitBreaker, manual_clock: ManualClock
) -> None:
    counter = Counter503()
    downloader = _build_downloader(counter)

    # Drive 5 failing downloads. Tenacity retries 3x internally, so
    # each download() call ends up being 3 HTTP hits counting as ONE
    # breaker failure.
    for _ in range(5):
        with pytest.raises(MediaDownloadError):
            downloader.download("mid")
        # Advance a second between attempts so the sliding window
        # logic is exercised realistically.
        manual_clock.advance(1)

    assert patched_breaker.state is CircuitState.OPEN
    hits_before = counter.calls
    assert hits_before > 0  # tenacity did retry, so > 5

    # 6th call must raise CircuitOpenError without touching httpx.
    with pytest.raises(CircuitOpenError) as exc_info:
        downloader.download("mid")
    assert exc_info.value.service_name == META_MEDIA_SERVICE
    assert counter.calls == hits_before  # short-circuited


def test_cooldown_allows_probe_and_success_closes_circuit(
    patched_breaker: CircuitBreaker, manual_clock: ManualClock
) -> None:
    # Phase 1: trip the circuit.
    counter = Counter503()
    downloader = _build_downloader(counter)
    for _ in range(5):
        with pytest.raises(MediaDownloadError):
            downloader.download("mid")
        manual_clock.advance(1)
    assert patched_breaker.state is CircuitState.OPEN

    # Phase 2: swap in a healthy handler. Clock still OPEN → call
    # short-circuits.
    payload = b"\xff\xd8\xff\xe0ok"

    def healthy(request: httpx.Request) -> httpx.Response:
        if "cdn" in request.url.host:
            return httpx.Response(
                200,
                content=payload,
                headers={"content-type": "image/jpeg"},
            )
        return httpx.Response(
            200,
            json={
                "url": "https://cdn.meta.test/blob",
                "mime_type": "image/jpeg",
                "file_size": str(len(payload)),
            },
        )

    healthy_client = httpx.Client(transport=httpx.MockTransport(healthy))
    recover_downloader = MetaMediaDownloader(
        access_token="t", client=healthy_client
    )

    # Still in cooldown — circuit must reject without calling the
    # healthy backend.
    with pytest.raises(CircuitOpenError):
        recover_downloader.download("mid")

    # Advance past cooldown → HALF_OPEN probe succeeds → CLOSED.
    manual_clock.advance(301)
    result = recover_downloader.download("mid")
    assert result.payload == payload
    assert patched_breaker.state is CircuitState.CLOSED

    # A subsequent normal call is straight-through (no probe gating).
    result2 = recover_downloader.download("mid")
    assert result2.payload == payload


def test_probe_failure_reopens_circuit_with_fresh_cooldown(
    patched_breaker: CircuitBreaker, manual_clock: ManualClock
) -> None:
    counter = Counter503()
    downloader = _build_downloader(counter)
    for _ in range(5):
        with pytest.raises(MediaDownloadError):
            downloader.download("mid")
        manual_clock.advance(1)
    first_reopen = patched_breaker.reopens_at
    assert patched_breaker.state is CircuitState.OPEN

    # Wait out the cooldown and let the probe fail.
    manual_clock.advance(301)
    with pytest.raises(MediaDownloadError):
        downloader.download("mid")

    assert patched_breaker.state is CircuitState.OPEN
    # New cooldown starts from probe-failure time, later than original.
    assert patched_breaker.reopens_at > first_reopen
