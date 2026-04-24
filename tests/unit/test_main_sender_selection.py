"""C10.3 — _build_outbound_sender fact-based selection tests.

Covers the 6 cases from phase-10.md §5:
1. override wins
2. TEST env → logging
3. DEV env → logging
4. PROD + full secrets → cloud
5. PROD + missing access_token → logging + WARN
6. PROD + missing phone_number_id → logging + WARN
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from whatsbot.adapters.whatsapp_sender import (
    LoggingMessageSender,
    WhatsAppCloudSender,
)
from whatsbot.config import Environment, Settings
from whatsbot.main import _build_outbound_sender
from whatsbot.ports.message_sender import MessageSender
from whatsbot.ports.secrets_provider import (
    KEY_META_ACCESS_TOKEN,
    KEY_META_PHONE_NUMBER_ID,
    SecretNotFoundError,
)


class _StubSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, key: str) -> str:
        if key not in self._values:
            raise SecretNotFoundError(key)
        return self._values[key]

    def set(self, key: str, value: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def rotate(self, key: str, new_value: str) -> None:  # pragma: no cover
        raise NotImplementedError


class _RecordingLog:
    """Stand-in for structlog logger — captures warning calls."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kwargs: Any) -> None:
        self.warnings.append((event, kwargs))


def _prod_settings(tmp_path: Path) -> Settings:
    return Settings(
        env=Environment.PROD,
        dry_run=False,
        db_path=tmp_path / "state.db",
        log_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backups",
    )


def _test_settings(tmp_path: Path) -> Settings:
    return Settings(
        env=Environment.TEST,
        dry_run=False,
        db_path=tmp_path / "state.db",
        log_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backups",
    )


def _dev_settings(tmp_path: Path) -> Settings:
    return Settings(
        env=Environment.DEV,
        dry_run=False,
        db_path=tmp_path / "state.db",
        log_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backups",
    )


