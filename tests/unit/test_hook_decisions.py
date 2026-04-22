"""Unit tests for whatsbot.domain.hook_decisions (pure logic)."""

from __future__ import annotations

import pytest

from whatsbot.domain.hook_decisions import (
    HookDecision,
    Verdict,
    allow,
    ask_user,
    deny,
)

pytestmark = pytest.mark.unit


def test_verdict_string_values_match_claude_contract() -> None:
    # Spec §7: Claude's hook protocol accepts "allow" and "deny"
    # verbatim. ask_user is internal but sharing the same enum keeps
    # the serialisation obvious.
    assert Verdict.ALLOW.value == "allow"
    assert Verdict.DENY.value == "deny"
    assert Verdict.ASK_USER.value == "ask_user"


def test_allow_defaults_reason_empty() -> None:
    d = allow()
    assert d.verdict is Verdict.ALLOW
    assert d.reason == ""
    assert d.is_terminal() is True


def test_allow_with_reason_preserves_reason() -> None:
    d = allow("allowlist hit: Bash(npm test)")
    assert d.reason == "allowlist hit: Bash(npm test)"


def test_deny_requires_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        deny("")


def test_deny_is_terminal() -> None:
    d = deny("blacklist: rm -rf /")
    assert d.verdict is Verdict.DENY
    assert d.is_terminal() is True


def test_ask_user_requires_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        ask_user("")


def test_ask_user_is_not_terminal() -> None:
    d = ask_user("unusual bash command")
    assert d.verdict is Verdict.ASK_USER
    assert d.is_terminal() is False


def test_hookdecision_is_frozen() -> None:
    d = allow()
    with pytest.raises(Exception):  # FrozenInstanceError is a subclass
        d.reason = "tampered"  # type: ignore[misc]


def test_hookdecision_equality_by_value() -> None:
    assert allow("x") == HookDecision(verdict=Verdict.ALLOW, reason="x")
    assert deny("rm") != deny("sudo")
