"""Unit tests for the pure helpers in whatsbot.http.meta_webhook."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from whatsbot.http.meta_webhook import (
    SIGNATURE_PREFIX,
    check_subscribe_challenge,
    iter_text_messages,
    verify_signature,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


# --- verify_signature ------------------------------------------------------


def test_verify_signature_accepts_correct_hmac() -> None:
    body = b'{"hello":"world"}'
    secret = "super-secret"
    assert verify_signature(body, _sign(body, secret), secret) is True


def test_verify_signature_rejects_wrong_secret() -> None:
    body = b'{"hello":"world"}'
    sig = _sign(body, "right")
    assert verify_signature(body, sig, "wrong") is False


def test_verify_signature_rejects_tampered_body() -> None:
    sig = _sign(b'{"hello":"world"}', "secret")
    assert verify_signature(b'{"hello":"WORLD"}', sig, "secret") is False


def test_verify_signature_rejects_missing_header() -> None:
    assert verify_signature(b"x", None, "secret") is False


def test_verify_signature_rejects_unprefixed_signature() -> None:
    """Header must start with 'sha256='. Bare hex is rejected."""
    digest = hmac.new(b"secret", b"x", hashlib.sha256).hexdigest()
    assert verify_signature(b"x", digest, "secret") is False


def test_verify_signature_rejects_garbage() -> None:
    assert verify_signature(b"x", "sha256=not-hex-just-text", "secret") is False
    assert verify_signature(b"x", "sha256=", "secret") is False


# --- check_subscribe_challenge ---------------------------------------------


def test_subscribe_challenge_returns_challenge_on_match() -> None:
    result = check_subscribe_challenge(
        mode="subscribe",
        token="my-token",
        challenge="42",
        expected_token="my-token",
    )
    assert result == "42"


def test_subscribe_challenge_rejects_wrong_token() -> None:
    assert (
        check_subscribe_challenge(
            mode="subscribe",
            token="wrong",
            challenge="42",
            expected_token="my-token",
        )
        is None
    )


def test_subscribe_challenge_rejects_wrong_mode() -> None:
    assert (
        check_subscribe_challenge(
            mode="unsubscribe",
            token="my-token",
            challenge="42",
            expected_token="my-token",
        )
        is None
    )


def test_subscribe_challenge_rejects_missing_params() -> None:
    assert (
        check_subscribe_challenge(mode=None, token="t", challenge="42", expected_token="t") is None
    )
    assert (
        check_subscribe_challenge(mode="subscribe", token=None, challenge="42", expected_token="t")
        is None
    )
    assert (
        check_subscribe_challenge(mode="subscribe", token="t", challenge=None, expected_token="t")
        is None
    )


# --- iter_text_messages ----------------------------------------------------


def _load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / f"{name}.json").read_text())  # type: ignore[no-any-return]


def test_iter_extracts_single_text_from_ping_fixture() -> None:
    msgs = list(iter_text_messages(_load("meta_ping")))
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.sender == "+491701234567"
    assert msg.text == "/ping"
    assert msg.msg_id == "wamid.PING_FIXTURE_001"


def test_iter_extracts_status_fixture() -> None:
    msgs = list(iter_text_messages(_load("meta_status")))
    assert msgs[0].text == "/status"


def test_iter_skips_non_text_messages() -> None:
    """Image fixture has type=image — must produce zero text messages."""
    assert list(iter_text_messages(_load("meta_non_text"))) == []


def test_iter_handles_empty_payload() -> None:
    assert list(iter_text_messages({})) == []


def test_iter_handles_malformed_payload_gracefully() -> None:
    # entry is not a list
    assert list(iter_text_messages({"entry": "not-a-list"})) == []
    # changes missing
    assert list(iter_text_messages({"entry": [{}]})) == []
    # value not a dict
    assert list(iter_text_messages({"entry": [{"changes": [{"value": 42}]}]})) == []


def test_iter_skips_messages_with_missing_fields() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"type": "text", "text": {}, "from": "+49"},
                                {"type": "text", "text": {"body": "ok"}},  # no from
                                {"type": "text", "text": {"body": "ok"}, "from": "+49"},
                            ]
                        }
                    }
                ]
            }
        ]
    }
    msgs = list(iter_text_messages(payload))
    assert len(msgs) == 1
    assert msgs[0].text == "ok"
