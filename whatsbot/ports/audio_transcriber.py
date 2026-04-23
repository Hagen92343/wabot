"""Audio-transcriber port — turn a WAV file into a text transcript.

Phase-7 C7.4 plugs whisper.cpp under this interface. The application
layer consumes the cleaned transcript and forwards it as a Claude
prompt via :meth:`whatsbot.application.session_service.SessionService.send_prompt`.

The ``language`` parameter is an optional BCP-47 code (``"de"``,
``"en"``) or ``None`` to let whisper auto-detect. WhatsApp voice
notes come in mixed German/English for us, so ``None`` is the right
default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class TranscriptionError(RuntimeError):
    """Raised when transcription fails (missing binary, model missing,
    subprocess crash, empty output). ``reason`` is log-safe and the
    webhook renders a generic fallback reply — we don't show the raw
    whisper-cli stderr to the user."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AudioTranscriber(Protocol):
    """Turn a 16 kHz mono WAV into the transcribed text."""

    def transcribe(
        self, wav_path: Path, *, language: str | None = None
    ) -> str:
        """Return the raw (pre-cleanup) transcript.

        Raises :class:`TranscriptionError` on any failure. Returns an
        empty string if whisper genuinely decides there was nothing to
        transcribe (pure silence) — the caller handles that case
        specifically so the user isn't left wondering.
        """
