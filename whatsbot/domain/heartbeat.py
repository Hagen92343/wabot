"""Heartbeat domain — pure helpers around the bot↔watchdog protocol.

Spec §4 + §7 + FMEA #4. The bot writes a touch-file at
``/tmp/whatsbot-heartbeat`` every ``HEARTBEAT_INTERVAL_SECONDS``;
the Watchdog LaunchAgent (a separate, language-agnostic process)
reads the file's mtime every 30 s and, if it's older than
``HEARTBEAT_STALE_AFTER_SECONDS``, takes the bot to be dead and
performs the same emergency tear-down that ``/panic`` does.

Only constants + pure helpers live here. The actual file IO lands
in the file-heartbeat-writer adapter; the loop that calls it is the
``HeartbeatPumper`` application service; the watchdog itself is a
shell script under ``bin/`` because it needs to run even if our
Python venv is broken.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

# How often the bot writes the heartbeat file (Spec §7).
HEARTBEAT_INTERVAL_SECONDS: Final[int] = 30

# How stale the heartbeat must be before the watchdog acts (Spec §7).
# 120 s = four missed heartbeats — survives transient pauses from
# garbage collection, slow disk, or a temporarily unresponsive
# webhook handler, but flags real crashes within reasonable wall
# time.
HEARTBEAT_STALE_AFTER_SECONDS: Final[int] = 120


def is_heartbeat_stale(
    mtime: float | None,
    *,
    now: float,
    threshold_seconds: int = HEARTBEAT_STALE_AFTER_SECONDS,
) -> bool:
    """Decide whether the watchdog should treat the heartbeat as
    stale.

    ``mtime`` is what ``os.path.getmtime`` returns (seconds since
    epoch). ``None`` means the file was never written — that's
    treated as stale so a never-started bot doesn't lock the
    watchdog into "wait forever" mode.
    """
    if mtime is None:
        return True
    return (now - mtime) >= threshold_seconds


def format_heartbeat_payload(
    *,
    now: datetime,
    pid: int,
    version: str,
) -> str:
    """Return the human-readable string that gets written to the
    heartbeat file.

    A bare touch would suffice — the watchdog only checks mtime — but
    the file content makes ``cat`` debugging trivial: who am I, what
    version, when was the last heartbeat.
    """
    return (
        f"whatsbot heartbeat\n"
        f"version={version}\n"
        f"pid={pid}\n"
        f"ts={now.isoformat()}\n"
    )
