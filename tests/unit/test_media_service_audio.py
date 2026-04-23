"""C7.3 — MediaService.process_audio_to_wav tests.

Covers the audio Stage-1 pipeline (download → validate → cache → ffmpeg
convert) with a :class:`FakeAudioConverter`. The real ffmpeg adapter is
exercised separately in :mod:`tests.integration.test_ffmpeg_audio_converter`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from whatsbot.application.media_service import MediaService
from whatsbot.ports.audio_converter import AudioConversionError
from whatsbot.ports.media_cache import CachedItem
from whatsbot.ports.media_downloader import (
    DownloadedMedia,
    MediaDownloader,
    MediaDownloadError,
)


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


class FakeAudioConverter:
    """Writes a tiny WAV stub at ``output_path`` and records the call.

    Optionally raises :class:`AudioConversionError` on the first call
    (for failure-containment assertions).
    """

    def __init__(self, *, raise_error: str | None = None) -> None:
        self.calls: list[tuple[Path, Path]] = []
        self.raise_error = raise_error

    def to_wav_16k_mono(self, input_path: Path, output_path: Path) -> None:
        self.calls.append((input_path, output_path))
        if self.raise_error is not None:
            raise AudioConversionError(self.raise_error)
        # Minimal RIFF WAVE header so downstream code that peeks the
        # file can identify it — empty data chunk is fine here.
        wav_bytes = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
        output_path.write_bytes(wav_bytes)


def _make_service(
    tmp_path: Path,
    *,
    downloader: MediaDownloader | None = None,
    active: str | None = "alpha",
    converter: FakeAudioConverter | None = None,
    wire_converter: bool = True,
) -> tuple[MediaService, FakeCache, FakeSessionService, FakeAudioConverter | None]:
    ogg_payload = b"OggS\x00\x02" + b"\x00" * 512
    if downloader is None:
        downloader = FakeDownloader(payload=ogg_payload, mime="audio/ogg")
    cache = FakeCache(tmp_path / "cache")
    session = FakeSessionService()
    if converter is None and wire_converter:
        converter = FakeAudioConverter()
    service = MediaService(
        downloader=downloader,
        cache=cache,
        active_project=FakeActiveProject(active),
        session_service=session,
        audio_converter=converter if wire_converter else None,
    )
    return service, cache, session, converter


def test_process_audio_to_wav_happy_path(tmp_path: Path) -> None:
    service, cache, session, converter = _make_service(tmp_path)
    outcome = service.process_audio_to_wav(
        media_id="voice1", mime="audio/ogg", sender="+491"
    )

    assert outcome.kind == "audio_staged"
    assert outcome.project == "alpha"
    assert outcome.cache_path is not None
    assert outcome.cache_path.suffix == ".ogg"
    assert outcome.cache_path.exists()
    assert outcome.wav_path is not None
    assert outcome.wav_path.suffix == ".wav"
    assert outcome.wav_path.exists()
    assert "Transkribiere" in outcome.reply

    # Source blob was stored once with the right suffix.
    assert len(cache.stores) == 1
    assert cache.stores[0][2] == ".ogg"

    # Converter was called exactly once with (source_path, wav_path).
    assert converter is not None
    assert len(converter.calls) == 1
    src, dst = converter.calls[0]
    assert src == outcome.cache_path
    assert dst == outcome.wav_path

    # No prompt sent in C7.3 — that's C7.4 once whisper lands.
    assert session.prompts == []


def test_process_audio_to_wav_no_active_project(tmp_path: Path) -> None:
    service, cache, _session, converter = _make_service(tmp_path, active=None)
    outcome = service.process_audio_to_wav(
        media_id="voice1", mime="audio/ogg", sender="+491"
    )
    assert outcome.kind == "no_active_project"
    assert "/p" in outcome.reply
    assert cache.stores == []
    assert converter is not None
    assert converter.calls == []


def test_process_audio_to_wav_download_failure(tmp_path: Path) -> None:
    downloader = FakeDownloader(
        payload=b"", mime="", raise_error="no network"
    )
    service, cache, _session, converter = _make_service(
        tmp_path, downloader=downloader
    )
    outcome = service.process_audio_to_wav(
        media_id="voice1", mime="audio/ogg", sender="+491"
    )
    assert outcome.kind == "download_failed"
    assert cache.stores == []
    assert converter is not None
    assert converter.calls == []


def test_process_audio_to_wav_rejects_disallowed_mime(tmp_path: Path) -> None:
    downloader = FakeDownloader(payload=b"OggS\x00", mime="video/mp4")
    service, cache, _session, converter = _make_service(
        tmp_path, downloader=downloader
    )
    outcome = service.process_audio_to_wav(
        media_id="voice1", mime="video/mp4", sender="+491"
    )
    assert outcome.kind == "validation_failed"
    assert cache.stores == []
    assert converter is not None
    assert converter.calls == []


def test_process_audio_to_wav_rejects_oversized(tmp_path: Path) -> None:
    # 26 MB > 25 MB cap
    big = b"OggS\x00" + b"\x00" * (26 * 1024 * 1024)
    downloader = FakeDownloader(payload=big, mime="audio/ogg")
    service, cache, _session, converter = _make_service(
        tmp_path, downloader=downloader
    )
    outcome = service.process_audio_to_wav(
        media_id="voice1", mime="audio/ogg", sender="+491"
    )
    assert outcome.kind == "validation_failed"
    assert "zu gross" in outcome.reply.lower() or "zu groß" in outcome.reply.lower()
    assert cache.stores == []
    assert converter is not None
    assert converter.calls == []


def test_process_audio_to_wav_rejects_magic_bytes_mismatch(tmp_path: Path) -> None:
    # MIME says audio/ogg but the bytes are a PDF header.
    downloader = FakeDownloader(payload=b"%PDF-1.4\n", mime="audio/ogg")
    service, cache, _session, converter = _make_service(
        tmp_path, downloader=downloader
    )
    outcome = service.process_audio_to_wav(
        media_id="voice1", mime="audio/ogg", sender="+491"
    )
    assert outcome.kind == "validation_failed"
    assert "Inhalt" in outcome.reply
    assert cache.stores == []
    assert converter is not None
    assert converter.calls == []


def test_process_audio_to_wav_ffmpeg_failure_is_contained(tmp_path: Path) -> None:
    converter = FakeAudioConverter(raise_error="ffmpeg exited 1")
    service, cache, _session, _c = _make_service(
        tmp_path, converter=converter
    )
    outcome = service.process_audio_to_wav(
        media_id="voice1", mime="audio/ogg", sender="+491"
    )

    assert outcome.kind == "conversion_failed"
    # Source was already cached before the convert attempt; we leave
    # it on disk so the next delivery (or an operator restart) can
    # inspect what we tried to convert.
    assert outcome.cache_path is not None
    assert outcome.cache_path.exists()
    assert outcome.wav_path is None
    assert "fehlgeschlagen" in outcome.reply.lower()


def test_process_audio_to_wav_without_converter_wired(tmp_path: Path) -> None:
    service, cache, _session, _converter = _make_service(
        tmp_path, wire_converter=False
    )
    outcome = service.process_audio_to_wav(
        media_id="voice1", mime="audio/ogg", sender="+491"
    )
    assert outcome.kind == "conversion_failed"
    # Never reaches download — fails fast on missing wiring.
    assert cache.stores == []


def test_process_audio_to_wav_uses_graph_mime_over_hint(tmp_path: Path) -> None:
    # Caller hints audio/wav but Graph says the blob is audio/ogg —
    # Graph wins because it's closer to the actual content.
    downloader = FakeDownloader(payload=b"OggS\x00\x02", mime="audio/ogg")
    service, _cache, _session, _converter = _make_service(
        tmp_path, downloader=downloader
    )
    outcome = service.process_audio_to_wav(
        media_id="voice1", mime="audio/wav", sender="+491"
    )
    assert outcome.kind == "audio_staged"
    assert outcome.cache_path is not None
    assert outcome.cache_path.suffix == ".ogg"  # came from Graph's MIME


def test_process_audio_to_wav_falls_back_to_caller_mime(tmp_path: Path) -> None:
    # Graph returns no MIME — caller's hint is the only truth we have.
    downloader = FakeDownloader(payload=b"OggS\x00\x02", mime="")
    service, _cache, _session, _converter = _make_service(
        tmp_path, downloader=downloader
    )
    outcome = service.process_audio_to_wav(
        media_id="voice1", mime="audio/ogg", sender="+491"
    )
    assert outcome.kind == "audio_staged"
    assert outcome.cache_path is not None
    assert outcome.cache_path.suffix == ".ogg"
