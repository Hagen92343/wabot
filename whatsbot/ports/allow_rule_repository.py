"""AllowRuleRepository port — persistence for per-project allow rules.

Phase 2.4 backs this with the SQLite ``allow_rules`` table from Spec §19.
The table schema also feeds the per-project ``.claude/settings.json``
(via ``application.settings_writer``), so we keep both stores in sync at
the application layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from whatsbot.domain.allow_rules import AllowRulePattern, AllowRuleSource


@dataclass(frozen=True, slots=True)
class StoredAllowRule:
    """Persisted allow rule with bookkeeping fields."""

    id: int
    project_name: str
    pattern: AllowRulePattern
    source: AllowRuleSource
    created_at: datetime


class AllowRuleRepository(Protocol):
    """Protocol — see ``adapters.sqlite_allow_rule_repository`` for the
    real implementation. Tests can swap in an in-memory dict-backed fake."""

    def add(
        self,
        project_name: str,
        pattern: AllowRulePattern,
        source: AllowRuleSource,
    ) -> StoredAllowRule:
        """Persist the rule. If an identical (project + tool + pattern)
        already exists, return that one unchanged (idempotent)."""

    def remove(self, project_name: str, pattern: AllowRulePattern) -> bool:
        """Delete by tool+pattern. Returns ``True`` if a row was removed."""

    def list_for_project(self, project_name: str) -> list[StoredAllowRule]:
        """All rules for a project, sorted by ``id`` (insertion order)."""
