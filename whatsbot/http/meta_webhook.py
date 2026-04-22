"""Meta WhatsApp webhook — signature check, subscribe challenge, payload parse.

Reference: https://developers.facebook.com/docs/graph-api/webhooks/getting-started

Two endpoints share the same path ``/webhook``:

* ``GET``: subscribe-challenge handshake. Meta queries with
  ``hub.mode=subscribe&hub.verify_token=<X>&hub.challenge=<Y>``. We echo
  ``hub.challenge`` as plain text iff the token matches the one in Keychain.

* ``POST``: incoming messages. We
  1. verify the ``X-Hub-Signature-256`` HMAC against the raw body
     (constant-time compare),
  2. drop unknown senders silently (200 OK + WARN log) so an attacker
     can't enumerate the whitelist,
  3. extract text messages and route each through ``domain.commands.route``,
  4. dispatch the reply via the injected ``MessageSender``.

Signature verification and subscribe-challenge logic live as pure module-level
functions so they can be unit-tested without a FastAPI ``TestClient``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final

import structlog
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse

from whatsbot.config import Environment, Settings
from whatsbot.domain import commands, whitelist
from whatsbot.logging_setup import get_logger
from whatsbot.ports.message_sender import MessageSender
from whatsbot.ports.secrets_provider import (
    KEY_ALLOWED_SENDERS,
    KEY_META_APP_SECRET,
    KEY_META_VERIFY_TOKEN,
    SecretNotFoundError,
    SecretsProvider,
)

SIGNATURE_HEADER: Final = "X-Hub-Signature-256"
SIGNATURE_PREFIX: Final = "sha256="


# --- Pure helpers (unit-testable without FastAPI) ---------------------------


def verify_signature(raw_body: bytes, header_value: str | None, app_secret: str) -> bool:
    """Constant-time HMAC-SHA256 check against the raw request body.

    Returns ``False`` for any malformed or missing signature so callers can
    silently drop the request without leaking which check failed.
    """
    if header_value is None or not header_value.startswith(SIGNATURE_PREFIX):
        return False
    sent = header_value[len(SIGNATURE_PREFIX) :].strip()
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sent, expected)


def check_subscribe_challenge(
    mode: str | None,
    token: str | None,
    challenge: str | None,
    expected_token: str,
) -> str | None:
    """Return ``challenge`` iff Meta's subscribe-handshake parameters match.

    Returns ``None`` for any mismatch so the caller can return 403.
    """
    if mode != "subscribe" or token is None or challenge is None:
        return None
    if not hmac.compare_digest(token, expected_token):
        return None
    return challenge


@dataclass(frozen=True, slots=True)
class TextMessage:
    sender: str
    text: str
    msg_id: str


def iter_text_messages(payload: dict[str, object]) -> Iterator[TextMessage]:
    """Yield each text message from a Meta WhatsApp webhook payload.

    Robust against missing / unexpected keys: we silently skip anything that
    isn't a text message rather than raising — Meta will retry malformed
    deliveries on its own and we want to fail closed but quiet.
    """
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes", []) or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            for message in value.get("messages", []) or []:
                if not isinstance(message, dict):
                    continue
                if message.get("type") != "text":
                    continue
                text_obj = message.get("text")
                if not isinstance(text_obj, dict):
                    continue
                body = text_obj.get("body")
                sender = message.get("from")
                msg_id = message.get("id", "")
                if not isinstance(body, str) or not isinstance(sender, str):
                    continue
                yield TextMessage(sender=sender, text=body, msg_id=str(msg_id))


# --- Router factory ---------------------------------------------------------


def build_router(
    *,
    settings: Settings,
    secrets: SecretsProvider,
    sender: MessageSender,
    started_at_monotonic: float,
    version: str,
    db_ok_callback: Callable[[], bool] | None = None,
) -> APIRouter:
    """Construct the ``/webhook`` APIRouter with the dependencies wired in.

    Why a factory: tests need to inject mock secrets/sender; the LaunchAgent
    needs the real Keychain + WhatsApp adapter. FastAPI's Depends() would
    work too but a closure is simpler at this stage.
    """
    log = get_logger("whatsbot.webhook")
    router = APIRouter()

    # Resolve the allowed-senders / verify-token snapshots once at router-build
    # time. Both are configured at install via `make setup-secrets` and are
    # stable for the bot's lifetime; rotation requires a process restart.
    try:
        allowed = whitelist.parse_whitelist(secrets.get(KEY_ALLOWED_SENDERS))
    except SecretNotFoundError:
        allowed = frozenset()
    try:
        verify_token = secrets.get(KEY_META_VERIFY_TOKEN)
    except SecretNotFoundError:
        verify_token = ""
    try:
        app_secret = secrets.get(KEY_META_APP_SECRET)
    except SecretNotFoundError:
        app_secret = ""

    # Signature check is skipped only when we have no app secret AND we're in
    # a non-production env. In prod the secrets gate would have aborted
    # startup if the secret were missing, so app_secret is guaranteed here.
    skip_sig_check = settings.env is not Environment.PROD and not app_secret

    # ---- GET: subscribe challenge ----
    @router.get("/webhook", response_class=PlainTextResponse)
    async def subscribe_handshake(request: Request) -> str:
        params = request.query_params
        challenge = check_subscribe_challenge(
            mode=params.get("hub.mode"),
            token=params.get("hub.verify_token"),
            challenge=params.get("hub.challenge"),
            expected_token=verify_token,
        )
        if challenge is None:
            log.warning(
                "subscribe_challenge_rejected",
                mode=params.get("hub.mode"),
                has_token=bool(params.get("hub.verify_token")),
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        log.info("subscribe_challenge_ok")
        return challenge

    # ---- POST: incoming messages ----
    @router.post("/webhook")
    async def receive(request: Request) -> Response:
        raw = await request.body()

        # Signature gate. In dev with no app-secret configured we accept the
        # request so `tests/send_fixture.sh` works pre-`make setup-secrets`.
        if not skip_sig_check:
            if not verify_signature(raw, request.headers.get(SIGNATURE_HEADER), app_secret):
                log.warning(
                    "signature_invalid",
                    has_header=request.headers.get(SIGNATURE_HEADER) is not None,
                    body_len=len(raw),
                )
                # 200 + silent drop — never tell the attacker the check failed.
                return Response(status_code=status.HTTP_200_OK)
        else:
            log.warning("signature_check_skipped_dev_mode")

        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            log.warning("payload_not_json", body_len=len(raw))
            return Response(status_code=status.HTTP_200_OK)
        if not isinstance(payload, dict):
            log.warning("payload_not_object")
            return Response(status_code=status.HTTP_200_OK)

        for msg in iter_text_messages(payload):
            structlog.contextvars.bind_contextvars(wa_msg_id=msg.msg_id, sender=msg.sender)
            try:
                if not whitelist.is_allowed(msg.sender, allowed):
                    log.warning("sender_not_allowed")
                    continue

                snapshot = commands.StatusSnapshot(
                    version=version,
                    uptime_seconds=time.monotonic() - started_at_monotonic,
                    db_ok=(db_ok_callback() if db_ok_callback is not None else True),
                    env=settings.env.value,
                )
                result = commands.route(msg.text, snapshot)
                log.info(
                    "command_routed",
                    command=result.command,
                    reply_len=len(result.reply),
                )
                sender.send_text(to=msg.sender, body=result.reply)
            finally:
                structlog.contextvars.unbind_contextvars("wa_msg_id", "sender")

        return Response(status_code=status.HTTP_200_OK)

    return router
