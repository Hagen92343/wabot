"""ProjectRepository port — persistence abstraction for the projects table.

Domain + Application talk to projects through this port. The concrete
implementation in ``adapters/sqlite_project_repository.py`` writes to the
SQLite ``projects`` table from Spec §19; tests can substitute an in-memory
fake by wrapping the same SQLite adapter against ``:memory:``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from whatsbot.domain.projects import Mode, Project


class ProjectAlreadyExistsError(ValueError):
    """Raised when ``create`` is called with a name that already exists."""


class ProjectNotFoundError(KeyError):
    """Raised when ``get`` is called with a name that doesn't exist."""


class ProjectRepository(Protocol):
    """CRUD over the ``projects`` table. Methods are intentionally narrow —
    no batch ops in Phase 2 since we expect a handful of projects per user."""

    def create(self, project: Project) -> None:
        """Persist a new project. Raises ``ProjectAlreadyExistsError`` on
        a duplicate name."""

    def get(self, name: str) -> Project:
        """Return the project or raise ``ProjectNotFoundError``."""

    def list_all(self) -> list[Project]:
        """Return all projects, sorted by ``name``. Empty list if none."""

    def delete(self, name: str) -> None:
        """Remove the project row. Raises ``ProjectNotFoundError`` if absent."""

    def exists(self, name: str) -> bool: ...

    def exists_with_path(self, path: Path) -> bool:
        """Return True if some project row already points at ``path``.

        Used by ``/import`` to reject double-registration of the same
        directory under different names.
        """

    def update_mode(self, name: str, mode: Mode) -> None:
        """Change only the ``mode`` column. Used by ``/mode`` switches
        (C4.3) and by the startup YOLO → Normal coercion (C4.7).
        Raises ``ProjectNotFoundError`` if the row is missing."""
