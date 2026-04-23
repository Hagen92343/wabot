"""C7.1 — domain/media.py + domain/magic_bytes.py unit tests."""

from __future__ import annotations

import pytest

from whatsbot.domain.magic_bytes import (
    looks_like_audio,
    looks_like_image,
    looks_like_pdf,
)
from whatsbot.domain.media import (
    ALLOWED_MIMES_PER_KIND,
    MAX_BYTES_PER_KIND,
    SUPPORTED_KINDS,
    UNSUPPORTED_KINDS,
    MediaKind,
    MediaValidationError,
    classify_meta_kind,
    suffix_for_mime,
    validate_mime,
    validate_size,
)

# --- classify_meta_kind ---------------------------------------------------


@pytest.mark.parametrize(
    ("meta_type", "expected"),
    [
        ("image", MediaKind.IMAGE),
        ("IMAGE", MediaKind.IMAGE),
        (" image ", MediaKind.IMAGE),
        ("audio", MediaKind.AUDIO),
        ("voice", MediaKind.AUDIO),  # WhatsApp sometimes uses "voice"
        ("document", MediaKind.DOCUMENT),
        ("video", MediaKind.VIDEO),
        ("location", MediaKind.LOCATION),
        ("sticker", MediaKind.STICKER),
        ("contacts", MediaKind.CONTACT),
        ("contact", MediaKind.CONTACT),
        ("reaction", MediaKind.UNKNOWN),  # Not handled
        ("", MediaKind.UNKNOWN),
        (None, MediaKind.UNKNOWN),
    ],
)
def test_classify_meta_kind(meta_type: str | None, expected: MediaKind) -> None:
    assert classify_meta_kind(meta_type) is expected


def test_supported_and_unsupported_are_disjoint_and_complete() -> None:
    """Every kind is either supported, unsupported, or UNKNOWN."""
    assert SUPPORTED_KINDS.isdisjoint(UNSUPPORTED_KINDS)
    assert MediaKind.UNKNOWN not in SUPPORTED_KINDS
    assert MediaKind.UNKNOWN not in UNSUPPORTED_KINDS
    all_kinds = set(MediaKind)
    categorized = SUPPORTED_KINDS | UNSUPPORTED_KINDS | {MediaKind.UNKNOWN}
    assert all_kinds == categorized


# --- validate_size --------------------------------------------------------


def test_validate_size_image_under_limit() -> None:
    validate_size(MediaKind.IMAGE, 1024)  # 1 KB — well under 10 MB


def test_validate_size_image_at_limit() -> None:
    validate_size(MediaKind.IMAGE, MAX_BYTES_PER_KIND[MediaKind.IMAGE])


def test_validate_size_image_over_limit_raises() -> None:
    limit = MAX_BYTES_PER_KIND[MediaKind.IMAGE]
    with pytest.raises(MediaValidationError) as exc_info:
        validate_size(MediaKind.IMAGE, limit + 1)
    assert "zu gross" in str(exc_info.value)


def test_validate_size_negative_raises() -> None:
    with pytest.raises(MediaValidationError, match="negative"):
        validate_size(MediaKind.IMAGE, -1)


def test_validate_size_unsupported_kind_raises() -> None:
    with pytest.raises(MediaValidationError):
        validate_size(MediaKind.VIDEO, 1024)


# --- validate_mime --------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "mime"),
    [
        (MediaKind.IMAGE, "image/jpeg"),
        (MediaKind.IMAGE, "IMAGE/JPEG"),  # case-insensitive
        (MediaKind.IMAGE, "image/jpeg; charset=binary"),  # params ok
        (MediaKind.IMAGE, "image/png"),
        (MediaKind.IMAGE, "image/webp"),
        (MediaKind.IMAGE, "image/heic"),
        (MediaKind.DOCUMENT, "application/pdf"),
        (MediaKind.AUDIO, "audio/ogg"),
        (MediaKind.AUDIO, "audio/opus"),
        (MediaKind.AUDIO, "audio/mp4"),
    ],
)
def test_validate_mime_accepts_allowed(kind: MediaKind, mime: str) -> None:
    validate_mime(kind, mime)  # does not raise


@pytest.mark.parametrize(
    ("kind", "mime"),
    [
        (MediaKind.IMAGE, "application/pdf"),
        (MediaKind.IMAGE, "text/html"),
        (MediaKind.DOCUMENT, "image/jpeg"),
        (MediaKind.AUDIO, "video/mp4"),
        (MediaKind.IMAGE, ""),
        (MediaKind.IMAGE, "   "),
        (MediaKind.IMAGE, None),
    ],
)
def test_validate_mime_rejects_disallowed(
    kind: MediaKind, mime: str | None
) -> None:
    with pytest.raises(MediaValidationError):
        validate_mime(kind, mime)


def test_validate_mime_unsupported_kind_raises() -> None:
    with pytest.raises(MediaValidationError):
        validate_mime(MediaKind.STICKER, "image/webp")


