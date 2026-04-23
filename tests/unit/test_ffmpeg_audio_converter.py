"""Unit tests for whatsbot.adapters.ffmpeg_audio_converter.

We don't shell out to real ffmpeg here — we redirect ``ffmpeg`` to a
small helper script on PATH that lets us control the exit code, stderr
and whether the output file actually gets written. That keeps the test
deterministic and usable on machines without ffmpeg installed.

The real-binary integration test lives in
:mod:`tests.integration.test_ffmpeg_real`.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from whatsbot.adapters.ffmpeg_audio_converter import FfmpegAudioConverter
from whatsbot.ports.audio_converter import AudioConversionError

pytestmark = pytest.mark.unit


@pytest.fixture
def fake_ffmpeg_dir(tmp_path: Path) -> Iterator[Path]:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    yield bin_dir


def _install_fake(bin_dir: Path, body: str) -> Path:
    script = bin_dir / "ffmpeg"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(
        script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return script


def _set_path(monkeypatch: pytest.MonkeyPatch, bin_dir: Path) -> None:
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


# --- happy path ----------------------------------------------------------


def test_convert_succeeds_when_ffmpeg_writes_output(
    fake_ffmpeg_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # ffmpeg argv shape ends with ``<output>``. Our fake finds it by
    # position (last arg) and writes a plausible WAV there.
    _install_fake(
        fake_ffmpeg_dir,
        # shell sees args 0..N; output is the final argv entry.
        'out="${@: -1}"; printf "RIFF\\x04\\x00\\x00\\x00WAVE" > "$out"; '
        "exit 0",
    )
    _set_path(monkeypatch, fake_ffmpeg_dir)

    src = tmp_path / "in.ogg"
    src.write_bytes(b"OggS\x00\x02")
    dst = tmp_path / "out.wav"

    FfmpegAudioConverter().to_wav_16k_mono(src, dst)
    assert dst.exists()
    assert dst.read_bytes().startswith(b"RIFF")


def test_convert_creates_output_parent_dir(
    fake_ffmpeg_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake(
        fake_ffmpeg_dir,
        'out="${@: -1}"; printf "RIFF" > "$out"; exit 0',
    )
    _set_path(monkeypatch, fake_ffmpeg_dir)

    src = tmp_path / "in.ogg"
    src.write_bytes(b"OggS")
    # Output path's parent doesn't exist yet.
    dst = tmp_path / "nested" / "deeper" / "out.wav"
    assert not dst.parent.exists()

    FfmpegAudioConverter().to_wav_16k_mono(src, dst)
    assert dst.exists()


# --- error paths ---------------------------------------------------------


def test_convert_missing_input_raises(tmp_path: Path) -> None:
    src = tmp_path / "does-not-exist.ogg"
    dst = tmp_path / "out.wav"
    with pytest.raises(AudioConversionError, match="Input fehlt"):
        FfmpegAudioConverter().to_wav_16k_mono(src, dst)


def test_convert_nonzero_exit_surfaces_stderr_tail(
    fake_ffmpeg_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake(
        fake_ffmpeg_dir,
        "echo 'bad codec: something exploded' 1>&2; exit 1",
    )
    _set_path(monkeypatch, fake_ffmpeg_dir)

    src = tmp_path / "in.ogg"
    src.write_bytes(b"OggS")
    dst = tmp_path / "out.wav"

    with pytest.raises(AudioConversionError) as exc_info:
        FfmpegAudioConverter().to_wav_16k_mono(src, dst)
    msg = str(exc_info.value)
    assert "exit 1" in msg
    assert "bad codec" in msg


def test_convert_exit_zero_but_empty_output_raises(
    fake_ffmpeg_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # exit 0 but write nothing — guard against silent failure.
    _install_fake(fake_ffmpeg_dir, "exit 0")
    _set_path(monkeypatch, fake_ffmpeg_dir)

    src = tmp_path / "in.ogg"
    src.write_bytes(b"OggS")
    dst = tmp_path / "out.wav"

    with pytest.raises(AudioConversionError, match="empty or missing"):
        FfmpegAudioConverter().to_wav_16k_mono(src, dst)


def test_convert_exit_zero_with_empty_written_output_raises(
    fake_ffmpeg_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Writes the file but with zero bytes.
    _install_fake(
        fake_ffmpeg_dir,
        'out="${@: -1}"; : > "$out"; exit 0',
    )
    _set_path(monkeypatch, fake_ffmpeg_dir)

    src = tmp_path / "in.ogg"
    src.write_bytes(b"OggS")
    dst = tmp_path / "out.wav"

    with pytest.raises(AudioConversionError, match="empty or missing"):
        FfmpegAudioConverter().to_wav_16k_mono(src, dst)


def test_convert_missing_binary_raises(tmp_path: Path) -> None:
    src = tmp_path / "in.ogg"
    src.write_bytes(b"OggS")
    dst = tmp_path / "out.wav"
    with pytest.raises(AudioConversionError, match="ffmpeg nicht gefunden"):
        FfmpegAudioConverter(ffmpeg_binary="/nonexistent/ffmpeg").to_wav_16k_mono(
            src, dst
        )


def test_convert_timeout_raises(
    fake_ffmpeg_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # sleep past the 0.5s timeout we'll use.
    _install_fake(fake_ffmpeg_dir, "sleep 3")
    _set_path(monkeypatch, fake_ffmpeg_dir)

    src = tmp_path / "in.ogg"
    src.write_bytes(b"OggS")
    dst = tmp_path / "out.wav"

    with pytest.raises(AudioConversionError, match="timeout"):
        FfmpegAudioConverter(timeout_seconds=0.5).to_wav_16k_mono(src, dst)


def test_convert_passes_correct_argv(
    fake_ffmpeg_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Record the argv our adapter invokes.
    argv_dump = tmp_path / "argv.txt"
    _install_fake(
        fake_ffmpeg_dir,
        f'printf "%s\\n" "$@" > "{argv_dump}"; '
        'out="${@: -1}"; printf "RIFF" > "$out"; exit 0',
    )
    _set_path(monkeypatch, fake_ffmpeg_dir)

    src = tmp_path / "in.ogg"
    src.write_bytes(b"OggS")
    dst = tmp_path / "out.wav"

    FfmpegAudioConverter().to_wav_16k_mono(src, dst)
    argv = argv_dump.read_text().splitlines()
    # Sanity check: the key flags are there.
    assert "-i" in argv
    assert "-ar" in argv
    assert argv[argv.index("-ar") + 1] == "16000"
    assert "-ac" in argv
    assert argv[argv.index("-ac") + 1] == "1"
    assert "-f" in argv
    assert argv[argv.index("-f") + 1] == "wav"
    assert str(src) in argv
    assert str(dst) in argv
    assert argv[-1] == str(dst)  # output is last
