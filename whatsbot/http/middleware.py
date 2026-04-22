"""HTTP middleware: correlation IDs and constant-time response padding.

``CorrelationIdMiddleware`` (Spec §15)
    Tags every request with a freshly generated ULID, binds it into the
    structlog ``contextvars`` so every log line emitted while handling that
    request inherits the ``msg_id`` field. Also surfaces the ID via the
    ``X-Correlation-Id`` response header so curl/clients can quote it back
    when asking for support.

``ConstantTimeMiddleware`` (Spec §5)
    Pads responses to a minimum wall-clock duration on a configurable set of
    paths (default ``/webhook``). Without padding the bot would leak its
    sender-whitelist via timing differences — accepted requests run through
    the full handler while rejected ones return immediately, which is
    enumerable from the public side of the Cloudflare tunnel.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp
from ulid import ULID


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Tag every request with a ULID + bind into structlog contextvars."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        msg_id = str(ULID())
        token = structlog.contextvars.bind_contextvars(msg_id=msg_id)
        try:
            response = await call_next(request)
        finally:
            # ``bind_contextvars`` returns the previous tokens so we can
            # restore them, leaving no contamination between requests.
            structlog.contextvars.reset_contextvars(**token)
        response.headers["X-Correlation-Id"] = msg_id
        return response


class ConstantTimeMiddleware(BaseHTTPMiddleware):
    """Pad responses to ``min_duration_ms`` on the configured paths.

    ``paths`` is a tuple of path *prefixes*. Empty = pad every request.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        min_duration_ms: int = 200,
        paths: tuple[str, ...] = (),
    ) -> None:
        super().__init__(app)
        self._min_seconds = min_duration_ms / 1000.0
        self._paths = paths

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if self._paths and not any(request.url.path.startswith(p) for p in self._paths):
            return await call_next(request)
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        remaining = self._min_seconds - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)
        return response
