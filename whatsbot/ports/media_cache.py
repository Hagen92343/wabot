"""Media-cache port — persistent on-disk cache for downloaded media.

Spec §4 + §16: the cache lives under
``~/Library/Caches/whatsbot/media/`` with a 7-day TTL. The application
layer doesn't care about filesystem details; it stores a blob and gets
a path back that Claude can read.

``secure_delete`` (Spec §16) overwrites the file with zeros before
``unlink`` to reduce casual forensic recoverability. This is best-effort
on APFS and documented as such in SECURITY.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CachedItem:
    """Filesystem-observable metadata for a cached blob."""

    path: Path
    size_bytes: int
    mtime: float


class MediaCache(Protocol):
    """Narrow storage contract — store, resolve, list, secure-delete."""

    def store(self, media_id: str, payload: bytes, suffix: str) -> Path:
        """Persist ``payload`` atomically under
        ``<cache_dir>/<media_id><suffix>`` and return the final path.

        Atomic via ``<path>.tmp`` + ``os.replace`` so a crashed bot
        doesn't leave a half-written file that looks valid to a later
        read. Overwrites any existing file with the same ``media_id +
        suffix`` (Meta re-delivers the same id on retries).
        """

    def path_for(self, media_id: str, suffix: str) -> Path:
        """Return the canonical filesystem path for ``(media_id, suffix)``.

        Does not check whether the file exists — callers use this for
        Claude-prompt assembly where the path is included verbatim.
        """

    def list_all(self) -> list[CachedItem]:
        """Return every currently cached item, ordered oldest first by mtime.

        Used by the C7.5 sweeper to pick TTL / size-cap eviction
        candidates.
        """

    def secure_delete(self, path: Path) -> None:
        """Overwrite ``path`` with zeros (best-effort) and ``unlink``.

        Idempotent: a missing file is a silent no-op. The overwrite
        pass is synchronous with ``fsync`` so a later ``Get File Info``
        won't show the original bytes; APFS copy-on-write limits how
        effective this is on modern Mac filesystems (documented).
        """
