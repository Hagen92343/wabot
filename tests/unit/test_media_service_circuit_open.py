"""C8.3 — MediaService maps CircuitOpenError to a friendly
``⚠️ [service] momentan nicht erreichbar, re-try in ...``-reply
instead of crashing out of the webhook."""

from __future__ import annotations

import time
from pathlib import Path

from whatsbot.adapters.resilience import CircuitOpenError
from whatsbot.application.media_service import MediaService


class _DownloadRefuser:
    def __init__(self, *, service_name: str, reopens_at: float) -> None:
        self._service = service_name
        self._reopens = reopens_at
        self.calls = 0

    def download(self, media_id: str):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise CircuitOpenError(self._service, self._reopens)


class _EmptyCache:
    def __init__(self, root: Path) -> None:
        self._root = root

    def store(self, media_id: str, payload: bytes, suffix: str) -> Path:
        raise AssertionError("cache must not be touched when circuit open")

    def path_for(self, media_id: str, suffix: str) -> Path:
        return self._root / f"{media_id}{suffix}"

    def list_all(self):  # type: ignore[no-untyped-def]
        return []

    def secure_delete(self, path: Path) -> None:
        return None


class _ActiveProject:
    def get_active(self) -> str:
        return "alpha"

    def set_active(self, raw: str) -> str:  # pragma: no cover
        return raw


class _SessionService:
    def send_prompt(self, *a, **kw) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("send_prompt must not fire when circuit open")

    def ensure_started(self, *a, **kw) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("ensure_started must not fire when circuit open")


class _NoopAudioConverter:
    """Unused when download fails first — just satisfies the
    ``audio_converter is None`` guard in process_audio_to_wav."""

    def to_wav_16k_mono(self, input_path: Path, output_path: Path) -> None:
        raise AssertionError("converter must not fire when download short-circuits")


def test_image_download_circuit_open_returns_friendly_reply(tmp_path: Path) -> None:
    reopens = time.monotonic() + 120  # 2 minutes
    downloader = _DownloadRefuser(
        service_name="meta_media", reopens_at=reopens
    )
    service = MediaService(
        downloader=downloader,  # type: ignore[arg-type]
        cache=_EmptyCache(tmp_path),  # type: ignore[arg-type]
        active_project=_ActiveProject(),  # type: ignore[arg-type]
        session_service=_SessionService(),  # type: ignore[arg-type]
    )

    outcome = service.process_image(
        media_id="m42", caption=None, sender="+4917..."
    )

    assert outcome.kind == "circuit_open"
    assert "meta_media" in outcome.reply
    assert "momentan nicht erreichbar" in outcome.reply
    # Countdown is rendered — either seconds or m-form.
    assert (
        "s." in outcome.reply
        or "m." in outcome.reply
        or "h " in outcome.reply
    )
    assert downloader.calls == 1


def test_pdf_download_circuit_open_returns_same_friendly_reply(tmp_path: Path) -> None:
    reopens = time.monotonic() + 60
    downloader = _DownloadRefuser(
        service_name="meta_media", reopens_at=reopens
    )
    service = MediaService(
        downloader=downloader,  # type: ignore[arg-type]
        cache=_EmptyCache(tmp_path),  # type: ignore[arg-type]
        active_project=_ActiveProject(),  # type: ignore[arg-type]
        session_service=_SessionService(),  # type: ignore[arg-type]
    )

    outcome = service.process_pdf(
        media_id="m77", caption="read me", sender="+4917..."
    )

    assert outcome.kind == "circuit_open"
    assert "meta_media" in outcome.reply


def test_audio_download_circuit_open_returns_friendly_reply(tmp_path: Path) -> None:
    reopens = time.monotonic() + 30
    downloader = _DownloadRefuser(
        service_name="meta_media", reopens_at=reopens
    )
    service = MediaService(
        downloader=downloader,  # type: ignore[arg-type]
        cache=_EmptyCache(tmp_path),  # type: ignore[arg-type]
        active_project=_ActiveProject(),  # type: ignore[arg-type]
        session_service=_SessionService(),  # type: ignore[arg-type]
        audio_converter=_NoopAudioConverter(),  # type: ignore[arg-type]
    )

    outcome = service.process_audio(
        media_id="m99", mime="audio/ogg", sender="+4917..."
    )

    assert outcome.kind == "circuit_open"
    assert "meta_media" in outcome.reply
    # Download refused → no cache, no wav, no session.
    assert outcome.cache_path is None
    assert outcome.wav_path is None
