"""Unit tests for whatsbot.application.output_service."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from whatsbot.application.output_service import OutputService
from whatsbot.domain.output_guard import THRESHOLD_BYTES
from whatsbot.domain.pending_outputs import PendingOutput

pytestmark = pytest.mark.unit


# ---- stubs --------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, *, to: str, body: str) -> None:
        self.sent.append((to, body))


class _InMemoryRepo:
    def __init__(self) -> None:
        self.rows: dict[str, PendingOutput] = {}

    def create(self, output: PendingOutput) -> None:
        assert output.msg_id not in self.rows, "duplicate msg_id"
        self.rows[output.msg_id] = output

    def get(self, msg_id: str) -> PendingOutput | None:
        return self.rows.get(msg_id)

    def latest_open(self) -> PendingOutput | None:
        if not self.rows:
            return None
        return max(self.rows.values(), key=lambda r: r.created_at)

    def resolve(self, msg_id: str) -> bool:
        return self.rows.pop(msg_id, None) is not None

    def delete_expired(self, now_ts: int) -> list[str]:
        victims = [k for k, v in self.rows.items() if v.deadline_ts <= now_ts]
        for k in victims:
            del self.rows[k]
        return victims


@pytest.fixture
def tmp_outputs(tmp_path: Path) -> Iterator[Path]:
    d = tmp_path / "outputs"
    d.mkdir()
    yield d


@pytest.fixture
def svc(tmp_outputs: Path) -> tuple[OutputService, _Recorder, _InMemoryRepo]:
    recorder = _Recorder()
    repo = _InMemoryRepo()
    service = OutputService(
        sender=recorder,
        repo=repo,
        outputs_dir=tmp_outputs,
        now_fn=lambda: 1_000_000,
    )
    return service, recorder, repo


# ---- deliver() ----------------------------------------------------------


def test_deliver_small_body_passes_straight_through(
    svc: tuple[OutputService, _Recorder, _InMemoryRepo],
) -> None:
    service, recorder, repo = svc
    service.deliver(to="+49", body="hello")
    assert recorder.sent == [("+49", "hello")]
    assert repo.rows == {}


def test_deliver_at_threshold_passes_through(
    svc: tuple[OutputService, _Recorder, _InMemoryRepo],
) -> None:
    service, recorder, repo = svc
    body = "x" * THRESHOLD_BYTES  # exactly 10 KB → fine
    service.deliver(to="+49", body=body)
    assert recorder.sent == [("+49", body)]
    assert repo.rows == {}


def test_deliver_over_threshold_stashes_and_warns(
    svc: tuple[OutputService, _Recorder, _InMemoryRepo],
    tmp_outputs: Path,
) -> None:
    service, recorder, repo = svc
    body = "x" * (THRESHOLD_BYTES + 100)  # > 10 KB

    service.deliver(to="+49", body=body, project_name="alpha")

    # One outbound: the warning, not the body.
    assert len(recorder.sent) == 1
    to, warning = recorder.sent[0]
    assert to == "+49"
    assert "/send" in warning and "/discard" in warning and "/save" in warning
    assert "KB" in warning

    # Exactly one pending row, and a matching file on disk.
    assert len(repo.rows) == 1
    row = next(iter(repo.rows.values()))
    assert row.project_name == "alpha"
    assert row.size_bytes == len(body.encode("utf-8"))
    file_path = Path(row.output_path)
    assert file_path.parent == tmp_outputs
    assert file_path.exists()
    assert file_path.read_text(encoding="utf-8") == body


def test_deliver_fallback_on_write_failure(
    svc: tuple[OutputService, _Recorder, _InMemoryRepo],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disk write failure → fall back to direct send rather than dropping."""
    service, recorder, repo = svc

    def fail_write(self: Path, *a: object, **kw: object) -> int:
        raise OSError("simulated disk full")

    monkeypatch.setattr(Path, "write_text", fail_write)
    body = "y" * (THRESHOLD_BYTES + 1)
    service.deliver(to="+49", body=body)

    # Fallback: body arrives, no pending row.
    assert recorder.sent == [("+49", body)]
    assert repo.rows == {}