class _FakeSender:
    """Injectable sender for the override-path test."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, *, to: str, body: str) -> None:
        self.sent.append((to, body))


# ---------------------------------------------------------------------
# #1 override takes precedence over everything
# ---------------------------------------------------------------------


def test_override_takes_precedence_in_prod(tmp_path: Path) -> None:
    override: MessageSender = _FakeSender()
    secrets = _StubSecrets(
        {
            KEY_META_ACCESS_TOKEN: "tok",
            KEY_META_PHONE_NUMBER_ID: "PNID",
        }
    )
    log = _RecordingLog()

    result = _build_outbound_sender(
        settings=_prod_settings(tmp_path),
        secrets=secrets,
        override=override,
        log=log,
    )
    assert result is override
    assert log.warnings == []


def test_override_takes_precedence_in_test(tmp_path: Path) -> None:
    override: MessageSender = _FakeSender()
    result = _build_outbound_sender(
        settings=_test_settings(tmp_path),
        secrets=None,
        override=override,
        log=_RecordingLog(),
    )
    assert result is override


# ---------------------------------------------------------------------
# #2 TEST env always uses logging sender
# ---------------------------------------------------------------------


def test_test_env_uses_logging_sender(tmp_path: Path) -> None:
    result = _build_outbound_sender(
        settings=_test_settings(tmp_path),
        secrets=None,
        override=None,
        log=_RecordingLog(),
    )
    assert isinstance(result, LoggingMessageSender)


# ---------------------------------------------------------------------
# #3 DEV env always uses logging sender (even with full secrets)
# ---------------------------------------------------------------------


def test_dev_env_uses_logging_sender_even_with_full_secrets(tmp_path: Path) -> None:
    secrets = _StubSecrets(
        {
            KEY_META_ACCESS_TOKEN: "tok",
            KEY_META_PHONE_NUMBER_ID: "PNID",
        }
    )
    log = _RecordingLog()
    result = _build_outbound_sender(
        settings=_dev_settings(tmp_path),
        secrets=secrets,
        override=None,
        log=log,
    )
    assert isinstance(result, LoggingMessageSender)
    # DEV is not a warn-case — no noise in the logs.
    assert log.warnings == []


# ---------------------------------------------------------------------
# #4 PROD + full secrets → WhatsAppCloudSender
# ---------------------------------------------------------------------


def test_prod_env_with_full_secrets_returns_cloud_sender(tmp_path: Path) -> None:
    secrets = _StubSecrets(
        {
            KEY_META_ACCESS_TOKEN: "tok-abc",
            KEY_META_PHONE_NUMBER_ID: "PNID-42",
        }
    )
    log = _RecordingLog()
    result = _build_outbound_sender(
        settings=_prod_settings(tmp_path),
        secrets=secrets,
        override=None,
        log=log,
    )
    assert isinstance(result, WhatsAppCloudSender)
    assert log.warnings == []


# ---------------------------------------------------------------------
# #5 PROD + missing access_token → logging + WARN
# ---------------------------------------------------------------------


def test_prod_env_missing_access_token_falls_back(tmp_path: Path) -> None:
    secrets = _StubSecrets({KEY_META_PHONE_NUMBER_ID: "PNID"})
    log = _RecordingLog()
    result = _build_outbound_sender(
        settings=_prod_settings(tmp_path),
        secrets=secrets,
        override=None,
        log=log,
    )
    assert isinstance(result, LoggingMessageSender)
    assert any(
        event == "meta_credentials_missing_falling_back_to_logging_sender"
        for event, _ in log.warnings
    )


def test_prod_env_empty_access_token_falls_back(tmp_path: Path) -> None:
    secrets = _StubSecrets(
        {
            KEY_META_ACCESS_TOKEN: "",
            KEY_META_PHONE_NUMBER_ID: "PNID",
        }
    )
    log = _RecordingLog()
    result = _build_outbound_sender(
        settings=_prod_settings(tmp_path),
        secrets=secrets,
        override=None,
        log=log,
    )
    assert isinstance(result, LoggingMessageSender)


# ---------------------------------------------------------------------
# #6 PROD + missing phone_number_id → logging + WARN
# ---------------------------------------------------------------------


def test_prod_env_missing_phone_number_id_falls_back(tmp_path: Path) -> None:
    secrets = _StubSecrets({KEY_META_ACCESS_TOKEN: "tok"})
    log = _RecordingLog()
    result = _build_outbound_sender(
        settings=_prod_settings(tmp_path),
        secrets=secrets,
        override=None,
        log=log,
    )
    assert isinstance(result, LoggingMessageSender)
    assert any(
        event == "meta_credentials_missing_falling_back_to_logging_sender"
        for event, _ in log.warnings
    )


# ---------------------------------------------------------------------
# Extra guard — PROD with completely None secrets (shouldn't happen
# in practice but we harden the path).
# ---------------------------------------------------------------------


def test_prod_env_with_no_secrets_provider_falls_back(tmp_path: Path) -> None:
    log = _RecordingLog()
    result = _build_outbound_sender(
        settings=_prod_settings(tmp_path),
        secrets=None,
        override=None,
        log=log,
    )
    assert isinstance(result, LoggingMessageSender)
    assert any(
        event == "meta_credentials_missing_no_secrets_provider"
        for event, _ in log.warnings
    )


@pytest.mark.parametrize(
    "env",
    [Environment.TEST, Environment.DEV],
)
def test_non_prod_envs_never_attempt_cloud_sender(
    tmp_path: Path, env: Environment
) -> None:
    """Even if the Keychain is populated in a non-prod env, we want
    the logging sender — tests + dev machines should not accidentally
    hit Meta."""
    settings = Settings(
        env=env,
        dry_run=False,
        db_path=tmp_path / "state.db",
        log_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backups",
    )
    secrets = _StubSecrets(
        {
            KEY_META_ACCESS_TOKEN: "tok",
            KEY_META_PHONE_NUMBER_ID: "PNID",
        }
    )
    result = _build_outbound_sender(
        settings=settings,
        secrets=secrets,
        override=None,
        log=_RecordingLog(),
    )
    assert isinstance(result, LoggingMessageSender)
