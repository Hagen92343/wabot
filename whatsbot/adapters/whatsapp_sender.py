"""Outbound WhatsApp adapters.

``LoggingMessageSender``
    Used in dev/test and as a fallback when Meta credentials are missing.
    Writes a structured log line and does not contact the network. Lets us
    iterate on the bot before WhatsApp Business approval is in place.

``WhatsAppCloudSender``
    POSTs to Meta Graph ``/<phone_number_id>/messages`` with a Bearer
    token. Retries on network errors + 5xx via tenacity (3 attempts,
    exponential backoff). 4xx short-circuits immediately — a
    malformed/auth-failed request does not get better on retry.

Both implementations share the ``@resilient(META_SEND_SERVICE)``
decorator so a sustained Meta outage trips the module-level circuit
breaker (Spec §25 FMEA #1). The three tenacity retries count as ONE
breaker failure, matching the MetaMediaDownloader semantics.
"""

from __future__ import annotations

from typing import Final

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from whatsbot.adapters.resilience import resilient
from whatsbot.logging_setup import get_logger
from whatsbot.ports.message_sender import MessageSender, MessageSendError

# Service-name registered in the circuit-breaker registry.
# Kept in module scope so tests and /status can introspect via
# :func:`resilience.get_breaker`.
META_SEND_SERVICE: Final[str] = "meta_send"

DEFAULT_GRAPH_API_VERSION: Final[str] = "v23.0"
DEFAULT_GRAPH_BASE_URL: Final[str] = "https://graph.facebook.com"
DEFAULT_CONNECT_TIMEOUT: Final[float] = 5.0
DEFAULT_READ_TIMEOUT: Final[float] = 30.0


class _RetryableSendError(Exception):
    """Internal marker so tenacity retries network failures + 5xx but
    not permanent 4xx. Never escapes the adapter."""


class LoggingMessageSender:
    """``MessageSender`` that logs the would-be send instead of calling Meta.

    Mirrors the interface of the real adapter so swapping it in
    ``create_app()`` is a single dependency change.
    """

    def __init__(self) -> None:
        self._log = get_logger("whatsbot.sender.logging")

    def send_text(self, *, to: str, body: str) -> None:
        self._log.info(
            "outbound_message_dev",
            to=to,
            body_preview=body[:200],
            body_len=len(body),
        )


class WhatsAppCloudSender:
    """Implements :class:`~whatsbot.ports.message_sender.MessageSender`
    against Meta Graph ``/<phone_number_id>/messages``.

    Constructed once at startup with the long-lived access token and
    shared by all outbound paths (command replies, hook prompts, kill
    notifications, …). Thread-safe for sequential callers; the per-call
    ``httpx.Client`` is short-lived to prevent connection-state leak
    across errors.
    """

    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        graph_base_url: str = DEFAULT_GRAPH_BASE_URL,
        api_version: str = DEFAULT_GRAPH_API_VERSION,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        if not access_token:
            raise ValueError("access_token must be non-empty")
        if not phone_number_id:
            raise ValueError("phone_number_id must be non-empty")
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self._graph_base_url = graph_base_url.rstrip("/")
        self._api_version = api_version
        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=read_timeout,
            pool=connect_timeout,
        )
        # Tests inject their own client (MockTransport). In prod we
        # build a short-lived one per request so connection state
        # cannot leak across errors.
        self._client = client
        self._log = get_logger("whatsbot.sender.whatsapp")

    @resilient(META_SEND_SERVICE)
    def send_text(self, *, to: str, body: str) -> None:
        # tenacity retries happen *inside* one @resilient call so three
        # HTTP attempts count as ONE breaker failure. Matches the
        # MetaMediaDownloader invariant (see its comment, lines 92-95).
        recipient = to.lstrip("+").strip()
        if not recipient:
            raise MessageSendError("recipient leer")
        try:
            self._send_with_retry(recipient, body)
        except _RetryableSendError as exc:
            raise MessageSendError(str(exc)) from exc

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=16),
        retry=retry_if_exception_type(_RetryableSendError),
    )
    def _send_with_retry(self, recipient: str, body: str) -> None:
        client = self._client or httpx.Client(timeout=self._timeout)
        should_close = self._client is None
        try:
            message_id = self._post_text(client, recipient, body)
        finally:
            if should_close:
                client.close()
        self._log.info(
            "outbound_message_sent",
            to_tail4=recipient[-4:] if len(recipient) >= 4 else recipient,
            body_len=len(body),
            message_id=message_id,
        )

    # ---- inner helpers --------------------------------------------------

    def _post_text(
        self, client: httpx.Client, recipient: str, body: str
    ) -> str:
        url = (
            f"{self._graph_base_url}/{self._api_version}/"
            f"{self._phone_number_id}/messages"
        )
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }
        try:
            response = client.post(
                url, headers=self._auth_headers(), json=payload
            )
        except httpx.TransportError as exc:
            raise _RetryableSendError(f"Graph transport error: {exc}") from exc

        self._raise_if_error(response)
        return self._extract_message_id(response)

    def _raise_if_error(self, response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        if 500 <= response.status_code < 600:
            raise _RetryableSendError(f"send returned HTTP {response.status_code}")
        # 4xx — not retryable (auth failed, invalid recipient, etc.).
        # We log status-code only, not body — Meta 4xx bodies sometimes
        # echo the input and we would rather not re-log the user text.
        self._log.warning(
            "outbound_message_failed",
            status_code=response.status_code,
        )
        raise MessageSendError(f"send fehlgeschlagen: HTTP {response.status_code}")

    @staticmethod
    def _extract_message_id(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return ""
        if not isinstance(body, dict):
            return ""
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return ""
        first = messages[0]
        if not isinstance(first, dict):
            return ""
        message_id = first.get("id")
        return message_id if isinstance(message_id, str) else ""

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }


# Re-export the protocol so callers don't need two imports.
__all__ = [
    "LoggingMessageSender",
    "MessageSender",
    "META_SEND_SERVICE",
    "WhatsAppCloudSender",
]
