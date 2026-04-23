"""C7.4 — MediaService.process_audio (stage-1 + whisper + send_prompt)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from whatsbot.application.media_service import MediaService
from whatsbot.ports.audio_converter import AudioConversionError
from whatsbot.ports.audio_transcriber import TranscriptionError
from whatsbot.ports.media_cache import CachedItem
from whatsbot.ports.media_downloader import (
    DownloadedMedia,
    MediaDownloadError,
    MediaDownloader,
)


class FakeDownloader:
    def __init__(
        self, *, payload: bytes, mime: str, raise_error: str | None = None
    ) -> None:
        self.payload = payload
        self.mime = mime
        self.raise_error = raise_error

    def download(self, media_id: str) -> DownloadedMedia:
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
        self._root.mkdir(parents=True, exist_ok=True)
        self.stored: list[Path] = []

    def store(self, media_id: str, payload: bytes, suffix: str) -> Path:
        path = self._root / f"{media_id}{suffix}"
        path.write_bytes(payload)
        self.stored.append(path)
        return path

    def path_for(self, media_id: str, suffix: str) -> Path:
        return self._root / f"{media_id}{suffix}"

    def list_all(self) -> list[CachedItem]:
        return []

    def secure_delete(self, path: Path) -> None:
        if path.exists():
            path.unlink()


class FakeActiveProject:
    def __init__(self, active: str | None = "alpha") -> None:
        self.active = active

    def get_active(self) -> str | None:
        return self.active

    def set_active(self, raw_name: str) -> str:  # pragma: no cover
        self.active = raw_name
        return raw_name


@dataclass
class _Prompt:
    project: str
    text: str


class FakeSession:
    def __init__(self) -> None:
        self.prompts: list[_Prompt] = []

    def send_prompt(self, project_name: str, text: str) -> None:
        self.prompts.append(_Prompt(project=project_name, text=text))


class FakeConverter:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path]] = []

    def to_wav_16k_mono(self, input_path: Path, output_path: Path) -> None:
        self.calls.append((input_path, output_path))
        output_path.write_bytes(b"RIFF\x24\x00\x00\x00WAVE")


class BrokenConverter:
    def to_wav_16k_mono(self, input_path: Path, output_path: Path) -> None:
        raise AudioConversionError("ffmpeg exploded")


class FakeTranscriber:
    def __init__(self, *, text: str = "Hallo Claude") -> None:
        self.text = text
        self.calls: list[tuple[Path, str | None]] = []

    def transcribe(self, wav_path: Path, *, language: str | None = None) -> str:
        self.calls.append((wav_path, language))
        return self.text


class BrokenTranscriber:
    def transcribe(self, wav_path: Path, *, language: str | None = None) -> str:
        raise TranscriptionError("whisper died")


def _make_service(
    tmp_path: Path,
    *,
    downloader: MediaDownloader | None = None,
    active: str | None = "alpha",
    converter: FakeConverter | BrokenConverter | None = None,
    transcriber: object | None = None,
    wire_transcriber: bool = True,
) -> tuple[MediaService, FakeCache, FakeSession]:
    ogg = b"OggS\x00\x02" + b"\x00" * 512
    if downloader is None:
        downloader = FakeDownloader(payload=ogg, mime="audio/ogg")
    cache = FakeCache(tmp_path / "cache")
    session = FakeSession()
    service = MediaService(
        downloader=downloader,
        cache=cache,
        active_project=FakeActiveProject(active),
        session_service=session,  # type: ignore[arg-type]
        audio_converter=converter or FakeConverter(),
        audio_transcriber=transcriber  # type: ignore[arg-type]
        if wire_transcriber
        else None,
    )
    return service, cache, session


# ---- happy path ----------------------------------------------------------


def test_process_audio_happy_path(tmp_path: Path) -> None:
    transcriber = FakeTranscriber(text="Hallo Claude, fasse das Dokument zusammen.")
    service, cache, session = _make_service(
        tmp_path, transcriber=transcriber
    )
    outcome = service.process_audio(
        media_id="voice1", mime="audio/ogg", sender="+491"
    )
    assert outcome.kind == "sent"
    assert outcome.project == "alpha"
    assert outcome.wav_path is not None
    assert outcome.wav_path.exists()
    # Reply says Voice + project.
    assert "Voice" in outcome.reply
    assert "alpha" in outcome.reply
    # Prompt was sent as the cleaned transcript (no analysiere/lies
    # prefix — voice is just speech).
    assert len(session.prompts) == 1
    assert session.prompts[0].project == "alpha"
    assert (
        session.prompts[0].text
        == "Hallo Claude, fasse das Dokument zusammen."
    )
    # Whisper received the WAV we stored.
    assert len(transcriber.calls) == 1
    wav_arg, lang = transcriber.calls[0]
    assert wav_arg == outcome.wav_path
    assert lang is None  # auto-detect


# ---- stage-1 failures propagate unchanged --------------------------------


def test_process_audio_propagates_no_active_project(tmp_path: Path) -> None:
    transcriber = FakeTranscriber()
    service, _cache, session = _make_service(
        tmp_path, transcriber=transcriber, active=None
    )
    outcome = service.process_audio(
        media_id="voice1", mime="audio/ogg", sender="+491"
    )
    assert outcome.kind == "no_active_project"
    assert "/p" in outcome.reply
    assert transcriber.calls == []
    assert session.prompts == []


def test_process_audio_propagates_download_failure(tmp_path: Path) -> None:
    downloader = FakeDownloader(
        payload=b"", mime="", raise_error="network down"
    )
    transcriber = FakeTranscriber()
    service, _cache, session = _make_service(
        tmp_path, downloader=downloader, transcriber=transcriber
    )
    outcome = service.process_audio(
        media_id="voice1", mime="audio/ogg", sender="+491"
    )
    assert outcome.kind == "download_failed"
    assert transcriber.calls == []
    assert session.prompts == []


def test_process_audio_propagates_conversion_failure(tmp_path: Path) -> None:
    transcriber = FakeTranscriber()
    service, _cache, session = _make_service(
        tmp_path, converter=BrokenConverter(), transcriber=transcriber
    )
    outcome = service.process_audio(
        media_id="voice1", mime="audio/ogg", sender="+491"
    )
    assert outcome.kind == "conversion_failed"
    assert transcriber.calls == []
    assert session.prompts == []


# ---- stage-2 failures ----------------------------------------------------


def test_process_audio_transcription_failure(tmp_path: Path) -> None:
    service, _cache, session = _make_service(
        tmp_path, transcriber=BrokenTranscriber()
    )
    outcome = service.process_audio(
        media_id="voice1", mime="audio/ogg", sender="+491"
    )
    assert outcome.kind == "transcription_failed"
    assert "fehlgeschlagen" in outcome.reply.lower()
    # WAV still on disk — useful for operator retry.
    assert outcome.wav_path is not None
    assert outcome.wav_path.exists()
    assert session.prompts == []


def test_process_audio_transcriber_not_wired(tmp_path: Path) -> None:
    service, _cache, session = _make_service(
        tmp_path, wire_transcriber=True, transcriber=None
    )
    # Override: we want an unwired transcriber to test the missing path.
    service._audio_transcriber = None  # type: ignore[attr-defined]
    outcome = service.process_audio(
        media_id="voice1", mime="audio/ogg", sender="+491"
    )
    assert outcome.kind == "transcription_failed"
    assert "konfiguriert" in outcome.reply.lower()
    assert session.prompts == []


def test_process_audio_empty_transcript(tmp_path: Path) -> None:
    transcriber = FakeTranscriber(text="[BLANK_AUDIO]\n\n  ")
    service, _cache, session = _make_service(
        tmp_path, transcriber=transcriber
    )
    outcome = service.process_audio(
        media_id="voice1", mime="audio/ogg", sender="+491"
    )
    assert outcome.kind == "empty_transcript"
    assert "Kein Sprachinhalt" in outcome.reply
    assert session.prompts == []


# ---- transcript is cleaned before send_prompt ---------------------------


def test_process_audio_cleans_transcript_before_sending(tmp_path: Path) -> None:
    # Whisper output with markup + timestamp prefixes — clean_transcript
    # should strip them, and only the clean text goes to send_prompt.
    noisy = (
        "[00:00:00.000 --> 00:00:02.000] hallo\n"
        "[BLANK_AUDIO]\n"
        "[00:00:02.000 --> 00:00:04.000] claude"
    )
    transcriber = FakeTranscriber(text=noisy)
    service, _cache, session = _make_service(
        tmp_path, transcriber=transcriber
    )
    outcome = service.process_audio(
        media_id="voice1", mime="audio/ogg", sender="+491"
    )
    assert outcome.kind == "sent"
    assert len(session.prompts) == 1
    prompt_text = session.prompts[0].text
    assert "BLANK_AUDIO" not in prompt_text
    assert "-->" not in prompt_text
    assert "hallo" in prompt_text
    assert "claude" in prompt_text
