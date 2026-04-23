"""Integration tests for ``bin/watchdog.sh`` (Phase 6 C6.4).

We invoke the real shell script via subprocess against a temp
heartbeat path, panic-marker path, and log path. ``tmux`` /
``pkill`` / ``osascript`` get redirected to no-op stubs on PATH so
we never touch the developer's real sessions.

Skipped when ``bash`` isn't available (it always is on macOS+Linux,
but tests should be defensive).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("bash") is None, reason="bash not available"
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "bin" / "watchdog.sh"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    """A directory with no-op ``tmux``, ``pkill``, ``osascript`` stubs.

    Each stub records its argv to ``stub-calls.log`` so the tests can
    assert what the script invoked. They never touch the real system.
    """
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    log = tmp_path / "stub-calls.log"
    template = textwrap.dedent(
        """\
        #!/usr/bin/env bash
        echo "{name} $@" >> "{log}"
        if [ "{name}" = "tmux" ] && [ "$1" = "list-sessions" ]; then
            echo "wb-stub-a"
            echo "wb-stub-b"
            echo "personal"
        fi
        exit 0
        """
    )
    for name in ("tmux", "pkill", "osascript"):
        path = bindir / name
        path.write_text(template.format(name=name, log=log), encoding="utf-8")
        path.chmod(0o755)
    return bindir


def _run(
    workdir: Path,
    stub_bin: Path,
    *,
    heartbeat_age_seconds: float | None = None,
    panic_marker: bool = False,
    threshold: int = 120,
    heartbeat_pid: int | None = None,
    fake_uptime: int | None = None,
    boot_grace: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke watchdog.sh with the given heartbeat-state setup."""
    heartbeat_path = workdir / "whatsbot-heartbeat"
    panic_path = workdir / "whatsbot-PANIC"
    log_path = workdir / "logs" / "watchdog.jsonl"

    if heartbeat_age_seconds is not None:
        body = "seed"
        if heartbeat_pid is not None:
            body = f"whatsbot heartbeat\npid={heartbeat_pid}\n"
        heartbeat_path.write_text(body, encoding="utf-8")
        # Roll mtime backwards by ``heartbeat_age_seconds``.
        target_mtime = time.time() - heartbeat_age_seconds
        os.utime(heartbeat_path, (target_mtime, target_mtime))

    if panic_marker:
        panic_path.write_text("", encoding="utf-8")

    env = {
        **os.environ,
        "PATH": f"{stub_bin}:{os.environ.get('PATH', '')}",
        "WHATSBOT_HEARTBEAT": str(heartbeat_path),
        "WHATSBOT_PANIC_MARKER": str(panic_path),
        "WHATSBOT_WATCHDOG_LOG": str(log_path),
        "WHATSBOT_WATCHDOG_STALE_SECONDS": str(threshold),
    }
    if fake_uptime is not None:
        env["WHATSBOT_WATCHDOG_FAKE_UPTIME"] = str(fake_uptime)
    if boot_grace is not None:
        env["WHATSBOT_WATCHDOG_BOOT_GRACE_SECONDS"] = str(boot_grace)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _read_log(workdir: Path) -> list[dict[str, object]]:
    log = workdir / "logs" / "watchdog.jsonl"
    if not log.exists():
        return []
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(ln) for ln in lines if ln.startswith("{")]


def _read_stub_calls(workdir: Path) -> list[str]:
    log = workdir / "stub-calls.log"
    if not log.exists():
        return []
    return log.read_text(encoding="utf-8").strip().splitlines()


# ---- panic-marker short-circuit ---------------------------------


def test_watchdog_skips_when_panic_marker_present(
    workdir: Path, stub_bin: Path
) -> None:
    proc = _run(
        workdir,
        stub_bin,
        heartbeat_age_seconds=999.0,  # would normally trigger
        panic_marker=True,
    )
    assert proc.returncode == 0
    events = [r["event"] for r in _read_log(workdir)]
    assert "watchdog_skip_panic_active" in events
    # No tmux/pkill calls should have happened.
    assert _read_stub_calls(workdir) == []


