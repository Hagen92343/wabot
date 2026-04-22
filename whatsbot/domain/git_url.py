"""Git-URL-Whitelist — pure validation, no I/O.

Spec §13: only github.com / gitlab.com / bitbucket.org are accepted, and
only via the three canonical URL schemes (https, git@, ssh://). Anything
else is rejected so the bot can't be tricked into cloning attacker-
controlled hosts. The single-user assumption means we don't need
configurable hosts — if you ever do, change ``ALLOWED_HOSTS`` and
regenerate the regexes.
"""

from __future__ import annotations

import re
from typing import Final

ALLOWED_HOSTS: Final[tuple[str, ...]] = (
    "github.com",
    "gitlab.com",
    "bitbucket.org",
)

# Owner/repo segments must be non-empty and contain no path separators or
# obvious shell metacharacters. The trailing ``.git`` and trailing slash are
# both optional.
_HOST_GROUP = "|".join(re.escape(h) for h in ALLOWED_HOSTS)
_REPO_SEG = r"[A-Za-z0-9._-]+"

_HTTPS_PATTERN = re.compile(rf"^https://({_HOST_GROUP})/{_REPO_SEG}/{_REPO_SEG}(?:\.git)?/?$")
_SSH_AT_PATTERN = re.compile(rf"^git@({_HOST_GROUP}):{_REPO_SEG}/{_REPO_SEG}(?:\.git)?$")
_SSH_URL_PATTERN = re.compile(rf"^ssh://git@({_HOST_GROUP})/{_REPO_SEG}/{_REPO_SEG}(?:\.git)?$")


class DisallowedGitUrlError(ValueError):
    """Raised when a clone URL fails the host- or scheme-whitelist."""


def validate_git_url(url: str) -> str:
    """Return the canonical (stripped) URL or raise ``DisallowedGitUrlError``.

    The function does not normalise the URL further (no SSH-to-HTTPS
    rewrite, no .git appending) — we hand it through to ``git clone`` as-is
    so the user's expectation matches what we actually fetch.
    """
    if not isinstance(url, str):
        raise DisallowedGitUrlError(f"URL muss ein String sein, bekam {type(url).__name__}")
    candidate = url.strip()
    for pattern in (_HTTPS_PATTERN, _SSH_AT_PATTERN, _SSH_URL_PATTERN):
        if pattern.fullmatch(candidate):
            return candidate
    raise DisallowedGitUrlError(
        f"URL '{url}' nicht erlaubt. "
        f"Erlaubte Hosts: {', '.join(ALLOWED_HOSTS)} "
        f"via https://, git@<host>: oder ssh://git@<host>/."
    )
