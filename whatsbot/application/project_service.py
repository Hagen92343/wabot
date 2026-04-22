"""ProjectService — Use-Cases over the project store + filesystem.

Two layers of work:

* **Filesystem**: Each project owns ``~/projekte/<name>/`` with bot-managed
  dotfiles (``.whatsbot/``, ``.claudeignore``, ``CLAUDE.md`` template).
  Created by ``create_empty`` (just the dir layout) or ``create_from_git``
  (the dir + git clone + post-clone scaffolding + smart-detection).

* **Persistence**: The ``projects`` row via ``ProjectRepository``.

Active-project tracking (``/p <name>``) lives in ``app_state`` as the
``active_project`` row — that's wired in C2.3 once switching matters.
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from whatsbot.application import post_clone
from whatsbot.domain.git_url import validate_git_url
from whatsbot.domain.projects import (
    Mode,
    Project,
    ProjectListing,
    SourceMode,
    validate_project_name,
)
from whatsbot.domain.smart_detection import DetectionResult, detect
from whatsbot.logging_setup import get_logger
from whatsbot.ports.git_clone import GitClone, GitCloneError
from whatsbot.ports.project_repository import (
    ProjectAlreadyExistsError,
    ProjectRepository,
)


class ProjectFilesystemError(RuntimeError):
    """Raised when the on-disk project directory can't be (un)created."""


@dataclass(frozen=True, slots=True)
class GitCreationOutcome:
    """Result of ``create_from_git``: the persisted Project plus the
    smart-detection summary so the command handler can mention how many
    Allow-Rule suggestions are waiting."""

    project: Project
    detection: DetectionResult


class ProjectService:
    """High-level operations on the project collection."""

    def __init__(
        self,
        repository: ProjectRepository,
        conn: sqlite3.Connection,
        projects_root: Path,
        git_clone: GitClone | None = None,
    ) -> None:
        self._repo = repository
        self._conn = conn
        self._projects_root = projects_root
        self._git_clone = git_clone
        self._log = get_logger("whatsbot.projects")

    # ---- create empty -----------------------------------------------------

    def create_empty(self, raw_name: str) -> Project:
        """Create an empty project: validate name, allocate dir, persist row.

        Failure modes (all surface as raised exceptions to the caller, which
        the command handler turns into user-visible replies):
            - ``InvalidProjectNameError`` — bad name
            - ``ProjectAlreadyExistsError`` — duplicate
            - ``ProjectFilesystemError`` — directory unavailable
        """
        name = validate_project_name(raw_name)
        path = self._reserve_path(name)

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
            self._safe_rmdir_empty(path)
            raise

        self._log.info(
            "project_created",
            name=name,
            source_mode=project.source_mode.value,
            path=str(path),
        )
        return project

    # ---- create from git --------------------------------------------------

    def create_from_git(self, raw_name: str, raw_url: str) -> GitCreationOutcome:
        """Clone a repo into ``~/projekte/<name>/`` and register it.

        Steps:
            1. Validate name + URL (whitelist).
            2. Reserve the target path (refuse duplicates / leftover dirs).
            3. ``git clone --depth 50``.
            4. Write ``.claudeignore`` + ``.whatsbot/config.json`` +
               ``CLAUDE.md`` (only if missing) + ``.whatsbot/outputs/``.
            5. Run smart-detection, write ``suggested-rules.json`` if any.
            6. INSERT the row.

        Any failure between steps 3 and 6 wipes the cloned tree so the next
        attempt isn't blocked by leftover state.
        """
        if self._git_clone is None:
            raise ProjectFilesystemError(
                "GitClone-Adapter nicht konfiguriert; create_from_git "
                "verfuegbar erst nach DI-Setup in main.py."
            )
        name = validate_project_name(raw_name)
        url = validate_git_url(raw_url)
        path = self._reserve_path(name)

        try:
            self._git_clone.clone(url, path)
        except GitCloneError as exc:
            self._safe_rmtree(path)
            raise ProjectFilesystemError(f"git clone fehlgeschlagen: {exc}") from exc

        # Post-clone scaffolding. Wrap in try so we can roll back the entire
        # tree on any failure here too.
        try:
            (path / ".whatsbot").mkdir(exist_ok=True)
            (path / ".whatsbot" / "outputs").mkdir(exist_ok=True)
            post_clone.write_claudeignore(path)
            post_clone.write_config_json(
                path,
                project_name=name,
                source_url=url,
                source_mode=SourceMode.GIT.value,
            )
            post_clone.write_claude_md_if_missing(path, project_name=name)
            detection = detect(path)
            post_clone.write_suggested_rules(path, detection)
        except OSError as exc:
            self._safe_rmtree(path)
            raise ProjectFilesystemError(f"Post-clone scaffolding fehlgeschlagen: {exc}") from exc

        project = Project(
            name=name,
            source_mode=SourceMode.GIT,
            source=url,
            created_at=datetime.now(UTC),
            mode=Mode.NORMAL,
        )
        try:
            self._repo.create(project)
        except Exception:
            self._safe_rmtree(path)
            raise

        self._log.info(
            "project_cloned",
            name=name,
            source_url=url,
            artifacts=detection.artifacts_found,
            suggested_rules=len(detection.suggested_rules),
            path=str(path),
        )
        return GitCreationOutcome(project=project, detection=detection)

    # ---- read -------------------------------------------------------------

    def list_all(self, *, active_name: str | None = None) -> list[ProjectListing]:
        return [
            ProjectListing(project=p, is_active=(p.name == active_name))
            for p in self._repo.list_all()
        ]

    # ---- helpers ----------------------------------------------------------

    def _reserve_path(self, name: str) -> Path:
        """Verify name is free in DB and on disk, return the target path."""
        path = self._projects_root / name
        if self._repo.exists(name):
            raise ProjectAlreadyExistsError(f"Projekt '{name}' existiert schon.")
        if path.exists():
            raise ProjectFilesystemError(
                f"Verzeichnis {path} existiert schon, gehört aber zu keinem"
                f" registrierten Projekt. Manuell aufräumen."
            )
        return path

    @staticmethod
    def _safe_rmdir_empty(path: Path) -> None:
        # Best-effort rollback for create_empty: only the dirs we created.
        for sub in (path / ".whatsbot" / "outputs", path / ".whatsbot", path):
            with contextlib.suppress(OSError):
                sub.rmdir()

    @staticmethod
    def _safe_rmtree(path: Path) -> None:
        # Recursive rollback for create_from_git, where we own the entire
        # tree (we cloned it from scratch). ignore_errors so partial trees
        # don't block cleanup further.
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
