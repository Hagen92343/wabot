"""Unit tests for the launchd plist templates.

We don't invoke ``launchctl`` here — that's an integration concern handled
manually via ``make deploy-launchd``. These tests verify the templates render
to valid Apple Property Lists with all the required keys.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_BOT = REPO_ROOT / "launchd" / "com.DOMAIN.whatsbot.plist.template"
TEMPLATE_BAK = REPO_ROOT / "launchd" / "com.DOMAIN.whatsbot.backup.plist.template"

_BOT_VARS: dict[str, str] = {
    "DOMAIN": "local",
    "UVICORN": "/repo/venv/bin/uvicorn",
    "REPO_DIR": "/repo",
    "WHATSBOT_PORT": "8000",
    "WHATSBOT_ENV": "prod",
    "SSH_AUTH_SOCK": "/Users/me/.ssh/agent.sock",
    "LOG_DIR": "/Users/me/Library/Logs/whatsbot",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/Users/me",
}

_BAK_VARS: dict[str, str] = {
    "DOMAIN": "local",
    "REPO_DIR": "/repo",
    "LOG_DIR": "/Users/me/Library/Logs/whatsbot",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/Users/me",
}


def _render(template_path: Path, **substitutions: str) -> dict[str, Any]:
    text = template_path.read_text(encoding="utf-8")
    for key, value in substitutions.items():
        text = text.replace(f"__{key}__", value)
    leftover = re.findall(r"__[A-Z_]+__", text)
    assert leftover == [], f"unfilled placeholders in {template_path.name}: {sorted(set(leftover))}"
    return plistlib.loads(text.encode("utf-8"))


# --- Bot template -----------------------------------------------------------


def test_bot_template_label_uses_domain() -> None:
    plist = _render(TEMPLATE_BOT, **_BOT_VARS)
    assert plist["Label"] == "com.local.whatsbot"


def test_bot_template_keepalive_only_on_failure() -> None:
    plist = _render(TEMPLATE_BOT, **_BOT_VARS)
    # Spec §22 / phase-1.md: KeepAlive must restart on crash but not on
    # graceful exit (so /panic actually stops the bot).
    assert plist["KeepAlive"] == {"SuccessfulExit": False}


def test_bot_template_runs_at_load() -> None:
    plist = _render(TEMPLATE_BOT, **_BOT_VARS)
    assert plist["RunAtLoad"] is True


def test_bot_template_invokes_uvicorn_with_factory_args() -> None:
    plist = _render(TEMPLATE_BOT, **_BOT_VARS)
    args = plist["ProgramArguments"]
    assert args[0] == "/repo/venv/bin/uvicorn"
    assert "whatsbot.main:create_app" in args
    assert "--factory" in args
    assert "--host" in args and "127.0.0.1" in args
    assert "--port" in args and "8000" in args


def test_bot_template_environment_variables() -> None:
    plist = _render(TEMPLATE_BOT, **_BOT_VARS)
    env = plist["EnvironmentVariables"]
    assert env["WHATSBOT_ENV"] == "prod"
    assert env["SSH_AUTH_SOCK"] == "/Users/me/.ssh/agent.sock"
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert env["HOME"] == "/Users/me"


def test_bot_template_log_paths_under_log_dir() -> None:
    plist = _render(TEMPLATE_BOT, **_BOT_VARS)
    log_dir = _BOT_VARS["LOG_DIR"]
    assert plist["StandardOutPath"] == f"{log_dir}/launchd-stdout.log"
    assert plist["StandardErrorPath"] == f"{log_dir}/launchd-stderr.log"


def test_bot_template_working_dir_is_repo() -> None:
    plist = _render(TEMPLATE_BOT, **_BOT_VARS)
    assert plist["WorkingDirectory"] == "/repo"


def test_bot_template_marks_process_as_background() -> None:
    plist = _render(TEMPLATE_BOT, **_BOT_VARS)
    assert plist["ProcessType"] == "Background"


# --- Backup template --------------------------------------------------------


def test_backup_template_label_includes_backup_suffix() -> None:
    plist = _render(TEMPLATE_BAK, **_BAK_VARS)
    assert plist["Label"] == "com.local.whatsbot.backup"


def test_backup_template_runs_at_3am() -> None:
    plist = _render(TEMPLATE_BAK, **_BAK_VARS)
    # Spec §22: tägliches DB-Backup um 03:00.
    assert plist["StartCalendarInterval"] == {"Hour": 3, "Minute": 0}


def test_backup_template_does_not_run_at_load() -> None:
    plist = _render(TEMPLATE_BAK, **_BAK_VARS)
    assert plist["RunAtLoad"] is False


def test_backup_template_invokes_backup_db_script() -> None:
    plist = _render(TEMPLATE_BAK, **_BAK_VARS)
    args = plist["ProgramArguments"]
    assert args[0] == "/bin/bash"
    assert args[1] == "/repo/bin/backup-db.sh"


def test_backup_template_log_paths_under_log_dir() -> None:
    plist = _render(TEMPLATE_BAK, **_BAK_VARS)
    log_dir = _BAK_VARS["LOG_DIR"]
    assert plist["StandardOutPath"] == f"{log_dir}/launchd-backup-stdout.log"
    assert plist["StandardErrorPath"] == f"{log_dir}/launchd-backup-stderr.log"
