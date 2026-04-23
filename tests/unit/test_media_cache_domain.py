"""C7.5 — domain/media_cache.py pure-eviction tests."""

from __future__ import annotations

from pathlib import Path

from whatsbot.domain.media_cache import (
    CACHE_MAX_BYTES,
    CACHE_TTL_SECONDS,
    is_expired,
    select_expired,
    select_for_eviction,
)
from whatsbot.ports.media_cache import CachedItem


def _item(name: str, *, size: int, mtime: float) -> CachedItem:
    return CachedItem(path=Path(f"/tmp/cache/{name}"), size_bytes=size, mtime=mtime)


# --- is_expired / select_expired -----------------------------------------


def test_is_expired_past_ttl() -> None:
    item = _item("a", size=1, mtime=1_000_000.0)
    # 8 days after mtime.
    now = 1_000_000.0 + 8 * 24 * 3600
    assert is_expired(item, now=now)


def test_is_expired_within_ttl() -> None:
    item = _item("a", size=1, mtime=1_000_000.0)
    now = 1_000_000.0 + 60  # 1 minute old
    assert not is_expired(item, now=now)


def test_is_expired_exact_boundary_is_expired() -> None:
    """``>=`` boundary prevents flicker between sweeps."""
    item = _item("a", size=1, mtime=1_000_000.0)
    now = 1_000_000.0 + CACHE_TTL_SECONDS
    assert is_expired(item, now=now)


def test_is_expired_custom_ttl() -> None:
    item = _item("a", size=1, mtime=100.0)
    assert not is_expired(item, now=105.0, ttl_seconds=10)
    assert is_expired(item, now=115.0, ttl_seconds=10)


def test_select_expired_partitions() -> None:
    now = 1_000_000.0
    fresh = _item("fresh", size=10, mtime=now - 60)
    stale_a = _item("stale_a", size=20, mtime=now - CACHE_TTL_SECONDS - 10)
    stale_b = _item("stale_b", size=30, mtime=now - CACHE_TTL_SECONDS - 1_000)
    items = [fresh, stale_a, stale_b]
    out = select_expired(items, now=now)
    # Order preserved.
    assert out == [stale_a, stale_b]


def test_select_expired_empty_list() -> None:
    assert select_expired([], now=1_000_000.0) == []


def test_select_expired_all_fresh() -> None:
    now = 1_000_000.0
    items = [_item(f"i{i}", size=1, mtime=now - 1) for i in range(5)]
    assert select_expired(items, now=now) == []


# --- select_for_eviction --------------------------------------------------


def test_select_for_eviction_empty_list() -> None:
    assert select_for_eviction([]) == []


def test_select_for_eviction_under_cap_no_op() -> None:
    items = [_item(f"i{i}", size=10, mtime=float(i)) for i in range(5)]
    assert select_for_eviction(items, max_size=1_000) == []


def test_select_for_eviction_exact_cap_no_op() -> None:
    items = [_item("a", size=100, mtime=1.0), _item("b", size=100, mtime=2.0)]
    assert select_for_eviction(items, max_size=200) == []


def test_select_for_eviction_oldest_first() -> None:
    # 4 items × 100 bytes each = 400 total. Cap at 250 → evict the
    # two oldest to get under.
    items = [
        _item("oldest", size=100, mtime=1.0),
        _item("old", size=100, mtime=2.0),
        _item("newer", size=100, mtime=3.0),
        _item("newest", size=100, mtime=4.0),
    ]
    victims = select_for_eviction(items, max_size=250)
    assert [v.path.name for v in victims] == ["oldest", "old"]


def test_select_for_eviction_single_large_item_exceeds_cap() -> None:
    items = [
        _item("big", size=2 * CACHE_MAX_BYTES, mtime=1.0),
        _item("tiny", size=1, mtime=2.0),
    ]
    victims = select_for_eviction(items)
    # Big alone is already over cap — evict it.
    assert [v.path.name for v in victims] == ["big"]


def test_select_for_eviction_stops_when_under_cap() -> None:
    # 3 items × 1000 bytes = 3000. Cap 1500. Should evict just one
    # (oldest), stop because we're then at 2000... still over. Evict
    # second. Now at 1000, stop.
    items = [
        _item("a", size=1000, mtime=1.0),
        _item("b", size=1000, mtime=2.0),
        _item("c", size=1000, mtime=3.0),
    ]
    victims = select_for_eviction(items, max_size=1500)
    assert [v.path.name for v in victims] == ["a", "b"]


def test_select_for_eviction_uses_supplied_current_size() -> None:
    """Callers can pre-sum to avoid the double pass."""
    items = [_item("a", size=10, mtime=1.0), _item("b", size=10, mtime=2.0)]
    # Claim the current size is 20 explicitly.
    assert select_for_eviction(items, current_size=20, max_size=30) == []
    assert select_for_eviction(items, current_size=20, max_size=15) == [items[0]]
