"""Outbound size-guard — Spec §10 10KB threshold + WhatsApp chunker.

Every outgoing body runs through ``is_oversized``. If true, the caller
replaces the body with ``format_warning`` and stashes the original in
``pending_outputs`` so the user can review via ``/send``, ``/discard``,
or ``/save``.

The threshold is deliberately conservative (10 KB, not 4096 chars which
is the WhatsApp per-message limit) — a 30 KB log dump produced by
Claude in a lapse of attention shouldn't silently ship over 8 messages
without the user's explicit ok.

Applies in every mode, including YOLO (Spec §10): the user's
acceptance of risk around tool invocations doesn't extend to
unreviewed bulk exfiltration.

Pure module — no I/O, no state. The orchestration (file-write,
DB-row, warning-send) lives in ``application.output_service``.
"""

from __future__ import annotations

from typing import Final

# 10 KB in bytes — Spec §10. Counted in UTF-8 bytes so a German
# umlaut-heavy text stays within the intent (ä = 2 bytes in utf-8).
THRESHOLD_BYTES: Final[int] = 10 * 1024

# WhatsApp Cloud API rejects messages longer than 4096 chars. We chunk
# at 3800 to leave headroom for a "(n/m)" prefix plus margin.
WHATSAPP_MAX_BODY_CHARS: Final[int] = 4096
CHUNK_CHARS: Final[int] = 3800


def body_size_bytes(body: str) -> int:
    """UTF-8 byte length of ``body`` — matches WhatsApp's wire cost."""
    return len(body.encode("utf-8"))


def is_oversized(body: str, *, threshold: int = THRESHOLD_BYTES) -> bool:
    """True iff the body exceeds the size threshold in UTF-8 bytes."""
    return body_size_bytes(body) > threshold


def format_warning(size_bytes: int, char_count: int) -> str:
    """Exact Spec §10 warning text, ready to send in place of the body."""
    kb = size_bytes / 1024
    # En-dashes are intentional — the spec dialog uses German typographic
    # dashes between command and gloss. Ruff's ambiguous-char warning
    # doesn't apply to user-facing copy.
    return (
        f"⚠️ Claude will ~{kb:.1f}KB senden ({char_count} chars).\n"
        "/send    – senden\n"  # noqa: RUF001
        "/discard – verwerfen\n"  # noqa: RUF001
        "/save    – nur speichern, nicht senden"  # noqa: RUF001
    )


def chunk_for_whatsapp(body: str, *, chunk_size: int = CHUNK_CHARS) -> list[str]:
    """Split ``body`` into WhatsApp-safe chunks.

    Prefixes each chunk with ``(i/n)`` so the user can follow the
    sequence even if delivery is reordered. A single-chunk body gets
    no prefix — no point cluttering the common case.
    """
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be positive, got {chunk_size}")
    if not body:
        return [""]
    raw = [body[i : i + chunk_size] for i in range(0, len(body), chunk_size)]
    if len(raw) == 1:
        return raw
    return [f"({i + 1}/{len(raw)})\n{part}" for i, part in enumerate(raw)]
