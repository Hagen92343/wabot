"""whisper.cpp-backed :class:`AudioTranscriber`.

Command shape (whisper.cpp ``whisper-cli``):

    whisper-cli -m <model> -l <lang|auto> -f <wav> -nt -np -otxt -of <stem>

* ``-nt`` kills timestamps in the stdout stream (we strip them
  defensively in :mod:`whatsbot.domain.transcription` too).
* ``-np`` kills the progress printer so stdout stays focused on text.
* ``-otxt -of <stem>`` writes the transcript to ``<stem>.txt``; we
  prefer reading from that file over stdout because some whisper.cpp
  builds interleave informational lines with the transcript on stdout.

Timeout: 60 s. A 60-second voice note transcribes in 2-6 seconds on M1
with the ``small`` model (Spec §20 budget: <10 s). 60 s gives us 10x
headroom for worst-case CPU contention.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from whatsbot.adapters.resilience import resilient
from whatsbot.logging_setup import get_logger
from whatsbot.ports.audio_transcriber import TranscriptionError

# Breaker service-name. whisper.cpp is a local binary so "outage"
# here usually means broken model file / corrupted install; once
# it surfaces we trip the breaker so we don't spin subprocesses
# in a tight loop.
WHISPER_SERVICE: str = "whisper"

DEFAULT_TIMEOUT_SECONDS: float = 60.0
DEFAULT_BINARY: str = "whisper-cli"


class WhisperCppTranscriber:
    """Concrete :class:`~whatsbot.ports.audio_transcriber.AudioTranscriber`."""

    def __init__(
        self,
        *,
        model_path: Path,
        whisper_binary: str = DEFAULT_BINARY,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._model = Path(model_path).expanduser()
        self._binary = whisper_binary
        self._timeout_s = timeout_seconds
        self._log = get_logger("whatsbot.whisper")
        # Best-effort early diagnostic — if the binary isn't on PATH
        # we log now rather than waiting for the first inbound voice.
        if shutil.which(self._binary) is None:
            self._log.warning(
                "whisper_binary_missing",
                binary=self._binary,
            )

    @resilient(WHISPER_SERVICE)
    def transcribe(
        self, wav_path: Path, *, language: str | None = None
    ) -> str:
        if not wav_path.exists():
            raise TranscriptionError(f"WAV fehlt: {wav_path}")
        if not self._model.exists():
            raise TranscriptionError(
                f"Whisper-Model fehlt: {self._model}"
            )
        with tempfile.TemporaryDirectory(prefix="wb-whisper-") as tmp_raw:
            tmp_dir = Path(tmp_raw)
            stem = tmp_dir / "out"
            cmd = [
                self._binary,
                "-m",
                str(self._model),
                "-l",
                language or "auto",
                "-f",
                str(wav_path),
                "-nt",  # no timestamps
                "-np",  # no progress bar
                "-otxt",  # write <stem>.txt
                "-of",
                str(stem),
            ]
            try:
                result = subprocess.run(  # noqa: S603 — argv list, no shell
                    cmd,
                    capture_output=True,
                    timeout=self._timeout_s,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise TranscriptionError(
                    f"whisper-cli nicht gefunden ({self._binary!r})."
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise TranscriptionError(
                    f"whisper-cli timeout nach {self._timeout_s:.0f}s."
                ) from exc

            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                tail = stderr[-500:] if len(stderr) > 500 else stderr
                raise TranscriptionError(
                    f"whisper-cli failed (exit {result.returncode}): {tail}"
                )

            txt_path = stem.with_suffix(".txt")
            if txt_path.exists():
                text = txt_path.read_text(encoding="utf-8", errors="replace")
            else:
                # Fallback to stdout if the build ignored ``-otxt`` (some
                # old whisper.cpp versions do). We accept the risk of
                # informational noise — domain.transcription.clean_transcript
                # strips the common patterns.
                text = result.stdout.decode("utf-8", errors="replace")

        self._log.info(
            "audio_transcribed",
            wav_path=str(wav_path),
            language=language or "auto",
            raw_chars=len(text),
        )
        return text
