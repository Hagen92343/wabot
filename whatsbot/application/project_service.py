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


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """Result of ``import_existing``.

    Reports the persisted project row, smart-detection output, and two
    bookkeeping lists so the command handler can phrase the WhatsApp
    reply precisely:

    * ``artifacts_created`` — names of files the bot dropped into the
      imported directory (e.g. ``CLAUDE.md``, ``.claudeignore``,
      ``.whatsbot/config.json``).
    * ``artifacts_preserved`` — names of files the bot found already
      present and refused to overwrite.
    * ``warnings`` — non-fatal hints, e.g. path under a TCC-protected
      macOS directory.
    """

    project: Project
    detection: DetectionResult
    artifacts_created: list[str]
    artifacts_preserved: list[str]
    warnings: list[str]


class InvalidImportPathError(ValueError):
    """Raised when ``/import`` is given a path we can't accept."""


# Paths we refuse to import regardless of what the user claims. These
# are either sensitive secret stores or system directories where Claude
# running arbitrary Bash would be catastrophic.
#
# Note: macOS symlinks /etc → /private/etc, /var → /private/var, etc.
# We do NOT include /private here because /private/tmp and
# /private/var/folders (user cache / pytest tmp_path) must remain
# importable for tests. System-critical paths under /private
# (/private/etc, /private/var/…) get matched via their resolved form
# below.
_PROTECTED_ROOTS: tuple[Path, ...] = (
    Path.home() / "Library",
    Path.home() / ".ssh",
    Path.home() / ".aws",
    Path.home() / ".gnupg",
    Path.home() / ".config" / "gh",
    Path.home() / ".1password",
    Path("/etc"),
    Path("/System"),
    Path("/Library"),
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
)

# Paths where macOS TCC (iCloud / sandbox guards) will often block the
# bot's writes even when the logic says allow. We don't refuse — the
# user knows their own setup — but we warn so a `git push` that silently
# doesn't actually push isn't a mystery later.
_TCC_WARN_ROOTS: tuple[Path, ...] = (
    Path.home() / "Desktop",
    Path.home() / "Documents",
    Path.home() / "Downloads",
    Path.home() / "Pictures",
    Path.home() / "Movies",
    Path.home() / "Music",
)


def _is_under_root(path: Path, root: Path) -> bool:
    """Return True if ``path`` is inside ``root`` (or equals it).

    Both sides get ``.resolve()`` first so symlink-equivalent paths
    (e.g. /etc and /private/etc on macOS) match correctly.
    """
    try:
        path.resolve().relative_to(root.resolve() if root.exists() else root)
    except ValueError:
        return False
    return True


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

    # ---- import existing --------------------------------------------------

    def import_existing(self, raw_name: str, raw_path: str) -> ImportOutcome:
        """Register an already-existing directory as a whatsbot project.

        Unlike ``create_empty`` / ``create_from_git``, we don't own the
        filesystem here — the user points us at their bestehender Ordner.
        Steps:

        1. Validate name + path (absolute, exists, not in protected roots,
           not already registered by name or path).
        2. Idempotently drop in missing bot-managed dotfiles. Existing
           user files are never overwritten.
        3. Run smart-detection + write ``suggested-rules.json``.
        4. Persist the DB row with ``source_mode=IMPORTED`` and the
           explicit path.

        Failure modes:
            - ``InvalidProjectNameError`` — bad name
            - ``InvalidImportPathError`` — bad / protected / duplicate path
            - ``ProjectAlreadyExistsError`` — name already used
            - ``ProjectFilesystemError`` — can't write artifacts
        """
        name = validate_project_name(raw_name)

        # Path shape.
        try:
            path = Path(raw_path).expanduser()
        except (TypeError, ValueError) as exc:
            raise InvalidImportPathError(f"Pfad '{raw_path}' ist kein gueltiger Pfad.") from exc
        if not path.is_absolute():
            raise InvalidImportPathError(
                f"Pfad muss absolut sein (mit / anfangen), bekam '{raw_path}'."
            )
        if not path.exists():
            raise InvalidImportPathError(f"Pfad '{path}' existiert nicht.")
        if not path.is_dir():
            raise InvalidImportPathError(f"Pfad '{path}' ist kein Verzeichnis.")

        # Normalise (resolve symlinks / relative components) so we register
        # a canonical path.
        path = path.resolve()

        # Protected-root check.
        for root in _PROTECTED_ROOTS:
            if _is_under_root(path, root):
                raise InvalidImportPathError(
                    f"Pfad '{path}' liegt in einem geschuetzten Bereich "
                    f"({root}); Import abgelehnt."
                )

        # Duplicate checks.
        if self._repo.exists(name):
            raise ProjectAlreadyExistsError(f"Projekt '{name}' existiert schon.")
        if self._repo.exists_with_path(path):
            raise InvalidImportPathError(
                f"Pfad '{path}' ist schon unter einem anderen Namen registriert."
            )

        warnings: list[str] = []
        for warn_root in _TCC_WARN_ROOTS:
            if _is_under_root(path, warn_root):
                warnings.append(
                    f"Pfad liegt unter {warn_root.name}/ — macOS TCC kann "
                    f"Schreibzugriffe blockieren. Siehe docs/OPERATING.md."
                )
                break

        # Artefakte — idempotent, niemals ueberschreiben was der User hat.
        artifacts_created: list[str] = []
        artifacts_preserved: list[str] = []
        try:
            (path / ".whatsbot").mkdir(exist_ok=True)
            (path / ".whatsbot" / "outputs").mkdir(exist_ok=True)

            ignore_written = post_clone.write_claudeignore_if_missing(path)
            if ignore_written is None:
                artifacts_preserved.append(".claudeignore")
            else:
                artifacts_created.append(".claudeignore")

            config_written = post_clone.write_config_json_if_missing(
                path,
                project_name=name,
                source_url=str(path),
                source_mode=SourceMode.IMPORTED.value,
            )
            if config_written is None:
                artifacts_preserved.append(".whatsbot/config.json")
            else:
                artifacts_created.append(".whatsbot/config.json")

            claude_md_written = post_clone.write_claude_md_if_missing(
                path, project_name=name
            )
            if claude_md_written is None:
                artifacts_preserved.append("CLAUDE.md")
            else:
                artifacts_created.append("CLAUDE.md")

            detection = detect(path)
            rules_written = post_clone.write_suggested_rules(path, detection)
            if rules_written is not None:
                artifacts_created.append(".whatsbot/suggested-rules.json")
        except OSError as exc:
            raise ProjectFilesystemError(
                f"Konnte Import-Artefakte in {path} nicht anlegen: {exc}"
            ) from exc

        project = Project(
            name=name,
            source_mode=SourceMode.IMPORTED,
            source=str(path),
            created_at=datetime.now(UTC),
            mode=Mode.NORMAL,
            path=path,
        )
        self._repo.create(project)

        self._log.info(
            "project_imported",
            name=name,
            path=str(path),
            artifacts_created=artifacts_created,
            artifacts_preserved=artifacts_preserved,
            suggested_rules=len(detection.suggested_rules),
            warnings=warnings,
        )
        return ImportOutcome(
            project=project,
            detection=detection,
            artifacts_created=artifacts_created,
            artifacts_preserved=artifacts_preserved,
            warnings=warnings,
        )

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
