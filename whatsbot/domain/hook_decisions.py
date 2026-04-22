"""Hook decision model — pure, no I/O.

The Pre-Tool-Hook (Spec §7) has exactly three possible outcomes for any
call it intercepts:

* ``Allow`` — run the tool without further gatekeeping.
* ``Deny``  — refuse with a user-visible reason; Claude receives Exit 2
              with the reason on stderr.
* ``AskUser`` — open a ``pending_confirmations`` row and block the hook
                up to 5 minutes until the user answers on WhatsApp.

This module is the *domain* side of that decision: how we model the
three outcomes. The ``AskUser`` branch only returns a carrier — the
service layer is responsible for opening the confirmation, sending the
WhatsApp prompt, and polling until resolution.

C3.1 only plumbs ``Allow``/``Deny``. ``AskUser`` gets real wiring in
C3.2 (deny-patterns) and C3.3 (PIN confirmation flow).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


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
