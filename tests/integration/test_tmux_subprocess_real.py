"""Integration smoke for SubprocessTmuxController against the real
``tmux`` binary.

Every test uses a UUID-suffixed session name so multiple runs (and
concurrent suites) don't collide. Each test cleans up behind itself
even on failure.

Skipped if ``tmux`` isn't on PATH — CI environments without it still
get the unit-test coverage.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from whatsbot.adapters.tmux_subprocess import SubprocessTmuxController

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("tmux") is None, reason="tmux not installed"
    ),
]


@pytest.fixture
def ctrl() -> SubprocessTmuxController:
    return SubprocessTmuxController()


@pytest.fixture
def session_name() -> Iterator[str]:
    name = f"wb-test-{uuid.uuid4().hex[:8]}"
    yield name
    # Best-effort cleanup — ignore failures so the test suite's actual
    # result shows through.
    subprocess.run(
        ["tmux", "kill-session", "-t", name],
        capture_output=True,
        check=False,
    )


def test_roundtrip_create_has_kill(
    ctrl: SubprocessTmuxController, session_name: str, tmp_path: Path
) -> None:
    assert ctrl.has_session(session_name) is False
    ctrl.new_session(session_name, cwd=tmp_path)
    assert ctrl.has_session(session_name) is True
    assert session_name in ctrl.list_sessions(prefix="wb-test-")

    assert ctrl.kill_session(session_name) is True
    assert ctrl.has_session(session_name) is False
    # Second kill on the same (now absent) name is a no-op False.
    assert ctrl.kill_session(session_name) is False


def test_send_text_lands_in_pane(
    ctrl: SubprocessTmuxController, session_name: str, tmp_path: Path
) -> None:
    ctrl.new_session(session_name, cwd=tmp_path)
    marker = f"hello-{uuid.uuid4().hex[:6]}"
    # Use printf so the pane actually emits the marker without any shell
    # history chatter.
    ctrl.send_text(session_name, f"printf {marker}")
    # Tmux runs the command asynchronously; wait briefly for the pane
    # output to settle.
    time.sleep(0.2)
    captured = subprocess.run(
        ["tmux", "capture-pane", "-t", session_name, "-p"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert marker in captured.stdout


def test_set_status_does_not_raise(
    ctrl: SubprocessTmuxController, session_name: str, tmp_path: Path
) -> None:
    ctrl.new_session(session_name, cwd=tmp_path)
    # Smoke: tmux accepts the theme invocation and our wrapper doesn't
    # choke on the emoji label. We don't assert the rendered bar
    # contents — tmux doesn't expose that from a detached session in
    # a clean way.
    ctrl.set_status(session_name, color="green", label="🟢 NORMAL [wb-test]")
