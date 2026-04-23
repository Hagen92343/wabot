"""Subprocess-based ``AudioConverter`` — shells out to ffmpeg.

Command shape:
    ffmpeg -hide_banner -loglevel error -y -i <input>
           -ar 16000 -ac 1 -f wav <output>

* ``-y`` overwrites ``<output>`` without prompting — we pass a path we
  own, never the user's file.
* ``-ar 16000`` downsamples to 16 kHz; ``-ac 1`` collapses to mono.
  Both are required by whisper.cpp for best-effort fast transcription
  (Spec §16).
* ``-f wav`` forces the output container even if ``<output>`` doesn't
  have a ``.wav`` suffix.
* ``-loglevel error`` keeps stderr small; we tail it on failure.

Timeout: 30 s. A 60 s voice note converts in well under a second on
M1 in practice. If the subprocess hangs we'd rather kill it than let
an inbound message block the webhook event loop.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from whatsbot.logging_setup import get_logger
from whatsbot.ports.audio_converter import AudioConversionError

DEFAULT_TIMEOUT_SECONDS: float = 30.0


class FfmpegAudioConverter:
    """Concrete :class:`~whatsbot.ports.audio_converter.AudioConverter`."""

    def __init__(
        self,
        *,
        ffmpeg_binary: str = "ffmpeg",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._binary = ffmpeg_binary
        self._timeout_s = timeout_seconds
        self._log = get_logger("whatsbot.ffmpeg")

    def to_wav_16k_mono(self, input_path: Path, output_path: Path) -> None:
        if not input_path.exists():
            raise AudioConversionError(f"Input fehlt: {input_path}")
        # Ensure parent dir for the output; callers normally point us
        # at the existing media-cache dir, but a test might pass a
        # fresh tmp dir without ensuring it.
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self._binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-f",
            "wav",
            str(output_path),
        ]
        try:
            result = subprocess.run(  # noqa: S603 — argv list, no shell
                cmd,
                capture_output=True,
                timeout=self._timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AudioConversionError(
                f"ffmpeg nicht gefunden ({self._binary!r})."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AudioConversionError(
                f"ffmpeg timeout nach {self._timeout_s:.0f}s."
            ) from exc

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            tail = stderr[-500:] if len(stderr) > 500 else stderr
            raise AudioConversionError(
                f"ffmpeg failed (exit {result.returncode}): {tail}"
            )

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise AudioConversionError(
                "ffmpeg exited 0 but output file is empty or missing"
            )

        self._log.info(
            "audio_converted",
            input_path=str(input_path),
            output_path=str(output_path),
            output_bytes=output_path.stat().st_size,
        )
