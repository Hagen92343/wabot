"""Secrets-Provider-Port — Keychain-/Vault-Abstraktion.

Weder Domain noch Application greifen direkt auf `keyring`, `os.environ` oder
Konfigurationsdateien zu. Stattdessen sprechen sie diesen Port an. Konkrete
Backends (macOS Keychain in Production, In-Memory-Mock in Tests) leben in
`whatsbot/adapters/`.

Spec §4 verlangt exakt sieben Pflicht-Secrets. Diese sind hier als Konstanten
deklariert, damit Magic-Strings im Restcode vermieden werden.
"""

from __future__ import annotations

from typing import Final, Protocol

SERVICE_NAME: Final = "whatsbot"

# --- Spec §4: die 7 Keychain-Einträge ---------------------------------------
KEY_META_APP_SECRET: Final = "meta-app-secret"
KEY_META_VERIFY_TOKEN: Final = "meta-verify-token"
KEY_META_ACCESS_TOKEN: Final = "meta-access-token"
KEY_META_PHONE_NUMBER_ID: Final = "meta-phone-number-id"
KEY_ALLOWED_SENDERS: Final = "allowed-senders"
KEY_PANIC_PIN: Final = "panic-pin"
KEY_HOOK_SHARED_SECRET: Final = "hook-shared-secret"

ALL_KEYS: Final[tuple[str, ...]] = (
    KEY_META_APP_SECRET,
    KEY_META_VERIFY_TOKEN,
    KEY_META_ACCESS_TOKEN,
    KEY_META_PHONE_NUMBER_ID,
    KEY_ALLOWED_SENDERS,
    KEY_PANIC_PIN,
    KEY_HOOK_SHARED_SECRET,
)


class SecretNotFoundError(KeyError):
    """Raised when a required secret is missing from the backend."""


class SecretsProvider(Protocol):
    """Lese-/Schreib-/Rotations-Schnittstelle für Secrets."""

    def get(self, key: str) -> str: ...

    def set(self, key: str, value: str) -> None: ...

    def rotate(self, key: str, new_value: str) -> None: ...


def verify_all_present(provider: SecretsProvider) -> list[str]:
    """Liste fehlender Pflicht-Secrets. Leer == alles OK.

    Wird beim App-Startup geprüft (Spec §4: harter Abbruch bei Fehlen).
    """
    missing: list[str] = []
    for key in ALL_KEYS:
        try:
            provider.get(key)
        except SecretNotFoundError:
            missing.append(key)
    return missing
