"""macOS ``osascript``-backed notifier.

``osascript -e 'display notification "body" with title "title"'`` is
the cheapest way to get a native macOS Notification Center banner
without any extra deps. We never raise: if osascript is missing
(non-macOS dev box) or returns non-zero (TCC not granted), we log a
warning and return — the notifier is decorative, not load-bearing.
"""

from __future__ import annotations

import shutil
import subprocess

from whatsbot.logging_setup import get_logger


def _escape_for_applescript(value: str) -> str:
    """Escape backslashes + double-quotes for an AppleScript string
    literal. Newlines stay as-is; macOS Notification Center happily
    renders multi-line bodies."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


class OsascriptNotifier:
    """``osascript``-shaped notifier.

    Constructed once at app start. The constructor checks for the
    binary and stashes a flag so the per-call check is a no-op.
    """

    def __init__(self, *, osascript_binary: str | None = None) -> None:
        self._binary = osascript_binary or "osascript"
        self._log = get_logger("whatsbot.notifier")
        self._available = shutil.which(self._binary) is not None
        if not self._available:
            self._log.info(
                "notifier_binary_missing_fallback_noop",
                binary=self._binary,
            )

    def send(self, *, title: str, body: str, sound: bool = False) -> None:
        if not self._available:
            return
        title_escaped = _escape_for_applescript(title)
        body_escaped = _escape_for_applescript(body)
        script = (
            f'display notification "{body_escaped}" '
            f'with title "{title_escaped}"'
        )
        if sound:
            script += ' sound name "Submarine"'
        try:
            subprocess.run(  # noqa: S603 — argv list, no shell
                [self._binary, "-e", script],
                capture_output=True,
                check=False,
                timeout=5.0,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._log.warning(
                "notifier_send_failed",
                title=title,
                error=str(exc),
            )
