"""Unit tests for whatsbot.domain.projects."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from whatsbot.domain.projects import (
    InvalidProjectNameError,
    Mode,
    Project,
    ProjectListing,
    SourceMode,
    format_listing,
    resolved_path,
    validate_project_name,
)

pytestmark = pytest.mark.unit


# --- validate_project_name -------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "ab",
        "myproject",
        "my-app",
        "site_v2",
        "x" * 32,
        "0starts-with-digit",
        "a1",
    ],
)
def test_valid_names_pass(name: str) -> None:
    assert validate_project_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "a",  # too short
        "x" * 33,  # too long
        "_leading-underscore",
        "Has-Capital",
        "with space",
        "with.dot",
        "umlautü",  # contains a non-ASCII character
        # NOTE: trailing/leading whitespace is intentionally stripped by the
        # validator (see test_validate_strips_outer_whitespace), so we don't
        # include "trailing-space " here.
    ],
)
def test_invalid_names_raise(name: str) -> None:
    with pytest.raises(InvalidProjectNameError):
        validate_project_name(name)


def test_validate_strips_outer_whitespace() -> None:
    assert validate_project_name("  ok-name  ") == "ok-name"


@pytest.mark.parametrize(
    "name", ["ls", "p", "rm", "new", "info", "help", "ping", "status", "trash", ".", ".."]
)
def test_reserved_names_rejected(name: str) -> None:
    with pytest.raises(InvalidProjectNameError, match="reserviert"):
        validate_project_name(name)


def test_validate_rejects_non_string() -> None:
    with pytest.raises(InvalidProjectNameError):
        validate_project_name(123)  # type: ignore[arg-type]


# --- Project dataclass -----------------------------------------------------


def test_project_validates_on_construction() -> None:
    with pytest.raises(InvalidProjectNameError):
        Project(
            name="BAD NAME",
            source_mode=SourceMode.EMPTY,
            created_at=datetime.now(UTC),
        )


def test_project_defaults_match_spec() -> None:
    p = Project(
        name="alpha",
        source_mode=SourceMode.EMPTY,
        created_at=datetime.now(UTC),
    )
    assert p.mode is Mode.NORMAL
    assert p.default_model == "sonnet"
    assert p.last_used_at is None
    assert p.source is None


# --- format_listing --------------------------------------------------------


def test_format_listing_empty_returns_friendly_hint() -> None:
    text = format_listing([])
    assert "noch keine Projekte" in text
    assert "/new <name>" in text


def test_format_listing_includes_mode_emoji_and_active_marker() -> None:
    p1 = Project(
        name="alpha",
        source_mode=SourceMode.EMPTY,
        created_at=datetime.now(UTC),
        mode=Mode.NORMAL,
    )
    p2 = Project(
        name="beta",
        source_mode=SourceMode.GIT,
        created_at=datetime.now(UTC),
        mode=Mode.YOLO,
    )
    text = format_listing(
        [
            ProjectListing(project=p1, is_active=True),
            ProjectListing(project=p2, is_active=False),
        ]
    )
    assert "▶" in text
    assert "🟢" in text  # NORMAL emoji
    assert "🔴" in text  # YOLO emoji
    assert "alpha" in text and "beta" in text
    assert "(empty)" in text and "(git)" in text


# --- Project.path + resolved_path (Phase 11) -------------------------------


def test_project_accepts_none_path_for_legacy() -> None:
    p = Project(
        name="legacy",
        source_mode=SourceMode.EMPTY,
        created_at=datetime(2026, 4, 24, tzinfo=UTC),
    )
    assert p.path is None


def test_project_accepts_absolute_path_for_import() -> None:
    p = Project(
        name="wabot",
        source_mode=SourceMode.IMPORTED,
        created_at=datetime(2026, 4, 24, tzinfo=UTC),
        path=Path("/Users/hagenmarggraf/whatsbot"),
    )
    assert p.path == Path("/Users/hagenmarggraf/whatsbot")


def test_project_rejects_relative_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        Project(
            name="bad",
            source_mode=SourceMode.IMPORTED,
            created_at=datetime(2026, 4, 24, tzinfo=UTC),
            path=Path("relative/path"),
        )


def test_source_mode_has_imported_value() -> None:
    assert SourceMode.IMPORTED.value == "imported"
    assert SourceMode("imported") is SourceMode.IMPORTED


def test_resolved_path_returns_explicit_path_when_set(tmp_path: Path) -> None:
    p = Project(
        name="wabot",
        source_mode=SourceMode.IMPORTED,
        created_at=datetime(2026, 4, 24, tzinfo=UTC),
        path=tmp_path / "elsewhere",
    )
    assert resolved_path(p, projects_root=Path("/home/x/projekte")) == tmp_path / "elsewhere"


def test_resolved_path_falls_back_to_projects_root(tmp_path: Path) -> None:
    p = Project(
        name="scratch",
        source_mode=SourceMode.EMPTY,
        created_at=datetime(2026, 4, 24, tzinfo=UTC),
    )
    assert resolved_path(p, projects_root=tmp_path) == tmp_path / "scratch"
