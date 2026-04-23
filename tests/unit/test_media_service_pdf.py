"""C7.2 — MediaService.process_pdf tests.

C7.1 tests already cover the shared download/validate/cache/send
skeleton via process_image. These tests focus on the PDF-specific
behaviour: 20 MB cap, application/pdf MIME allow-list, the "%PDF-"
magic-bytes gate, and the ``lies <path>: <caption>`` prompt shape.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from whatsbot.application.media_service import MediaService
from whatsbot.ports.media_cache import CachedItem
from whatsbot.ports.media_downloader import (
    DownloadedMedia,
    MediaDownloadError,
    MediaDownloader,
)


# Reuse the same fakes shape as the image tests so the coverage
# profile across the two kinds stays comparable. We keep this file
# self-contained rather than importing from the image module so a
# future divergence (e.g. audio needs a different fake) doesn't
# trigger cross-test churn.


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

    def set_active(self, raw_name: str) -> str:  # pragma: no cover
        self.active = raw_name
        return raw_name


@dataclass
class _PromptCall:
    project: str
    text: str


class FakeSessionService:
    def __init__(self) -> None:
        self.prompts: list[_PromptCall] = []

    def send_prompt(self, project_name: str, text: str) -> None:
        self.prompts.append(_PromptCall(project=project_name, text=text))


def _make_service(
    tmp_path: Path,
    *,
    downloader: MediaDownloader | None = None,
    active: str | None = "alpha",
) -> tuple[MediaService, FakeCache, FakeSessionService]:
    pdf_payload = b"%PDF-1.4\n" + b"\x00" * 256
    if downloader is None:
        downloader = FakeDownloader(payload=pdf_payload, mime="application/pdf")
    cache = FakeCache(tmp_path / "cache")
    session = FakeSessionService()
    service = MediaService(
        downloader=downloader,
        cache=cache,
        active_project=FakeActiveProject(active),
        session_service=session,
    )
    return service, cache, session


def test_process_pdf_happy_path(tmp_path: Path) -> None:
    service, cache, session = _make_service(tmp_path)
    outcome = service.process_pdf(
        media_id="doc1", caption="fasse zusammen", sender="+491"
    )

    assert outcome.kind == "sent"
    assert outcome.project == "alpha"
    assert outcome.cache_path is not None
    assert outcome.cache_path.suffix == ".pdf"
    assert outcome.cache_path.exists()

    # Cache stored with .pdf suffix.
    assert len(cache.stores) == 1
    assert cache.stores[0][2] == ".pdf"

    # Prompt uses the "lies <path>: <caption>" shape — distinct from
    # the "analysiere" form used for images.
    assert len(session.prompts) == 1
    text = session.prompts[0].text
    assert text.startswith("lies ")
    assert str(outcome.cache_path) in text
    assert "fasse zusammen" in text

    # Reply mentions "PDF" rather than "Bild".
    assert "PDF" in outcome.reply
    assert "alpha" in outcome.reply


def test_process_pdf_without_caption(tmp_path: Path) -> None:
    service, _cache, session = _make_service(tmp_path)
    outcome = service.process_pdf(
        media_id="doc1", caption=None, sender="+491"
    )
    assert outcome.kind == "sent"
    assert session.prompts[0].text == f"lies {outcome.cache_path}"


def test_process_pdf_no_active_project(tmp_path: Path) -> None:
    service, cache, session = _make_service(tmp_path, active=None)
    outcome = service.process_pdf(
        media_id="doc1", caption=None, sender="+491"
    )
    assert outcome.kind == "no_active_project"
    assert "/p" in outcome.reply
    assert cache.stores == []
    assert session.prompts == []


def test_process_pdf_rejects_non_pdf_mime(tmp_path: Path) -> None:
    downloader = FakeDownloader(payload=b"%PDF-1.4\n", mime="image/jpeg")
    service, cache, session = _make_service(tmp_path, downloader=downloader)
    outcome = service.process_pdf(
        media_id="doc1", caption=None, sender="+491"
    )
    assert outcome.kind == "validation_failed"
    assert cache.stores == []
    assert session.prompts == []


def test_process_pdf_rejects_oversized(tmp_path: Path) -> None:
    # 21 MB > 20 MB limit
    big = b"%PDF-1.4\n" + b"\x00" * (21 * 1024 * 1024)
    downloader = FakeDownloader(payload=big, mime="application/pdf")
    service, cache, session = _make_service(tmp_path, downloader=downloader)
    outcome = service.process_pdf(
        media_id="doc1", caption=None, sender="+491"
    )
    assert outcome.kind == "validation_failed"
    assert "zu gross" in outcome.reply.lower() or "zu groß" in outcome.reply.lower()
    assert cache.stores == []
    assert session.prompts == []


def test_process_pdf_rejects_magic_bytes_mismatch(tmp_path: Path) -> None:
    # MIME says application/pdf but the bytes are a JPEG header.
    downloader = FakeDownloader(
        payload=b"\xff\xd8\xff\xe0\x00\x10JFIF",
        mime="application/pdf",
    )
    service, cache, session = _make_service(tmp_path, downloader=downloader)
    outcome = service.process_pdf(
        media_id="doc1", caption=None, sender="+491"
    )
    assert outcome.kind == "validation_failed"
    assert "Inhalt" in outcome.reply
    assert cache.stores == []
    assert session.prompts == []


def test_process_pdf_download_failure(tmp_path: Path) -> None:
    downloader = FakeDownloader(
        payload=b"", mime="", raise_error="network down"
    )
    service, cache, session = _make_service(tmp_path, downloader=downloader)
    outcome = service.process_pdf(
        media_id="doc1", caption=None, sender="+491"
    )
    assert outcome.kind == "download_failed"
    assert cache.stores == []
    assert session.prompts == []
