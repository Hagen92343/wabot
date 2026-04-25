"""Shared pytest fixtures for whatsbot tests.

Two principles:
1. Tests must NEVER touch the user's real macOS Keychain. The ``mock_keyring``
   fixture monkeypatches ``keyring.{get,set,delete}_password`` with a per-test
   in-memory dict so the production adapter under test runs unchanged.
2. Tests must NEVER touch the real state-DB or backup dir. The autouse
   ``_redirect_default_paths`` fixture redirects
   ``whatsbot.config._DEFAULT_DB_PATH`` and
   ``whatsbot.config._DEFAULT_BACKUP_DIR`` to ``tmp_path`` for every test,
   so any code path that falls back to the defaults — including
   ``Settings(env=PROD)`` without a ``db_path`` override — lands in
   ephemeral test storage. Add ``@pytest.mark.live_paths`` to opt out
   (only for tests that explicitly assert on the production defaults).

   Mini-Phase 12 documented this as the ``Settings.db_path``-default
   leak: ``create_app(Settings(env=PROD))`` in integration tests would
   silently open ``~/Library/Application Support/whatsbot/state.db``,
   triggering live migrations and (worse) wiring the transcript watcher
   onto the running production session. The autouse fixture closes that
   hole at the source.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from keyring.errors import PasswordDeleteError


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live_paths: opt out of the per-test redirection of "
        "_DEFAULT_DB_PATH/_DEFAULT_BACKUP_DIR (very rarely needed — "
        "only for tests that explicitly assert on the production paths).",
    )


@pytest.fixture(autouse=True)
def _redirect_default_paths(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point ``whatsbot.config._DEFAULT_DB_PATH`` and ``_DEFAULT_BACKUP_DIR``
    at ``tmp_path`` for the duration of this test.

    ``Settings.db_path`` is declared with ``Field(default_factory=lambda:
    _DEFAULT_DB_PATH)`` — the lambda resolves the module attribute at
    instantiation time, so monkeypatching the attribute before the
    Settings() call is enough to redirect the default.
    """
    if request.node.get_closest_marker("live_paths") is not None:
        return
    safe_db = tmp_path / "state.db"
    safe_backup = tmp_path / "backups"
    monkeypatch.setattr("whatsbot.config._DEFAULT_DB_PATH", safe_db)
    monkeypatch.setattr("whatsbot.config._DEFAULT_BACKUP_DIR", safe_backup)


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Iterator[None]:
    """Reset structlog + stdlib logging between tests.

    ``configure_logging`` caches loggers and binds contextvars, so a noisy
    test could leak state into the next one. This fixture runs after every
    test (autouse) to restore a clean slate.
    """
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


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
