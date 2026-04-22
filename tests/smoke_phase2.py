"""Phase-2 in-process smoke test.

Drives the full ``CommandHandler`` against an isolated temp tree + in-memory
SQLite DB. No Keychain, no LaunchAgent, no network — just the application
layer end-to-end. Runs all the Phase-2 flows mentioned in current-phase.md
section C2.8 and prints pass/fail per step.

Usage:
    source venv/bin/activate
    python tests/smoke_phase2.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_allow_rule_repository import SqliteAllowRuleRepository
from whatsbot.adapters.sqlite_app_state_repository import SqliteAppStateRepository
from whatsbot.adapters.sqlite_pending_delete_repository import (
    SqlitePendingDeleteRepository,
)
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.application.active_project_service import ActiveProjectService
from whatsbot.application.allow_service import AllowService
from whatsbot.application.command_handler import CommandHandler
from whatsbot.application.delete_service import DeleteService
from whatsbot.application.project_service import ProjectService
from whatsbot.ports.git_clone import GitClone
from whatsbot.ports.secrets_provider import KEY_PANIC_PIN


class StubGitClone(GitClone):
    """Creates a deterministic npm + git scaffold instead of hitting the network."""

    def clone(
        self,
        url: str,
        dest: Path,
        *,
        depth: int = 50,
        timeout_seconds: float = 180.0,
    ) -> None:
        dest.mkdir(parents=True, exist_ok=False)
        (dest / "package.json").write_text('{"name":"smokegit","version":"0.0.0"}')
        (dest / ".git").mkdir()
        (dest / ".git" / "config").write_text("[core]\nrepositoryformatversion = 0\n")
        (dest / "README.md").write_text("smoke repo")


class StubSecrets:
    """Pre-seeded panic-pin = '1234' for /rm confirm."""

    def __init__(self) -> None:
        self._store = {KEY_PANIC_PIN: "1234"}

    def get(self, key: str) -> str:
        from whatsbot.ports.secrets_provider import SecretNotFoundError

        if key not in self._store:
            raise SecretNotFoundError(key)
        return self._store[key]

    def set(self, key: str, value: str) -> None:
        self._store[key] = value

    def rotate(self, key: str, new_value: str) -> None:
        self._store[key] = new_value


PASSES: list[str] = []
FAILS: list[str] = []


def step(label: str, condition: bool, detail: str = "") -> None:
    marker = "✓" if condition else "✗"
    tag = f" — {detail}" if detail else ""
    line = f"  {marker} {label}{tag}"
    print(line)
    (PASSES if condition else FAILS).append(label)


def build_handler(tmp: Path) -> tuple[CommandHandler, Path, Path]:
    conn = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(conn)
    projects_root = tmp / "projekte"
    projects_root.mkdir()
    trash_root = tmp / "trash"
    trash_root.mkdir()
    project_repo = SqliteProjectRepository(conn)
    project_service = ProjectService(
        repository=project_repo,
        conn=conn,
        projects_root=projects_root,
        git_clone=StubGitClone(),
    )
    allow_service = AllowService(
        rule_repo=SqliteAllowRuleRepository(conn),
        project_repo=project_repo,
        projects_root=projects_root,
    )
    app_state = SqliteAppStateRepository(conn)
    active = ActiveProjectService(app_state=app_state, projects=project_repo)
    delete = DeleteService(
        pending_repo=SqlitePendingDeleteRepository(conn),
        project_repo=project_repo,
        app_state=app_state,
        secrets=StubSecrets(),
        projects_root=projects_root,
        trash_root=trash_root,
    )
    handler = CommandHandler(
        project_service=project_service,
        allow_service=allow_service,
        active_project=active,
        delete_service=delete,
        version="0.1.0-smoke",
        started_at_monotonic=time.monotonic(),
        env="smoke",
    )
    return handler, projects_root, trash_root


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="whatsbot-smoke-"))
    try:
        h, projects_root, trash_root = build_handler(tmp)

        print("── Phase-2 Smoke ──")

        # --- /new empty ----------------------------------------------------
        r = h.handle("/new smoketest")
        step("/new smoketest → angelegt", "smoketest" in r.reply and "✅" in r.reply, r.reply.replace("\n", " | "))

        # --- /ls shows it --------------------------------------------------
        r = h.handle("/ls")
        step("/ls listet smoketest", "smoketest" in r.reply)

        # --- /new git + smart-detection -----------------------------------
        r = h.handle("/new smokegit git https://github.com/octocat/Hello-World")
        step(
            "/new git mit Smart-Detection → npm + .git Rules",
            "geklont" in r.reply and "package.json" in r.reply and ".git" in r.reply,
            r.reply.replace("\n", " | "),
        )

        # --- /p active project --------------------------------------------
        r = h.handle("/p smokegit")
        step("/p smokegit → aktiv", "aktiv" in r.reply.lower() and "smokegit" in r.reply)

        # --- /allow batch review -----------------------------------------
        r = h.handle("/allow batch review")
        step(
            "/allow batch review → listet Vorschläge",
            "Vorschlaege" in r.reply and "Bash(npm test)" in r.reply,
        )

        # --- /allow batch approve ----------------------------------------
        r = h.handle("/allow batch approve")
        step(
            "/allow batch approve → N neue Rules übernommen",
            "neue Rules" in r.reply,
            r.reply.replace("\n", " | "),
        )

        # --- /allow manual single-rule add --------------------------------
        r = h.handle("/allow Bash(echo hi)")
        step("/allow Bash(echo hi) → hinzugefügt", "Bash(echo hi)" in r.reply and "✅" in r.reply)

        # --- /allowlist sections ----------------------------------------
        r = h.handle("/allowlist")
        step(
            "/allowlist zeigt beide Sources",
            "[smart_detection]" in r.reply and "[manual]" in r.reply,
        )

        # --- /deny removes rule ----------------------------------------
        r = h.handle("/deny Bash(echo hi)")
        step("/deny Bash(echo hi) → entfernt", "🗑" in r.reply)

        # --- URL-Whitelist blocks off-host ----------------------------
        r = h.handle("/new evilproj git https://evil.example.com/x/y")
        step(
            "URL-Whitelist blockt evil.example.com",
            "🚫" in r.reply and "nicht erlaubt" in r.reply.lower(),
        )

        # --- /rm → 60s window ----------------------------------------
        r = h.handle("/rm smoketest")
        step(
            "/rm smoketest → 60s Bestätigungs-Fenster",
            "Bestätige" in r.reply and "60" in r.reply,
        )

        # --- /rm with wrong PIN -------------------------------------
        r = h.handle("/rm smoketest 9999")
        step(
            "/rm smoketest mit falscher PIN → abgelehnt, Projekt bleibt",
            "Falsche PIN" in r.reply,
        )
        step(
            "  · Projektverzeichnis existiert weiterhin",
            (projects_root / "smoketest").exists(),
        )

        # --- /rm confirm ---------------------------------------------
        r = h.handle("/rm smoketest 1234")
        step(
            "/rm smoketest 1234 → gelöscht + in Trash",
            "gelöscht" in r.reply and "🗑" in r.reply,
        )
        step(
            "  · Projektverzeichnis weg",
            not (projects_root / "smoketest").exists(),
        )
        trash_matches = list(trash_root.glob("whatsbot-smoketest-*"))
        step(
            "  · Trash-Kopie unter whatsbot-smoketest-*",
            len(trash_matches) == 1 and trash_matches[0].is_dir(),
            str(trash_matches[0]) if trash_matches else "(keine)",
        )

        # --- /ls after delete ----------------------------------------
        r = h.handle("/ls")
        step(
            "/ls zeigt smoketest nicht mehr",
            "smoketest" not in r.reply and "smokegit" in r.reply,
        )

        # --- unknown command fallback -----------------------------------
        r = h.handle("/nope")
        step("unbekanntes Kommando → freundlicher Hinweis", "unbekanntes Kommando" in r.reply)

        print()
        print(f"── Ergebnis: {len(PASSES)} passed, {len(FAILS)} failed ──")
        return 0 if not FAILS else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
