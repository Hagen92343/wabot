"""Unit tests for SubprocessTmuxController — mocks subprocess.run."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from whatsbot.adapters.tmux_subprocess import SubprocessTmuxController
from whatsbot.ports.tmux_controller import TmuxError

pytestmark = pytest.mark.unit


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["tmux"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class _RunRecorder:
    """Captures every subprocess.run call + returns a queued response."""

    def __init__(
        self, responses: list[subprocess.CompletedProcess[str]]
    ) -> None:
        self.calls: list[list[str]] = []
        self._responses = list(responses)

    def __call__(
        self, args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        # Defensive: tests mustn't accidentally use shell=True.
        assert kwargs.get("shell", False) is False
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True
        self.calls.append(list(args))
        if not self._responses:
            return _completed()
        return self._responses.pop(0)


# ---- has_session -------------------------------------------------------


def test_has_session_true_on_exit_0(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _RunRecorder([_completed(returncode=0)])
    monkeypatch.setattr(subprocess, "run", rec)
    assert SubprocessTmuxController().has_session("wb-alpha") is True
    assert rec.calls[0] == ["tmux", "has-session", "-t", "wb-alpha"]


def test_has_session_false_on_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _RunRecorder([_completed(1)]))
    assert SubprocessTmuxController().has_session("wb-alpha") is False


# ---- list_sessions -----------------------------------------------------


def test_list_sessions_parses_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _RunRecorder(
        [_completed(stdout="wb-alpha\nwb-beta\nother-thing\n")]
    )
    monkeypatch.setattr(subprocess, "run", rec)
    names = SubprocessTmuxController().list_sessions()
    assert names == ["wb-alpha", "wb-beta", "other-thing"]
    assert rec.calls[0] == [
        "tmux",
        "list-sessions",
        "-F",
        "#{session_name}",
    ]


def test_list_sessions_filters_by_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _RunRecorder(
            [_completed(stdout="wb-alpha\nwb-beta\nother-thing\n")]
        ),
    )
    bot_only = SubprocessTmuxController().list_sessions(prefix="wb-")
    assert bot_only == ["wb-alpha", "wb-beta"]


def test_list_sessions_no_server_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # tmux without a running server exits 1 and prints to stderr.
    monkeypatch.setattr(
        subprocess,
        "run",
        _RunRecorder([_completed(1, stderr="no server")]),
    )
    assert SubprocessTmuxController().list_sessions() == []


# ---- new_session -------------------------------------------------------


def test_new_session_passes_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _RunRecorder([_completed(0)])
    monkeypatch.setattr(subprocess, "run", rec)
    SubprocessTmuxController().new_session(
        "wb-alpha", cwd=Path("/tmp/projekte/alpha")
    )
    assert rec.calls[0] == [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "wb-alpha",
        "-c",
        "/tmp/projekte/alpha",
    ]


def test_new_session_raises_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _RunRecorder([_completed(1, stderr="duplicate session name")]),
    )
    with pytest.raises(TmuxError, match="duplicate session name"):
        SubprocessTmuxController().new_session("wb-alpha", cwd=".")


# ---- kill_session ------------------------------------------------------


def test_kill_session_true_when_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _RunRecorder([_completed(0)])
    monkeypatch.setattr(subprocess, "run", rec)
    assert SubprocessTmuxController().kill_session("wb-alpha") is True
    assert rec.calls[0] == ["tmux", "kill-session", "-t", "wb-alpha"]


def test_kill_session_false_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", _RunRecorder([_completed(1)]))
    # Absent ≠ error — we collapse to False for best-effort cleanup.
    assert SubprocessTmuxController().kill_session("wb-gone") is False


# ---- send_text ---------------------------------------------------------


def test_send_text_two_steps_literal_then_enter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _RunRecorder([_completed(0), _completed(0)])
    monkeypatch.setattr(subprocess, "run", rec)
    SubprocessTmuxController().send_text("wb-alpha", "echo hello")
    assert rec.calls[0] == [
        "tmux",
        "send-keys",
        "-l",
        "-t",
        "wb-alpha",
        "--",
        "echo hello",
    ]
    assert rec.calls[1] == ["tmux", "send-keys", "-t", "wb-alpha", "Enter"]


def test_send_text_raises_when_literal_step_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _RunRecorder([_completed(1, stderr="no such session")]),
    )
    with pytest.raises(TmuxError, match="send-keys -l"):
        SubprocessTmuxController().send_text("wb-gone", "hi")


def test_send_text_raises_when_enter_step_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _RunRecorder([_completed(0), _completed(1, stderr="?!")]),
    )
    with pytest.raises(TmuxError, match="Enter"):
        SubprocessTmuxController().send_text("wb-alpha", "hi")


def test_send_text_preserves_special_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _RunRecorder([_completed(0), _completed(0)])
    monkeypatch.setattr(subprocess, "run", rec)
    payload = "echo $HOME ; ls *.py && cat 'foo bar'"
    SubprocessTmuxController().send_text("wb-alpha", payload)
    # The literal flag + ``--`` separator is what keeps tmux from
    # re-interpreting any of these. The content lands unmodified.
    assert rec.calls[0][-1] == payload


# ---- set_status --------------------------------------------------------


def test_set_status_configures_style_and_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _RunRecorder([_completed(0), _completed(0)])
    monkeypatch.setattr(subprocess, "run", rec)
    SubprocessTmuxController().set_status(
        "wb-alpha", color="green", label="🟢 NORMAL"
    )
    assert rec.calls[0] == [
        "tmux",
        "set-option",
        "-t",
        "wb-alpha",
        "status-style",
        "bg=green,fg=white",
    ]
    assert rec.calls[1] == [
        "tmux",
        "set-option",
        "-t",
        "wb-alpha",
        "status-right",
        "🟢 NORMAL",
    ]


def test_set_status_raises_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _RunRecorder([_completed(1, stderr="unknown option")]),
    )
    with pytest.raises(TmuxError, match="status-style"):
        SubprocessTmuxController().set_status(
            "wb-alpha", color="green", label=""
        )


# ---- custom tmux binary ------------------------------------------------


def test_custom_tmux_binary_is_respected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _RunRecorder([_completed(0)])
    monkeypatch.setattr(subprocess, "run", rec)
    SubprocessTmuxController(tmux_binary="/opt/tmux").has_session("x")
    assert rec.calls[0][0] == "/opt/tmux"
