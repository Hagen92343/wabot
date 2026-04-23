"""UnlockService — PIN-gated lockdown release (Spec §11 ``/unlock``).

Mirrors ``ForceService``'s pattern: the actual lockdown bookkeeping
lives in ``LockdownService``; this service just adds the PIN gate
on top so the command handler doesn't have to duplicate the check.

Re-uses ``InvalidPinError`` and ``PanicPinNotConfiguredError`` from
``DeleteService`` — same Keychain entry, same semantics.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from whatsbot.application.delete_service import (
    InvalidPinError,
    PanicPinNotConfiguredError,
)
from whatsbot.application.lockdown_service import LockdownService
from whatsbot.domain.lockdown import LockdownState
from whatsbot.logging_setup import get_logger
from whatsbot.ports.secrets_provider import (
    KEY_PANIC_PIN,
    SecretNotFoundError,
    SecretsProvider,
)


@dataclass(frozen=True, slots=True)
class UnlockOutcome:
    """Result of a successful PIN-validated unlock."""

    previous_state: LockdownState
    new_state: LockdownState
    was_engaged: bool


class UnlockService:
    """High-level operation backing the ``/unlock`` command."""

    def __init__(
        self,
        *,
        lockdown_service: LockdownService,
        secrets: SecretsProvider,
    ) -> None:
        self._lockdown = lockdown_service
        self._secrets = secrets
        self._log = get_logger("whatsbot.unlock")

    def unlock(self, pin: str) -> UnlockOutcome:
        """Verify PIN, then disengage lockdown.

        Even if lockdown wasn't engaged, the PIN check still runs —
        cheap, and it means a stray ``/unlock`` from a stolen handset
        doesn't reveal "lockdown wasn't engaged anyway, no PIN
        needed" via timing.
        """
        self._verify_pin(pin)
        previous = self._lockdown.current()
        new_state = self._lockdown.disengage()
        self._log.info(
            "unlock_succeeded",
            was_engaged=previous.engaged,
            previous_reason=previous.reason,
        )
        return UnlockOutcome(
            previous_state=previous,
            new_state=new_state,
            was_engaged=previous.engaged,
        )

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
