"""C7.1 — MediaService tests (image pipeline + unsupported rejects)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from whatsbot.application.media_service import MediaService
from whatsbot.domain.media import MediaKind
from whatsbot.ports.media_cache import CachedItem
from whatsbot.ports.media_downloader import (
    DownloadedMedia,
    MediaDownloader,
    MediaDownloadError,
)

# ---- test doubles --------------------------------------------------------


class FakeDownloader:
    def __init__(
        self, *, payload: bytes, mime: str, raise_error: str | None = None
    ) -> None:
        self.payload = payload
        self.mime = mime
        self.raise_error = raise_error
        self.calls: list[str] = []

    def download(self, media_id: str) -> DownloadedMedia:
        self.calls.append(media_id)
        if self.raise_error is not None:
            raise MediaDownloadError(self.raise_error)
        return DownloadedMedia(
            payload=self.payload,
            mime=self.mime,
            sha256=hashlib.sha256(self.payload).hexdigest(),
        )


class FakeCache:
    def __init__(self, root: Path) -> None:
        self._root = root
        self.stores: list[tuple[str, bytes, str]] = []
        self._root.mkdir(parents=True, exist_ok=True)

    def store(self, media_id: str, payload: bytes, suffix: str) -> Path:
        self.stores.append((media_id, payload, suffix))
        path = self._root / f"{media_id}{suffix}"
        path.write_bytes(payload)
        return path

    def path_for(self, media_id: str, suffix: str) -> Path:
        return self._root / f"{media_id}{suffix}"

    def list_all(self) -> list[CachedItem]:
        return []

    def secure_delete(self, path: Path) -> None:
        if path.exists():
            path.unlink()


class FakeActiveProject:
    def __init__(self, active: str | None) -> None:
        self.active = active

    def get_active(self) -> str | None:
        return self.active

    # Unused here but part of the interface
    def set_active(self, raw_name: str) -> str:  # pragma: no cover
        self.active = raw_name
        return raw_name


@dataclass
class _PromptCall:
    project: str
    text: str


class FakeSessionService:
    def __init__(self, *, raise_error: Exception | None = None) -> None:
        self.prompts: list[_PromptCall] = []
        self.raise_error = raise_error

    def send_prompt(self, project_name: str, text: str) -> None:
        if self.raise_error is not None:
            raise self.raise_error
        self.prompts.append(_PromptCall(project=project_name, text=text))


# ---- fixtures -----------------------------------------------------------


def _make_service(
    tmp_path: Path,
    *,
    downloader: MediaDownloader | None = None,
    active: str | None = "alpha",
    session: FakeSessionService | None = None,
) -> tuple[MediaService, FakeCache, FakeSessionService]:
    jpeg_payload = b"\xff\xd8\xff\xe0" + b"\x00" * 128
    if downloader is None:
        downloader = FakeDownloader(payload=jpeg_payload, mime="image/jpeg")
    cache = FakeCache(tmp_path / "cache")
    if session is None:
        session = FakeSessionService()
    service = MediaService(
        downloader=downloader,
        cache=cache,
        active_project=FakeActiveProject(active),
        session_service=session,
    )
    return service, cache, session


# ---- process_image ------------------------------------------------------


def test_process_image_happy_path(tmp_path: Path) -> None:
    service, cache, session = _make_service(tmp_path)
    outcome = service.process_image(
        media_id="id1", caption="schau dir das an", sender="+491234"
    )

    assert outcome.kind == "sent"
    assert outcome.project == "alpha"
    assert outcome.cache_path is not None
    assert outcome.cache_path.exists()
    assert "📨" in outcome.reply
    assert "alpha" in outcome.reply

    assert len(cache.stores) == 1
    assert cache.stores[0][0] == "id1"
    assert cache.stores[0][2] == ".jpg"

    assert len(session.prompts) == 1
    prompt = session.prompts[0]
    assert prompt.project == "alpha"
    assert str(outcome.cache_path) in prompt.text
    assert "schau dir das an" in prompt.text
    assert prompt.text.startswith("analysiere ")


def test_process_image_without_caption(tmp_path: Path) -> None:
    service, _cache, session = _make_service(tmp_path)
    outcome = service.process_image(media_id="id1", caption=None, sender="+491")
    assert outcome.kind == "sent"
    assert session.prompts[0].text == f"analysiere {outcome.cache_path}"


def test_process_image_empty_caption_is_noop(tmp_path: Path) -> None:
    service, _cache, session = _make_service(tmp_path)
    outcome = service.process_image(
        media_id="id1", caption="   ", sender="+491"
    )
    assert outcome.kind == "sent"
    assert ":" not in session.prompts[0].text  # no caption appended


def test_process_image_no_active_project(tmp_path: Path) -> None:
    service, cache, session = _make_service(tmp_path, active=None)
    outcome = service.process_image(
        media_id="id1", caption="x", sender="+491"
    )
    assert outcome.kind == "no_active_project"
    assert "/p" in outcome.reply
    assert session.prompts == []
    assert cache.stores == []


def test_process_image_download_failure(tmp_path: Path) -> None:
    downloader = FakeDownloader(
        payload=b"", mime="", raise_error="network down"
    )
    service, cache, session = _make_service(tmp_path, downloader=downloader)
    outcome = service.process_image(
        media_id="id1", caption=None, sender="+491"
    )
    assert outcome.kind == "download_failed"
    assert cache.stores == []
    assert session.prompts == []


def test_process_image_rejects_disallowed_mime(tmp_path: Path) -> None:
    downloader = FakeDownloader(payload=b"\xff\xd8\xff", mime="application/pdf")
    service, cache, session = _make_service(tmp_path, downloader=downloader)
    outcome = service.process_image(
        media_id="id1", caption=None, sender="+491"
    )
    assert outcome.kind == "validation_failed"
    assert "MIME" in outcome.reply or "nicht erlaubt" in outcome.reply
    assert cache.stores == []
    assert session.prompts == []


def test_process_image_rejects_oversized(tmp_path: Path) -> None:
    big = b"\xff\xd8\xff" + b"\x00" * (11 * 1024 * 1024)  # 11 MB > limit
    downloader = FakeDownloader(payload=big, mime="image/jpeg")
    service, cache, session = _make_service(tmp_path, downloader=downloader)
    outcome = service.process_image(
        media_id="id1", caption=None, sender="+491"
    )
    assert outcome.kind == "validation_failed"
    assert "zu gross" in outcome.reply.lower() or "zu groß" in outcome.reply.lower()
    assert cache.stores == []
    assert session.prompts == []


def test_process_image_rejects_magic_mismatch(tmp_path: Path) -> None:
    # Claims image/png but actual bytes look like a PDF
    downloader = FakeDownloader(payload=b"%PDF-1.4\nhello", mime="image/png")
    service, cache, session = _make_service(tmp_path, downloader=downloader)
    outcome = service.process_image(
        media_id="id1", caption=None, sender="+491"
    )
    assert outcome.kind == "validation_failed"
    assert "Inhalt" in outcome.reply
    assert cache.stores == []
    assert session.prompts == []


def test_process_image_session_send_failure_preserves_cache(tmp_path: Path) -> None:
    session = FakeSessionService(raise_error=RuntimeError("tmux gone"))
    service, cache, _ = _make_service(tmp_path, session=session)
    outcome = service.process_image(
        media_id="id1", caption=None, sender="+491"
    )
    assert outcome.kind == "download_failed"
    # Cache was written before the send attempt — we keep it.
    assert len(cache.stores) == 1
    assert outcome.cache_path is not None
    assert outcome.cache_path.exists()


# ---- process_unsupported ------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        MediaKind.VIDEO,
        MediaKind.LOCATION,
        MediaKind.STICKER,
        MediaKind.CONTACT,
        MediaKind.UNKNOWN,
    ],
)
def test_process_unsupported_returns_reject(tmp_path: Path, kind: MediaKind) -> None:
    service, cache, session = _make_service(tmp_path)
    outcome = service.process_unsupported(kind)
    assert outcome.kind == "unsupported"
    assert outcome.reply  # non-empty
    # Never touches download / cache / session
    assert cache.stores == []
    assert session.prompts == []


def test_process_unsupported_reply_per_kind_is_distinct(tmp_path: Path) -> None:
    service, _, _ = _make_service(tmp_path)
    replies = {
        kind: service.process_unsupported(kind).reply
        for kind in (
            MediaKind.VIDEO,
            MediaKind.LOCATION,
            MediaKind.STICKER,
            MediaKind.CONTACT,
        )
    }
    assert len(set(replies.values())) == 4  # all distinct
    assert "Video" in replies[MediaKind.VIDEO]
    assert "Location" in replies[MediaKind.LOCATION]
    assert (
        "sticker" in replies[MediaKind.STICKER].lower()
        or "Sticker" in replies[MediaKind.STICKER]
    )
    assert "Kontakt" in replies[MediaKind.CONTACT]
