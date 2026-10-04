"""The language model behind a small interface.

The agent only knows `LlmBackend`. Two implementations: `OllamaBackend` (this machine, or ollama.com with an API
key) and `OpenAIBackend`, for the services that speak the OpenAI chat API (Gemini, DeepSeek, Mistral). Which one
is used, with which key, is decided one level up, in `providers.py`.

Messages are kept in Ollama's shape everywhere else (tool results named by `tool_name`, tool calls without ids,
their arguments as a dict); `OpenAIBackend` translates them. What a provider needs to get back with its tool
calls is carried along: the model's reasoning (`thinking` on the assistant message: DeepSeek refuses a tool call
sent back without it) and a call's `extra_content` (Gemini's thought signatures).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

import httpx
from ollama import AsyncClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    extra: dict[str, Any] | None = None  # what the provider wants back with this call (Gemini's thought signature)


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

    @staticmethod
    def _plain(messages: list[dict]) -> list[dict]:
        """The messages as Ollama takes them: the reasoning, and what other providers keep with a tool call,
        are left out."""
        plain = []
        for message in messages:
            message = {key: value for key, value in message.items() if key != "thinking"}
            if message.get("tool_calls"):
                message["tool_calls"] = [{"function": call["function"]} for call in message["tool_calls"]]
            plain.append(message)
        return plain

    async def stream(
        self, messages: list[dict], tools: list[dict] | None
    ) -> AsyncIterator[LlmChunk]:
        response = await self._client.chat(
            model=self.model,
            messages=self._plain(messages),
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


# ----------------------------------------------------------------------
# The services that speak the OpenAI chat API
# ----------------------------------------------------------------------
# Gemini refuses a function call sent back without the signature it came with; this value stands in for the
# signature of a call made before signatures were kept (or by another provider), as Google's documentation allows.
SKIP_SIGNATURE = "skip_thought_signature_validator"
OPENAI_CONNECT_TIMEOUT = 15.0
OPENAI_READ_TIMEOUT = 600.0  # the agent gives up on a silent model sooner (CLARA_LLM_IDLE_TIMEOUT)


class LlmError(Exception):
    """The provider refused the request: the message says why (never with the key)."""


@dataclass(frozen=True)
class OpenAIFlavor:
    """What sets one OpenAI-compatible service apart from the others."""

    stream_usage: bool = True  # ask for the token counts at the end of a stream (stream_options)
    reasoning_back: bool = False  # send the reasoning back with the tool calls (DeepSeek requires it)
    signatures: bool = False  # send each tool call's thought signature back (Gemini requires it)
    tool_names: bool = False  # name the tool on a tool result (Mistral)
    extra_body: tuple[tuple[str, Any], ...] = ()  # more fields for every chat request


def call_id(number: int) -> str:
    """Ids of tool calls, made up when the messages are sent: Mistral wants exactly 9 letters or digits."""
    return f"c{number:08d}"


def _arguments(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        log.warning("a tool call came with arguments that are not JSON: %.200s", text)
        return {}
    return value if isinstance(value, dict) else {}


def _error_detail(response: httpx.Response) -> str:
    """The reason an error answer gives, whatever shape the service wraps it in."""
    try:
        error: Any = response.json()
    except ValueError:
        return " ".join(response.text.split())[:500] or response.reason_phrase
    if isinstance(error, list) and error:  # Gemini can answer a list of errors
        error = error[0]
    if isinstance(error, dict):
        error = error.get("error", error)
    if isinstance(error, dict):
        error = error.get("message") or error.get("detail") or error
    return " ".join(str(error).split())[:500]


class OpenAIBackend:
    """A chat model behind an OpenAI-compatible API (`{host}/chat/completions` and `{host}/models`)."""

    def __init__(
        self,
        model: str,
        host: str,
        api_key: str | None,
        flavor: OpenAIFlavor | None = None,
        label: str = "The provider",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.model = model
        self._host = host.rstrip("/")
        self._api_key = api_key
        self._flavor = flavor or OpenAIFlavor()
        self._label = label
        self._transport = transport  # tests answer from here instead of the network

    def _client(self) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        return httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(OPENAI_CONNECT_TIMEOUT, read=OPENAI_READ_TIMEOUT),
            transport=self._transport,
        )

    async def _refused(self, response: httpx.Response) -> LlmError:
        await response.aread()
        detail = _error_detail(response)
        if response.status_code in (401, 403):
            return LlmError(f"{self._label} refused the API key (HTTP {response.status_code}): {detail}")
        return LlmError(f"{self._label} answered HTTP {response.status_code}: {detail}")

    def convert(self, messages: list[dict]) -> list[dict]:
        """The messages as the OpenAI API takes them: tool calls get ids, and each result the id of its call."""
        flavor = self._flavor
        converted: list[dict] = []
        number = 0
        waiting: list[str] = []  # ids of the last tool calls, in order: the results that follow answer them
        for message in messages:
            role = message["role"]
            if role == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
                calls = message.get("tool_calls") or []
                if calls:
                    waiting = []
                    entry["tool_calls"] = []
                    for call in calls:
                        number += 1
                        function = call.get("function") or {}
                        arguments = function.get("arguments") or {}
                        item: dict[str, Any] = {
                            "id": call_id(number),
                            "type": "function",
                            "function": {
                                "name": function.get("name", ""),
                                "arguments": arguments if isinstance(arguments, str)
                                else json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                        if flavor.signatures:
                            item["extra_content"] = call.get("extra_content") or {
                                "google": {"thought_signature": SKIP_SIGNATURE}
                            }
                        entry["tool_calls"].append(item)
                        waiting.append(item["id"])
                    if not entry["content"]:
                        entry["content"] = None
                    if flavor.reasoning_back:
                        entry["reasoning_content"] = message.get("thinking") or ""
                converted.append(entry)
            elif role == "tool":
                number += 1
                entry = {
                    "role": "tool",
                    "tool_call_id": waiting.pop(0) if waiting else call_id(number),
                    "content": message.get("content") or "",
                }
                if flavor.tool_names and message.get("tool_name"):
                    entry["name"] = message["tool_name"]
                converted.append(entry)
            else:
                converted.append({"role": role, "content": message.get("content") or ""})
        return converted

    def body(self, messages: list[dict], tools: list[dict] | None) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "messages": self.convert(messages), "stream": True}
        if tools:
            body["tools"] = tools
        if self._flavor.stream_usage:
            body["stream_options"] = {"include_usage": True}
        body.update(dict(self._flavor.extra_body))
        return body

    async def stream(self, messages: list[dict], tools: list[dict] | None) -> AsyncIterator[LlmChunk]:
        calls: dict[int, dict[str, Any]] = {}  # tool calls come in pieces, by index
        async with self._client() as client:
            request = client.stream("POST", f"{self._host}/chat/completions", json=self.body(messages, tools))
            async with request as response:
                if response.is_error:
                    raise await self._refused(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        part = json.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(part, dict):
                        continue
                    if part.get("error"):
                        error = part["error"]
                        raise LlmError(f"{self._label}: {error.get('message', error) if isinstance(error, dict) else error}")
                    usage = part.get("usage") or {}
                    chunk = LlmChunk(
                        prompt_tokens=int(usage.get("prompt_tokens") or 0),
                        completion_tokens=int(usage.get("completion_tokens") or 0),
                    )
                    for choice in part.get("choices") or []:
                        delta = choice.get("delta") or {}
                        chunk.text += delta.get("content") or ""
                        chunk.thinking += delta.get("reasoning_content") or delta.get("reasoning") or ""
                        for piece in delta.get("tool_calls") or []:
                            index = int(piece.get("index", len(calls)))
                            slot = calls.setdefault(index, {"name": "", "arguments": "", "extra": None})
                            function = piece.get("function") or {}
                            slot["name"] += function.get("name") or ""
                            arguments = function.get("arguments") or ""
                            slot["arguments"] += arguments if isinstance(arguments, str) else json.dumps(arguments)
                            if piece.get("extra_content"):
                                slot["extra"] = piece["extra_content"]
                    if chunk.text or chunk.thinking or chunk.prompt_tokens or chunk.completion_tokens:
                        yield chunk
        if calls:
            yield LlmChunk(
                tool_calls=[
                    ToolCall(slot["name"], _arguments(slot["arguments"]), slot["extra"])
                    for _, slot in sorted(calls.items())
                    if slot["name"]
                ]
            )

    async def list_models(self) -> list[str]:
        async with self._client() as client:
            response = await client.get(f"{self._host}/models")
            if response.is_error:
                raise await self._refused(response)
        models = response.json().get("data") or []
        names = {str(model.get("id", "")).removeprefix("models/") for model in models if isinstance(model, dict)}
        return sorted(name for name in names if name)

    async def verify(self) -> None:
        # Every one of these services wants the key to list its models: a bad key fails here
        try:
            await self.list_models()
        except LlmError as error:
            if "refused the API key" in str(error):
                raise PermissionError(str(error)) from None
            raise
