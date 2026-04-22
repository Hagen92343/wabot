"""Keychain-Adapter — speichert whatsbot-Secrets im macOS Keychain.

Backend: `keyring`-Library, die auf macOS auf `security`/Keychain Services
abbildet (Service-Name siehe `SERVICE_NAME`). Production-Default. In Tests
wird stattdessen das `MockKeyringBackend`-Fixture aus `tests/conftest.py`
verwendet, damit Unit-Tests nichts ins echte Keychain schreiben.

Spec §4 + §5 (Secrets gehören NIEMALS in `.env`-Dateien oder Code).
"""

from __future__ import annotations

import contextlib

import keyring
from keyring.errors import PasswordDeleteError

from whatsbot.ports.secrets_provider import SERVICE_NAME, SecretNotFoundError


class KeychainProvider:
    """`SecretsProvider`-Implementierung gegen macOS Keychain via `keyring`."""

    service: str

    def __init__(self, service: str = SERVICE_NAME) -> None:
        self.service = service

    def get(self, key: str) -> str:
        value = keyring.get_password(self.service, key)
        if value is None:
            raise SecretNotFoundError(
                f"Secret '{self.service}/{key}' fehlt im Keychain. "
                f"Setze es per `make setup-secrets`."
            )
        return value

    def set(self, key: str, value: str) -> None:
        keyring.set_password(self.service, key, value)

    def rotate(self, key: str, new_value: str) -> None:
        # Keychain-`set` überschreibt bereits; das Pre-Delete invalidiert
        # zusätzlich evtl. gecachte Einträge anderer Tools (defensive).
        with contextlib.suppress(PasswordDeleteError):
            keyring.delete_password(self.service, key)
        keyring.set_password(self.service, key, new_value)
