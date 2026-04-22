"""Hook decision model + pure evaluators — no I/O.

The Pre-Tool-Hook (Spec §7) has exactly three possible outcomes for any
call it intercepts:

* ``Allow`` — run the tool without further gatekeeping.
* ``Deny``  — refuse with a user-visible reason; Claude receives Exit 2
              with the reason on stderr.
* ``AskUser`` — open a ``pending_confirmations`` row and block the hook
                up to 5 minutes until the user answers on WhatsApp.

This module hosts both the outcome dataclasses *and* the pure evaluator
functions (``evaluate_bash``) that turn a command + current mode +
allow-list into one of those outcomes. The service layer stays
responsible for the ``AskUser`` round-trip (opening the confirmation,
sending the WhatsApp prompt, polling until resolution).

Spec §12 decision matrix, recap:

============  ==================  ===================  ======================
  mode         deny-pattern hit    allow-rule hit       neither
============  ==================  ===================  ======================
  normal       Deny (all modes)    Allow                AskUser
  strict       Deny                Allow                Deny (silent)
  yolo         Deny                Allow (irrelevant)   Allow
============  ==================  ===================  ======================

Even YOLO keeps the deny layer — that's the fail-closed guarantee for
the 17 irreversible patterns.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from whatsbot.domain.deny_patterns import match_bash_command, normalize_command
from whatsbot.domain.projects import Mode


class Verdict(StrEnum):
    """Shared decision outcome — matches Claude Code's hook contract.

    Spec §7 says Claude's hook protocol accepts ``permissionDecision``
    values ``allow`` and ``deny``. ``ask_user`` is our *internal*
    intermediate state — once the user answers, we collapse it into
    either ``allow`` or ``deny``.
    """

    ALLOW = "allow"
    DENY = "deny"
    ASK_USER = "ask_user"


@dataclass(frozen=True, slots=True)
class HookDecision:
    """The full outcome of a hook evaluation.

    ``reason`` flows through to Claude's stderr on deny and into the
    WhatsApp prompt on ask_user, so keep it short, user-readable, and
    free of raw tokens/paths (redaction happens one layer up).
    """

    verdict: Verdict
    reason: str = ""

    def is_terminal(self) -> bool:
        """True iff the decision is final (not ``ASK_USER``)."""
        return self.verdict is not Verdict.ASK_USER


def allow(reason: str = "") -> HookDecision:
    return HookDecision(verdict=Verdict.ALLOW, reason=reason)


def deny(reason: str) -> HookDecision:
    # Empty reason on a deny is almost always a bug — callers should
    # say *why*, because that's what the user sees on stderr.
    if not reason:
        raise ValueError("deny() requires a reason string")
    return HookDecision(verdict=Verdict.DENY, reason=reason)


def ask_user(reason: str) -> HookDecision:
    if not reason:
        raise ValueError("ask_user() requires a reason string")
    return HookDecision(verdict=Verdict.ASK_USER, reason=reason)


# --------------------------------------------------------------------
# evaluate_bash — the pure decision function
# --------------------------------------------------------------------


def _allow_match(command: str, allow_patterns: Sequence[str]) -> str | None:
    """Return the first allow-pattern that matches ``command``, or None.

    Allow-patterns stored in the DB are the *inner* pattern strings
    extracted from ``Bash(<pattern>)`` rules — e.g. ``npm test``,
    ``git status``, ``docker compose logs *``. Matching uses
    ``fnmatch.fnmatchcase`` against the normalised command so we get the
    same whitespace/quote robustness as the deny layer.
    """
    if not allow_patterns:
        return None
    normalized = normalize_command(command)
    for pattern in allow_patterns:
        if fnmatch.fnmatchcase(normalized, pattern):
            return pattern
    return None


def evaluate_bash(
    command: str,
    *,
    mode: Mode,
    allow_patterns: Sequence[str] = (),
) -> HookDecision:
    """Pure decision for a Bash invocation (Spec §12 matrix).

    Order of checks matters:

    1. Deny-pattern (the 17 from Spec §12) — hits **every** mode,
       including YOLO. This is the fail-closed guarantee.
    2. Allow-pattern — an explicit whitelist entry takes precedence
       over mode defaults.
    3. Mode fall-through:
         * Normal → AskUser (confirm on WhatsApp)
         * Strict → Deny (silent — user has to switch mode to proceed)
         * YOLO   → Allow (that's the whole point of YOLO)

    ``allow_patterns`` is a sequence of pre-extracted ``Bash(...)`` inner
    patterns — filter on the tool side before calling this function.
    """
    match = match_bash_command(command)
    if match is not None:
        return deny(
            f"deny pattern '{match.pattern.pattern}' — {match.pattern.reason}"
        )

    allowed = _allow_match(command, allow_patterns)
    if allowed is not None:
        return allow(f"matched allow rule 'Bash({allowed})'")

    if mode is Mode.NORMAL:
        return ask_user("command is not in allow-list — confirm with PIN")
    if mode is Mode.STRICT:
        return deny("strict mode: command not in allow-list")
    # YOLO: user explicitly accepted the risk.
    return allow("yolo mode")
