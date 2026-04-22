"""Unit tests for whatsbot.domain.git_url."""

from __future__ import annotations

import pytest

from whatsbot.domain.git_url import (
    ALLOWED_HOSTS,
    DisallowedGitUrlError,
    validate_git_url,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo",
        "https://github.com/owner/repo.git",
        "https://github.com/owner/repo/",
        "https://gitlab.com/owner/repo",
        "https://bitbucket.org/owner/repo",
        "git@github.com:owner/repo",
        "git@github.com:owner/repo.git",
        "git@gitlab.com:owner/repo.git",
        "git@bitbucket.org:owner/repo",
        "ssh://git@github.com/owner/repo",
        "ssh://git@gitlab.com/owner/repo.git",
        "ssh://git@bitbucket.org/owner/repo",
        "https://github.com/foo-bar/baz_qux.git",
        "https://github.com/foo.bar/baz",
    ],
)
def test_allowed_urls_pass(url: str) -> None:
    assert validate_git_url(url) == url


def test_strips_outer_whitespace() -> None:
    assert validate_git_url("  https://github.com/o/r  ") == "https://github.com/o/r"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        # Disallowed hosts
        "https://evil.example.com/owner/repo",
        "https://gitea.io/owner/repo",
        "https://github.io/owner/repo",  # github.IO is not github.COM
        # Wrong scheme
        "http://github.com/owner/repo",  # plain http
        "ftp://github.com/owner/repo",
        "file:///etc/passwd",
        # Missing path segments
        "https://github.com",
        "https://github.com/",
        "https://github.com/owner",
        "https://github.com/owner/",
        # Shell-like injection attempts
        "https://github.com/owner/repo;rm -rf /",
        "https://github.com/owner/repo $(whoami)",
        "https://github.com/owner/repo`id`",
        # Wrong git@ format
        "git@github.com/owner/repo",  # slash instead of colon
        "git@unknown.com:owner/repo",
    ],
)
def test_disallowed_urls_rejected(url: str) -> None:
    with pytest.raises(DisallowedGitUrlError):
        validate_git_url(url)


def test_non_string_rejected() -> None:
    with pytest.raises(DisallowedGitUrlError):
        validate_git_url(None)  # type: ignore[arg-type]
    with pytest.raises(DisallowedGitUrlError):
        validate_git_url(42)  # type: ignore[arg-type]


def test_allowed_hosts_constant_is_the_three_we_documented() -> None:
    """Spec §13 — exactly three. Anything else is a security regression."""
    assert ALLOWED_HOSTS == ("github.com", "gitlab.com", "bitbucket.org")
