"""End-to-end integration between hooks/pre_tool.py and the hook endpoint.

Runs a real TCP uvicorn server in the test process on an ephemeral port
and invokes the hook script with a piped JSON event. Verifies:

* happy-path: Bash → 200 allow
* read-only tool bypass (Read): never touches the HTTP layer
* fail-closed on auth mismatch, unreachable endpoint, malformed stdin
* Write/Edit uses ``file_path`` field
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from whatsbot.application.hook_service import HookService
from whatsbot.http.hook_endpoint import build_router
from whatsbot.ports.secrets_provider import KEY_HOOK_SHARED_SECRET, SecretNotFoundError

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_SCRIPT = REPO_ROOT / "hooks" / "pre_tool.py"
SHARED_SECRET = "shh-integration-test"


class StubSecrets:
    def __init__(self, secret: str | None) -> None:
        self._store: dict[str, str] = {}
        if secret is not None:
            self._store[KEY_HOOK_SHARED_SECRET] = secret

    def get(self, key: str) -> str:
        if key not in self._store:
            raise SecretNotFoundError(key)
        return self._store[key]

    def set(self, key: str, value: str) -> None:
        self._store[key] = value

    def rotate(self, key: str, new_value: str) -> None:
        self._store[key] = new_value


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _ServerThread(threading.Thread):
    def __init__(self, app: FastAPI, port: int) -> None:
        super().__init__(daemon=True)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="off",
        )
        self.server = uvicorn.Server(config)

    def run(self) -> None:  # pragma: no cover — runs in background thread
        self.server.run()


@pytest.fixture
def hook_server() -> Iterator[str]:
    """Spin up the hook app on a free port, yield the base URL, stop it."""
    app = FastAPI()
    app.include_router(build_router(secrets=StubSecrets(SHARED_SECRET), service=HookService()))

    port = _free_port()
    thread = _ServerThread(app, port)
    thread.start()
    # Wait for the server to accept connections.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:  # pragma: no cover — would indicate test infrastructure issue
        raise RuntimeError("hook server did not come up in time")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        thread.server.should_exit = True
        thread.join(timeout=5.0)


def _run_hook(stdin_payload: dict[str, object], *, url: str, secret: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(HOOK_SCRIPT)],
        input=json.dumps(stdin_payload),
        capture_output=True,
        text=True,
        timeout=15.0,
        check=False,
        env={
            "WHATSBOT_HOOK_URL": url,
            "WHATSBOT_HOOK_SECRET": secret,
            "PYTHONPATH": str(REPO_ROOT),
            "PATH": _minimal_path(),
        },
    )


def _minimal_path() -> str:
    """Preserve just enough of PATH for `python3` to be found."""
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")


# ---- happy path ---------------------------------------------------------


def test_bash_allow_roundtrip(hook_server: str) -> None:
    cp = _run_hook(
        {"tool": "Bash", "tool_input": {"command": "ls"}},
        url=hook_server,
        secret=SHARED_SECRET,
    )
    assert cp.returncode == 0, cp.stderr
    body = json.loads(cp.stdout)
    assert body["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_write_uses_file_path_field(hook_server: str) -> None:
    cp = _run_hook(
        {"tool": "Write", "tool_input": {"file_path": "/tmp/foo"}},
        url=hook_server,
        secret=SHARED_SECRET,
    )
    assert cp.returncode == 0, cp.stderr
    body = json.loads(cp.stdout)
    assert body["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_read_tool_bypasses_hook_entirely() -> None:
    # No server needed — Read/Grep/Glob must short-circuit without HTTP.
    cp = subprocess.run(
        ["python3", str(HOOK_SCRIPT)],
        input=json.dumps({"tool": "Read", "tool_input": {"file_path": "x"}}),
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
        env={
            "WHATSBOT_HOOK_URL": "http://127.0.0.1:1",  # intentionally unreachable
            "WHATSBOT_HOOK_SECRET": "unused",
            "PYTHONPATH": str(REPO_ROOT),
            "PATH": _minimal_path(),
        },
    )
    assert cp.returncode == 0
    body = json.loads(cp.stdout)
    assert body["hookSpecificOutput"]["permissionDecision"] == "allow"


# ---- fail-closed --------------------------------------------------------


def test_wrong_secret_is_fail_closed(hook_server: str) -> None:
    cp = _run_hook(
        {"tool": "Bash", "tool_input": {"command": "ls"}},
        url=hook_server,
        secret="wrong",
    )
    # Server replies 401 with deny JSON — the hook turns that into
    # Exit 0 + stdout deny (Claude treats it as a refusal).
    assert cp.returncode == 0
    body = json.loads(cp.stdout)
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_unreachable_endpoint_is_exit_2() -> None:
    cp = _run_hook(
        {"tool": "Bash", "tool_input": {"command": "ls"}},
        url="http://127.0.0.1:1",  # closed port
        secret=SHARED_SECRET,
    )
    assert cp.returncode == 2
    assert "unreachable" in cp.stderr.lower() or "refused" in cp.stderr.lower()


def test_empty_stdin_is_exit_2() -> None:
    cp = subprocess.run(
        ["python3", str(HOOK_SCRIPT)],
        input="",
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
        env={
            "WHATSBOT_HOOK_SECRET": SHARED_SECRET,
            "PYTHONPATH": str(REPO_ROOT),
            "PATH": _minimal_path(),
        },
    )
    assert cp.returncode == 2
    assert "empty stdin" in cp.stderr


def test_malformed_stdin_is_exit_2() -> None:
    cp = subprocess.run(
        ["python3", str(HOOK_SCRIPT)],
        input="{not json",
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
        env={
            "WHATSBOT_HOOK_SECRET": SHARED_SECRET,
            "PYTHONPATH": str(REPO_ROOT),
            "PATH": _minimal_path(),
        },
    )
    assert cp.returncode == 2
    assert "not JSON" in cp.stderr or "is not JSON" in cp.stderr


def test_missing_tool_field_is_exit_2() -> None:
    cp = subprocess.run(
        ["python3", str(HOOK_SCRIPT)],
        input=json.dumps({"tool_input": {"command": "ls"}}),
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
        env={
            "WHATSBOT_HOOK_SECRET": SHARED_SECRET,
            "PYTHONPATH": str(REPO_ROOT),
            "PATH": _minimal_path(),
        },
    )
    assert cp.returncode == 2


def test_unknown_tool_is_exit_2(hook_server: str) -> None:
    cp = _run_hook(
        {"tool": "FutureTool", "tool_input": {"x": 1}},
        url=hook_server,
        secret=SHARED_SECRET,
    )
    assert cp.returncode == 2
    assert "unknown tool" in cp.stderr


def test_bash_empty_command_is_exit_2(hook_server: str) -> None:
    cp = _run_hook(
        {"tool": "Bash", "tool_input": {"command": "  "}},
        url=hook_server,
        secret=SHARED_SECRET,
    )
    assert cp.returncode == 2


def test_write_missing_path_is_exit_2(hook_server: str) -> None:
    cp = _run_hook(
        {"tool": "Edit", "tool_input": {}},
        url=hook_server,
        secret=SHARED_SECRET,
    )
    assert cp.returncode == 2
