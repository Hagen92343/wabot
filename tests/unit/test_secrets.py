"""Unit tests for SecretsProvider port + Keychain adapter."""

from __future__ import annotations

import pytest

from whatsbot.adapters.keychain_provider import KeychainProvider
from whatsbot.ports.secrets_provider import (
    ALL_KEYS,
    KEY_ALLOWED_SENDERS,
    KEY_HOOK_SHARED_SECRET,
    KEY_META_ACCESS_TOKEN,
    KEY_META_APP_SECRET,
    KEY_META_PHONE_NUMBER_ID,
    KEY_META_VERIFY_TOKEN,
    KEY_PANIC_PIN,
    SERVICE_NAME,
    SecretNotFoundError,
    verify_all_present,
)

pytestmark = pytest.mark.unit


# --- ALL_KEYS sanity --------------------------------------------------------


def test_all_keys_matches_spec_section_4() -> None:
    """Spec §4 lists exactly seven keychain entries; the constant must mirror them."""
    expected = {
        KEY_META_APP_SECRET,
        KEY_META_VERIFY_TOKEN,
        KEY_META_ACCESS_TOKEN,
        KEY_META_PHONE_NUMBER_ID,
        KEY_ALLOWED_SENDERS,
        KEY_PANIC_PIN,
        KEY_HOOK_SHARED_SECRET,
    }
    assert set(ALL_KEYS) == expected
    assert len(ALL_KEYS) == 7  # no duplicates


def test_service_name_is_whatsbot() -> None:
    assert SERVICE_NAME == "whatsbot"


# --- KeychainProvider behaviour --------------------------------------------


def test_get_returns_stored_value(mock_keyring: dict[tuple[str, str], str]) -> None:
    mock_keyring[(SERVICE_NAME, KEY_META_APP_SECRET)] = "abc123"
    provider = KeychainProvider()
    assert provider.get(KEY_META_APP_SECRET) == "abc123"


def test_get_missing_raises_secret_not_found_error() -> None:
    # mock_keyring fixture missing on purpose; this test runs against the live
    # adapter but with a key that obviously doesn't exist. Use the mock to
    # guarantee no real Keychain access.
    pass  # superseded by the explicit fixture-based test below


def test_get_missing_raises(mock_keyring: dict[tuple[str, str], str]) -> None:
    provider = KeychainProvider()
    with pytest.raises(SecretNotFoundError) as exc:
        provider.get("nonexistent-key")
    assert "nonexistent-key" in str(exc.value)
    assert SERVICE_NAME in str(exc.value)


def test_set_then_get_roundtrip(mock_keyring: dict[tuple[str, str], str]) -> None:
    provider = KeychainProvider()
    provider.set(KEY_PANIC_PIN, "9182")
    assert provider.get(KEY_PANIC_PIN) == "9182"
    assert mock_keyring[(SERVICE_NAME, KEY_PANIC_PIN)] == "9182"


def test_set_overwrites_existing(mock_keyring: dict[tuple[str, str], str]) -> None:
    mock_keyring[(SERVICE_NAME, KEY_PANIC_PIN)] = "old"
    provider = KeychainProvider()
    provider.set(KEY_PANIC_PIN, "new")
    assert provider.get(KEY_PANIC_PIN) == "new"


def test_rotate_replaces_value_and_clears_first(
    mock_keyring: dict[tuple[str, str], str],
) -> None:
    mock_keyring[(SERVICE_NAME, KEY_HOOK_SHARED_SECRET)] = "old-secret"
    provider = KeychainProvider()
    provider.rotate(KEY_HOOK_SHARED_SECRET, "new-secret")
    assert provider.get(KEY_HOOK_SHARED_SECRET) == "new-secret"


def test_rotate_works_when_key_did_not_exist(
    mock_keyring: dict[tuple[str, str], str],
) -> None:
    """Rotate must still set a value even if no prior entry existed."""
    provider = KeychainProvider()
    provider.rotate(KEY_HOOK_SHARED_SECRET, "fresh")
    assert provider.get(KEY_HOOK_SHARED_SECRET) == "fresh"


def test_custom_service_name_isolates_storage(
    mock_keyring: dict[tuple[str, str], str],
) -> None:
    """Two providers with different services must not see each other's secrets."""
    a = KeychainProvider(service="whatsbot")
    b = KeychainProvider(service="whatsbot-test")
    a.set(KEY_PANIC_PIN, "AAA")
    b.set(KEY_PANIC_PIN, "BBB")
    assert a.get(KEY_PANIC_PIN) == "AAA"
    assert b.get(KEY_PANIC_PIN) == "BBB"


# --- verify_all_present -----------------------------------------------------


def test_verify_all_present_returns_missing_keys_when_empty(
    mock_keyring: dict[tuple[str, str], str],
) -> None:
    provider = KeychainProvider()
    missing = verify_all_present(provider)
    assert set(missing) == set(ALL_KEYS)


def test_verify_all_present_empty_when_all_set(
    mock_keyring: dict[tuple[str, str], str],
) -> None:
    provider = KeychainProvider()
    for key in ALL_KEYS:
        provider.set(key, f"value-for-{key}")
    assert verify_all_present(provider) == []


def test_verify_all_present_lists_only_missing(
    mock_keyring: dict[tuple[str, str], str],
) -> None:
    provider = KeychainProvider()
    provider.set(KEY_META_APP_SECRET, "x")
    provider.set(KEY_PANIC_PIN, "y")
    missing = verify_all_present(provider)
    assert KEY_META_APP_SECRET not in missing
    assert KEY_PANIC_PIN not in missing
    assert KEY_HOOK_SHARED_SECRET in missing
    assert len(missing) == len(ALL_KEYS) - 2
