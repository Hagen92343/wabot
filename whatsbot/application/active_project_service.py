"""ActiveProjectService — wraps the ``app_state.active_project`` row.

Lightweight by design: just two operations (get / set) plus an
existence-check on set so we never park the active pointer on a project
that doesn't exist anymore.
"""

from __future__ import annotations

from whatsbot.domain.projects import validate_project_name
from whatsbot.ports.app_state_repository import (
    KEY_ACTIVE_PROJECT,
    AppStateRepository,
)
from whatsbot.ports.project_repository import (
    ProjectNotFoundError,
    ProjectRepository,
)


class ActiveProjectService:
    def __init__(
        self,
        app_state: AppStateRepository,
        projects: ProjectRepository,
    ) -> None:
        self._app_state = app_state
        self._projects = projects

    def get_active(self) -> str | None:
        """Returns the currently-active project name, or ``None`` if none
        is set OR the stored value points at a project that's been
        deleted in the meantime (we self-heal by clearing it)."""
        name = self._app_state.get(KEY_ACTIVE_PROJECT)
        if name is None:
            return None
        if not self._projects.exists(name):
            self._app_state.delete(KEY_ACTIVE_PROJECT)
            return None
        return name

    def set_active(self, raw_name: str) -> str:
        """Validate + check existence + persist. Returns the canonical name."""
        name = validate_project_name(raw_name)
        if not self._projects.exists(name):
            raise ProjectNotFoundError(f"Projekt '{name}' nicht gefunden.")
        self._app_state.set(KEY_ACTIVE_PROJECT, name)
        return name
