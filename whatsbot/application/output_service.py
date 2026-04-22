"""OutputService — orchestrates the Spec §10 oversized-body dialogue.

Flow:

* ``deliver(to, body)``  — called instead of ``sender.send_text``. If
  the body is under the threshold we pass through to the injected
  ``MessageSender`` (usually already the ``RedactingMessageSender`` by
  this point). Over the threshold, we persist the raw body to disk,
  open a ``pending_outputs`` row, and send the user a short warning.
* ``resolve_send(to)``    — read the latest pending body, chunk it to
  WhatsApp's limit, send each chunk, drop the row + the file.
* ``resolve_discard(to)`` — drop the row + unlink the file.
* ``resolve_save(to)``    — drop the row only; file stays for
  forensic / debug use.

Design notes:

* The service holds the sender; callers pass ``to`` per call so a
  future multi-recipient mode doesn't require changing the sender.
* File naming is ``<msg_id>.md``; msg_id is a fresh ULID per deliver.
* We rely on ``pending_outputs.latest_open()`` for resolution: this is
  single-user, so the user's ``/send`` always refers to the last
  warning they saw. LIFO is the right default.
* Fail modes:
    - If the filesystem write fails, we fall back to sending the body
      directly (better to over-send than silently drop). A warning log
      records the fallback.
    - If ``resolve_send`` reads a file that's gone missing, we log
      warn + tell the user the output is unavailable.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ulid import ULID

from whatsbot.domain.output_guard import (
    body_size_bytes,
    chunk_for_whatsapp,
    format_warning,
    is_oversized,
)
from whatsbot.domain.pending_outputs import PendingOutput, compute_deadline
from whatsbot.logging_setup import get_logger
from whatsbot.ports.message_sender import MessageSender
from whatsbot.ports.pending_output_repository import PendingOutputRepository

ResolveKind = Literal["sent", "discarded", "saved", "none", "missing"]


@dataclass(frozen=True, slots=True)
class ResolveOutcome:
    """Structured summary the webhook turns into a user-facing message."""

    kind: ResolveKind
    msg_id: str | None = None
    size_bytes: int | None = None
    chunks_sent: int | None = None


class OutputService:
    def __init__(
        self,
        *,
        sender: MessageSender,
        repo: PendingOutputRepository,
        outputs_dir: Path,
        now_fn: Callable[[], int] | None = None,
    ) -> None:
        self._sender = sender
        self._repo = repo
        self._outputs_dir = outputs_dir
        self._now = now_fn or (lambda: int(time.time()))
        self._log = get_logger("whatsbot.output")
        self._outputs_dir.mkdir(parents=True, exist_ok=True)

    # ---- deliver -----------------------------------------------------

    def deliver(self, *, to: str, body: str, project_name: str = "_bot") -> None:
        """Direct-send a small body, or stash + warn for a large one."""
        if not is_oversized(body):
            self._sender.send_text(to=to, body=body)
            return

        size = body_size_bytes(body)
        char_count = len(body)
        msg_id = str(ULID())
        path = self._outputs_dir / f"{msg_id}.md"
        try:
            path.write_text(body, encoding="utf-8")
        except OSError:
            # Filesystem failure — fall back to direct send with a log.
            # Better to spill one big body than silently drop the user's
            # reply and leave them waiting.
            self._log.exception("output_spill_fallback", to=to, size_bytes=size)
            self._sender.send_text(to=to, body=body)
            return

        now = self._now()
        self._repo.create(
            PendingOutput(
                msg_id=msg_id,
                project_name=project_name,
                output_path=str(path),
                size_bytes=size,
                created_at=datetime.fromtimestamp(now, tz=UTC),
                deadline_ts=compute_deadline(now),
            )
        )
        self._log.info(
            "output_oversized_stashed",
            msg_id=msg_id,
            size_bytes=size,
            char_count=char_count,
            path=str(path),
            project=project_name,
        )
        self._sender.send_text(to=to, body=format_warning(size, char_count))

    # ---- resolve -----------------------------------------------------

    def resolve_send(self, *, to: str) -> ResolveOutcome:
        pending = self._repo.latest_open()
        if pending is None:
            return ResolveOutcome(kind="none")

        path = Path(pending.output_path)
        try:
            body = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._log.warning(
                "output_file_missing", msg_id=pending.msg_id, path=str(path)
            )
            self._repo.resolve(pending.msg_id)
            return ResolveOutcome(kind="missing", msg_id=pending.msg_id)

        chunks = chunk_for_whatsapp(body)
        for chunk in chunks:
            self._sender.send_text(to=to, body=chunk)

        self._repo.resolve(pending.msg_id)
        path.unlink(missing_ok=True)
        self._log.info(
            "output_sent",
            msg_id=pending.msg_id,
            size_bytes=pending.size_bytes,
            chunks=len(chunks),
        )
        return ResolveOutcome(
            kind="sent",
            msg_id=pending.msg_id,
            size_bytes=pending.size_bytes,
            chunks_sent=len(chunks),
        )

    def resolve_discard(self, *, to: str) -> ResolveOutcome:
        pending = self._repo.latest_open()
        if pending is None:
            return ResolveOutcome(kind="none")
        self._repo.resolve(pending.msg_id)
        Path(pending.output_path).unlink(missing_ok=True)
        self._log.info(
            "output_discarded",
            msg_id=pending.msg_id,
            size_bytes=pending.size_bytes,
        )
        return ResolveOutcome(
            kind="discarded",
            msg_id=pending.msg_id,
            size_bytes=pending.size_bytes,
        )

    def resolve_save(self, *, to: str) -> ResolveOutcome:
        pending = self._repo.latest_open()
        if pending is None:
            return ResolveOutcome(kind="none")
        self._repo.resolve(pending.msg_id)
        # File intentionally kept on disk.
        self._log.info(
            "output_saved_to_disk_only",
            msg_id=pending.msg_id,
            size_bytes=pending.size_bytes,
            path=pending.output_path,
        )
        return ResolveOutcome(
            kind="saved",
            msg_id=pending.msg_id,
            size_bytes=pending.size_bytes,
        )
