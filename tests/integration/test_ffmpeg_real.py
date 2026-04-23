"""Real-ffmpeg integration test for C7.3.

Builds an OGG/Opus input via ffmpeg itself (using ``anullsrc`` to
synthesise a second of silence) and runs :class:`FfmpegAudioConverter`
over it. Skipped when ``ffmpeg`` isn't installed; doesn't run in CI
by default because CI images don't all ship ffmpeg.

Why synthesise the input via ffmpeg instead of committing a test
fixture: keeps the repo free of binary blobs and guarantees the input
is valid on whatever ffmpeg version the local machine has.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from whatsbot.adapters.ffmpeg_audio_converter import FfmpegAudioConverter

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
    ),
]


def _make_silence_ogg(path: Path, *, seconds: float = 1.0) -> None:
    """Write a 1-second OGG/Opus silence file to ``path``.

    Uses ffmpeg's internal ``anullsrc`` — no external fixture needed.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r=48000:cl=mono:d={seconds}",
        "-c:a",
        "libopus",
        str(path),
    ]
    result = subprocess.run(  # noqa: S603 — argv list, no shell
        cmd, capture_output=True, timeout=30.0, check=False
    )
    if result.returncode != 0:
        pytest.skip(
            "ffmpeg on this machine refused to synthesise an OGG/Opus "
            f"test input (exit {result.returncode}): "
            f"{result.stderr.decode(errors='replace')[-200:]}"
        )


def test_convert_real_ogg_to_wav_16k_mono(tmp_path: Path) -> None:
    src = tmp_path / "silence.ogg"
    dst = tmp_path / "silence.wav"
    _make_silence_ogg(src, seconds=1.0)

    FfmpegAudioConverter().to_wav_16k_mono(src, dst)

    assert dst.exists()
    data = dst.read_bytes()
    # RIFF / WAVE envelope.
    assert data[0:4] == b"RIFF"
    assert data[8:12] == b"WAVE"
    # 'fmt ' chunk + probe the sample rate + channel count. Sample
    # rate is a 4-byte LE int at offset 24, channels is 2-byte LE
    # at offset 22, in a canonical WAV.
    assert data[20:22] == b"\x01\x00"  # PCM
    channels = int.from_bytes(data[22:24], "little")
    sample_rate = int.from_bytes(data[24:28], "little")
    assert channels == 1
    assert sample_rate == 16000
