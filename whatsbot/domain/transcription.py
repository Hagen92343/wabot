"""Transcript cleanup — pure, no I/O.

Phase-7 C7.4: whisper.cpp annotates its output with bracketed markers
for non-speech audio (``[BLANK_AUDIO]``, ``[Music]``, ``[Laughter]``)
and occasionally emits a header line or two. None of that is useful as
a Claude prompt — we strip it here before the transcript flows into
:meth:`whatsbot.application.session_service.SessionService.send_prompt`.

We also cap the cleaned transcript at 4000 characters so a runaway
voice note can't blow out the prompt buffer. A trailing ``…`` marks the
truncation so users see that something was cut.
"""

from __future__ import annotations

import re
from typing import Final

MAX_TRANSCRIPT_CHARS: Final[int] = 4000
"""Upper bound on the cleaned transcript length (Spec §16).

A 60-second German voice note comes out around 400-600 characters;
4000 gives us room for long monologues without sinking the context
window on a single turn.
"""

# Whisper.cpp / whisper-cli annotations. Treat them case-insensitively
# and match the whole bracketed expression so an accidental colon or
# trailing period doesn't escape the strip.
_BRACKET_ANNOTATION: Final = re.compile(
    r"\[(?:BLANK_AUDIO|Music|Laughter|Applause|Silence|MUSIC|LAUGHTER|APPLAUSE|inaudible|INAUDIBLE|background noise|sound effects?)\]",
    re.IGNORECASE,
)

# Some whisper.cpp builds prefix a timestamp like ``[00:00:00.000 --> 00:00:03.120]``
# to each line even with ``-nt``. The ``-nt`` flag kills them in newer builds,
# but we strip defensively so older or custom-built binaries don't leak these
# into prompts.
_TIMESTAMP_LINE: Final = re.compile(
    r"^\s*\[\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?\s*-->\s*\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?\]\s*"
)

# Collapse runs of blank lines (more than one consecutive newline) to
# a single newline. Regular in-line whitespace is left alone so German
# punctuation + compound words don't get mangled.
_MULTI_NEWLINE: Final = re.compile(r"\n\s*\n+")


def clean_transcript(raw: str) -> str:
    """Return a prompt-ready string from a raw whisper-cli output.

    Pipeline:

    1. Drop bracket annotations (``[BLANK_AUDIO]`` etc.) anywhere in
       the text, case-insensitively.
    2. Strip leading timestamp prefixes per line (``[00:00:01.000 -->
       00:00:04.500]`` and variants) — a defensive strip for older
       whisper-cli builds that ignore ``-nt``.
    3. Trim each line, collapse multi-line whitespace into a single
       newline, then trim the whole string.
    4. Truncate at :data:`MAX_TRANSCRIPT_CHARS` with a trailing ``…``
       so the user (and Claude) can see something was cut.
    """
    if not isinstance(raw, str):
        return ""
    text = _BRACKET_ANNOTATION.sub("", raw)
    lines = [
        _TIMESTAMP_LINE.sub("", line).strip()
        for line in text.splitlines()
    ]
    # Drop completely empty lines so the collapse below produces the
    # tightest output.
    joined = "\n".join(line for line in lines if line)
    joined = _MULTI_NEWLINE.sub("\n", joined).strip()
    if len(joined) > MAX_TRANSCRIPT_CHARS:
        # Leave room for the ellipsis character. We use a single
        # ``…`` (Unicode HORIZONTAL ELLIPSIS) rather than three dots
        # so downstream text-processing can cheaply detect the cut.
        return joined[: MAX_TRANSCRIPT_CHARS - 1] + "…"
    return joined
