"""Media-cache retention policy — pure, no I/O.

Phase-7 C7.5: Spec §16 calls for a 7-day TTL and a 1 GB total-size cap
on ``~/Library/Caches/whatsbot/media/``. The domain layer here decides
*which* items to evict; the application layer
(:class:`whatsbot.application.media_sweeper.MediaSweeper`) runs the
sweep on a timer and the adapter
(:class:`whatsbot.adapters.file_media_cache.FileMediaCache`) performs
the ``secure_delete`` (zero + fsync + unlink).

All functions are pure — no filesystem, no clock. The application
passes in the current timestamp and the snapshot of cached items so
every sweep step is unit-testable in isolation.
"""

from __future__ import annotations

from typing import Final

from whatsbot.ports.media_cache import CachedItem

CACHE_TTL_SECONDS: Final[int] = 7 * 24 * 3600
"""7 days — Spec §16."""

CACHE_MAX_BYTES: Final[int] = 1 * 1024 * 1024 * 1024
"""1 GB — Spec §16."""


def is_expired(
    item: CachedItem,
    *,
    now: float,
    ttl_seconds: int = CACHE_TTL_SECONDS,
) -> bool:
    """Return ``True`` iff ``now - item.mtime >= ttl_seconds``.

    The ``>=`` boundary matters: an item created exactly at the edge
    is treated as expired so the sweeper is monotonic — a file never
    flickers between expired and fresh across two close ticks.
    """
    return (now - item.mtime) >= ttl_seconds


def select_expired(
    items: list[CachedItem],
    *,
    now: float,
    ttl_seconds: int = CACHE_TTL_SECONDS,
) -> list[CachedItem]:
    """Partition ``items`` into the TTL-expired subset.

    Order of the returned list mirrors the input. Callers typically
    follow up with :func:`select_for_eviction` on the *remaining*
    items to also enforce the size cap.
    """
    return [
        item
        for item in items
        if is_expired(item, now=now, ttl_seconds=ttl_seconds)
    ]


def select_for_eviction(
    items: list[CachedItem],
    *,
    current_size: int | None = None,
    max_size: int = CACHE_MAX_BYTES,
) -> list[CachedItem]:
    """Return the oldest-first list of items to evict to get under ``max_size``.

    ``items`` must be sorted by mtime ascending (the port contract
    guarantees this via :meth:`MediaCache.list_all`). We don't re-sort
    defensively because a miss-sorted input would be a bug in the
    adapter that we'd want to surface as a failing test, not paper
    over with an extra sort.

    ``current_size`` defaults to ``sum(item.size_bytes for item in items)``
    when not provided; supply it explicitly when callers have already
    computed it to avoid two passes over the list.

    Empty list, already-under-cap, and exactly-at-cap all return ``[]``.
    """
    if not items:
        return []
    total = (
        current_size
        if current_size is not None
        else sum(item.size_bytes for item in items)
    )
    if total <= max_size:
        return []

    victims: list[CachedItem] = []
    remaining = total
    for item in items:  # oldest first
        if remaining <= max_size:
            break
        victims.append(item)
        remaining -= item.size_bytes
    return victims
