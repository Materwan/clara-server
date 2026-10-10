"""A request body has a size limit, whatever way it comes (a Content-Length, or chunks with none).

Without it a body is read whole into memory before anything looks at it, so one account could make the server
swallow gigabytes. The routes that take files have a larger limit than the others.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

MEGABYTE = 1_000_000
DEFAULT_LIMIT = 16 * MEGABYTE
# (path pattern, bytes): the first that matches decides
LIMITS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"^/v1/projects/\d+/files$"), 90 * MEGABYTE),  # a request of files (base64): see projectapi.MAX_UPLOAD_BYTES
    (re.compile(r"^/v1/documents/extract$"), 31 * MEGABYTE),  # a PDF: see webapi.MAX_PDF_BYTES
    (re.compile(r"^/v1/documents/docx$"), 31 * MEGABYTE),  # a Word document: see webapi.MAX_DOCX_BYTES
    (re.compile(r"^/v1/turns/[^/]+/tool-results$"), 64 * MEGABYTE),  # what a client's tools read
    (re.compile(r"^/v1/chat(/stream)?$"), 48 * MEGABYTE),  # a message's files, in base64: see attachments.py
)


def limit_for(path: str) -> int:
    for pattern, size in LIMITS:
        if pattern.match(path):
            return size
    return DEFAULT_LIMIT


class BodyLimitMiddleware:
    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable[[dict], Awaitable[None]]) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS", "DELETE"):
            await self.app(scope, receive, send)
            return
        limit = limit_for(scope["path"])
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                size = int(declared)
            except ValueError:
                await self._refuse(send, 400, "Bad Content-Length")
                return
            if size > limit:
                await self._refuse(send, 413, f"This request is too big: at most {limit // MEGABYTE} MB.")
                return
        seen = 0
        started = refused = False

        async def counted() -> dict:
            nonlocal seen, refused
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit and not refused:
                    # Answered here, at once: the framework would turn an error raised from here into a 400
                    refused = True
                    if not started:
                        await self._refuse(send, 413, f"This request is too big: at most {limit // MEGABYTE} MB.")
                    return {"type": "http.disconnect"}
            return message

        async def watched(message: dict) -> None:
            nonlocal started
            if refused:
                return  # the request was refused: whatever the application says now is not for the client
            started = started or message["type"] == "http.response.start"
            await send(message)

        await self.app(scope, counted, watched)

    @staticmethod
    async def _refuse(send: Callable[[dict], Awaitable[None]], status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})
