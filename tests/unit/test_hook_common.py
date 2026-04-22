"""Unit tests for hooks._common (stdlib only, no whatsbot deps)."""

from __future__ import annotations

import pytest

from hooks import _common
from hooks._common import (
    HookError,
    HookResponse,
    _parse_response,
    load_shared_secret,
)

pytestmark = pytest.mark.unit


# ---- load_shared_secret --------------------------------------------------


def test_load_secret_from_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_common.ENV_SECRET_OVERRIDE, "from-env")
    assert load_shared_secret() == "from-env"


def test_load_secret_no_security_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a non-macOS box or a stripped image, ``security`` is missing.
    We must surface that as HookError, not let FileNotFoundError escape."""
    monkeypatch.delenv(_common.ENV_SECRET_OVERRIDE, raising=False)
    # Force subprocess.run to simulate missing binary by pointing PATH at
    # an empty dir. Simpler: monkeypatch subprocess.run itself.
    import subprocess

    def fake_run(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("security")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(HookError, match="security.*CLI"):
        load_shared_secret()


def test_load_secret_returncode_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_common.ENV_SECRET_OVERRIDE, raising=False)
    import subprocess

    class FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = "not found"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeCompleted())
    with pytest.raises(HookError, match="not in keychain"):
        load_shared_secret()


def test_load_secret_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_common.ENV_SECRET_OVERRIDE, raising=False)
    import subprocess

    class FakeCompleted:
        returncode = 0
        stdout = "  \n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeCompleted())
    with pytest.raises(HookError, match="empty"):
        load_shared_secret()


# ---- _parse_response ----------------------------------------------------


def test_parse_response_allow() -> None:
    raw = (
        b'{"hookSpecificOutput":{"permissionDecision":"allow","permissionDecisionReason":"ok"}}'
    )
    resp = _parse_response(200, raw)
    assert resp == HookResponse(permission_decision="allow", reason="ok")
    assert resp.is_allow is True


def test_parse_response_deny() -> None:
    raw = b'{"hookSpecificOutput":{"permissionDecision":"deny","permissionDecisionReason":"nope"}}'
    resp = _parse_response(401, raw)
    assert resp.is_allow is False
    assert resp.reason == "nope"


def test_parse_response_malformed_json() -> None:
    with pytest.raises(HookError, match="malformed"):
        _parse_response(200, b"{not json")


def test_parse_response_non_object() -> None:
    with pytest.raises(HookError, match="non-object"):
        _parse_response(200, b'["allow"]')


def test_parse_response_missing_block() -> None:
    with pytest.raises(HookError, match="missing hookSpecificOutput"):
        _parse_response(200, b"{}")


def test_parse_response_unknown_decision() -> None:
    raw = b'{"hookSpecificOutput":{"permissionDecision":"maybe"}}'
    with pytest.raises(HookError, match="unknown decision"):
        _parse_response(200, raw)


def test_parse_response_reason_defaults_empty_when_missing() -> None:
    raw = b'{"hookSpecificOutput":{"permissionDecision":"allow"}}'
    resp = _parse_response(200, raw)
    assert resp.reason == ""
