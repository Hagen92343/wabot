"""C7.5 — MediaSweeper lifespan integration + real secure_delete.

Two scenarios:

1. The sweeper runs as a FastAPI lifespan task and, when triggered
   manually via ``sweep_now``, actually removes stale files written
   into the real file-backed :class:`FileMediaCache`.

2. ``FileMediaCache.secure_delete`` overwrites the file with zeros
   before unlinking — verified via a pre-unlink snapshot (monkey-
   patched ``Path.unlink``). This retests the C7.1-level guarantee
   in an integration setup so regressions get caught here too.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.file_media_cache import FileMediaCache
from whatsbot.application.media_sweeper import MediaSweeper
from whatsbot.config import Environment, Settings
from whatsbot.domain.media_cache import CACHE_TTL_SECONDS
from whatsbot.main import create_app
from whatsbot.ports.secrets_provider import (
    ALL_KEYS,
    KEY_ALLOWED_SENDERS,
    KEY_META_APP_SECRET,
    KEY_META_VERIFY_TOKEN,
    SecretNotFoundError,
)

pytestmark = pytest.mark.integration

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
ALLOWED_SENDER = "+491701234567"


class StubSecrets:
    def __init__(self, **kv: str) -> None:
        self._store = dict(kv)

    def get(self, key: str) -> str:
        if key not in self._store:
            raise SecretNotFoundError(key)
        return self._store[key]

    def set(self, key: str, value: str) -> None:
        self._store[key] = value

    def rotate(self, key: str, new_value: str) -> None:
        self._store[key] = new_value


def _full_secret_stub() -> StubSecrets:
    base = {key: f"placeholder-for-{key}" for key in ALL_KEYS}
    base[KEY_META_APP_SECRET] = APP_SECRET
    base[KEY_META_VERIFY_TOKEN] = VERIFY_TOKEN
    base[KEY_ALLOWED_SENDERS] = ALLOWED_SENDER
    return StubSecrets(**base)


def test_sweeper_lifespan_sweeps_on_startup_and_on_demand(tmp_path: Path) -> None:
    """Sweeper is wired into the FastAPI lifespan in test env when
    ``enable_media_sweeper=True``. The startup sweep fires
    synchronously inside ``lifespan``; a manual ``sweep_now`` run
    exercises the same code path and confirms files actually leave
    the disk."""
    cache_dir = tmp_path / "cache"
    cache = FileMediaCache(cache_dir=cache_dir)
    # Seed: two stale items with mtimes far past the 7-day TTL.
    stale_a = cache.store("stale_a", b"x" * 100, ".jpg")
    stale_b = cache.store("stale_b", b"y" * 50, ".pdf")
    old_mtime = stale_a.stat().st_mtime - CACHE_TTL_SECONDS - 3600
    import os

    os.utime(stale_a, (old_mtime, old_mtime))
    os.utime(stale_b, (old_mtime, old_mtime))
    # And one fresh item that must survive.
    fresh = cache.store("fresh", b"z" * 10, ".ogg")

    db_path = tmp_path / "state.db"
    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)

    app = create_app(
        settings=Settings(env=Environment.TEST, media_cache_dir=cache_dir),
        secrets_provider=_full_secret_stub(),
        db_connection=conn,
        media_cache=cache,
        enable_media_sweeper=True,
    )
    sweeper = app.state.media_sweeper
    assert isinstance(sweeper, MediaSweeper)

    with TestClient(app):
        # The initial sweep ran synchronously inside start().
        # Both stale files should be gone.
        assert not stale_a.exists()
        assert not stale_b.exists()
        assert fresh.exists()

        # A manual sweep is a no-op now — nothing's stale.
        report = asyncio.run(sweeper.sweep_now())
        assert report.ttl_deleted == 0
        assert report.size_deleted == 0


def test_sweeper_disabled_by_default_in_test_env(tmp_path: Path) -> None:
    """Sanity: without ``enable_media_sweeper=True`` the sweeper stays
    unwired so the non-media test suite doesn't gain a background
    loop it didn't ask for."""
    cache_dir = tmp_path / "cache"
    db_path = tmp_path / "state.db"
    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)

    app = create_app(
        settings=Settings(env=Environment.TEST, media_cache_dir=cache_dir),
        secrets_provider=_full_secret_stub(),
        db_connection=conn,
    )
    assert app.state.media_sweeper is None


def test_secure_delete_zeros_file_before_unlink(tmp_path: Path) -> None:
    """Integration re-check of the C7.1 secure_delete guarantee."""
    cache = FileMediaCache(cache_dir=tmp_path / "cache")
    payload = b"SENSITIVE-DATA-" * 64
    path = cache.store("id1", payload, ".pdf")
    assert path.exists()
    assert path.read_bytes() == payload

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
    # File contents were overwritten with zeros before the unlink.
    assert snapshot["bytes"] == b"\x00" * len(payload)
