"""Text-sanitisation for inbound WhatsApp text — strips control
characters that would otherwise reach the command router and tmux
layer. Phase 9 C9.3.

Why strip at all:

* ``\\x00`` (NUL) terminates C-strings and is occasionally a
  vector for truncation bugs in downstream libraries (sqlite,
  subprocess, tmux).
* ``\\x1b`` (ESC) is the start of ANSI escape sequences — landing
  in tmux would redraw the pane unpredictably.
* ``\\x07`` (BEL) triggers Terminal.app's bell; ``\\x08`` (BS)
  rewrites history.
* ``\\x7f`` (DEL) behaves unpredictably in different terminals.

What we keep:

* ``\\t`` (horizontal tab), ``\\n`` (newline), ``\\r`` (carriage
  return) — all legitimate in prompts / pasted code.
* Everything >= U+0020 — no Unicode normalisation, no emoji
  filtering, no full-width ASCII conversion. A user sending
  ``αβγ`` or ``🔥`` deserves the bot to forward it as-is.

Pure module — no I/O, safe to import from the webhook hot path.
"""

from __future__ import annotations

# Characters we explicitly keep from the C0 range. Everything else
# in U+0000..U+001F plus U+007F gets stripped.
_ALLOWED_C0: frozenset[int] = frozenset({0x09, 0x0A, 0x0D})  # tab, LF, CR


def sanitize_inbound_text(text: str) -> str:
    """Return ``text`` with unwanted control characters removed.

    Preserves Unicode, emoji, and the three whitespace-class C0
    controls (tab / LF / CR). Idempotent — applying twice produces
    the same result as once.
    """
    if not text:
        return text
    if not _needs_sanitize(text):
        return text
    return "".join(ch for ch in text if _keep(ch))


def _needs_sanitize(text: str) -> bool:
    """Fast-path — most messages are plain text. Only build a new
    string if we actually need to drop something."""
    return any(_drop(ch) for ch in text)


def _drop(ch: str) -> bool:
    cp = ord(ch)
    if cp < 0x20:
        return cp not in _ALLOWED_C0
    return cp == 0x7F


def _keep(ch: str) -> bool:
    return not _drop(ch)
