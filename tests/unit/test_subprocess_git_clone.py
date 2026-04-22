"""Unit tests for whatsbot.adapters.subprocess_git_clone.

We don't shell out to the real git binary here — instead we redirect
``git`` to a small helper script we drop into a tmp dir on PATH. That
gives us deterministic exit codes, stderr, and timing without making
the test suite touch the network.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from whatsbot.adapters.subprocess_git_clone import SubprocessGitClone
from whatsbot.ports.git_clone import GitCloneError

pytestmark = pytest.mark.unit


@pytest.fixture
def fake_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Drop a fake `git` binary on PATH so we control its behaviour."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    yield bin_dir
    # PATH cleanup happens automatically via monkeypatch teardown.


def _install_fake(bin_dir: Path, body: str) -> Path:
    script = bin_dir / "git"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _set_path(monkeypatch: pytest.MonkeyPatch, bin_dir: Path) -> None:
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


# --- happy path ------------------------------------------------------------


def test_clone_succeeds_when_git_returns_zero(
    fake_git: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Fake git that creates the dest dir and exits 0 — mimics a real clone.
    # Args: $1=clone $2=--depth $3=<n> $4=--quiet $5=<url> $6=<dest>
    _install_fake(
        fake_git,
        'mkdir -p "$6" && echo "fake clone" > "$6/README" && exit 0',
    )
    _set_path(monkeypatch, fake_git)

    dest = tmp_path / "out"
    SubprocessGitClone().clone("https://github.com/o/r", dest)
    assert (dest / "README").read_text() == "fake clone\n"


def test_clone_passes_depth_and_quiet_args(
    fake_git: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Args 1..N: clone --depth 50 --quiet <url> <dest>."""
    _install_fake(
        fake_git,
        'echo "$1 $2 $3 $4 $5 $6" > "$TMPDIR/git.args" && mkdir -p "$6" && exit 0',
    )
    _set_path(monkeypatch, fake_git)

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    dest = tmp_path / "out"
    SubprocessGitClone().clone("https://github.com/o/r", dest, depth=42)

    captured = (tmp_path / "git.args").read_text().strip()
    assert "clone" in captured
    assert "--depth 42" in captured
    assert "--quiet" in captured
    assert "https://github.com/o/r" in captured


# --- failure paths ---------------------------------------------------------


def test_clone_raises_when_git_returns_nonzero(
    fake_git: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake(
        fake_git,
        'echo "fatal: repository not found" >&2 && exit 128',
    )
    _set_path(monkeypatch, fake_git)

    with pytest.raises(GitCloneError, match="exit 128"):
        SubprocessGitClone().clone("https://github.com/o/r", tmp_path / "out")


def test_clone_includes_stderr_tail_in_message(
    fake_git: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake(
        fake_git,
        'echo "Permission denied (publickey)." >&2 && exit 128',
    )
    _set_path(monkeypatch, fake_git)

    with pytest.raises(GitCloneError, match="Permission denied"):
        SubprocessGitClone().clone("git@github.com:o/r", tmp_path / "out")


def test_clone_raises_when_git_binary_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(GitCloneError, match="nicht gefunden"):
        SubprocessGitClone(git_binary="not-a-real-binary").clone(
            "https://github.com/o/r", tmp_path / "out"
        )


def test_clone_raises_on_timeout(
    fake_git: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake(fake_git, "sleep 5 && exit 0")  # longer than the timeout
    _set_path(monkeypatch, fake_git)

    with pytest.raises(GitCloneError, match="timeout"):
        SubprocessGitClone().clone("https://github.com/o/r", tmp_path / "out", timeout_seconds=0.5)
