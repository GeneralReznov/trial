from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

_LEGACY_BODY_PATHS = {
    "/debate/start",
    "/debate/speech",
    "/debate/poi",
    "/debate/end",
    "/gd/start",
    "/gd/message",
    "/gd/end",
    "/interview/start",
    "/interview/answer",
    "/interview/next",
    "/interview/end",
}


class _BodyLimitExceeded(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Reject oversized API request bodies while ASGI receives their chunks."""

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        json_limit: int,
        multipart_limit: int,
    ) -> None:
        self.app = app
        self.json_limit = json_limit
        self.multipart_limit = multipart_limit

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Awaitable[dict[str, Any]]], send: Callable[..., Awaitable[None]]) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        if method in {"GET", "HEAD", "OPTIONS"} or (
            not path.startswith("/api/") and path not in _LEGACY_BODY_PATHS
        ):
            await self.app(scope, receive, send)
            return

        headers = {
            key.lower(): value
            for key, value in scope.get("headers", [])
        }
        content_type = headers.get(b"content-type", b"").lower()
        is_multipart = content_type.startswith(b"multipart/")
        limit = self.multipart_limit if is_multipart else self.json_limit
        too_large_detail = (
            b"Uploaded request is too large."
            if is_multipart
            else b"Request body is too large."
        )

        declared_length = headers.get(b"content-length")
        if declared_length:
            try:
                if int(declared_length) > limit:
                    await self._send_too_large(scope, send, too_large_detail)
                    return
            except ValueError:
                # Let the ASGI server/framework report malformed lengths.
                pass

        received = 0
        overflowed = False

        async def receive_limited() -> dict[str, Any]:
            nonlocal received, overflowed
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    overflowed = True
                    raise _BodyLimitExceeded
            return message

        response_started = False

        async def observe_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if overflowed:
                # Some middleware converts request-stream exceptions into a
                # 400 response. Hold that response so this outer layer can
                # consistently return the intended 413 instead.
                return
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive_limited, observe_send)
        except Exception:
            if not overflowed:
                raise
            if response_started:
                logger.warning("Request body exceeded its limit after response started: %s", path)
                return
            await self._send_too_large(scope, send, too_large_detail)
            return
        if overflowed:
            if response_started:
                logger.warning("Request body exceeded its limit after response started: %s", path)
                return
            await self._send_too_large(scope, send, too_large_detail)

    @staticmethod
    async def _send_too_large(
        scope: dict[str, Any],
        send: Callable[..., Awaitable[None]],
        detail: bytes,
    ) -> None:
        body = json.dumps({"detail": detail.decode("ascii")}).encode("ascii")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        if scope.get("http_version") in {"1.0", "1.1"}:
            headers.append((b"connection", b"close"))
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})