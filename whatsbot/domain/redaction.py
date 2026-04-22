"""Outgoing-message redaction — 4-stage pure pipeline (Spec §10).

Every WhatsApp body the bot sends runs through this regardless of the
active mode. Even YOLO must not leak tokens to the user's phone; if
Claude dumped a .env file or a cat of ~/.ssh/id_rsa, the contents are
masked before they leave the process.

The stages are layered cheap-to-expensive so the entropy scan only
sees what's left after the structural sweeps:

1. **Known key patterns** — AWS, GitHub PAT, OpenAI, Stripe, JWT,
   Bearer tokens. Fixed regexes, fast.
2. **Structural patterns** — PEM blocks, SSH public keys, DB URLs
   with embedded credentials, and ``KEY=VALUE`` pairs where the key
   is sensitive (``password``, ``secret``, ``token``, ``api_key``,
   ``credential``, ``access_key``).
3. **Entropy** — any remaining 40+ char whitespace-free token whose
   Shannon entropy exceeds 4.5 bits/char gets masked. URL-like
   tokens are skipped; tokens with zero digits are skipped (most
   false positives on camelCase identifiers look like this).
4. **Sensitive paths** — on any line mentioning ``~/.ssh``, ``~/.aws``,
   ``~/.gnupg``, ``~/Library/Keychains``, etc., long tokens on the
   same line get masked as ``<REDACTED:path-content>``. Belt and
   suspenders — stages 1-3 usually catch the real content; stage 4
   is for weird shapes we haven't catalogued.

Labels are preserved in the ``<REDACTED:xxx>`` placeholder so
support-style debugging stays useful even without the secret. The
``RedactionResult.hits`` tuple also surfaces them programmatically.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class Redaction:
    """Record of a single masked span. One per match, not per stage."""

    label: str


@dataclass(frozen=True, slots=True)
class RedactionResult:
    text: str
    hits: tuple[Redaction, ...]

    def __bool__(self) -> bool:
        return bool(self.hits)


# ---- Stage 1: known key patterns ----------------------------------------
#
# Order within this tuple only matters when two patterns could overlap;
# the regexes below are tight enough that they don't.

_KNOWN_KEY_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("aws-key", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
    # Classic PATs are exactly 36 chars, but we accept ≥36 so test
    # fixtures and possible format nudges don't silently slip through.
    ("gh-token", re.compile(r"\bgh[ps]_[A-Za-z0-9]{36,}\b")),
    ("gh-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82,}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("stripe-key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{24,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")),
    ("bearer", re.compile(r"Bearer [A-Za-z0-9._-]{20,}")),
)


def _stage1_known_keys(text: str) -> tuple[str, list[Redaction]]:
    hits: list[Redaction] = []
    for label, pattern in _KNOWN_KEY_PATTERNS:
        def repl(_m: re.Match[str], _label: str = label) -> str:
            hits.append(Redaction(_label))
            return f"<REDACTED:{_label}>"

        text = pattern.sub(repl, text)
    return text, hits


# ---- Stage 2: structural patterns ---------------------------------------

_PEM_RE: Final = re.compile(
    r"-----BEGIN [A-Z0-9 ]+-----[\s\S]+?-----END [A-Z0-9 ]+-----"
)

_SSH_PUBKEY_RE: Final = re.compile(
    r"\b(?:ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-nistp(?:256|384|521))"
    r"\s+[A-Za-z0-9+/=]{40,}"
)

_DB_CREDS_RE: Final = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mongodb|rediss?|amqps?|mssql)://"
    r"[^:@\s/]+:[^@\s]+@[^\s]+"
)

_SENSITIVE_ENV_RE: Final = re.compile(
    r"(?i)"
    r"(?P<key>\b(?:password|passwd|secret|token|api[_-]?key|apikey|"
    r"credential|access[_-]?key|auth[_-]?token)\b)"
    # sep tolerates a closing quote on the key (JSON-style
    # ``"password": "..."``) and an optional opening quote on the value.
    r"(?P<sep>\"?\s*[:=]\s*\"?)"
    r"(?P<value>[^\s\",;}\]]+)"
)


def _stage2_structural(text: str) -> tuple[str, list[Redaction]]:
    hits: list[Redaction] = []

    def pem_repl(_m: re.Match[str]) -> str:
        hits.append(Redaction("pem"))
        return "<REDACTED:pem>"

    text = _PEM_RE.sub(pem_repl, text)

    def ssh_repl(_m: re.Match[str]) -> str:
        hits.append(Redaction("ssh-pubkey"))
        return "<REDACTED:ssh-pubkey>"

    text = _SSH_PUBKEY_RE.sub(ssh_repl, text)

    def db_repl(_m: re.Match[str]) -> str:
        hits.append(Redaction("db-creds"))
        return "<REDACTED:db-creds>"

    text = _DB_CREDS_RE.sub(db_repl, text)

    def env_repl(m: re.Match[str]) -> str:
        key_slug = m.group("key").lower().replace("-", "_")
        label = f"env:{key_slug}"
        hits.append(Redaction(label))
        return f"{m.group('key')}{m.group('sep')}<REDACTED:{label}>"

    text = _SENSITIVE_ENV_RE.sub(env_repl, text)
    return text, hits


# ---- Stage 3: entropy ---------------------------------------------------

_TOKEN_RE: Final = re.compile(r"\S{40,}")
_URL_PREFIXES: Final = ("http://", "https://", "ftp://", "file://", "ssh://", "git://")


def _shannon_entropy(s: str) -> float:
    """Shannon entropy in bits/char. 0 for empty or constant strings."""
    if not s:
        return 0.0
    counts = Counter(s)
    total = len(s)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def _stage3_entropy(text: str, *, threshold: float = 4.5) -> tuple[str, list[Redaction]]:
    hits: list[Redaction] = []

    def repl(m: re.Match[str]) -> str:
        token = m.group(0)
        if token.startswith("<REDACTED:"):
            return token
        if token.startswith(_URL_PREFIXES):
            return token
        # Real secrets/tokens/hashes usually have digits. Filtering on
        # "contains at least one digit" removes most camelCase false
        # positives without missing base64 / hex / URL-safe payloads.
        if not any(ch.isdigit() for ch in token):
            return token
        if _shannon_entropy(token) <= threshold:
            return token
        hits.append(Redaction("high-entropy"))
        return "<REDACTED:high-entropy>"

    return _TOKEN_RE.sub(repl, text), hits


# ---- Stage 4: sensitive-path line content ------------------------------

_SENSITIVE_PATHS: Final[tuple[str, ...]] = (
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.config/gh",
    "~/.1password",
    "~/Library/Keychains",
)

_LONG_TOKEN_RE: Final = re.compile(r"\S{20,}")


def _line_touches_sensitive_path(line: str) -> bool:
    return any(p in line for p in _SENSITIVE_PATHS)


def _stage4_sensitive_paths(text: str) -> tuple[str, list[Redaction]]:
    hits: list[Redaction] = []
    out: list[str] = []
    for line in text.split("\n"):
        if not _line_touches_sensitive_path(line):
            out.append(line)
            continue

        def repl(m: re.Match[str]) -> str:
            token = m.group(0)
            if token.startswith("<REDACTED:"):
                return token
            if any(p in token for p in _SENSITIVE_PATHS):
                return token  # the path itself stays readable
            if token.startswith(_URL_PREFIXES):
                return token
            hits.append(Redaction("path-content"))
            return "<REDACTED:path-content>"

        out.append(_LONG_TOKEN_RE.sub(repl, line))
    return "\n".join(out), hits


# ---- Pipeline -----------------------------------------------------------


def redact(text: str) -> RedactionResult:
    """Run all four stages in order and return the scrubbed text + hits."""
    all_hits: list[Redaction] = []
    for stage in (
        _stage1_known_keys,
        _stage2_structural,
        _stage3_entropy,
        _stage4_sensitive_paths,
    ):
        text, hits = stage(text)
        all_hits.extend(hits)
    return RedactionResult(text=text, hits=tuple(all_hits))


if __name__ == "__main__":  # pragma: no cover — stdin CLI for manual smoke
    import sys

    result = redact(sys.stdin.read())
    sys.stdout.write(result.text)
    if result.hits:
        labels = sorted({h.label for h in result.hits})
        sys.stderr.write(f"\n# redacted {len(result.hits)} span(s): {', '.join(labels)}\n")
