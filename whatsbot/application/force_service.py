"""ForceService — PIN-gated lock override (Spec §11 ``/force``).

Wraps ``LockService.force_bot`` with a Keychain PIN check identical to
``DeleteService``'s. The ``/force`` command exists so that the bot can
override a stale ``local`` lock when the user knows the local terminal
is no longer using the project (e.g. they walked away and forgot
``/release``).

PIN-error semantics are shared with ``DeleteService`` — both
PIN-gated commands key off the same ``panic-pin`` Keychain entry
(Spec §5), so we re-use ``InvalidPinError`` and
``PanicPinNotConfiguredError`` instead of cloning them.

The send-prompt step lives in the command handler — this service is
only responsible for the PIN check and the lock takeover. That keeps
the unit tests narrow and lets the e2e tests prove both halves
without coupling them.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from whatsbot.application.delete_service import (
    InvalidPinError,
    PanicPinNotConfiguredError,
)
from whatsbot.application.lock_service import LockService
from whatsbot.domain.locks import SessionLock
from whatsbot.domain.projects import validate_project_name
from whatsbot.logging_setup import get_logger
from whatsbot.ports.project_repository import (
    ProjectNotFoundError,
    ProjectRepository,
)
from whatsbot.ports.secrets_provider import (
    KEY_PANIC_PIN,
    SecretNotFoundError,
    SecretsProvider,
)


@dataclass(frozen=True, slots=True)
class ForceOutcome:
    """Result of a successful PIN-validated force override."""

    project_name: str
    lock: SessionLock


class ForceService:
    """High-level operation backing the ``/force`` command."""

    def __init__(
        self,
        *,
        lock_service: LockService,
        project_repo: ProjectRepository,
        secrets: SecretsProvider,
    ) -> None:
        self._locks = lock_service
        self._projects = project_repo
        self._secrets = secrets
        self._log = get_logger("whatsbot.force")

    def force(self, raw_name: str, pin: str) -> ForceOutcome:
        """Verify PIN, then unconditionally take the bot lock.

        Validates the project name and existence first so we don't
        create a session_locks row that would fail the FK to a
        missing project — keeps error messages clean instead of
        leaking sqlite IntegrityError.
        """
        name = validate_project_name(raw_name)
        if not self._projects.exists(name):
            raise ProjectNotFoundError(f"Projekt '{name}' nicht gefunden.")
        self._verify_pin(pin)
        lock = self._locks.force_bot(name)
        self._log.info("force_acquired", project=name)
        return ForceOutcome(project_name=name, lock=lock)

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
