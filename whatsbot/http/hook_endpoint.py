"""Hook HTTP endpoint — Spec §7, §14.

Exposes two POST routes on a separate FastAPI app bound to
``127.0.0.1:8001``. The Pre-Tool-Hook script (``hooks/pre_tool.py``) is
the only legitimate caller; authentication is a shared secret read from
Keychain (``hook-shared-secret``) that travels in the
``X-Whatsbot-Hook-Secret`` header.

Any failure in this module has to be **fail-closed** (Spec §7): we
never reply "allow" on an error path, and we never raise through to
FastAPI's default 500 handler because that would bubble up as a
"no response" and the hook script would treat it as unreachable — which
is already fail-closed in the client, but we want the server to be
explicit too.
"""

from __future__ import annotations

import hmac
from typing import Final

from fastapi import APIRouter, Header, Request, status
from fastapi.responses import JSONResponse

from whatsbot.application.hook_service import HookService
from whatsbot.domain.hook_decisions import HookDecision, Verdict, deny
from whatsbot.logging_setup import get_logger
from whatsbot.ports.secrets_provider import (
    KEY_HOOK_SHARED_SECRET,
    SecretNotFoundError,
    SecretsProvider,
)

HOOK_SECRET_HEADER: Final = "X-Whatsbot-Hook-Secret"


def build_router(
    *,
    secrets: SecretsProvider,
    service: HookService,
) -> APIRouter:
    """Construct the hook APIRouter with auth + service wired in.

    The shared secret is captured once at router-build time. Rotation
    requires a process restart — the risk of a missed rotation is lower
    than the risk of a per-request Keychain read on the hot path.
    """
    log = get_logger("whatsbot.hook_endpoint")
    router = APIRouter()

    try:
        expected_secret = secrets.get(KEY_HOOK_SHARED_SECRET)
    except SecretNotFoundError:
        # No secret in Keychain → we can't authenticate anyone, which
        # means every request gets denied. That's the correct fail-closed
        # behaviour: better to break Claude's Bash than to silently let
        # an attacker through.
        expected_secret = ""
        log.warning("hook_shared_secret_missing")

    def _verify_auth(supplied: str | None) -> bool:
        if not expected_secret:
            return False
        if supplied is None:
            return False
        return hmac.compare_digest(supplied.encode("utf-8"), expected_secret.encode("utf-8"))

    def _decision_payload(decision: HookDecision) -> dict[str, object]:
        """Match Claude Code's hook contract (Spec §7).

        We only emit ``allow`` or ``deny`` — ``ASK_USER`` is collapsed
        to ``deny`` here because this endpoint returns synchronously in
        C3.1; the real async PIN round-trip lands in C3.3.
        """
        claude_decision = (
            decision.verdict.value if decision.verdict is not Verdict.ASK_USER else "deny"
        )
        return {
            "hookSpecificOutput": {
                "permissionDecision": claude_decision,
                "permissionDecisionReason": decision.reason,
            }
        }

    def _unauthorized() -> JSONResponse:
        # Silent: no log of the header value, no hint about *why* the
        # check failed. The bot logs it on its own side.
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=_decision_payload(deny("unauthorized")),
        )

    # ---- POST /hook/bash -----------------------------------------------
    @router.post("/hook/bash")
    async def bash_hook(
        request: Request,
        x_whatsbot_hook_secret: str | None = Header(default=None),
    ) -> JSONResponse:
        if not _verify_auth(x_whatsbot_hook_secret):
            log.warning("hook_bash_unauthorized", has_header=x_whatsbot_hook_secret is not None)
            return _unauthorized()

        try:
            body = await request.json()
        except Exception:
            log.warning("hook_bash_bad_json")
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=_decision_payload(deny("malformed request")),
            )

        command = body.get("command") if isinstance(body, dict) else None
        if not isinstance(command, str) or not command.strip():
            log.warning("hook_bash_no_command")
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=_decision_payload(deny("missing command")),
            )

        project = body.get("project") if isinstance(body, dict) else None
        session_id = body.get("session_id") if isinstance(body, dict) else None

        try:
            decision = await service.classify_bash(
                command=command,
                project=project if isinstance(project, str) else None,
                session_id=session_id if isinstance(session_id, str) else None,
            )
        except Exception as exc:
            log.error("hook_bash_service_error", error=str(exc))
            decision = deny("classifier error")

        return JSONResponse(status_code=status.HTTP_200_OK, content=_decision_payload(decision))

    # ---- POST /hook/write ----------------------------------------------
    @router.post("/hook/write")
    async def write_hook(
        request: Request,
        x_whatsbot_hook_secret: str | None = Header(default=None),
    ) -> JSONResponse:
        if not _verify_auth(x_whatsbot_hook_secret):
            log.warning("hook_write_unauthorized", has_header=x_whatsbot_hook_secret is not None)
            return _unauthorized()

        try:
            body = await request.json()
        except Exception:
            log.warning("hook_write_bad_json")
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=_decision_payload(deny("malformed request")),
            )

        path = body.get("path") if isinstance(body, dict) else None
        if not isinstance(path, str) or not path.strip():
            log.warning("hook_write_no_path")
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=_decision_payload(deny("missing path")),
            )

        project = body.get("project") if isinstance(body, dict) else None
        session_id = body.get("session_id") if isinstance(body, dict) else None

        try:
            decision = await service.classify_write(
                path=path,
                project=project if isinstance(project, str) else None,
                session_id=session_id if isinstance(session_id, str) else None,
            )
        except Exception as exc:
            log.error("hook_write_service_error", error=str(exc))
            decision = deny("classifier error")

        return JSONResponse(status_code=status.HTTP_200_OK, content=_decision_payload(decision))

    return router
