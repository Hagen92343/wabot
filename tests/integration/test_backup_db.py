"""Integration tests for bin/backup-db.sh.

We invoke the real shell script via subprocess against a temporary DB and
backup directory (set via env vars), then assert on the produced backup
file, the structured-log line on stdout, and the retention behaviour.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "bin" / "backup-db.sh"


def _seed_db(path: Path) -> None:
    conn = sqlite_repo.connect(path)
    sqlite_repo.apply_schema(conn)
    conn.execute(
        "INSERT INTO projects(name, source_mode, created_at) "
        "VALUES ('seed', 'empty', '2026-01-01')"
    )
    conn.close()


def _run(
    db: Path, backup_dir: Path, *, retention_days: int = 30
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "WHATSBOT_DB": str(db),
        "WHATSBOT_BACKUP_DIR": str(backup_dir),
        "WHATSBOT_BACKUP_RETENTION_DAYS": str(retention_days),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _last_json_line(text: str) -> dict[str, object]:
    line = [ln for ln in text.strip().splitlines() if ln.startswith("{")][-1]
    return json.loads(line)  # type: ignore[no-any-return]


# --- happy path ------------------------------------------------------------


def test_backup_creates_dated_file_with_intact_schema(
    tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    _seed_db(tmp_db_path)

    proc = _run(tmp_db_path, tmp_backup_dir)
    assert proc.returncode == 0, proc.stderr

    backups = list(tmp_backup_dir.glob("state.db.*"))
    assert len(backups) == 1
    backup_file = backups[0]
    assert backup_file.stat().st_size > 0

    # Read the backup as a real SQLite DB and confirm the seed row survived.
    conn = sqlite3.connect(backup_file)
    try:
        row = conn.execute("SELECT name FROM projects").fetchone()
        assert row[0] == "seed"
    finally:
        conn.close()


def test_backup_emits_structured_complete_event(tmp_db_path: Path, tmp_backup_dir: Path) -> None:
    _seed_db(tmp_db_path)
    proc = _run(tmp_db_path, tmp_backup_dir)
    assert proc.returncode == 0

    payload = _last_json_line(proc.stdout)
    assert payload["event"] == "backup_complete"
    assert payload["retention_days"] == 30
    assert payload["deleted_old"] == 0
    assert int(payload["size_bytes"]) > 0  # type: ignore[arg-type]
    assert str(payload["target"]).startswith(str(tmp_backup_dir))


def test_backup_is_idempotent_on_same_day(tmp_db_path: Path, tmp_backup_dir: Path) -> None:
    _seed_db(tmp_db_path)

    first = _run(tmp_db_path, tmp_backup_dir)
    assert first.returncode == 0
    second = _run(tmp_db_path, tmp_backup_dir)
    assert second.returncode == 0

    # Same date suffix — overwrite, not duplicate.
    backups = list(tmp_backup_dir.glob("state.db.*"))
    assert len(backups) == 1


# --- skip-on-missing -------------------------------------------------------


def test_backup_skipped_when_db_missing(tmp_db_path: Path, tmp_backup_dir: Path) -> None:
    # tmp_db_path was never created.
    proc = _run(tmp_db_path, tmp_backup_dir)
    assert proc.returncode == 0
    payload = _last_json_line(proc.stdout)
    assert payload["event"] == "backup_skipped_no_db"
    assert not tmp_backup_dir.exists()


# --- retention -------------------------------------------------------------


def test_retention_deletes_files_older_than_threshold(
    tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    _seed_db(tmp_db_path)
    tmp_backup_dir.mkdir(parents=True, exist_ok=True)

    # Plant two old backups (60 and 31 days back) and one fresh (1 day back).
    now = time.time()
    days = 86400
    old_60 = tmp_backup_dir / "state.db.2025-12-01"
    old_31 = tmp_backup_dir / "state.db.2026-01-15"
    fresh = tmp_backup_dir / "state.db.2026-04-21"
    for f, age_days in ((old_60, 60), (old_31, 31), (fresh, 1)):
        f.write_bytes(b"placeholder")
        os.utime(f, (now - age_days * days, now - age_days * days))

    proc = _run(tmp_db_path, tmp_backup_dir, retention_days=30)
    assert proc.returncode == 0

    # The fresh decoy AND today's new backup must remain. Both old ones go.
    remaining = sorted(p.name for p in tmp_backup_dir.glob("state.db.*"))
    assert "state.db.2025-12-01" not in remaining
    assert "state.db.2026-01-15" not in remaining
    assert "state.db.2026-04-21" in remaining
    # Today's backup is also present (named with current date)
    today_backups = [p for p in remaining if p != "state.db.2026-04-21"]
    assert len(today_backups) == 1

    payload = _last_json_line(proc.stdout)
    assert payload["deleted_old"] == 2


def test_retention_threshold_keeps_files_below_age_limit(
    tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    _seed_db(tmp_db_path)
    tmp_backup_dir.mkdir(parents=True, exist_ok=True)

    young = tmp_backup_dir / "state.db.2026-04-15"
    young.write_bytes(b"placeholder")
    # 5 days old — below the 30-day threshold, must survive.
    five_days_ago = time.time() - 5 * 86400
    os.utime(young, (five_days_ago, five_days_ago))

    proc = _run(tmp_db_path, tmp_backup_dir, retention_days=30)
    assert proc.returncode == 0

    payload = _last_json_line(proc.stdout)
    assert payload["deleted_old"] == 0
    assert young.exists()


def test_retention_zero_days_deletes_everything_older_than_one_day(
    tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    """Aggressive retention (e.g. RETENTION_DAYS=0 in tests) should still
    spare the fresh backup that the script just wrote."""
    _seed_db(tmp_db_path)
    tmp_backup_dir.mkdir(parents=True, exist_ok=True)

    yesterday = tmp_backup_dir / "state.db.2026-04-21"
    yesterday.write_bytes(b"x")
    one_day_back = time.time() - 86400 - 60  # just over a day
    os.utime(yesterday, (one_day_back, one_day_back))

    proc = _run(tmp_db_path, tmp_backup_dir, retention_days=0)
    assert proc.returncode == 0
    assert not yesterday.exists()
    # Today's freshly-written backup survives (mtime ~now, find -mtime +0
    # only matches files older than 1 full 24h period).
    today = list(tmp_backup_dir.glob("state.db.*"))
    assert len(today) == 1
