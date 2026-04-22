"""Shared pytest fixtures for whatsbot tests.

Two principles:
1. Tests must NEVER touch the user's real macOS Keychain. The ``mock_keyring``
   fixture monkeypatches ``keyring.{get,set,delete}_password`` with a per-test
   in-memory dict so the production adapter under test runs unchanged.
2. Tests must NEVER touch the real state-DB. Use the ``tmp_db_path`` fixture
   for an isolated DB file under ``tmp_path``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from keyring.errors import PasswordDeleteError


@pytest.fixture
def mock_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[tuple[str, str], str]]:
    """Replace keyring's storage with a dict bound to this test only."""
    store: dict[tuple[str, str], str] = {}

    def fake_get(service: str, key: str) -> str | None:
        return store.get((service, key))

    def fake_set(service: str, key: str, value: str) -> None:
        store[(service, key)] = value

    def fake_delete(service: str, key: str) -> None:
        if (service, key) not in store:
            raise PasswordDeleteError(f"not in store: {service}/{key}")
        del store[(service, key)]

    # Patch via the module symbol the adapter actually imports — the keyring
    # functions are looked up on the imported module, not on the keyring pkg.
    monkeypatch.setattr("whatsbot.adapters.keychain_provider.keyring.get_password", fake_get)
    monkeypatch.setattr("whatsbot.adapters.keychain_provider.keyring.set_password", fake_set)
    monkeypatch.setattr("whatsbot.adapters.keychain_provider.keyring.delete_password", fake_delete)
    yield store


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Path to a per-test SQLite file (does not exist until first connect)."""
    return tmp_path / "state.db"


@pytest.fixture
def tmp_backup_dir(tmp_path: Path) -> Path:
    """Per-test backup dir (does not exist until first write)."""
    return tmp_path / "backups"