# ---- alive case ---------------------------------------------------


def test_watchdog_quiet_when_heartbeat_fresh(
    workdir: Path, stub_bin: Path
) -> None:
    proc = _run(
        workdir,
        stub_bin,
        heartbeat_age_seconds=10.0,
        threshold=120,
    )
    assert proc.returncode == 0
    events = [r["event"] for r in _read_log(workdir)]
    assert "watchdog_alive" in events
    assert "watchdog_engaged" not in events
    assert _read_stub_calls(workdir) == []


def test_watchdog_just_below_threshold_still_alive(
    workdir: Path, stub_bin: Path
) -> None:
    proc = _run(
        workdir, stub_bin, heartbeat_age_seconds=119, threshold=120
    )
    assert proc.returncode == 0
    events = [r["event"] for r in _read_log(workdir)]
    assert "watchdog_alive" in events


# ---- engaged case ------------------------------------------------


def test_watchdog_engages_when_heartbeat_missing(
    workdir: Path, stub_bin: Path
) -> None:
    """Missing heartbeat = bot never started or has been killed —
    treat as stale."""
    proc = _run(workdir, stub_bin, heartbeat_age_seconds=None)
    assert proc.returncode == 0
    events = [r["event"] for r in _read_log(workdir)]
    assert "watchdog_engaged" in events
    assert "watchdog_pkill_done" in events


def test_watchdog_engages_when_heartbeat_stale(
    workdir: Path, stub_bin: Path
) -> None:
    proc = _run(
        workdir, stub_bin, heartbeat_age_seconds=999.0, threshold=120
    )
    assert proc.returncode == 0
    events = [r["event"] for r in _read_log(workdir)]
    assert "watchdog_engaged" in events
    # tmux + pkill stubs were called.
    calls = _read_stub_calls(workdir)
    assert any(call.startswith("tmux list-sessions") for call in calls)
    # Both wb-* sessions should have been killed individually.
    kill_calls = [c for c in calls if c.startswith("tmux kill-session")]
    assert any("wb-stub-a" in c for c in kill_calls)
    assert any("wb-stub-b" in c for c in kill_calls)
    # personal (non-wb) was NOT killed.
    assert not any("personal" in c for c in kill_calls)
    # pkill was called with the safe-claude pattern.
    assert any(
        c.startswith("pkill") and "safe-claude" in c for c in calls
    )


def test_watchdog_writes_panic_marker_after_engaging(
    workdir: Path, stub_bin: Path
) -> None:
    """The bot, when it comes back up, reads the panic marker as
    'lockdown engaged by the watchdog'."""
    proc = _run(workdir, stub_bin, heartbeat_age_seconds=999.0)
    assert proc.returncode == 0
    panic_marker = workdir / "whatsbot-PANIC"
    assert panic_marker.exists()


def test_watchdog_emits_notification(
    workdir: Path, stub_bin: Path
) -> None:
    _run(workdir, stub_bin, heartbeat_age_seconds=999.0)
    calls = _read_stub_calls(workdir)
    osa_calls = [c for c in calls if c.startswith("osascript ")]
    assert osa_calls, "watchdog should call osascript on engage"


# ---- structured logging ------------------------------------------


def test_log_lines_are_valid_json_with_required_fields(
    workdir: Path, stub_bin: Path
) -> None:
    _run(workdir, stub_bin, heartbeat_age_seconds=10.0)
    rows = _read_log(workdir)
    assert rows, "expected at least one log line"
    for row in rows:
        # Must have ts, logger, level, event — joins the rest of our
        # whatsbot.* logs cleanly.
        assert "ts" in row
        assert row.get("logger") == "whatsbot.watchdog"
        assert "level" in row
        assert "event" in row


# ---- C6.5 sleep-grace + boot-grace ----------------------------


