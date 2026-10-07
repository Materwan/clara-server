"""One HTTP client kept open per service the server talks to (a model provider, GitHub, Google, ollama.com's web
API): its connections, and their TLS sessions, are reused from one request to the next instead of being opened
again every time. The server closes them when it stops (server.py)."""

from __future__ import annotations

from typing import Any

import httpx


class SharedClient:
    """An `httpx.AsyncClient` made on first use with `options`, and made again if it was closed."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, **options: Any):
        self.transport = transport  # tests answer from here instead of the network (set it before the first use)
        self._options = options
        self._client: httpx.AsyncClient | None = None

    def get(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(transport=self.transport, **self._options)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
