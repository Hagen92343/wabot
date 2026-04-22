"""HookService — orchestrates the Pre-Tool-Hook decision flow.

C3.1 scope: *infrastructure*. The service classifies every Bash/Write/
Edit call as ``Allow`` by default so the hook round-trip can be
verified end-to-end before any real policy lands. The full pipeline
(deny-patterns, allow-rules, path-rules, PIN confirmation) gets layered
on in C3.2–C3.3 without changing this module's signature — callers
already receive ``HookDecision``.

Keeping the service intentionally boring here is deliberate: Phase 3's
very first checkpoint is "the wire works end-to-end", and mixing that
with the 17-pattern blacklist would make failures hard to localise.
"""

from __future__ import annotations

from whatsbot.domain.hook_decisions import HookDecision, allow
from whatsbot.logging_setup import get_logger


class HookService:
    """Classifies hook invocations. Stateless in C3.1."""

    def __init__(self) -> None:
        self._log = get_logger("whatsbot.hook")

    def classify_bash(
        self,
        *,
        command: str,
        project: str | None,
        session_id: str | None,
    ) -> HookDecision:
        """Decide what to do with a Bash invocation.

        C3.1 always returns ``allow`` — the decision matrix lands in
        C3.2. The logging contract is already in place so when C3.2
        hooks in the deny-patterns, we only extend, not rewrite.
        """
        self._log.info(
            "hook_bash_classified",
            command_preview=_preview(command),
            project=project,
            session_id=session_id,
            verdict="allow",
        )
        return allow("c3.1 stub: allow-by-default")

    def classify_write(
        self,
        *,
        path: str,
        project: str | None,
        session_id: str | None,
    ) -> HookDecision:
        """Decide what to do with a Write/Edit invocation.

        Same deal as ``classify_bash`` — allow-by-default for now,
        path-rules land in C3.2.
        """
        self._log.info(
            "hook_write_classified",
            path=path,
            project=project,
            session_id=session_id,
            verdict="allow",
        )
        return allow("c3.1 stub: allow-by-default")


def _preview(value: str, *, max_len: int = 200) -> str:
    """Return a shortened representation for logs. Never logs more than
    ``max_len`` chars so a huge command line can't blow the log budget."""
    if len(value) <= max_len:
        return value
    return value[:max_len] + f"…[+{len(value) - max_len} chars]"
