"""Filesystem-backed :class:`~whatsbot.ports.media_cache.MediaCache`.

Spec §4 puts the cache at ``~/Library/Caches/whatsbot/media/``; the
adapter creates the directory on demand. Writes are atomic
(``<name>.tmp`` + ``os.replace``), secure-delete nulls the file before
unlinking (Spec §16, best-effort on APFS).

Stored filenames are ``<media_id><suffix>`` — media_id is trusted to be
safe (Meta supplies opaque IDs), but we still sanitise via
``_safe_media_id`` to make sure a crafted ID can't traverse out of the
cache dir.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final

from whatsbot.logging_setup import get_logger
from whatsbot.ports.media_cache import CachedItem

_SAFE_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9._-]+$")
_CHUNK: Final[int] = 64 * 1024  # 64 KB zero-write chunk


class FileMediaCache:
    """Concrete MediaCache for prod + dev."""

    def __init__(self, *, cache_dir: Path) -> None:
        self._dir = Path(cache_dir).expanduser()
        self._log = get_logger("whatsbot.media_cache")

    def store(self, media_id: str, payload: bytes, suffix: str) -> Path:
        self._ensure_dir()
        safe_id = _safe_media_id(media_id)
        target = self._dir / f"{safe_id}{suffix}"
        tmp = target.with_suffix(target.suffix + ".tmp")
        # Write-then-replace: an interrupted process leaves a ``.tmp``
        # fragment behind, never a corrupt ``.jpg`` that looks valid.
        with tmp.open("wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        self._log.info(
            "media_cached",
            media_id=media_id,
            path=str(target),
            size_bytes=len(payload),
        )
        return target

    def path_for(self, media_id: str, suffix: str) -> Path:
        safe_id = _safe_media_id(media_id)
        return self._dir / f"{safe_id}{suffix}"

    def list_all(self) -> list[CachedItem]:
        if not self._dir.exists():
            return []
        items: list[CachedItem] = []
        for entry in self._dir.iterdir():
            if not entry.is_file():
                continue
            if entry.name.endswith(".tmp"):
                continue  # in-flight writes
            try:
                stat = entry.stat()
            except FileNotFoundError:
                continue  # raced with a concurrent sweep
            items.append(
                CachedItem(
                    path=entry,
                    size_bytes=stat.st_size,
                    mtime=stat.st_mtime,
                )
            )
        items.sort(key=lambda item: item.mtime)
        return items

    def secure_delete(self, path: Path) -> None:
        """Zero-overwrite-then-unlink. Silent no-op if the file is gone."""
        target = Path(path)
        try:
            size = target.stat().st_size
        except FileNotFoundError:
            return
        try:
            with target.open("r+b") as fh:
                remaining = size
                zeros = b"\x00" * _CHUNK
                while remaining > 0:
                    chunk = zeros if remaining >= _CHUNK else b"\x00" * remaining
                    fh.write(chunk)
                    remaining -= len(chunk)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            # Overwrite failures shouldn't block unlink — the file still
            # needs to be removed. Log and continue.
            self._log.warning(
                "media_secure_delete_overwrite_failed",
                path=str(target),
                error=str(exc),
            )
        try:
            target.unlink()
        except FileNotFoundError:
            return
        self._log.info("media_secure_deleted", path=str(target))

    # ---- internals ---------------------------------------------------

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)


def _safe_media_id(media_id: str) -> str:
    """Reject anything with path separators or non-ASCII oddities.

    Meta media IDs are numeric strings in practice, but we validate
    defensively so a crafted webhook can't smuggle ``../`` into the
    cache path.
    """
    if not isinstance(media_id, str):
        raise ValueError("media_id muss String sein")
    candidate = media_id.strip()
    if not candidate:
        raise ValueError("media_id leer")
    if not _SAFE_ID_PATTERN.fullmatch(candidate):
        raise ValueError(f"media_id {media_id!r} enthaelt unzulaessige Zeichen")
    return candidate