# --- suffix_for_mime ------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "mime", "expected"),
    [
        (MediaKind.IMAGE, "image/jpeg", ".jpg"),
        (MediaKind.IMAGE, "image/png", ".png"),
        (MediaKind.IMAGE, "image/webp", ".webp"),
        (MediaKind.IMAGE, "image/heic", ".heic"),
        (MediaKind.IMAGE, "image/heif", ".heic"),
        (MediaKind.DOCUMENT, "application/pdf", ".pdf"),
        (MediaKind.AUDIO, "audio/ogg", ".ogg"),
        (MediaKind.AUDIO, "audio/opus", ".ogg"),
        (MediaKind.AUDIO, "audio/mp4", ".m4a"),
        (MediaKind.AUDIO, "audio/mpeg", ".mp3"),
        (MediaKind.AUDIO, "audio/wav", ".wav"),
        (MediaKind.IMAGE, "image/unknown", ".img"),  # fallback per kind
        (MediaKind.IMAGE, "IMAGE/JPEG; charset=utf-8", ".jpg"),
    ],
)
def test_suffix_for_mime(kind: MediaKind, mime: str, expected: str) -> None:
    assert suffix_for_mime(kind, mime) == expected


# --- magic bytes: PDF ------------------------------------------------------


def test_looks_like_pdf_matches() -> None:
    assert looks_like_pdf(b"%PDF-1.4\n...")


def test_looks_like_pdf_rejects_empty() -> None:
    assert not looks_like_pdf(b"")


def test_looks_like_pdf_rejects_short() -> None:
    assert not looks_like_pdf(b"%PD")


def test_looks_like_pdf_rejects_other() -> None:
    assert not looks_like_pdf(b"\xff\xd8\xff\xe0")  # JPEG header
    assert not looks_like_pdf(b"<html>")


# --- magic bytes: image ----------------------------------------------------


def test_looks_like_image_jpeg() -> None:
    assert looks_like_image(b"\xff\xd8\xff\xe0\x00\x10JFIF")


def test_looks_like_image_png() -> None:
    assert looks_like_image(b"\x89PNG\r\n\x1a\n...")


def test_looks_like_image_webp() -> None:
    # RIFF <4 bytes size> WEBP
    payload = b"RIFF\x00\x00\x00\x00WEBPVP8 ..."
    assert looks_like_image(payload)


def test_looks_like_image_heic() -> None:
    payload = b"\x00\x00\x00\x20ftypheic..."
    assert looks_like_image(payload)


def test_looks_like_image_gif() -> None:
    assert looks_like_image(b"GIF89a...")
    assert looks_like_image(b"GIF87a...")


def test_looks_like_image_rejects_short() -> None:
    assert not looks_like_image(b"")
    assert not looks_like_image(b"\xff")


def test_looks_like_image_rejects_pdf() -> None:
    assert not looks_like_image(b"%PDF-1.4\n")


def test_looks_like_image_rejects_text() -> None:
    assert not looks_like_image(b"Hello, World!")


# --- magic bytes: audio ----------------------------------------------------


def test_looks_like_audio_ogg() -> None:
    assert looks_like_audio(b"OggS\x00\x02\x00\x00\x00")


def test_looks_like_audio_mp3_id3() -> None:
    assert looks_like_audio(b"ID3\x03\x00\x00\x00")


def test_looks_like_audio_mp3_sync() -> None:
    # Frame sync: 0xFF + top 3 bits of next byte set (0xFB = 11111011)
    assert looks_like_audio(b"\xff\xfb\x90\x00")


def test_looks_like_audio_wav() -> None:
    payload = b"RIFF\x24\x00\x00\x00WAVEfmt "
    assert looks_like_audio(payload)


def test_looks_like_audio_m4a() -> None:
    payload = b"\x00\x00\x00\x20ftypM4A "
    assert looks_like_audio(payload)


def test_looks_like_audio_rejects_short() -> None:
    assert not looks_like_audio(b"")
    assert not looks_like_audio(b"\xff")


def test_looks_like_audio_rejects_image() -> None:
    assert not looks_like_audio(b"\xff\xd8\xff\xe0")  # JPEG


def test_looks_like_audio_rejects_pdf() -> None:
    assert not looks_like_audio(b"%PDF-1.4\n")


# --- cross-check: image vs audio MIME maps ---------------------------------


def test_allowed_mimes_populated_for_supported_kinds() -> None:
    for kind in SUPPORTED_KINDS:
        assert kind in ALLOWED_MIMES_PER_KIND
        assert ALLOWED_MIMES_PER_KIND[kind]


def test_max_bytes_populated_for_supported_kinds() -> None:
    for kind in SUPPORTED_KINDS:
        assert kind in MAX_BYTES_PER_KIND
        assert MAX_BYTES_PER_KIND[kind] > 0
