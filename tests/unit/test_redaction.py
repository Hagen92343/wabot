"""Unit tests for whatsbot.domain.redaction — pure 4-stage pipeline."""

from __future__ import annotations

import pytest

from whatsbot.domain.redaction import Redaction, RedactionResult, redact

pytestmark = pytest.mark.unit


def _labels(result: RedactionResult) -> list[str]:
    return sorted({h.label for h in result.hits})


# --------------------------------------------------------------------
# Stage 1 — known keys (covers ≥6 types; satisfies phase-3.md ≥10 across stages)
# --------------------------------------------------------------------


class TestKnownKeyPatterns:
    def test_aws_access_key(self) -> None:
        r = redact("deploy uses AKIAIOSFODNN7EXAMPLE as key")
        assert "AKIAIOSFODNN7EXAMPLE" not in r.text
        assert "<REDACTED:aws-key>" in r.text
        assert "aws-key" in _labels(r)

    def test_github_personal_access_token(self) -> None:
        tok = "ghp_" + "a" * 36
        r = redact(f"export GITHUB_TOKEN={tok}")
        assert tok not in r.text
        # The env-KEY=VALUE pattern fires second; gh-token fires first
        # and already masks the raw token.
        assert "<REDACTED:gh-token>" in r.text

    def test_github_server_token(self) -> None:
        tok = "ghs_" + "b" * 36
        r = redact(f"server token: {tok}")
        assert "gh-token" in _labels(r)

    def test_github_fine_grained_pat(self) -> None:
        tok = "github_pat_" + "X" * 82
        r = redact(f"PAT = {tok}")
        assert "gh-token" in _labels(r)

    def test_openai_api_key(self) -> None:
        tok = "sk-" + "A" * 40 + "1"
        r = redact(f"OPENAI_API_KEY={tok}")
        assert tok not in r.text
        assert "openai-key" in _labels(r)

    def test_openai_project_key(self) -> None:
        tok = "sk-proj-" + "Z" * 40 + "1"
        r = redact(f"{tok}")
        assert "openai-key" in _labels(r)

    def test_stripe_live_secret(self) -> None:
        tok = "sk_live_" + "c" * 24
        r = redact(f"STRIPE_KEY={tok}")
        assert "stripe-key" in _labels(r)

    def test_stripe_restricted_key(self) -> None:
        tok = "rk_live_" + "d" * 24
        r = redact(f"stripe-rk: {tok}")
        assert "stripe-key" in _labels(r)

    def test_jwt(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0IiwibmFtZSI6IkpvaG4ifQ.signature"
        r = redact(f"Authorization cookie: {jwt}")
        assert jwt not in r.text
        assert "jwt" in _labels(r)

    def test_bearer_token(self) -> None:
        tok = "abc123_" + "d" * 30
        r = redact(f"Authorization: Bearer {tok}")
        assert tok not in r.text
        assert "bearer" in _labels(r)


# --------------------------------------------------------------------
# Stage 2 — structural patterns
# --------------------------------------------------------------------


class TestStructuralPatterns:
    def test_pem_private_key_block(self) -> None:
        text = (
            "BEFORE\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAvJhGJ5q...\n"
            "KeyMaterialLineTwo/MoreBase64==\n"
            "-----END RSA PRIVATE KEY-----\n"
            "AFTER"
        )
        r = redact(text)
        assert "MIIEowIBAA" not in r.text
        assert "<REDACTED:pem>" in r.text
        assert "BEFORE" in r.text and "AFTER" in r.text

    def test_pem_openssh_private_key(self) -> None:
        text = (
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAA...\n"
            "-----END OPENSSH PRIVATE KEY-----"
        )
        r = redact(text)
        assert "b3BlbnNzaC1r" not in r.text
        assert "pem" in _labels(r)

    def test_ssh_rsa_public_key(self) -> None:
        key = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7vbq" + "a" * 40 + " hagen@mac"
        r = redact(key)
        # The algorithm marker + payload collapses; the comment (hagen@mac)
        # after a space isn't matched (the regex only captures up to the
        # first whitespace run).
        assert "AAAAB3NzaC1" not in r.text
        assert "<REDACTED:ssh-pubkey>" in r.text

    def test_db_url_with_credentials(self) -> None:
        r = redact("DATABASE_URL=postgres://admin:s3cr3t@db:5432/foo")
        assert "s3cr3t" not in r.text
        assert "admin:" not in r.text  # user part disappears with the scheme
        assert "db-creds" in _labels(r)

    def test_mongodb_url_with_credentials(self) -> None:
        r = redact("mongodb://user:p%40ss@db.example:27017/admin")
        assert "p%40ss" not in r.text
        assert "db-creds" in _labels(r)

    def test_env_value_password(self) -> None:
        r = redact("password=hunter2")
        assert "hunter2" not in r.text
        assert "env:password" in _labels(r)

    def test_env_value_stops_at_comma(self) -> None:
        # JSON-ish input: value must end at the comma, not swallow it.
        r = redact('"password": "hunter2", "keep": "ok"')
        assert "hunter2" not in r.text
        assert '"keep": "ok"' in r.text

    def test_env_value_stops_at_brace(self) -> None:
        r = redact("{token=abc123def456}")
        assert "abc123def456" not in r.text
        assert r.text.endswith("}")

    def test_env_value_case_insensitive_key(self) -> None:
        r = redact("API_KEY = myapikeyvalue")
        assert "myapikeyvalue" not in r.text
        assert "env:api_key" in _labels(r)

    def test_env_value_colon_separator(self) -> None:
        r = redact("secret: topsecretvalue")
        assert "topsecretvalue" not in r.text
        assert "env:secret" in _labels(r)

    def test_env_value_does_not_match_password_hash(self) -> None:
        # `password_hash` ends with a word char, so the \b after
        # `password` does NOT match. The key as a whole is treated as
        # "something_else" and we leave it alone.
        r = redact("password_hash = bcrypt:$2a$12$...something...")
        # No env:password redaction — the phrase isn't a secret carrier.
        assert all(not h.label.startswith("env:password") for h in r.hits)


# --------------------------------------------------------------------
# Stage 3 — entropy
# --------------------------------------------------------------------


class TestEntropyStage:
    def test_high_entropy_token_is_redacted(self) -> None:
        # Use a neutral preamble so the stage-2 env matcher doesn't
        # fire on words like "token". 40+ chars, entropy > 4.5, digits.
        blob = "Ab1Yx9Pq2Bn4Kv7Wc6Ez3Tm8Hl5Sr0Jf1Od2Gi3Ua4Nh5"
        r = redact(f"value {blob} yay")
        assert "<REDACTED:high-entropy>" in r.text

    def test_low_entropy_hex_hash_is_preserved(self) -> None:
        # 40-char hex SHA — alphabet of 16, entropy around 4.0, below
        # threshold → passes through unredacted.
        sha = "a" * 20 + "b" * 20
        r = redact(f"commit {sha} landed")
        assert sha in r.text
        assert "high-entropy" not in _labels(r)

    def test_url_is_not_treated_as_high_entropy(self) -> None:
        url = "https://example.com/api/v1/users/12345678/sessions/abcdef123456789"
        r = redact(f"hit {url}")
        assert url in r.text

    def test_camelcase_identifier_is_not_redacted(self) -> None:
        # 44 chars, letters only, no digits → filtered out by the
        # digit-required rule.
        ident = "getUsersWithActiveSessionsAndRecentPurchases"
        r = redact(ident)
        assert ident in r.text

    def test_normal_prose_is_untouched(self) -> None:
        prose = (
            "This is a normal sentence with regular English words. "
            "Nothing secret here, just prose that happens to be long."
        )
        r = redact(prose)
        assert r.text == prose
        assert r.hits == ()

    def test_short_token_below_40_chars_passes_through(self) -> None:
        r = redact("short_random_token_123")  # 22 chars
        assert r.text == "short_random_token_123"


# --------------------------------------------------------------------
# Stage 4 — sensitive-path line content
# --------------------------------------------------------------------


class TestSensitivePathStage:
    def test_bare_path_mention_keeps_readable(self) -> None:
        # Short content on a sensitive-path line stays readable —
        # we only scrub long tokens.
        r = redact("contents of ~/.ssh/id_rsa follows below")
        assert "~/.ssh/id_rsa" in r.text

    def test_long_token_near_sensitive_path_is_redacted(self) -> None:
        # 24 chars, no digits, < 40 chars — stage 3 skips (no digits +
        # length threshold), but stage 4 fires because the line mentions
        # a sensitive path and the token is ≥20 chars.
        r = redact("~/.ssh/id_rsa: leftoverFileBodyContent")
        assert "leftoverFileBodyContent" not in r.text
        assert "path-content" in _labels(r)

    def test_path_line_preserves_the_path_itself(self) -> None:
        r = redact("~/.aws/credentials: ThisValueShouldBeGoneFromTheOutput_xyz")
        assert "~/.aws/credentials" in r.text

    def test_path_on_one_line_does_not_redact_other_lines(self) -> None:
        text = (
            "cat ~/.ssh/id_rsa\n"
            "This other line has a long identifier HelloIAmALongIdentifier42XX but no path\n"
        )
        r = redact(text)
        assert "HelloIAmALongIdentifier42XX" in r.text


# --------------------------------------------------------------------
# Pipeline integration + RedactionResult semantics
# --------------------------------------------------------------------


class TestPipelineIntegration:
    def test_mixed_secrets_all_get_redacted(self) -> None:
        text = (
            "AWS: AKIAIOSFODNN7EXAMPLE\n"
            "GH: ghp_" + "1" * 36 + "\n"
            "DB: postgres://admin:s3cr3t@db:5432/foo\n"
            "password=hunter2\n"
            "-----BEGIN RSA PRIVATE KEY-----\nblah==\n-----END RSA PRIVATE KEY-----\n"
        )
        r = redact(text)
        labels = _labels(r)
        assert "aws-key" in labels
        assert "gh-token" in labels
        assert "db-creds" in labels
        assert "env:password" in labels
        assert "pem" in labels

    def test_redaction_result_is_truthy_when_hits_exist(self) -> None:
        assert bool(redact("AKIAIOSFODNN7EXAMPLE")) is True

    def test_redaction_result_is_falsy_on_clean_input(self) -> None:
        assert bool(redact("hello world")) is False

    def test_empty_input_produces_no_hits(self) -> None:
        r = redact("")
        assert r.text == ""
        assert r.hits == ()

    def test_already_redacted_markers_are_idempotent(self) -> None:
        # Running redact() on an already-redacted output should not
        # double-replace the placeholders.
        once = redact("AKIAIOSFODNN7EXAMPLE")
        twice = redact(once.text)
        assert twice.text == once.text
        assert twice.hits == ()

    def test_hit_record_is_frozen_dataclass(self) -> None:
        from dataclasses import FrozenInstanceError

        h = Redaction("aws-key")
        with pytest.raises(FrozenInstanceError):
            h.label = "tampered"  # type: ignore[misc]
