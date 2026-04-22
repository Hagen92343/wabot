"""Unit tests for the HTTP middleware classes."""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from whatsbot.http.middleware import ConstantTimeMiddleware, CorrelationIdMiddleware

pytestmark = pytest.mark.unit


# --- CorrelationIdMiddleware ------------------------------------------------


def _make_app(*middleware_args: tuple[type, dict[str, object]]) -> FastAPI:
    app = FastAPI()
    for cls, kwargs in middleware_args:
        app.add_middleware(cls, **kwargs)

    @app.get("/probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/webhook")
    async def webhook() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_correlation_id_header_is_set() -> None:
    app = _make_app((CorrelationIdMiddleware, {}))
    client = TestClient(app)
    response = client.get("/probe")
    cid = response.headers.get("x-correlation-id")
    assert cid is not None
    assert len(cid) == 26  # ULID canonical length


def test_correlation_ids_are_unique_per_request() -> None:
    app = _make_app((CorrelationIdMiddleware, {}))
    client = TestClient(app)
    ids = {client.get("/probe").headers["x-correlation-id"] for _ in range(5)}
    assert len(ids) == 5


# --- ConstantTimeMiddleware -------------------------------------------------

# Padding tests use generous tolerances because perf_counter on macOS / CI
# can drift by tens of milliseconds; the goal is to verify "at least pad" and
# "do not pad" behaviour, not to assert exact wall-clock numbers.
PAD_MS = 250
TOLERANCE = 0.05  # 50 ms slop


def test_constant_time_pads_when_paths_empty_means_all() -> None:
    app = _make_app((ConstantTimeMiddleware, {"min_duration_ms": PAD_MS}))
    client = TestClient(app)
    start = time.perf_counter()
    response = client.get("/probe")
    elapsed = time.perf_counter() - start
    assert response.status_code == 200
    assert elapsed >= (PAD_MS / 1000) - TOLERANCE


def test_constant_time_pads_matching_path() -> None:
    app = _make_app((ConstantTimeMiddleware, {"min_duration_ms": PAD_MS, "paths": ("/webhook",)}))
    client = TestClient(app)
    start = time.perf_counter()
    client.get("/webhook")
    elapsed = time.perf_counter() - start
    assert elapsed >= (PAD_MS / 1000) - TOLERANCE


def test_constant_time_skips_non_matching_paths() -> None:
    app = _make_app((ConstantTimeMiddleware, {"min_duration_ms": 500, "paths": ("/webhook",)}))
    client = TestClient(app)
    start = time.perf_counter()
    client.get("/probe")
    elapsed = time.perf_counter() - start
    # Without padding the handler returns in well under 500 ms.
    assert elapsed < 0.4


def test_constant_time_does_not_shorten_already_long_responses() -> None:
    """If the handler is already slow, the middleware must not truncate it."""
    app = FastAPI()

    @app.get("/lazy")
    async def lazy() -> dict[str, bool]:
        time.sleep(0.15)  # 150 ms of real work
        return {"ok": True}

    app.add_middleware(ConstantTimeMiddleware, min_duration_ms=100, paths=())
    client = TestClient(app)
    start = time.perf_counter()
    client.get("/lazy")
    elapsed = time.perf_counter() - start
    assert elapsed >= 0.14  # roughly the handler's own latency, not truncated
