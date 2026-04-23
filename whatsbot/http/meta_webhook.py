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
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

import structlog
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse

from whatsbot.application.command_handler import CommandHandler
from whatsbot.application.confirmation_coordinator import ConfirmationCoordinator
from whatsbot.application.media_service import MediaService
from whatsbot.application.output_service import OutputService, ResolveOutcome
from whatsbot.config import Environment, Settings
from whatsbot.domain import whitelist
from whatsbot.domain.injection import detect_triggers
from whatsbot.domain.media import SUPPORTED_KINDS, MediaKind, classify_meta_kind
from whatsbot.logging_setup import get_logger
from whatsbot.ports.message_sender import MessageSender
from whatsbot.ports.secrets_provider import (
    KEY_ALLOWED_SENDERS,
    KEY_META_APP_SECRET,
    KEY_META_VERIFY_TOKEN,
    KEY_PANIC_PIN,
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


@dataclass(frozen=True, slots=True)
class MediaMessage:
    """One inbound WhatsApp non-text message.

    ``media_id`` is ``None`` for kinds that don't carry a downloadable
    blob (location, contact) — the webhook still surfaces them so the
    user gets a reject reply instead of a silent drop.
    ``mime`` comes from Meta and is validated defensively in the
    application layer.
    """

    sender: str
    kind: MediaKind
    msg_id: str
    media_id: str | None = None
    mime: str | None = None
    caption: str | None = None


def iter_media_messages(payload: dict[str, object]) -> Iterator[MediaMessage]:
    """Yield each non-text message from a Meta WhatsApp webhook payload.

    Mirrors the structure of :func:`iter_text_messages` but handles the
    full set of Phase-7 kinds. Unknown kinds yield with
    :attr:`MediaKind.UNKNOWN` so the dispatcher can reply with a
    friendly reject rather than crash on a new Meta message type.
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
                meta_type = message.get("type")
                if meta_type == "text":
                    continue  # handled by iter_text_messages
                sender = message.get("from")
                if not isinstance(sender, str):
                    continue
                kind = classify_meta_kind(
                    meta_type if isinstance(meta_type, str) else None
                )
                msg_id = str(message.get("id", ""))
                media_id, mime, caption = _extract_media_fields(message, kind)
                yield MediaMessage(
                    sender=sender,
                    kind=kind,
                    msg_id=msg_id,
                    media_id=media_id,
                    mime=mime,
                    caption=caption,
                )


def _extract_media_fields(
    message: dict[str, object], kind: MediaKind
) -> tuple[str | None, str | None, str | None]:
    """Pull ``(media_id, mime, caption)`` from the per-kind sub-object.

    Meta puts the actual media metadata under the type name:
    ``message["image"] = {"id": "...", "mime_type": "...", "caption": "..."}``.
    Missing fields → ``None`` at that position; the dispatcher handles
    the nulls gracefully.
    """
    # Kinds without a downloadable blob: nothing under the type name
    # we care about, or the shape is different (location, contacts).
    if kind in {MediaKind.LOCATION, MediaKind.CONTACT, MediaKind.UNKNOWN}:
        return None, None, None
    payload_key = {
        MediaKind.IMAGE: "image",
        MediaKind.AUDIO: "audio",
        MediaKind.DOCUMENT: "document",
        MediaKind.VIDEO: "video",
        MediaKind.STICKER: "sticker",
    }.get(kind)
    if payload_key is None:
        return None, None, None
    sub = message.get(payload_key)
    if not isinstance(sub, dict):
        return None, None, None
    raw_id = sub.get("id")
    raw_mime = sub.get("mime_type")
    raw_caption = sub.get("caption")
    media_id = raw_id if isinstance(raw_id, str) and raw_id else None
    mime = raw_mime if isinstance(raw_mime, str) and raw_mime else None
    caption = raw_caption if isinstance(raw_caption, str) and raw_caption else None
    return media_id, mime, caption


# --- Router factory ---------------------------------------------------------


def build_router(
    *,
    settings: Settings,
    secrets: SecretsProvider,
    sender: MessageSender,
    command_handler: CommandHandler,
    coordinator: ConfirmationCoordinator | None = None,
    output_service: OutputService | None = None,
    media_service: MediaService | None = None,
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
    # PIN used to resolve pending Hook confirmations via WhatsApp.
    # Missing PIN + coordinator wired = coordinator still accepts
    # "nein" rejections; acceptance is impossible, which is fail-closed.
    try:
        panic_pin = secrets.get(KEY_PANIC_PIN)
    except SecretNotFoundError:
        panic_pin = ""

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

                # Spec §9 injection telegraph detection — audit-only for
                # now. Phase 4 will actually wrap the text via
                # ``domain.injection.sanitize`` when forwarding to Claude;
                # today we just record the signal for forensic review.
                triggers = detect_triggers(msg.text)
                if triggers:
                    log.warning(
                        "injection_suspected",
                        triggers=list(triggers),
                        text_len=len(msg.text),
                    )

                # If there is a pending Hook confirmation waiting on the
                # user, intercept PIN / "nein" *before* routing to the
                # normal command dispatcher — otherwise the PIN text
                # would be swallowed as an unknown command.
                if coordinator is not None:
                    resolved = coordinator.try_resolve(msg.text, pin=panic_pin)
                    if resolved is not None:
                        reply = (
                            "✅ Freigabe erteilt."
                            if resolved.accepted
                            else "🚫 Abgelehnt."
                        )
                        log.info(
                            "hook_confirmation_resolved",
                            confirmation_id=resolved.confirmation_id,
                            accepted=resolved.accepted,
                        )
                        sender.send_text(to=msg.sender, body=reply)
                        continue

                # /send, /discard, /save act on the most recent
                # pending_outputs row. They're intercepted here (not
                # in CommandHandler) because the sender phone number
                # is known at this layer, and the resolution steps
                # need to actually ship or unlink content rather than
                # just return a string reply.
                if output_service is not None:
                    stripped = msg.text.strip()
                    if stripped in ("/send", "/discard", "/save"):
                        outcome = _resolve_pending_output(
                            stripped, to=msg.sender, service=output_service
                        )
                        sender.send_text(
                            to=msg.sender, body=_format_output_outcome(outcome, stripped)
                        )
                        log.info(
                            "pending_output_resolved",
                            action=stripped,
                            outcome=outcome.kind,
                            msg_id=outcome.msg_id,
                        )
                        continue

                result = command_handler.handle(msg.text)
                log.info(
                    "command_routed",
                    command=result.command,
                    reply_len=len(result.reply),
                )
                if output_service is not None:
                    output_service.deliver(to=msg.sender, body=result.reply)
                else:
                    sender.send_text(to=msg.sender, body=result.reply)
            finally:
                structlog.contextvars.unbind_contextvars("wa_msg_id", "sender")

        # --- non-text messages (images, pdf, audio, rejects) ---
        for media in iter_media_messages(payload):
            structlog.contextvars.bind_contextvars(
                wa_msg_id=media.msg_id, sender=media.sender
            )
            try:
                if not whitelist.is_allowed(media.sender, allowed):
                    log.warning("sender_not_allowed")
                    continue
                media_reply = _dispatch_media(media, media_service)
                if media_reply is not None:
                    sender.send_text(to=media.sender, body=media_reply)
            finally:
                structlog.contextvars.unbind_contextvars("wa_msg_id", "sender")

        return Response(status_code=status.HTTP_200_OK)

    return router


def _resolve_pending_output(
    action: str, *, to: str, service: OutputService
) -> ResolveOutcome:
    if action == "/send":
        return service.resolve_send(to=to)
    if action == "/discard":
        return service.resolve_discard(to=to)
    if action == "/save":
        return service.resolve_save(to=to)
    # Not reachable — caller validates.
    raise ValueError(f"unknown output action: {action!r}")


def _format_output_outcome(outcome: ResolveOutcome, action: str) -> str:
    if outcome.kind == "none":
        return "Kein wartender Output."
    if outcome.kind == "missing":
        return "⚠️ Gespeicherter Output nicht mehr auf der Platte."
    if outcome.kind == "sent":
        chunks = outcome.chunks_sent or 1
        return f"✅ Gesendet ({outcome.size_bytes} bytes, {chunks} chunk(s))."
    if outcome.kind == "discarded":
        return "🗑 Verworfen."
    if outcome.kind == "saved":
        return "💾 Gespeichert (nur auf der Platte)."
    # Defensive — future-proof if ResolveKind grows.
    return f"OK ({action}: {outcome.kind})"


def _dispatch_media(
    media: MediaMessage, service: MediaService | None
) -> str | None:
    """Route one inbound media message to the :class:`MediaService`.

    Returns the user-facing reply text, or ``None`` if the message was
    handled silently (shouldn't happen for C7.1 — every kind produces
    a reply). Unsupported kinds fall through to ``process_unsupported``
    even when ``service`` is ``None`` because those replies are cheap
    and we want to tell the sender regardless.
    """
    # Unsupported kinds never need a service — we answer from a static
    # map. Route them here so a misconfigured bot still replies.
    if media.kind not in SUPPORTED_KINDS:
        if service is None:
            return _STATIC_UNSUPPORTED_REPLY.get(
                media.kind, _STATIC_UNSUPPORTED_REPLY[MediaKind.UNKNOWN]
            )
        return service.process_unsupported(media.kind).reply

    # Supported kinds need a real service + a downloadable media_id.
    if service is None:
        return "⚠️ Medien werden gerade nicht angenommen."
    if media.media_id is None:
        return "⚠️ Medien-ID fehlt, bitte noch einmal schicken."

    if media.kind is MediaKind.IMAGE:
        outcome = service.process_image(
            media_id=media.media_id,
            caption=media.caption,
            sender=media.sender,
        )
        return outcome.reply
    if media.kind is MediaKind.DOCUMENT:
        outcome = service.process_pdf(
            media_id=media.media_id,
            caption=media.caption,
            sender=media.sender,
        )
        return outcome.reply
    # AUDIO — fully wired in C7.4; until then we treat it as a reject
    # so the user isn't left wondering whether anything happened.
    return service.process_unsupported(media.kind).reply


_STATIC_UNSUPPORTED_REPLY: Final[dict[MediaKind, str]] = {
    MediaKind.VIDEO: "🎬 Video wird nicht unterstützt, bitte Screenshot.",
    MediaKind.LOCATION: "📍 Location-Pins werden ignoriert.",
    MediaKind.STICKER: "😄 Nice sticker, aber ich brauche Text/Voice.",
    MediaKind.CONTACT: "📇 Kontaktkarten werden ignoriert.",
    MediaKind.UNKNOWN: "⚠️ Dieser Nachrichtentyp wird nicht unterstützt.",
}
