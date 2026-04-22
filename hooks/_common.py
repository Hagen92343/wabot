"""Shared helpers for the Pre-Tool hook.

The hook is a short-lived subprocess invoked by Claude Code on every
tool call. It has to be fast, hermetic, and **fail-closed** — any
unexpected condition must deny the tool invocation rather than fall
through to allow.

Two concerns live here:

* **Secret loading** — reads ``hook-shared-secret`` from macOS Keychain
  via the ``security`` CLI. The ``whatsbot`` Python package is **not**
  imported; the hook script must stay importable even when the full
  venv isn't on PATH.
* **HTTP IPC** — tight-timeout client against the bot's hook listener
  on ``127.0.0.1:8001``.

Both are written with the stdlib so the hook script can run from a
bare Python 3.12 interpreter if the venv path ever breaks.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Final

SERVICE_NAME: Final = "whatsbot"
SECRET_KEY: Final = "hook-shared-secret"
HOOK_URL_BASE: Final = "http://127.0.0.1:8001"
HOOK_SECRET_HEADER: Final = "X-Whatsbot-Hook-Secret"

# Timeouts in seconds. Connect is tight because the listener is loopback;
# read is the poll budget for synchronous decisions. The 5-minute ask-user
# path gets its own longer timeout when C3.3 lands.
CONNECT_TIMEOUT: Final = 2.0
READ_TIMEOUT: Final = 10.0

# Env-var overrides — used by tests to avoid touching the real Keychain.
ENV_SECRET_OVERRIDE: Final = "WHATSBOT_HOOK_SECRET"
ENV_URL_OVERRIDE: Final = "WHATSBOT_HOOK_URL"


class HookError(RuntimeError):
    """Any failure inside the hook that warrants Exit 2 + stderr reason."""


@dataclass(frozen=True, slots=True)
class HookResponse:
    """Parsed response from the bot's hook endpoint."""

    permission_decision: str  # "allow" | "deny"
    reason: str

    @property
    def is_allow(self) -> bool:
        return self.permission_decision == "allow"


def load_shared_secret() -> str:
    """Fetch the shared secret from the Keychain (or env-override).

    Raises ``HookError`` if no secret is available — caller must treat
    that as fail-closed.
    """
    override = os.environ.get(ENV_SECRET_OVERRIDE)
    if override is not None:
        return override

    try:
        completed = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE_NAME, "-a", SECRET_KEY, "-w"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HookError("keychain read timed out") from exc
    except FileNotFoundError as exc:
        raise HookError("`security` CLI not found (macOS-only)") from exc

    if completed.returncode != 0:
        raise HookError("shared secret not in keychain — run `make setup-secrets`")
    secret = completed.stdout.strip()
    if not secret:
        raise HookError("shared secret empty")
    return secret


def hook_url() -> str:
    return os.environ.get(ENV_URL_OVERRIDE, HOOK_URL_BASE)


def call_hook(
    *,
    path: str,
    payload: dict[str, object],
    secret: str,
    connect_timeout: float = CONNECT_TIMEOUT,
    read_timeout: float = READ_TIMEOUT,
) -> HookResponse:
    """POST to the bot's hook endpoint. Returns the parsed decision.

    All failure modes collapse into ``HookError`` with a short reason —
    the top-level script maps that to Exit 2 + stderr. We never, ever
    bubble up an exception that would let Claude interpret the hook as
    a pass-through.
    """
    url = f"{hook_url()}{path}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            HOOK_SECRET_HEADER: secret,
        },
    )

    # urllib has one ``timeout`` argument shared by connect + read. We pass
    # the larger of the two so short remote stalls still succeed. Genuine
    # unreachability fails fast via ConnectionRefusedError regardless.
    timeout = max(connect_timeout, read_timeout)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # 4xx / 5xx with a body — the endpoint itself said deny.
        try:
            raw = exc.read()
            return _parse_response(exc.code, raw)
        except Exception:  # noqa: BLE001
            raise HookError(f"hook http error {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise HookError(f"hook unreachable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise HookError("hook timed out") from exc

    return _parse_response(status, raw)


def _parse_response(status: int, raw: bytes) -> HookResponse:
    try:
        data = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise HookError(f"hook returned malformed JSON (status={status})") from exc
    if not isinstance(data, dict):
        raise HookError(f"hook returned non-object JSON (status={status})")

    block = data.get("hookSpecificOutput")
    if not isinstance(block, dict):
        raise HookError(f"hook response missing hookSpecificOutput (status={status})")
    decision = block.get("permissionDecision")
    reason = block.get("permissionDecisionReason", "") or ""
    if decision not in ("allow", "deny"):
        raise HookError(f"hook returned unknown decision {decision!r} (status={status})")

    return HookResponse(
        permission_decision=str(decision),
        reason=str(reason) if isinstance(reason, str) else "",
    )
