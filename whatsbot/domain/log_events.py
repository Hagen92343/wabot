"""Structured log-entry domain — pure helpers for reading the
JSONL sinks in ``settings.log_dir``.

Spec §15 records every bot event as a single-line JSON object in
``app.jsonl``. The :class:`LogEntry` dataclass mirrors the slice
we care about for the Phase-8 diagnostics commands (``/log``,
``/errors``). The full payload stays reachable via
:attr:`LogEntry.raw`.

Pure module. The I/O layer that actually tails the file lives in
:mod:`whatsbot.adapters.file_log_reader`; domain-core just owns
the shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

# Levels we expose as "errors" through ``/errors``. Warnings are
# surfaced because the bot logs circuit-opens, fail-closed denies,
# and similar operational red-flags at WARNING (Spec §15 audit log).
ERROR_LEVELS: Final[frozenset[str]] = frozenset({"error", "warning", "critical"})


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One parsed line out of an ``app.jsonl`` sink.

    All fields default to empty strings / ``None`` so partially-written
    events (shutdown race, crashed formatter) still yield an entry
    with enough metadata to surface through ``/errors`` instead of
    being silently dropped.
    """

    ts: str = ""
    level: str = ""
    logger: str = ""
    event: str = ""
    msg_id: str | None = None
    project: str | None = None
    mode: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.level.lower() in ERROR_LEVELS


def parse_log_line(line: str) -> LogEntry | None:
    """Parse a single JSONL line into a :class:`LogEntry`.

    Returns ``None`` for empty / blank lines and for lines that
    don't decode as a JSON object — we never raise on bad input.
    A crashing log-tail would be worse than a missing entry.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None

    return LogEntry(
        ts=_as_str(payload.get("timestamp") or payload.get("ts")),
        level=_as_str(payload.get("level")),
        logger=_as_str(payload.get("logger")),
        event=_as_str(payload.get("event")),
        msg_id=_as_optional_str(payload.get("msg_id")),
        project=_as_optional_str(payload.get("project")),
        mode=_as_optional_str(payload.get("mode")),
        raw=payload,
    )


def filter_by_msg_id(entries: list[LogEntry], msg_id: str) -> list[LogEntry]:
    """Return entries whose ``msg_id`` matches exactly. Pure."""
    target = msg_id.strip()
    if not target:
        return []
    return [e for e in entries if e.msg_id == target]


def filter_errors(entries: list[LogEntry]) -> list[LogEntry]:
    """Return only error-level entries (warning + error + critical).
    Pure."""
    return [e for e in entries if e.is_error]


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
