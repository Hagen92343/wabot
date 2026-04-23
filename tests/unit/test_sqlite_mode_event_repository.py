"""Unit tests for SqliteModeEventRepository."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_mode_event_repository import (
    SqliteModeEventRepository,
)
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.domain.mode_events import ModeEvent, ModeEventKind
from whatsbot.domain.projects import Mode, Project, SourceMode

pytestmark = pytest.mark.unit


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(c)
    # Seed a project so the mode_events rows have a plausible
    # project_name referent (the schema allows orphan project_name
    # since there's no FK on mode_events).
    SqliteProjectRepository(c).create(
        Project(
            name="alpha",
            source_mode=SourceMode.EMPTY,
            created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            mode=Mode.NORMAL,
        )
    )
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def repo(conn: sqlite3.Connection) -> SqliteModeEventRepository:
    return SqliteModeEventRepository(conn)


def _event(
    *,
    id: str = "e-1",
    project: str = "alpha",
    kind: ModeEventKind = ModeEventKind.SWITCH,
    from_mode: Mode | None = Mode.NORMAL,
    to_mode: Mode = Mode.STRICT,
    msg_id: str | None = "01HQ...",
    at: datetime | None = None,
) -> ModeEvent:
    return ModeEvent(
        id=id,
        project_name=project,
        kind=kind,
        from_mode=from_mode,
        to_mode=to_mode,
        at=at or datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
        msg_id=msg_id,
    )


def test_record_and_roundtrip(repo: SqliteModeEventRepository) -> None:
    ev = _event()
    repo.record(ev)
    events = repo.list_for_project("alpha")
    assert len(events) == 1
    assert events[0] == ev


def test_list_is_newest_first(repo: SqliteModeEventRepository) -> None:
    older = _event(id="e-1", at=datetime(2026, 4, 22, 10, 0, tzinfo=UTC))
    newer = _event(id="e-2", at=datetime(2026, 4, 22, 15, 0, tzinfo=UTC))
    repo.record(older)
    repo.record(newer)
    events = repo.list_for_project("alpha")
    assert [e.id for e in events] == ["e-2", "e-1"]


def test_record_rejects_duplicate_id(repo: SqliteModeEventRepository) -> None:
    repo.record(_event(id="dup"))
    with pytest.raises(sqlite3.IntegrityError):
        repo.record(_event(id="dup"))


def test_reboot_reset_event_kind_roundtrips(
    repo: SqliteModeEventRepository,
) -> None:
    ev = _event(
        id="e-reset",
        kind=ModeEventKind.REBOOT_RESET,
        from_mode=Mode.YOLO,
        to_mode=Mode.NORMAL,
        msg_id=None,
    )
    repo.record(ev)
    assert repo.list_for_project("alpha")[0].kind is ModeEventKind.REBOOT_RESET


def test_event_with_null_from_mode(repo: SqliteModeEventRepository) -> None:
    # Reboot-reset before we know the prior mode (defensive case).
    ev = _event(
        id="e-null",
        kind=ModeEventKind.REBOOT_RESET,
        from_mode=None,
        to_mode=Mode.NORMAL,
    )
    repo.record(ev)
    assert repo.list_for_project("alpha")[0].from_mode is None


def test_unknown_project_returns_empty(
    repo: SqliteModeEventRepository,
) -> None:
    assert repo.list_for_project("ghost") == []
