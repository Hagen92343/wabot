"""AllowService — manage Allow-Rules per project.

Coordinates three storage layers:

* ``allow_rules`` table (source of truth, queried with ``list_rules``)
* per-project ``.claude/settings.json`` (rendered from the table on every
  write so Claude Code sees the current set immediately)
* ``.whatsbot/suggested-rules.json`` (smart-detection output; consumed by
  ``batch_review`` and ``batch_approve``, deleted after approval)

All public methods take a ``project_name`` so callers don't need to
shuttle the project's filesystem path themselves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from whatsbot.application import settings_writer
from whatsbot.domain.allow_rules import (
    AllowRulePattern,
    AllowRuleSource,
    InvalidAllowRuleError,
    parse_pattern,
)
from whatsbot.logging_setup import get_logger
from whatsbot.ports.allow_rule_repository import (
    AllowRuleRepository,
    StoredAllowRule,
)
from whatsbot.ports.project_repository import (
    ProjectNotFoundError,
    ProjectRepository,
)


@dataclass(frozen=True, slots=True)
class SuggestedRule:
    """One entry from ``.whatsbot/suggested-rules.json``."""

    pattern: AllowRulePattern
    reason: str


@dataclass(frozen=True, slots=True)
class BatchApproveOutcome:
    """Summary of ``batch_approve``: how many rules were added vs. already
    present, and whether the suggested-rules.json was deleted."""

    added: list[StoredAllowRule]
    already_present: list[AllowRulePattern]
    suggestions_cleared: bool


class NoSuggestedRulesError(RuntimeError):
    """Raised when ``batch_*`` runs but no suggested-rules.json exists."""


class AllowService:
    def __init__(
        self,
        rule_repo: AllowRuleRepository,
        project_repo: ProjectRepository,
        projects_root: Path,
    ) -> None:
        self._rules = rule_repo
        self._projects = project_repo
        self._projects_root = projects_root
        self._log = get_logger("whatsbot.allow")

    # ---- list ------------------------------------------------------------

    def list_rules(self, project_name: str) -> list[StoredAllowRule]:
        self._require_project(project_name)
        return self._rules.list_for_project(project_name)

    # ---- single add / remove --------------------------------------------

    def add_manual(self, project_name: str, raw_pattern: str) -> StoredAllowRule:
        """Parse a user-typed ``Tool(pattern)`` string and persist it."""
        self._require_project(project_name)
        pattern = parse_pattern(raw_pattern)
        stored = self._rules.add(project_name, pattern, AllowRuleSource.MANUAL)
        self._sync_settings(project_name)
        self._log.info(
            "allow_rule_added",
            project=project_name,
            tool=pattern.tool,
            pattern=pattern.pattern,
            source="manual",
        )
        return stored

    def remove(self, project_name: str, raw_pattern: str) -> bool:
        self._require_project(project_name)
        pattern = parse_pattern(raw_pattern)
        removed = self._rules.remove(project_name, pattern)
        if removed:
            self._sync_settings(project_name)
            self._log.info(
                "allow_rule_removed",
                project=project_name,
                tool=pattern.tool,
                pattern=pattern.pattern,
            )
        return removed

    # ---- batch from suggested-rules.json --------------------------------

    def batch_review(self, project_name: str) -> list[SuggestedRule]:
        """Read but don't apply the suggested-rules.json. Empty list if no
        suggestions remain (or never existed)."""
        self._require_project(project_name)
        return list(self._iter_suggestions(project_name))

    def batch_approve(self, project_name: str) -> BatchApproveOutcome:
        """Persist every entry from suggested-rules.json (idempotent),
        then delete the file so the next /allow batch review is empty.

        Raises ``NoSuggestedRulesError`` if there's nothing to approve."""
        self._require_project(project_name)
        suggestions = list(self._iter_suggestions(project_name))
        if not suggestions:
            raise NoSuggestedRulesError(f"Keine Vorschlaege fuer '{project_name}' vorhanden.")

        # Snapshot existing patterns to classify added vs. already-present.
        before = {
            (r.pattern.tool, r.pattern.pattern) for r in self._rules.list_for_project(project_name)
        }

        added: list[StoredAllowRule] = []
        already_present: list[AllowRulePattern] = []
        for entry in suggestions:
            key = (entry.pattern.tool, entry.pattern.pattern)
            stored = self._rules.add(project_name, entry.pattern, AllowRuleSource.SMART_DETECTION)
            if key in before:
                already_present.append(entry.pattern)
            else:
                added.append(stored)

        # Delete the suggestions file so the next /allow batch review is
        # empty and the user can't accidentally re-apply.
        path = self._suggestions_path(project_name)
        suggestions_cleared = False
        try:
            path.unlink()
            suggestions_cleared = True
        except FileNotFoundError:
            pass

        # Sync to settings.json once at the end.
        self._sync_settings(project_name)

        self._log.info(
            "allow_rules_batch_approved",
            project=project_name,
            added=len(added),
            already_present=len(already_present),
        )
        return BatchApproveOutcome(
            added=added,
            already_present=already_present,
            suggestions_cleared=suggestions_cleared,
        )

    # ---- helpers --------------------------------------------------------

    def _require_project(self, project_name: str) -> None:
        if not self._projects.exists(project_name):
            raise ProjectNotFoundError(f"Projekt '{project_name}' nicht gefunden.")

    def _suggestions_path(self, project_name: str) -> Path:
        return self._projects_root / project_name / ".whatsbot" / "suggested-rules.json"

    def _iter_suggestions(self, project_name: str) -> list[SuggestedRule]:
        path = self._suggestions_path(project_name)
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        raw_rules = payload.get("suggested_rules", []) if isinstance(payload, dict) else []
        results: list[SuggestedRule] = []
        for entry in raw_rules:
            if not isinstance(entry, dict):
                continue
            tool = entry.get("tool")
            pattern = entry.get("pattern")
            reason = entry.get("reason", "")
            if not isinstance(tool, str) or not isinstance(pattern, str):
                continue
            try:
                # Run through the parser/validator so an invalid entry
                # gets dropped rather than written through.
                parsed = parse_pattern(f"{tool}({pattern})")
            except InvalidAllowRuleError:
                continue
            results.append(
                SuggestedRule(
                    pattern=parsed,
                    reason=str(reason),
                )
            )
        return results

    def _sync_settings(self, project_name: str) -> None:
        rules = self._rules.list_for_project(project_name)
        patterns = [r.pattern for r in rules]
        project_dir = self._projects_root / project_name
        settings_writer.write_allow_rules(project_dir, patterns)
