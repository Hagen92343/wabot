"""Magic-bytes detection for media validation — pure, no I/O.

The MIME type Meta hands us in the webhook comes from the sender's
device and can be spoofed. We also run a magic-bytes check on the first
few bytes of the downloaded payload so a sender can't pass off a
``.sh`` script as an ``image/png``.

All functions here return ``False`` on payloads that are too short to
match a signature; they never raise. Callers decide whether a failed
check means reject.
"""

from __future__ import annotations

from typing import Final

_JPEG_SOI: Final[bytes] = b"\xff\xd8\xff"
_PNG_MAGIC: Final[bytes] = b"\x89PNG\r\n\x1a\n"
_GIF87A: Final[bytes] = b"GIF87a"
_GIF89A: Final[bytes] = b"GIF89a"
_RIFF: Final[bytes] = b"RIFF"
_WEBP_TAG: Final[bytes] = b"WEBP"
_WAVE_TAG: Final[bytes] = b"WAVE"
_HEIF_FTYP_BRANDS: Final[frozenset[bytes]] = frozenset(
    {b"heic", b"heix", b"mif1", b"msf1", b"heim", b"heis", b"hevc", b"hevx"}
)
_MP4_FTYP_BRANDS: Final[frozenset[bytes]] = frozenset(
    {b"mp41", b"mp42", b"isom", b"iso2", b"M4A ", b"M4V ", b"dash"}
)
_ID3: Final[bytes] = b"ID3"
_OGG: Final[bytes] = b"OggS"
_EBML: Final[bytes] = b"\x1a\x45\xdf\xa3"  # Matroska/WebM
_PDF_PREFIX: Final[bytes] = b"%PDF-"


def looks_like_pdf(payload: bytes, mime: str | None = None) -> bool:
    """``True`` iff ``payload`` starts with the PDF magic prefix.

    Spec §16 validates PDFs via the first five bytes — full structural
    validation is Claude's problem once the file is forwarded. The
    ``mime`` argument is accepted for signature parity with the other
    ``looks_like_*`` predicates and is ignored.
    """
    del mime  # unused — signature kept uniform across predicates
    return payload.startswith(_PDF_PREFIX)


def looks_like_image(payload: bytes, mime: str | None = None) -> bool:
    """``True`` iff ``payload`` starts with a known image signature.

    Recognises JPEG, PNG, WEBP, HEIC/HEIF, GIF. ``mime`` is optional —
    when provided, it helps distinguish ambiguous container formats
    (e.g. WEBP lives inside RIFF, MP4 inside ``ftyp`` boxes) but a
    payload with a clear magic match is accepted regardless.
    """
    if payload.startswith(_JPEG_SOI):
        return True
    if payload.startswith(_PNG_MAGIC):
        return True
    if payload.startswith(_GIF87A) or payload.startswith(_GIF89A):
        return True
    if _looks_like_webp(payload):
        return True
    return _looks_like_heif(payload)


def looks_like_audio(payload: bytes, mime: str | None = None) -> bool:
    """``True`` iff ``payload`` starts with a known audio signature.

    Recognises OGG/Opus, MP3 (ID3v2 tag or MPEG sync), MP4/M4A,
    WAV (RIFF + WAVE), WebM. WhatsApp voice messages come through as
    OGG/Opus so that's the primary hit.
    """
    if payload.startswith(_OGG):
        return True
    if payload.startswith(_ID3):
        return True
    if _looks_like_mp3_sync(payload):
        return True
    if _looks_like_wav(payload):
        return True
    if _looks_like_mp4_audio(payload):
        return True
    return payload.startswith(_EBML)  # WebM


# --- helpers --------------------------------------------------------------


def _looks_like_webp(payload: bytes) -> bool:
    # RIFF <size:4> WEBP ...
    if len(payload) < 12:
        return False
    return payload[0:4] == _RIFF and payload[8:12] == _WEBP_TAG


def _looks_like_wav(payload: bytes) -> bool:
    # RIFF <size:4> WAVE ...
    if len(payload) < 12:
        return False
    return payload[0:4] == _RIFF and payload[8:12] == _WAVE_TAG


def _looks_like_heif(payload: bytes) -> bool:
    # ISO-BMFF: <size:4> 'ftyp' <major_brand:4> ...
    if len(payload) < 12:
        return False
    if payload[4:8] != b"ftyp":
        return False
    brand = payload[8:12]
    return brand in _HEIF_FTYP_BRANDS


def _looks_like_mp4_audio(payload: bytes) -> bool:
    # ISO-BMFF MP4 containers — M4A etc. Same ftyp layout as HEIF but
    # different brand set.
    if len(payload) < 12:
        return False
    if payload[4:8] != b"ftyp":
        return False
    return payload[8:12] in _MP4_FTYP_BRANDS


def _looks_like_mp3_sync(payload: bytes) -> bool:
    # MPEG Audio frame sync: first byte 0xFF, top 3 bits of second byte set.
    if len(payload) < 2:
        return False
    if payload[0] != 0xFF:
        return False
    return (payload[1] & 0xE0) == 0xE0
