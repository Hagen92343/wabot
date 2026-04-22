"""C3.6 — fail-closed smoke for the Pre-Tool hook.

Spec §7 and the phase-3 Rules C3.6 checkpoint spell out the boundaries:

* Bot unreachable      → Exit 2 with a stderr reason.
* Shared-Secret mismatch → Exit 0 + deny JSON (already covered by
  ``test_hook_script.test_wrong_secret_is_fail_closed``).
* Server crash (5xx)   → Exit 2 — covered here.
* Malformed response   → Exit 2 — covered here.
* Timeout              → Exit 2 — covered here.

These are the cases that aren't in ``test_hook_script.py``. Together
they close the fail-closed matrix; if any of them ever drifted into
"allow" the hook would be a silent hole.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

_FactoryType = Callable[[FastAPI], str]

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_SCRIPT = REPO_ROOT / "hooks" / "pre_tool.py"
SHARED_SECRET = "shh-fail-closed"


# ---- TCP server harness (same pattern as test_hook_script.py) -----------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _ServerThread(threading.Thread):
    def __init__(self, app: FastAPI, port: int) -> None:
        super().__init__(daemon=True)
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
        )
        self.server = uvicorn.Server(config)

    def run(self) -> None:  # pragma: no cover — background
        self.server.run()


def _start(app: FastAPI) -> tuple[_ServerThread, str]:
    port = _free_port()
    thread = _ServerThread(app, port)
    thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:  # pragma: no cover
        raise RuntimeError("fail-closed smoke server didn't come up")
    return thread, f"http://127.0.0.1:{port}"


def _stop(thread: _ServerThread) -> None:
    thread.server.should_exit = True
    thread.join(timeout=5.0)


def _minimal_path() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")


def _run_hook(
    stdin_payload: dict[str, object], *, url: str, secret: str = SHARED_SECRET
) -> subprocess.CompletedProcess[str]:
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


def _bash_event() -> dict[str, object]:
    return {"tool": "Bash", "tool_input": {"command": "ls"}}


# ---- individual misbehaving apps ---------------------------------------


def _crash_app() -> FastAPI:
    app = FastAPI()

    @app.post("/hook/bash")
    async def boom() -> JSONResponse:
        # 500 with a JSON body that's not the hook contract. urllib
        # reads 5xx as HTTPError — the hook falls back to _parse_response
        # which can't find hookSpecificOutput and raises HookError.
        return JSONResponse(status_code=500, content={"error": "intentional crash"})

    return app


def _text_reply_app() -> FastAPI:
    app = FastAPI()

    @app.post("/hook/bash", response_class=PlainTextResponse)
    async def plain() -> str:
        return "not json at all"

    return app


def _non_object_app() -> FastAPI:
    app = FastAPI()

    @app.post("/hook/bash")
    async def scalar() -> JSONResponse:
        # Valid JSON, but a string — hook expects a top-level object.
        return JSONResponse(content="deny")

    return app


def _missing_block_app() -> FastAPI:
    app = FastAPI()

    @app.post("/hook/bash")
    async def shaped_wrong() -> JSONResponse:
        # Top-level object, but lacks hookSpecificOutput.
        return JSONResponse(content={"ok": True})

    return app


def _unknown_decision_app() -> FastAPI:
    app = FastAPI()

    @app.post("/hook/bash")
    async def ambiguous() -> JSONResponse:
        return JSONResponse(
            content={
                "hookSpecificOutput": {
                    "permissionDecision": "maybe",
                    "permissionDecisionReason": "?",
                }
            }
        )

    return app


def _slow_app() -> FastAPI:
    app = FastAPI()

    @app.post("/hook/bash")
    async def slow() -> JSONResponse:
        # Sleep longer than the hook's read timeout so urllib fires
        # TimeoutError, which maps to HookError → Exit 2.
        await _sleep(15.0)  # exceeds READ_TIMEOUT (10 s)
        return JSONResponse(
            content={
                "hookSpecificOutput": {
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "late",
                }
            }
        )

    return app


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


# ---- shared fixture ----------------------------------------------------


@pytest.fixture
def server_for() -> Iterator[_FactoryType]:
    """Returns a factory that starts a chosen FastAPI app and stops it
    after the test — keeps each scenario's misbehaviour isolated."""
    started: list[_ServerThread] = []

    def _factory(app: FastAPI) -> str:
        thread, url = _start(app)
        started.append(thread)
        return url

    try:
        yield _factory
    finally:
        for thread in started:
            _stop(thread)


# ---- the actual smoke cases -------------------------------------------


def test_server_500_is_exit_2(server_for: _FactoryType) -> None:
    url = server_for(_crash_app())
    cp = _run_hook(_bash_event(), url=url)
    assert cp.returncode == 2, cp.stderr
    assert "whatsbot hook" in cp.stderr


def test_non_json_reply_is_exit_2(server_for: _FactoryType) -> None:
    url = server_for(_text_reply_app())
    cp = _run_hook(_bash_event(), url=url)
    assert cp.returncode == 2, cp.stderr
    assert "JSON" in cp.stderr or "malformed" in cp.stderr


def test_non_object_reply_is_exit_2(server_for: _FactoryType) -> None:
    url = server_for(_non_object_app())
    cp = _run_hook(_bash_event(), url=url)
    assert cp.returncode == 2, cp.stderr
    assert "non-object" in cp.stderr or "object" in cp.stderr


def test_missing_hookspecificoutput_is_exit_2(server_for: _FactoryType) -> None:
    url = server_for(_missing_block_app())
    cp = _run_hook(_bash_event(), url=url)
    assert cp.returncode == 2, cp.stderr
    assert "hookSpecificOutput" in cp.stderr


def test_unknown_permission_decision_is_exit_2(server_for: _FactoryType) -> None:
    url = server_for(_unknown_decision_app())
    cp = _run_hook(_bash_event(), url=url)
    assert cp.returncode == 2, cp.stderr
    assert "decision" in cp.stderr.lower()


# Timeout test takes ~10s (one READ_TIMEOUT cycle). Always runs — the
# timeout path is important enough that we don't gate it behind a mark.
def test_slow_endpoint_triggers_timeout(server_for: _FactoryType) -> None:
    url = server_for(_slow_app())
    cp = _run_hook(_bash_event(), url=url)
    assert cp.returncode == 2, cp.stderr
    assert "timed out" in cp.stderr.lower() or "timeout" in cp.stderr.lower()
