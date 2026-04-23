"""C7.1 — FileMediaCache filesystem-adapter tests."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from whatsbot.adapters.file_media_cache import FileMediaCache


def _cache(tmp_path: Path) -> FileMediaCache:
    return FileMediaCache(cache_dir=tmp_path / "cache")


def test_store_creates_directory(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    assert not (tmp_path / "cache").exists()
    cache.store("id_1", b"hello", ".jpg")
    assert (tmp_path / "cache").is_dir()


def test_store_returns_canonical_path(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    target = cache.store("abc123", b"\xff\xd8\xff", ".jpg")
    assert target == (tmp_path / "cache" / "abc123.jpg")
    assert target.read_bytes() == b"\xff\xd8\xff"


def test_store_is_atomic_no_tmp_left_over(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.store("id", b"payload", ".pdf")
    residual = list((tmp_path / "cache").glob("*.tmp"))
    assert residual == []


def test_store_overwrites_existing_same_id(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.store("id", b"original", ".jpg")
    cache.store("id", b"replaced", ".jpg")
    assert (tmp_path / "cache" / "id.jpg").read_bytes() == b"replaced"


@pytest.mark.parametrize(
    "bad_id",
    ["", "   ", "../escape", "abc/def", "has space", "has\nline"],
)
def test_store_rejects_unsafe_id(tmp_path: Path, bad_id: str) -> None:
    cache = _cache(tmp_path)
    with pytest.raises(ValueError):
        cache.store(bad_id, b"x", ".jpg")


def test_path_for_no_file_access(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    # path_for must not touch the filesystem.
    target = cache.path_for("not-there", ".jpg")
    assert target == (tmp_path / "cache" / "not-there.jpg")
    assert not target.exists()
    assert not (tmp_path / "cache").exists()


def test_list_all_empty(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    assert cache.list_all() == []


def test_list_all_sorted_oldest_first(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    p1 = cache.store("a", b"x" * 10, ".jpg")
    time.sleep(0.01)
    p2 = cache.store("b", b"y" * 20, ".pdf")
    time.sleep(0.01)
    p3 = cache.store("c", b"z" * 30, ".ogg")
    items = cache.list_all()
    assert [item.path for item in items] == [p1, p2, p3]
    assert [item.size_bytes for item in items] == [10, 20, 30]


def test_list_all_skips_tmp_files(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.store("x", b"x", ".jpg")
    # Simulate an interrupted write
    (tmp_path / "cache" / "zombie.jpg.tmp").write_bytes(b"trash")
    items = cache.list_all()
    assert len(items) == 1
    assert items[0].path.name == "x.jpg"


def test_secure_delete_missing_is_noop(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.secure_delete(tmp_path / "never-existed.jpg")  # no raise


def test_secure_delete_removes_file(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    path = cache.store("id1", b"original-bytes", ".jpg")
    assert path.exists()
    cache.secure_delete(path)
    assert not path.exists()


def test_secure_delete_overwrites_before_unlink(tmp_path: Path) -> None:
    """Confirm the zero-overwrite step runs — we observe it by
    snapshotting the file between the write and the unlink via a
    controlled intercept."""
    cache = _cache(tmp_path)
    original = b"SECRET" * 100
    path = cache.store("id1", original, ".pdf")

    # Patch unlink to observe the current bytes at that moment.
    snapshot: dict[str, bytes] = {}

    orig_unlink = Path.unlink

    def capturing_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == path and path.exists():
            snapshot["bytes"] = path.read_bytes()
        orig_unlink(self, *args, **kwargs)

    Path.unlink = capturing_unlink  # type: ignore[method-assign]
    try:
        cache.secure_delete(path)
    finally:
        Path.unlink = orig_unlink  # type: ignore[method-assign]

    assert not path.exists()
    assert snapshot["bytes"] == b"\x00" * len(original)


def test_list_all_race_with_concurrent_removal(tmp_path: Path) -> None:
    """A file disappearing between iterdir and stat must not raise."""
    cache = _cache(tmp_path)
    path = cache.store("id1", b"x", ".jpg")

    # Pre-remove behind the adapter's back.
    os.unlink(path)
    # Cache dir exists but file is gone — list_all should return []
    assert cache.list_all() == []
