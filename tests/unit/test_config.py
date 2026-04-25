"""Unit tests for whatsbot.config."""

from __future__ import annotations

import pytest

from whatsbot.adapters.keychain_provider import KeychainProvider
from whatsbot.config import (
    Environment,
    InvalidEnvironmentError,
    SecretsValidationError,
    Settings,
    assert_secrets_present,
)
from whatsbot.ports.secrets_provider import ALL_KEYS, SERVICE_NAME

pytestmark = pytest.mark.unit


# --- Settings.from_env ------------------------------------------------------


def test_from_env_defaults_to_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WHATSBOT_ENV", raising=False)
    monkeypatch.delenv("WHATSBOT_DRY_RUN", raising=False)
    s = Settings.from_env()
    assert s.env is Environment.PROD
    assert s.dry_run is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("dev", Environment.DEV),
        ("DEV", Environment.DEV),
        (" prod ", Environment.PROD),
        ("test", Environment.TEST),
    ],
)
def test_from_env_parses_environment(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: Environment
) -> None:
    monkeypatch.setenv("WHATSBOT_ENV", raw)
    assert Settings.from_env().env is expected


def test_from_env_rejects_invalid_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHATSBOT_ENV", "rocket")
    with pytest.raises(InvalidEnvironmentError, match="rocket"):
        Settings.from_env()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("anything-else", False),
    ],
)
def test_from_env_parses_dry_run_flag(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    monkeypatch.delenv("WHATSBOT_ENV", raising=False)
    monkeypatch.setenv("WHATSBOT_DRY_RUN", raw)
    assert Settings.from_env().dry_run is expected


@pytest.mark.live_paths
def test_default_paths_under_user_home() -> None:
    s = Settings()
    assert "Library/Logs/whatsbot" in str(s.log_dir)
    assert "Library/Application Support/whatsbot" in str(s.db_path)
    assert "Backups/whatsbot" in str(s.backup_dir)


# --- assert_secrets_present -------------------------------------------------


def test_prod_raises_when_any_secret_missing(
    mock_keyring: dict[tuple[str, str], str],
) -> None:
    settings = Settings(env=Environment.PROD)
    with pytest.raises(SecretsValidationError) as exc:
        assert_secrets_present(KeychainProvider(), settings)
    msg = str(exc.value)
    # Error message must point at the operator action.
    assert "make setup-secrets" in msg
    # And mention at least one of the missing keys.
    assert "meta-app-secret" in msg


def test_dev_returns_missing_list_without_raising(
    mock_keyring: dict[tuple[str, str], str],
) -> None:
    settings = Settings(env=Environment.DEV)
    missing = assert_secrets_present(KeychainProvider(), settings)
    assert set(missing) == set(ALL_KEYS)


def test_test_env_returns_missing_list_without_raising(
    mock_keyring: dict[tuple[str, str], str],
) -> None:
    settings = Settings(env=Environment.TEST)
    missing = assert_secrets_present(KeychainProvider(), settings)
    assert set(missing) == set(ALL_KEYS)


def test_prod_passes_when_all_secrets_set(
    mock_keyring: dict[tuple[str, str], str],
) -> None:
    for key in ALL_KEYS:
        mock_keyring[(SERVICE_NAME, key)] = f"value-{key}"
    settings = Settings(env=Environment.PROD)
    assert assert_secrets_present(KeychainProvider(), settings) == []