def test_pid_alive_grace_skips_engage_on_stale_heartbeat(
    workdir: Path, stub_bin: Path
) -> None:
    """Mac-Sleep scenario: heartbeat is stale because the bot was
    suspended along with the OS, but the bot's PID is still alive.
    Watchdog must NOT engage."""
    # Use the test process's own PID — guaranteed alive.
    own_pid = os.getpid()
    proc = _run(
        workdir,
        stub_bin,
        heartbeat_age_seconds=999.0,
        heartbeat_pid=own_pid,
        # Also pin uptime way past boot-grace to isolate the
        # PID-alive-grace path.
        fake_uptime=99_999,
    )
    assert proc.returncode == 0
    events = [r["event"] for r in _read_log(workdir)]
    assert "watchdog_grace_pid_alive" in events
    assert "watchdog_engaged" not in events
    # No tear-down side effects.
    assert _read_stub_calls(workdir) == []
    assert not (workdir / "whatsbot-PANIC").exists()


def test_dead_pid_still_engages_after_grace_window(
    workdir: Path, stub_bin: Path
) -> None:
    """Bot-died scenario: heartbeat has a PID that doesn't exist
    anymore. Watchdog engages normally."""
    # Spawn a short-lived child, capture its PID, wait for it to
    # die — that PID is now reliably dead.
    spawned = subprocess.run(
        ["bash", "-c", "echo $$"], capture_output=True, text=True, check=True
    )
    dead_pid = int(spawned.stdout.strip())
    # Be defensive: if by accident the PID was recycled, skip.
    if subprocess.run(
        ["kill", "-0", str(dead_pid)], capture_output=True, check=False
    ).returncode == 0:
        pytest.skip("PID was recycled, can't test dead-PID path")

    proc = _run(
        workdir,
        stub_bin,
        heartbeat_age_seconds=999.0,
        heartbeat_pid=dead_pid,
        fake_uptime=99_999,
    )
    assert proc.returncode == 0
    events = [r["event"] for r in _read_log(workdir)]
    assert "watchdog_engaged" in events
    assert "watchdog_grace_pid_alive" not in events


def test_boot_grace_skips_engage_on_missing_heartbeat(
    workdir: Path, stub_bin: Path
) -> None:
    """First wake-up after boot: bot might still be coming up,
    no heartbeat yet. Watchdog gives it grace."""
    proc = _run(
        workdir,
        stub_bin,
        heartbeat_age_seconds=None,  # missing
        fake_uptime=10,  # 10 s after boot
        boot_grace=300,
    )
    assert proc.returncode == 0
    events = [r["event"] for r in _read_log(workdir)]
    assert "watchdog_grace_recent_boot" in events
    assert "watchdog_engaged" not in events
    assert _read_stub_calls(workdir) == []


def test_no_boot_grace_after_uptime_window(
    workdir: Path, stub_bin: Path
) -> None:
    """Long-running system + missing heartbeat = real bot crash.
    Watchdog engages."""
    proc = _run(
        workdir,
        stub_bin,
        heartbeat_age_seconds=None,
        fake_uptime=99_999,
        boot_grace=300,
    )
    assert proc.returncode == 0
    events = [r["event"] for r in _read_log(workdir)]
    assert "watchdog_engaged" in events
    assert "watchdog_grace_recent_boot" not in events


def test_pid_alive_check_works_without_pid_field(
    workdir: Path, stub_bin: Path
) -> None:
    """Old heartbeat format without 'pid=' line: PID-grace can't
    apply, watchdog falls through to its normal engage path."""
    # Use the legacy "seed" body — no pid= line.
    proc = _run(
        workdir,
        stub_bin,
        heartbeat_age_seconds=999.0,
        heartbeat_pid=None,  # → uses 'seed' body
        fake_uptime=99_999,
    )
    assert proc.returncode == 0
    events = [r["event"] for r in _read_log(workdir)]
    assert "watchdog_engaged" in events
    assert "watchdog_grace_pid_alive" not in events


def _ensure_iter() -> None:  # pragma: no cover
    # Silence the otherwise-unused Iterator import.
    _: Iterator[int]  # type: ignore[type-arg]


__all__ = ["_ensure_iter"]
