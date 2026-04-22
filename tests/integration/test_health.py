"""Integration tests for the FastAPI app — /health and /metrics."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import whatsbot
from whatsbot.config import Environment, Settings
from whatsbot.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
def client() -> TestClient:
    """Build an isolated app instance bound to env=test (no Keychain)."""
    app = create_app(Settings(env=Environment.TEST))
    return TestClient(app)


def test_health_returns_ok_json(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["version"] == whatsbot.__version__
    assert body["env"] == "test"
    assert isinstance(body["uptime_seconds"], int | float)
    assert body["uptime_seconds"] >= 0


def test_health_includes_correlation_id_header(client: TestClient) -> None:
    response = client.get("/health")
    cid = response.headers.get("x-correlation-id")
    assert cid is not None
    # ULIDs are 26 chars in canonical Crockford-Base32
    assert len(cid) == 26
    assert cid.isalnum()


def test_two_requests_get_distinct_correlation_ids(client: TestClient) -> None:
    first = client.get("/health").headers["x-correlation-id"]
    second = client.get("/health").headers["x-correlation-id"]
    assert first != second


def test_metrics_returns_empty_text(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.text == ""
    assert response.headers["content-type"].startswith("text/plain")


def test_unknown_route_is_404_with_correlation_id(client: TestClient) -> None:
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    # Even error responses must carry the correlation id for log-tracing.
    assert "x-correlation-id" in {k.lower() for k in response.headers}


def test_create_app_accepts_settings_object() -> None:
    app1 = create_app(Settings(env=Environment.TEST))
    app2 = create_app(Settings(env=Environment.TEST))
    # Each call returns a fresh FastAPI instance.
    assert app1 is not app2
