"""Sender-Whitelist parsing — pure, no I/O.

Source-of-truth is the Keychain entry ``allowed-senders``: a comma-separated
list of WhatsApp phone numbers (international format, e.g. ``+491701234567``).
The adapter in ``main.py`` reads the secret and feeds it to ``parse_whitelist``;
``is_allowed`` then checks individual sender numbers from inbound payloads.
"""

from __future__ import annotations


def parse_whitelist(raw: str) -> frozenset[str]:
    """Split ``raw`` on commas, strip whitespace, drop empty entries.

    Returns a ``frozenset`` so membership lookups are O(1) and the result is
    safe to share across requests without defensive copying.
    """
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def is_allowed(sender: str, whitelist: frozenset[str]) -> bool:
    """Exact match. No prefix tricks, no "+ optional" — Meta sends consistent
    international format and we want to fail closed on anything else.
    """
    return sender in whitelist
