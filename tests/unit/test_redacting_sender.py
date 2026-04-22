"""Unit tests for whatsbot.adapters.redacting_sender."""

from __future__ import annotations

import pytest

from whatsbot.adapters.redacting_sender import RedactingMessageSender

pytestmark = pytest.mark.unit


class _Capture:
    """Records the last send_text call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def send_text(self, *, to: str, body: str) -> None:
        self.calls.append((to, body))


def test_clean_body_passes_through_verbatim() -> None:
    inner = _Capture()
    sender = RedactingMessageSender(inner)
    sender.send_text(to="+49", body="hello world")
    assert inner.calls == [("+49", "hello world")]


def test_aws_key_is_scrubbed_before_reaching_inner() -> None:
    inner = _Capture()
    sender = RedactingMessageSender(inner)
    sender.send_text(to="+49", body="deploy key: AKIAIOSFODNN7EXAMPLE")
    assert len(inner.calls) == 1
    to, body = inner.calls[0]
    assert to == "+49"
    assert "AKIAIOSFODNN7EXAMPLE" not in body
    assert "<REDACTED:aws-key>" in body


def test_env_password_is_scrubbed() -> None:
    inner = _Capture()
    sender = RedactingMessageSender(inner)
    sender.send_text(to="+49", body="password=hunter2")
    (_, body), = inner.calls
    assert "hunter2" not in body
    assert "<REDACTED:env:password>" in body


def test_multiple_sends_are_independent() -> None:
    inner = _Capture()
    sender = RedactingMessageSender(inner)
    sender.send_text(to="+49", body="first msg, no secrets")
    sender.send_text(to="+50", body="ghp_" + "a" * 36)
    sender.send_text(to="+51", body="third msg")
    assert inner.calls[0] == ("+49", "first msg, no secrets")
    assert inner.calls[1][0] == "+50"
    assert "<REDACTED:gh-token>" in inner.calls[1][1]
    assert inner.calls[2] == ("+51", "third msg")


def test_empty_body_roundtrips_untouched() -> None:
    inner = _Capture()
    sender = RedactingMessageSender(inner)
    sender.send_text(to="+49", body="")
    assert inner.calls == [("+49", "")]
