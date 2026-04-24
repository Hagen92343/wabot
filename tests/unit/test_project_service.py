"""Unit tests for whatsbot.application.project_service."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.application.project_service import (
    InvalidImportPathError,
    ProjectFilesystemError,
    ProjectService,
)
from whatsbot.domain.projects import (
    InvalidProjectNameError,
    Mode,
    SourceMode,
)
from whatsbot.ports.project_repository import ProjectAlreadyExistsError

pytestmark = pytest.mark.unit


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projekte"
    root.mkdir()
    return root


@pytest.fixture
def service(conn: sqlite3.Connection, projects_root: Path) -> ProjectService:
    return ProjectService(
        repository=SqliteProjectRepository(conn),
        conn=conn,
        projects_root=projects_root,
    )


# --- create_empty: happy path ----------------------------------------------


def test_create_empty_persists_row(service: ProjectService) -> None:
    project = service.create_empty("alpha")
    assert project.name == "alpha"
    assert project.source_mode is SourceMode.EMPTY
    assert project.mode is Mode.NORMAL
    assert project.created_at is not None


def test_create_empty_creates_directory_with_whatsbot_subdir(
    service: ProjectService, projects_root: Path
) -> None:
    service.create_empty("alpha")
    assert (projects_root / "alpha").is_dir()
    assert (projects_root / "alpha" / ".whatsbot").is_dir()
    assert (projects_root / "alpha" / ".whatsbot" / "outputs").is_dir()


def test_create_empty_appears_in_list(service: ProjectService) -> None:
    service.create_empty("alpha")
    listings = service.list_all()
    assert len(listings) == 1
    assert listings[0].project.name == "alpha"
    assert listings[0].is_active is False


def test_list_marks_active_project(service: ProjectService) -> None:
    service.create_empty("alpha")
    service.create_empty("beta")
    listings = service.list_all(active_name="beta")
    flags = {entry.project.name: entry.is_active for entry in listings}
    assert flags == {"alpha": False, "beta": True}


# --- create_empty: error paths ---------------------------------------------


def test_create_empty_rejects_invalid_name(service: ProjectService) -> None:
    with pytest.raises(InvalidProjectNameError):
        service.create_empty("INVALID NAME")


def test_create_empty_rejects_duplicate(service: ProjectService) -> None:
    service.create_empty("alpha")
    with pytest.raises(ProjectAlreadyExistsError, match="alpha"):
        service.create_empty("alpha")


def test_create_empty_rejects_when_dir_already_exists(
    service: ProjectService, projects_root: Path
) -> None:
    """A leftover directory without a DB row should NOT be silently
    overwritten — that would clobber whatever the user put there."""
    (projects_root / "leftover").mkdir()
    with pytest.raises(ProjectFilesystemError, match="leftover"):
        service.create_empty("leftover")


def test_create_empty_rolls_back_dir_when_db_insert_fails(
    service: ProjectService, projects_root: Path, conn: sqlite3.Connection
) -> None:
    """If the INSERT explodes mid-flight (e.g. CHECK constraint), the
    freshly-created directory must be removed so the next attempt isn't
    blocked by a stale leftover."""
    # Sabotage the INSERT by dropping the table — repo.create will raise
    # an OperationalError, which the service propagates after rollback.
    conn.execute("DROP TABLE projects")
    with pytest.raises(sqlite3.OperationalError):
        service.create_empty("alpha")
    # Rollback should have removed the directory.
    assert not (projects_root / "alpha").exists()


# --- list_all: empty -------------------------------------------------------


def test_list_all_empty(service: ProjectService) -> None:
    assert service.list_all() == []


# --- import_existing (Phase 11) --------------------------------------------


def _existing_dir(tmp_path: Path, name: str = "existing") -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


def test_import_existing_happy_path(service: ProjectService, tmp_path: Path) -> None:
    target = _existing_dir(tmp_path, "wabot")
    outcome = service.import_existing("wabot", str(target))
    assert outcome.project.name == "wabot"
    assert outcome.project.source_mode is SourceMode.IMPORTED
    assert outcome.project.path == target.resolve()
    assert outcome.project.source == str(target.resolve())
    # Artefakte landeten im target, nicht unter projects_root.
    assert (target / ".whatsbot" / "config.json").is_file()
    assert (target / ".whatsbot" / "outputs").is_dir()
    assert (target / "CLAUDE.md").is_file()
    assert (target / ".claudeignore").is_file()
    # "CLAUDE.md" + "config.json" + "ignore" frisch geschrieben
    assert ".claudeignore" in outcome.artifacts_created
    assert "CLAUDE.md" in outcome.artifacts_created
    assert ".whatsbot/config.json" in outcome.artifacts_created


def test_import_existing_preserves_existing_claude_md(
    service: ProjectService, tmp_path: Path
) -> None:
    target = _existing_dir(tmp_path)
    target_claude = target / "CLAUDE.md"
    target_claude.write_text("# already here\nhands off\n", encoding="utf-8")
    outcome = service.import_existing("mine", str(target))
    # Unsere Template ueberschreibt nichts.
    assert target_claude.read_text(encoding="utf-8") == "# already here\nhands off\n"
    assert "CLAUDE.md" in outcome.artifacts_preserved
    assert "CLAUDE.md" not in outcome.artifacts_created


def test_import_existing_preserves_existing_claudeignore(
    service: ProjectService, tmp_path: Path
) -> None:
    target = _existing_dir(tmp_path)
    (target / ".claudeignore").write_text("my-secret-list\n", encoding="utf-8")
    outcome = service.import_existing("mine", str(target))
    assert (target / ".claudeignore").read_text(encoding="utf-8") == "my-secret-list\n"
    assert ".claudeignore" in outcome.artifacts_preserved


def test_import_existing_rejects_relative_path(service: ProjectService) -> None:
    with pytest.raises(InvalidImportPathError, match="absolut"):
        service.import_existing("rel", "relative/path")


def test_import_existing_rejects_missing_path(
    service: ProjectService, tmp_path: Path
) -> None:
    missing = tmp_path / "does_not_exist"
    with pytest.raises(InvalidImportPathError, match="existiert nicht"):
        service.import_existing("missing", str(missing))


def test_import_existing_rejects_file_path(
    service: ProjectService, tmp_path: Path
) -> None:
    file_target = tmp_path / "regular_file.txt"
    file_target.write_text("hi", encoding="utf-8")
    with pytest.raises(InvalidImportPathError, match="kein Verzeichnis"):
        service.import_existing("afile", str(file_target))


def test_import_existing_rejects_invalid_name(
    service: ProjectService, tmp_path: Path
) -> None:
    target = _existing_dir(tmp_path)
    with pytest.raises(InvalidProjectNameError):
        service.import_existing("Invalid Name!!", str(target))


def test_import_existing_rejects_duplicate_name(
    service: ProjectService, tmp_path: Path
) -> None:
    target_one = _existing_dir(tmp_path, "first")
    target_two = _existing_dir(tmp_path, "second")
    service.import_existing("wabot", str(target_one))
    with pytest.raises(ProjectAlreadyExistsError):
        service.import_existing("wabot", str(target_two))


def test_import_existing_rejects_duplicate_path(
    service: ProjectService, tmp_path: Path
) -> None:
    target = _existing_dir(tmp_path)
    service.import_existing("first", str(target))
    with pytest.raises(InvalidImportPathError, match="unter einem anderen Namen"):
        service.import_existing("second", str(target))


def test_import_existing_rejects_protected_root(
    service: ProjectService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Make Path.home() resolve to tmp_path so .ssh falls under our sandbox.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".ssh").mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    # We need to re-import the module so _PROTECTED_ROOTS picks up fake home.
    # Instead: directly pass a path that matches the existing _PROTECTED_ROOTS.
    target = fake_home / ".ssh"
    from whatsbot.application import project_service as ps

    monkeypatch.setattr(
        ps,
        "_PROTECTED_ROOTS",
        (fake_home / ".ssh",),
    )
    with pytest.raises(InvalidImportPathError, match="geschuetzt"):
        service.import_existing("sshproj", str(target))


def test_import_existing_warns_on_tcc_path(
    service: ProjectService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    desktop = fake_home / "Desktop"
    desktop.mkdir()
    target = desktop / "myproj"
    target.mkdir()

    from whatsbot.application import project_service as ps

    monkeypatch.setattr(ps, "_TCC_WARN_ROOTS", (desktop,))
    monkeypatch.setattr(ps, "_PROTECTED_ROOTS", ())
    outcome = service.import_existing("myproj", str(target))
    assert any("Desktop" in w for w in outcome.warnings)
    assert outcome.project.source_mode is SourceMode.IMPORTED


def test_import_existing_runs_smart_detection(
    service: ProjectService, tmp_path: Path
) -> None:
    target = _existing_dir(tmp_path, "node_proj")
    (target / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    outcome = service.import_existing("node_proj", str(target))
    assert "package.json" in outcome.detection.artifacts_found
    assert len(outcome.detection.suggested_rules) > 0
    assert (target / ".whatsbot" / "suggested-rules.json").is_file()
    assert ".whatsbot/suggested-rules.json" in outcome.artifacts_created


def test_import_existing_resolves_symlinks(
    service: ProjectService, tmp_path: Path
) -> None:
    real = tmp_path / "real_dir"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    outcome = service.import_existing("linked", str(link))
    # path should be the resolved real path, so a second import of the
    # real path gets deduplicated.
    assert outcome.project.path == real.resolve()
    with pytest.raises(InvalidImportPathError, match="unter einem anderen Namen"):
        service.import_existing("also", str(real))


def test_import_existing_does_not_touch_projects_root(
    service: ProjectService, tmp_path: Path, projects_root: Path
) -> None:
    target = _existing_dir(tmp_path)
    service.import_existing("mine", str(target))
    # projects_root stays empty — import didn't smuggle a dir in.
    assert list(projects_root.iterdir()) == []


def test_import_existing_appears_in_list(
    service: ProjectService, tmp_path: Path
) -> None:
    target = _existing_dir(tmp_path, "listme")
    service.import_existing("listme", str(target))
    names = [entry.project.name for entry in service.list_all()]
    assert "listme" in names