# ---- resolve_send() ----------------------------------------------------


def test_resolve_send_with_no_pending_returns_none(
    svc: tuple[OutputService, _Recorder, _InMemoryRepo],
) -> None:
    service, _recorder, _repo = svc
    outcome = service.resolve_send(to="+49")
    assert outcome.kind == "none"


def test_resolve_send_chunks_and_clears(
    svc: tuple[OutputService, _Recorder, _InMemoryRepo],
    tmp_outputs: Path,
) -> None:
    service, recorder, repo = svc
    body = "x" * (THRESHOLD_BYTES + 50)
    service.deliver(to="+49", body=body)
    msg_id = next(iter(repo.rows.keys()))
    recorder.sent.clear()  # ignore the warning

    outcome = service.resolve_send(to="+49")

    assert outcome.kind == "sent"
    assert outcome.msg_id == msg_id
    assert outcome.chunks_sent is not None and outcome.chunks_sent >= 3
    # Rebuild the body from the chunks (strip the "(i/n)\n" prefix).
    joined = "".join(
        (body_str.split("\n", 1)[1] if body_str.startswith("(") else body_str)
        for _, body_str in recorder.sent
    )
    assert joined == body
    # Row + file cleaned up.
    assert repo.rows == {}
    assert list(tmp_outputs.iterdir()) == []


def test_resolve_send_missing_file_reports_missing(
    svc: tuple[OutputService, _Recorder, _InMemoryRepo],
    tmp_outputs: Path,
) -> None:
    service, _recorder, repo = svc
    # Row without a corresponding file on disk.
    repo.create(
        PendingOutput(
            msg_id="orphan",
            project_name="alpha",
            output_path=str(tmp_outputs / "does-not-exist.md"),
            size_bytes=50_000,
            created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            deadline_ts=2_000_000,
        )
    )
    outcome = service.resolve_send(to="+49")
    assert outcome.kind == "missing"
    # Row is removed so the user isn't stuck with a ghost.
    assert repo.rows == {}


# ---- resolve_discard() / resolve_save() --------------------------------


def test_resolve_discard_removes_row_and_file(
    svc: tuple[OutputService, _Recorder, _InMemoryRepo],
    tmp_outputs: Path,
) -> None:
    service, _recorder, repo = svc
    service.deliver(to="+49", body="x" * (THRESHOLD_BYTES + 1))
    row = next(iter(repo.rows.values()))
    path = Path(row.output_path)
    assert path.exists()

    outcome = service.resolve_discard(to="+49")

    assert outcome.kind == "discarded"
    assert repo.rows == {}
    assert not path.exists()


def test_resolve_save_removes_row_but_keeps_file(
    svc: tuple[OutputService, _Recorder, _InMemoryRepo],
) -> None:
    service, _recorder, repo = svc
    service.deliver(to="+49", body="x" * (THRESHOLD_BYTES + 1))
    row = next(iter(repo.rows.values()))
    path = Path(row.output_path)
    assert path.exists()

    outcome = service.resolve_save(to="+49")

    assert outcome.kind == "saved"
    assert repo.rows == {}
    # Spec §10 semantics — "nur speichern, nicht senden".
    assert path.exists()


def test_resolve_save_no_pending_returns_none(
    svc: tuple[OutputService, _Recorder, _InMemoryRepo],
) -> None:
    service, _recorder, _repo = svc
    assert service.resolve_save(to="+49").kind == "none"


def test_resolve_discard_no_pending_returns_none(
    svc: tuple[OutputService, _Recorder, _InMemoryRepo],
) -> None:
    service, _recorder, _repo = svc
    assert service.resolve_discard(to="+49").kind == "none"
