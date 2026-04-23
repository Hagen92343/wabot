"""C7.1 — MetaMediaDownloader httpx adapter tests."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

import httpx
import pytest

from whatsbot.adapters.meta_media_downloader import MetaMediaDownloader
from whatsbot.ports.media_downloader import MediaDownloadError

HttpHandler = Callable[[httpx.Request], httpx.Response]


def _mock_transport(handler: HttpHandler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _make_downloader(
    handler: HttpHandler,
    *,
    access_token: str = "t-abc",
) -> MetaMediaDownloader:
    transport = _mock_transport(handler)
    client = httpx.Client(transport=transport)
    return MetaMediaDownloader(access_token=access_token, client=client)


def test_download_two_step_success() -> None:
    payload = b"\xff\xd8\xff\xe0some-jpeg-bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        # Bearer header must be present on both calls.
        assert request.headers["authorization"] == "Bearer t-abc"
        if request.url.path.endswith("/12345"):
            return httpx.Response(
                200,
                json={
                    "url": "https://cdn.meta.test/blob/xyz",
                    "mime_type": "image/jpeg",
                    "file_size": str(len(payload)),
                },
            )
        if "cdn.meta.test" in request.url.host:
            return httpx.Response(
                200,
                content=payload,
                headers={"content-type": "image/jpeg"},
            )
        return httpx.Response(500)

    downloader = _make_downloader(handler)
    result = downloader.download("12345")

    assert result.payload == payload
    assert result.mime == "image/jpeg"
    assert result.sha256 == hashlib.sha256(payload).hexdigest()


def test_download_prefers_graph_mime_over_content_type() -> None:
    payload = b"%PDF-1.4\nfoo"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/12345"):
            return httpx.Response(
                200,
                json={
                    "url": "https://cdn.meta.test/blob",
                    "mime_type": "application/pdf",
                },
            )
        return httpx.Response(
            200,
            content=payload,
            headers={"content-type": "application/octet-stream"},
        )

    downloader = _make_downloader(handler)
    result = downloader.download("12345")
    assert result.mime == "application/pdf"


def test_download_falls_back_to_content_type_when_graph_missing_mime() -> None:
    payload = b"OggS\x00"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/12345"):
            return httpx.Response(
                200,
                json={"url": "https://cdn.meta.test/blob"},
            )
        return httpx.Response(
            200,
            content=payload,
            headers={"content-type": "audio/ogg"},
        )

    downloader = _make_downloader(handler)
    result = downloader.download("12345")
    assert result.mime == "audio/ogg"


def test_download_rejects_non_string_media_id() -> None:
    downloader = _make_downloader(lambda req: httpx.Response(200))
    with pytest.raises(MediaDownloadError):
        downloader.download("")
    with pytest.raises(MediaDownloadError):
        downloader.download("   ")


def test_download_4xx_is_permanent_error() -> None:
    call_counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_counter["n"] += 1
        return httpx.Response(404)

    downloader = _make_downloader(handler)
    with pytest.raises(MediaDownloadError) as exc_info:
        downloader.download("missing")
    assert "404" in str(exc_info.value)
    # No retry on 4xx — single call.
    assert call_counter["n"] == 1


def test_download_5xx_triggers_retries() -> None:
    call_counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_counter["n"] += 1
        return httpx.Response(503)

    downloader = _make_downloader(handler)
    with pytest.raises(MediaDownloadError):
        downloader.download("flaky")
    # tenacity: 3 attempts
    assert call_counter["n"] == 3


def test_download_retry_recovers() -> None:
    state = {"calls": 0}
    payload = b"%PDF-1.4\n"

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(502)
        if request.url.path.endswith("/id1"):
            return httpx.Response(
                200,
                json={"url": "https://cdn.meta.test/b", "mime_type": "application/pdf"},
            )
        return httpx.Response(
            200, content=payload, headers={"content-type": "application/pdf"}
        )

    downloader = _make_downloader(handler)
    result = downloader.download("id1")
    assert result.mime == "application/pdf"
    # first Graph call failed, retried, then the bytes call succeeded
    assert state["calls"] >= 3


def test_download_graph_response_missing_url_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"mime_type": "image/png"})

    downloader = _make_downloader(handler)
    with pytest.raises(MediaDownloadError, match="url"):
        downloader.download("broken")


def test_download_empty_payload_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/id1"):
            return httpx.Response(
                200,
                json={"url": "https://cdn.meta.test/x", "mime_type": "image/png"},
            )
        return httpx.Response(
            200, content=b"", headers={"content-type": "image/png"}
        )

    downloader = _make_downloader(handler)
    with pytest.raises(MediaDownloadError, match="leeren"):
        downloader.download("id1")


def test_constructor_requires_access_token() -> None:
    with pytest.raises(ValueError):
        MetaMediaDownloader(access_token="")
