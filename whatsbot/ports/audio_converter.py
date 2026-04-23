"""Audio-converter port — normalise any inbound audio to 16 kHz mono WAV.

Phase-7 C7.3: whisper.cpp reads WAV the fastest and most reliably when
input is 16 kHz mono. WhatsApp voice notes land as OGG/Opus by default,
so the conversion is mandatory. We also accept MP3/MP4/WAV formats and
normalise them to the same shape so the downstream transcriber (C7.4)
only has to deal with one canonical input.

The port is thin on purpose — the adapter shells out to ffmpeg and the
application layer chains it with the cache and the transcriber. Tests
inject a :class:`FakeAudioConverter` (in the test suite) that records
the call arguments without touching ffmpeg.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class AudioConversionError(RuntimeError):
    """Raised when the converter cannot produce a valid output file.

    The ``reason`` string is safe to log but, like
    :class:`whatsbot.ports.media_downloader.MediaDownloadError`, it's
    not meant for direct WhatsApp display — the webhook layer renders
    a generic fallback reply.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AudioConverter(Protocol):
    """Convert an arbitrary audio file to 16 kHz mono WAV."""

    def to_wav_16k_mono(self, input_path: Path, output_path: Path) -> None:
        """Write the converted WAV to ``output_path``.

        Overwrites any file already at ``output_path``. Raises
        :class:`AudioConversionError` on any failure (missing binary,
        unreadable input, non-audio content, timeout).
        """
