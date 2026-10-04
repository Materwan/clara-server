"""The language model behind a small interface.

The agent only knows `LlmBackend`; `OllamaBackend` is the one implementation.
Where Ollama runs (this machine, or ollama.com with an API key) is decided
one level up, in `providers.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

import httpx
from ollama import AsyncClient


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class LlmChunk:
    """One piece of a streamed answer: some text, the model's reasoning (`thinking`, for the models that
    show it apart), tool calls, or the final token counts."""

    text: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LlmBackend(Protocol):
    model: str

    def stream(
        self, messages: list[dict], tools: list[dict] | None
    ) -> AsyncIterator[LlmChunk]: ...

    async def list_models(self) -> list[str]: ...

    async def verify(self) -> None:
        """Raise if the provider cannot be used (unreachable, credentials refused)."""
        ...


class OllamaBackend:
    def __init__(
        self,
        model: str,
        host: str | None = None,
        api_key: str | None = None,
        client: Any = None,
        num_ctx: int | None = None,
    ):
        self.model = model
        self._options = {"num_ctx": num_ctx} if num_ctx else None  # Ollama's own default is tiny
        self._host = (host or "").rstrip("/")
        self._api_key = api_key
        if client is None:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
            client = AsyncClient(host=host, headers=headers)
        self._client = client

    async def stream(
        self, messages: list[dict], tools: list[dict] | None
    ) -> AsyncIterator[LlmChunk]:
        response = await self._client.chat(
            model=self.model,
            messages=messages,
            tools=tools or None,
            stream=True,
            options=self._options,
        )
        async for part in response:
            message = part.message
            yield LlmChunk(
                text=message.content or "",
                thinking=getattr(message, "thinking", None) or "",
                tool_calls=[
                    ToolCall(call.function.name, dict(call.function.arguments or {}))
                    for call in message.tool_calls or []
                ],
                prompt_tokens=part.prompt_eval_count or 0,
                completion_tokens=part.eval_count or 0,
            )

    async def list_models(self) -> list[str]:
        """Models this server offers (installed ones locally, the catalogue on ollama.com)."""
        response = await self._client.list()
        return sorted(model.model for model in response.models if model.model)

    async def verify(self) -> None:
        # Listing models is public on ollama.com, so it proves nothing about the key:
        # /api/me answers 401 to a bad key. Anything else (404...) falls through to the listing.
        if self._api_key and self._host:
            async with httpx.AsyncClient(timeout=8.0) as http:
                response = await http.post(
                    f"{self._host}/api/me", headers={"Authorization": f"Bearer {self._api_key}"}
                )
            if response.status_code in (401, 403):
                raise PermissionError(f"the API key was rejected by {self._host}")
        await self.list_models()
