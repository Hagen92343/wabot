"""GitClone port — abstraction over `git clone` for testability.

In production an adapter shells out to the real ``git`` binary; tests
substitute an in-memory implementation that just creates the destination
directory with a fixture layout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class GitCloneError(RuntimeError):
    """Raised on any clone failure (timeout, non-zero exit, missing git)."""


class GitClone(Protocol):
    def clone(
        self,
        url: str,
        dest: Path,
        *,
        depth: int = 50,
        timeout_seconds: float = 180.0,
    ) -> None:
        """Clone ``url`` into ``dest``. Raises ``GitCloneError`` on failure.

        Implementations MUST NOT pre-create ``dest`` — git rejects an
        existing non-empty target. On failure the caller cleans up.
        """
