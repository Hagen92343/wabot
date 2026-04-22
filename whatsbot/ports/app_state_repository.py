"""AppStateRepository port — small key/value store for global bot state.

Backed by the ``app_state`` table from Spec §19 with reserved keys:
``active_project``, ``lockdown``, ``version``, ``last_heartbeat``.
"""

from __future__ import annotations

from typing import Final, Protocol

KEY_ACTIVE_PROJECT: Final = "active_project"
KEY_LOCKDOWN: Final = "lockdown"
KEY_VERSION: Final = "version"
KEY_LAST_HEARTBEAT: Final = "last_heartbeat"


class AppStateRepository(Protocol):
    def get(self, key: str) -> str | None:
        """Return the stored value or ``None`` if the key is unset."""

    def set(self, key: str, value: str) -> None:
        """Upsert the value."""

    def delete(self, key: str) -> bool:
        """Remove the key. Returns ``True`` if a row was deleted."""
