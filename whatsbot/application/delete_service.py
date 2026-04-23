"""DeleteService — ``/rm`` use-cases: request, confirm (with PIN), cleanup.

Two-step destruction per Spec §11:

1. ``request_delete(name)`` — validate the project exists, write a row to
   ``pending_deletes`` with a 60-second deadline, return the ``PendingDelete``
   so the command handler can render "bestaetige mit /rm <name> <PIN>".
2. ``confirm_delete(name, pin)`` — inside the window + correct PIN → move
   the project tree to ``~/.Trash/whatsbot-<name>-<timestamp>`` and delete
   the project row. ``ON DELETE CASCADE`` cleans up ``claude_sessions``,
   ``session_locks`` and ``allow_rules`` automatically (Spec §19).

The PIN itself lives in the macOS Keychain (Spec §4, key ``panic-pin``).
We compare with ``hmac.compare_digest`` so the check runs in
constant-time regardless of where the wrong PIN diverges from the right
one.
"""

from __future__ import annotations

import contextlib
import hmac
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from whatsbot.domain.pending_deletes import PendingDelete, compute_deadline
from whatsbot.domain.projects import validate_project_name
from whatsbot.logging_setup import get_logger
from whatsbot.ports.app_state_repository import (
    KEY_ACTIVE_PROJECT,
    AppStateRepository,
)
from whatsbot.ports.pending_delete_repository import PendingDeleteRepository
from whatsbot.ports.project_repository import (
    ProjectNotFoundError,
    ProjectRepository,
)
from whatsbot.ports.secrets_provider import (
    KEY_PANIC_PIN,
    SecretNotFoundError,
    SecretsProvider,
)


def _DEFAULT_CLOCK() -> int:
    return int(time.time())


class NoPendingDeleteError(RuntimeError):
    """Raised when ``confirm_delete`` is called without a prior request."""


class PendingDeleteExpiredError(RuntimeError):
    """Raised when the 60-second confirm window has elapsed."""


class InvalidPinError(RuntimeError):
    """Raised when the supplied PIN doesn't match the Keychain value."""


class PanicPinNotConfiguredError(RuntimeError):
    """Raised when the Keychain has no ``panic-pin`` entry. Setup bug, not
    user error — surfaces as a clear error message instead of silently
    letting any PIN through."""


@dataclass(frozen=True, slots=True)
class DeleteOutcome:
    """Result of a successful ``confirm_delete``."""

    project_name: str
    trashed_to: Path


class DeleteService:
    """High-level operations backing the ``/rm`` command."""

    def __init__(
        self,
        *,
        pending_repo: PendingDeleteRepository,
        project_repo: ProjectRepository,
        app_state: AppStateRepository,
        secrets: SecretsProvider,
        projects_root: Path,
        trash_root: Path | None = None,
        clock: Callable[[], int] = _DEFAULT_CLOCK,
    ) -> None:
        self._pending = pending_repo
        self._projects = project_repo
        self._app_state = app_state
        self._secrets = secrets
        self._projects_root = projects_root
        self._trash_root = trash_root if trash_root is not None else Path.home() / ".Trash"
        self._clock = clock
        self._log = get_logger("whatsbot.delete")

    # ---- step 1: request --------------------------------------------------

    def request_delete(self, raw_name: str) -> PendingDelete:
        """Open a 60-second confirm window for ``raw_name``.

        A second request on the same project before expiry simply resets
        the deadline — the DB UPSERT is intentional.
        """
        name = validate_project_name(raw_name)
        if not self._projects.exists(name):
            raise ProjectNotFoundError(f"Projekt '{name}' nicht gefunden.")

        pending = PendingDelete(
            project_name=name,
            deadline_ts=compute_deadline(self._clock()),
        )
        self._pending.upsert(pending)
        self._log.info(
            "delete_requested",
            project=name,
            deadline_ts=pending.deadline_ts,
        )
        return pending

    # ---- step 2: confirm --------------------------------------------------

    def confirm_delete(self, raw_name: str, pin: str) -> DeleteOutcome:
        """Move the project to Trash if the window is still open and the
        PIN matches. Raises otherwise."""
        name = validate_project_name(raw_name)
        pending = self._pending.get(name)
        if pending is None:
            raise NoPendingDeleteError(
                f"Kein offener /rm-Request fuer '{name}'. "
                f"Tippe zuerst /rm {name}."
            )

        now = self._clock()
        if pending.is_expired(now):
            # Stale row: clean it up so the next /rm starts fresh.
            self._pending.delete(name)
            raise PendingDeleteExpiredError(
                f"Bestaetigungs-Fenster fuer '{name}' abgelaufen. "
                f"Tippe /rm {name} erneut."
            )

        self._verify_pin(pin)

        trashed_to = self._move_to_trash(name)
        # CASCADE in the schema wipes allow_rules / claude_sessions /
        # session_locks automatically when the projects row goes.
        self._projects.delete(name)
        self._pending.delete(name)
        if self._app_state.get(KEY_ACTIVE_PROJECT) == name:
            self._app_state.delete(KEY_ACTIVE_PROJECT)

        self._log.info(
            "delete_confirmed",
            project=name,
            trashed_to=str(trashed_to),
        )
        return DeleteOutcome(project_name=name, trashed_to=trashed_to)

    # ---- sweeper ----------------------------------------------------------

    def cleanup_expired(self) -> list[str]:
        """Remove every pending row whose deadline has passed. Returns the
        names that were evicted so the caller can log them."""
        evicted = self._pending.delete_expired(self._clock())
        if evicted:
            self._log.info("delete_pending_expired", projects=evicted)
        return evicted

    # ---- internals --------------------------------------------------------

    def _verify_pin(self, supplied: str) -> None:
        try:
            expected = self._secrets.get(KEY_PANIC_PIN)
        except SecretNotFoundError as exc:
            raise PanicPinNotConfiguredError(
                "Panic-PIN ist im Keychain nicht gesetzt. "
                "Tippe am Mac `make setup-secrets`."
            ) from exc
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            raise InvalidPinError("Falsche PIN.")

    def _move_to_trash(self, name: str) -> Path:
        source = self._projects_root / name
        self._trash_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
        dest = self._trash_root / f"whatsbot-{name}-{stamp}"
        # Extremely unlikely collision (same second), but handle it
        # deterministically so we never silently overwrite.
        counter = 1
        while dest.exists():
            dest = self._trash_root / f"whatsbot-{name}-{stamp}-{counter}"
            counter += 1

        if source.exists():
            shutil.move(str(source), str(dest))
            return dest
        # Project row exists but the directory is already gone — still a
        # valid delete (the row is the source of truth). Leave a marker dir
        # so the user sees that we acted; suppressing OSError keeps this
        # best-effort.
        with contextlib.suppress(OSError):
            dest.mkdir(parents=True, exist_ok=False)
        return dest
