"""Meta Graph media-download adapter — two-step Bearer-auth fetch.

Flow (Meta Graph v23.0, stable contract used across WhatsApp Cloud API):

1. ``GET https://graph.facebook.com/<ver>/<media_id>`` with
   ``Authorization: Bearer <access-token>`` → JSON with ``url``,
   ``mime_type``, ``file_size``.
2. ``GET <url>`` with the same Bearer → raw media bytes + Content-Type
   (we prefer the Graph-reported MIME from step 1 but fall back to
   ``Content-Type`` if missing).

Retries are driven by tenacity — 3 attempts, exponential backoff, only
on network-level failures and 5xx. 4xx responses short-circuit because
retrying a malformed/auth-failed request won't help.

Timeouts: 5 s connect, 30 s read — a 20 MB PDF on a mobile link still
lands comfortably under 30 s; larger should be rejected upstream by
:func:`whatsbot.domain.media.validate_size` before we even call here.
"""

from __future__ import annotations

import hashlib
from typing import Final

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from whatsbot.logging_setup import get_logger
from whatsbot.ports.media_downloader import (
    DownloadedMedia,
    MediaDownloadError,
)

DEFAULT_GRAPH_API_VERSION: Final[str] = "v23.0"
DEFAULT_GRAPH_BASE_URL: Final[str] = "https://graph.facebook.com"
DEFAULT_CONNECT_TIMEOUT: Final[float] = 5.0
DEFAULT_READ_TIMEOUT: Final[float] = 30.0


class _RetryableDownloadError(Exception):
    """Internal marker so tenacity retries network failures + 5xx but
    not permanent 4xx. Never escapes the adapter."""


class MetaMediaDownloader:
    """Implements :class:`~whatsbot.ports.media_downloader.MediaDownloader`.

    Constructed once at startup with the long-lived access token and
    shared by all media kinds.
    """

    def __init__(
        self,
        *,
        access_token: str,
        graph_base_url: str = DEFAULT_GRAPH_BASE_URL,
        api_version: str = DEFAULT_GRAPH_API_VERSION,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        if not access_token:
            raise ValueError("access_token must be non-empty")
        self._access_token = access_token
        self._graph_base_url = graph_base_url.rstrip("/")
        self._api_version = api_version
        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=read_timeout,
            pool=connect_timeout,
        )
        # Tests inject their own client. In prod we build a short-lived
        # one per request so connection state can't leak across errors.
        self._client = client
        self._log = get_logger("whatsbot.media_downloader")

    def download(self, media_id: str) -> DownloadedMedia:
        if not media_id or not media_id.strip():
            raise MediaDownloadError("media_id leer")
        try:
            return self._download_with_retry(media_id)
        except _RetryableDownloadError as exc:
            raise MediaDownloadError(str(exc)) from exc

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=16),
        retry=retry_if_exception_type(_RetryableDownloadError),
    )
    def _download_with_retry(self, media_id: str) -> DownloadedMedia:
        client = self._client or httpx.Client(timeout=self._timeout)
        should_close = self._client is None
        try:
            metadata = self._fetch_metadata(client, media_id)
            payload, content_type = self._fetch_bytes(client, metadata["url"])
        finally:
            if should_close:
                client.close()
        mime = metadata.get("mime_type") or content_type or ""
        if not mime:
            raise MediaDownloadError("Meta lieferte keinen MIME-Type")
        if not payload:
            raise MediaDownloadError("Meta lieferte leeren Payload")
        sha = hashlib.sha256(payload).hexdigest()
        self._log.info(
            "media_downloaded",
            media_id=media_id,
            mime=mime,
            size_bytes=len(payload),
            sha256=sha,
        )
        return DownloadedMedia(payload=payload, mime=mime, sha256=sha)

    # ---- inner helpers ----------------------------------------------

    def _fetch_metadata(self, client: httpx.Client, media_id: str) -> dict[str, str]:
        url = f"{self._graph_base_url}/{self._api_version}/{media_id}"
        try:
            response = client.get(url, headers=self._auth_headers())
        except httpx.TransportError as exc:
            raise _RetryableDownloadError(f"Graph transport error: {exc}") from exc

        self._raise_if_error(response, stage="metadata")

        try:
            body = response.json()
        except ValueError as exc:
            raise MediaDownloadError(
                "Graph-Metadata-Response ist kein JSON"
            ) from exc
        if not isinstance(body, dict):
            raise MediaDownloadError("Graph-Metadata-Response ist kein Objekt")
        url_field = body.get("url")
        if not isinstance(url_field, str) or not url_field.strip():
            raise MediaDownloadError("Graph-Metadata ohne 'url'-Feld")
        return {
            "url": url_field,
            "mime_type": str(body.get("mime_type") or ""),
            "file_size": str(body.get("file_size") or ""),
        }

    def _fetch_bytes(
        self, client: httpx.Client, media_url: str
    ) -> tuple[bytes, str]:
        try:
            response = client.get(media_url, headers=self._auth_headers())
        except httpx.TransportError as exc:
            raise _RetryableDownloadError(f"Media transport error: {exc}") from exc

        self._raise_if_error(response, stage="bytes")
        content_type = str(response.headers.get("content-type") or "")
        # Content-Type may come with ';' parameters; the caller normalises.
        return response.content, content_type

    def _raise_if_error(self, response: httpx.Response, *, stage: str) -> None:
        if 200 <= response.status_code < 300:
            return
        if 500 <= response.status_code < 600:
            raise _RetryableDownloadError(
                f"{stage} returned HTTP {response.status_code}"
            )
        # 4xx — not retryable (auth failed, media gone, etc.)
        raise MediaDownloadError(
            f"{stage} fehlgeschlagen: HTTP {response.status_code}"
        )

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}
