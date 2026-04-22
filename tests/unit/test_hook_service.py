"""Unit tests for whatsbot.application.hook_service (C3.1 scope)."""

from __future__ import annotations

import pytest

from whatsbot.application.hook_service import HookService
from whatsbot.domain.hook_decisions import Verdict

pytestmark = pytest.mark.unit


def test_bash_allow_by_default_in_c31() -> None:
    svc = HookService()
    d = svc.classify_bash(command="ls", project="alpha", session_id="s1")
    assert d.verdict is Verdict.ALLOW


def test_bash_with_no_project_still_allowed() -> None:
    svc = HookService()
    d = svc.classify_bash(command="pwd", project=None, session_id=None)
    assert d.verdict is Verdict.ALLOW


def test_write_allow_by_default_in_c31() -> None:
    svc = HookService()
    d = svc.classify_write(
        path="/Users/me/projekte/alpha/README.md", project="alpha", session_id="s1"
    )
    assert d.verdict is Verdict.ALLOW


def test_extreme_command_preview_does_not_leak_into_exceptions(caplog: pytest.LogCaptureFixture) -> None:
    """Even a huge command must not cause the service to raise — the
    log-preview helper clips it instead."""
    svc = HookService()
    huge = "echo " + "x" * 5000
    d = svc.classify_bash(command=huge, project="alpha", session_id="s1")
    assert d.verdict is Verdict.ALLOW
