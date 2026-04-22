"""ProjectService — Use-Cases over the project store + filesystem.

Two layers of work:

* **Filesystem**: Each project owns ``~/projekte/<name>/`` with a few
  bot-specific dotfiles (``.whatsbot/`` for outputs, ``.claudeignore`` etc.
  later). Phase 2.1 just creates the directory; .claudeignore + CLAUDE.md
  templates land in C2.2 with the git-clone path.

* **Persistence**: The ``projects`` row via ``ProjectRepository``.

Active-project tracking (``/p <name>``) lives in ``app_state`` as the
``active_project`` row — that's wired in C2.2/C2.3 once switching matters.
For C2.1 we only need create + list.
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from whatsbot.domain.projects import (
    Mode,
    Project,
    ProjectListing,
    SourceMode,
    validate_project_name,
)
from whatsbot.logging_setup import get_logger
from whatsbot.ports.project_repository import (
    ProjectAlreadyExistsError,
    ProjectRepository,
)


class ProjectFilesystemError(RuntimeError):
    """Raised when the on-disk project directory can't be (un)created."""


class ProjectService:
    """High-level operations on the project collection."""

    def __init__(
        self,
        repository: ProjectRepository,
        conn: sqlite3.Connection,
        projects_root: Path,
    ) -> None:
        self._repo = repository
        self._conn = conn
        self._projects_root = projects_root
        self._log = get_logger("whatsbot.projects")

    # ---- create -----------------------------------------------------------

    def create_empty(self, raw_name: str) -> Project:
        """Create an empty project: validate name, allocate dir, persist row.

        Failure modes (all surface as raised exceptions to the caller, which
        the command handler turns into user-visible replies):
            - ``InvalidProjectNameError`` — bad name
            - ``ProjectAlreadyExistsError`` — duplicate
            - ``ProjectFilesystemError`` — directory unavailable
        """
        name = validate_project_name(raw_name)
        path = self._projects_root / name

        if self._repo.exists(name):
            raise ProjectAlreadyExistsError(f"Projekt '{name}' existiert schon.")
        if path.exists():
            # FS-state out of sync with DB — refuse to clobber.
            raise ProjectFilesystemError(
                f"Verzeichnis {path} existiert schon, gehört aber zu keinem"
                f" registrierten Projekt. Manuell aufräumen."
            )

        try:
            path.mkdir(parents=True, exist_ok=False)
            (path / ".whatsbot").mkdir(exist_ok=True)
            (path / ".whatsbot" / "outputs").mkdir(exist_ok=True)
        except OSError as exc:
            raise ProjectFilesystemError(f"Konnte {path} nicht anlegen: {exc}") from exc

        project = Project(
            name=name,
            source_mode=SourceMode.EMPTY,
            created_at=datetime.now(UTC),
            mode=Mode.NORMAL,
        )
        try:
            self._repo.create(project)
        except Exception:
            # Roll back the directory so a failed insert doesn't leave
            # filesystem state behind.
            self._safe_rmdir(path)
            raise

        self._log.info(
            "project_created",
            name=name,
            source_mode=project.source_mode.value,
            path=str(path),
        )
        return project

    # ---- read -------------------------------------------------------------

    def list_all(self, *, active_name: str | None = None) -> list[ProjectListing]:
        return [
            ProjectListing(project=p, is_active=(p.name == active_name))
            for p in self._repo.list_all()
        ]

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _safe_rmdir(path: Path) -> None:
        # Best-effort rollback: only delete dirs we know we created (the
        # .whatsbot subdir + the project root). Don't recurse into anything
        # the user might have started filling in the meantime.
        for sub in (path / ".whatsbot" / "outputs", path / ".whatsbot", path):
            # Either non-empty (user dropped files into it) or never existed
            # — both safe to ignore during rollback.
            with contextlib.suppress(OSError):
                sub.rmdir()
