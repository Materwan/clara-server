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

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from ollama import AsyncClient

from .httpclient import SharedClient

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
    notice: str = ""  # not from the model: the agent says it is waiting to ask again (see agent._model)


class LlmBackend(Protocol):
    model: str

    def stream(
        self, messages: list[dict], tools: list[dict] | None
    ) -> AsyncIterator[LlmChunk]: ...

    async def list_models(self) -> list[str]: ...

    async def verify(self) -> None:
        """Raise if the provider cannot be used (unreachable, credentials refused)."""
        ...


OLLAMA_SHOW_AT_ONCE = 4  # models whose record is asked for at the same time


def ollama_record(capabilities: list[str] | None, model_info: dict[str, Any] | None) -> dict:
    """Ollama's words for a model, as `models.Capabilities.from_record` takes them. `capabilities` None: an Ollama
    that does not say what a model can do, so the thinking, the tools and the images stay unknown."""
    context = next((value for key, value in (model_info or {}).items() if key.endswith(".context_length")), None)
    if capabilities is None:
        return {"context": context}
    said = set(capabilities)
    return {"thinking": "thinking" in said, "tools": "tools" in said, "vision": "vision" in said, "context": context}


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
            pictures = message.get("images") or []
            message = {key: value for key, value in message.items() if key not in ("thinking", "images")}
            if message.get("tool_calls"):
                message["tool_calls"] = [{"function": call["function"]} for call in message["tool_calls"]]
            if pictures:  # the ollama client takes the bytes of a picture
                message["images"] = [base64.b64decode(picture["data"]) for picture in pictures]
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

    async def model_sizes(self) -> dict[str, str]:
        """What the server says of each model's size ("7.2B", "134.52M"): only the models that say it."""
        response = await self._client.list()
        sizes = {}
        for model in response.models:
            size = getattr(getattr(model, "details", None), "parameter_size", None)
            if model.model and size:
                sizes[model.model] = str(size)
        return sizes

    async def model_capabilities(self, names: list[str]) -> dict[str, dict]:
        """What each model says it can do (`/api/show`: thinking, tools, vision, its context window). A model that
        does not answer is left out; the others are not held up by it."""
        slots = asyncio.Semaphore(OLLAMA_SHOW_AT_ONCE)

        async def ask(name: str) -> tuple[str, dict] | None:
            async with slots:
                try:
                    shown = await self._client.show(name)
                except Exception:  # one model that cannot be shown must not hide the rest
                    return None
            return name, ollama_record(shown.capabilities, shown.modelinfo)

        answers = await asyncio.gather(*(ask(name) for name in names))
        return dict(answer for answer in answers if answer is not None)

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
    """The provider refused the request: the message says why (never with the key). `status` is the HTTP
    status when there is one, `retry_after` the seconds the provider asked to wait."""

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class OpenAIFlavor:
    """What sets one OpenAI-compatible service apart from the others."""

    stream_usage: bool = True  # ask for the token counts at the end of a stream (stream_options)
    reasoning_back: bool = False  # send the reasoning back with the tool calls (DeepSeek requires it)
    signatures: bool = False  # send each tool call's thought signature back (Gemini requires it)
    tool_names: bool = False  # name the tool on a tool result (Mistral)
    extra_body: tuple[tuple[str, Any], ...] = ()  # more fields for every chat request
    native_models: str = ""  # the provider's own list of models, when its OpenAI one says only the names (Gemini)


# Gemini's model record says whether a model thinks, not whether it calls tools or reads images. Its chat models (the
# gemini-* ones that generate text) do both (ai.google.dev/gemini-api/docs/models); the speech, image, embedding and
# live models are not chat models, so for those two stay unknown.
GEMINI_NOT_CHAT = ("tts", "image", "embedding", "transcribe", "audio", "live", "bidi")


def google_record(model_id: str, model: dict) -> dict:
    """Gemini's words for a model, as `models.Capabilities.from_record` takes them (`context`: its input limit)."""
    chat = (
        model_id.startswith("gemini-")
        and "generateContent" in (model.get("supportedGenerationMethods") or [])
        and not any(word in model_id for word in GEMINI_NOT_CHAT)
    )
    return {
        "thinking": bool(model.get("thinking")),
        "tools": True if chat else None,
        "vision": True if chat else None,
        "context": model.get("inputTokenLimit"),
    }


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
        self._transport = transport
        # one client for every request: each model round reuses the open connection (transport: tests answer from it)
        self._http = SharedClient(
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=httpx.Timeout(OPENAI_CONNECT_TIMEOUT, read=OPENAI_READ_TIMEOUT),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _refused(self, response: httpx.Response) -> LlmError:
        await response.aread()
        detail = _error_detail(response)
        status = response.status_code
        if status in (401, 403):
            return LlmError(f"{self._label} refused the API key (HTTP {status}): {detail}", status)
        try:
            retry_after = float(response.headers.get("retry-after", ""))
        except ValueError:
            retry_after = None
        return LlmError(f"{self._label} answered HTTP {status}: {detail}", status, retry_after)

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
            elif role == "user" and message.get("images"):  # a picture: the text, then each picture as a data URL
                content: list[dict[str, Any]] = [{"type": "text", "text": message.get("content") or ""}]
                for picture in message["images"]:
                    url = f"data:{picture['mime']};base64,{picture['data']}"
                    content.append({"type": "image_url", "image_url": {"url": url}})
                converted.append({"role": role, "content": content})
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
        request = self._http.get().stream("POST", f"{self._host}/chat/completions", json=self.body(messages, tools))
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
                    code = error.get("code") if isinstance(error, dict) else None
                    raise LlmError(
                        f"{self._label}: {error.get('message', error) if isinstance(error, dict) else error}",
                        code if isinstance(code, int) else None,
                    )
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
        response = await self._http.get().get(f"{self._host}/models")
        if response.is_error:
            raise await self._refused(response)
        models = response.json().get("data") or []
        names = {str(model.get("id", "")).removeprefix("models/") for model in models if isinstance(model, dict)}
        return sorted(name for name in names if name)

    async def model_capabilities(self, names: list[str]) -> dict[str, dict]:
        """What each model can do, from the provider's own list (`flavor.native_models`): Gemini's OpenAI list has only
        the names. Nothing for a service that has no such list."""
        if not self._flavor.native_models or not self._api_key:
            return {}
        found: dict[str, dict] = {}
        page = ""
        # its own API takes the key in its own header; the Bearer of the OpenAI one is refused there
        async with httpx.AsyncClient(timeout=OPENAI_CONNECT_TIMEOUT, transport=self._transport) as http:
            while True:
                response = await http.get(
                    self._flavor.native_models,
                    params={"pageSize": 1000, **({"pageToken": page} if page else {})},
                    headers={"x-goog-api-key": self._api_key},
                )
                if response.is_error:
                    raise await self._refused(response)
                data = response.json()
                for model in data.get("models") or []:
                    model_id = str(model.get("name", "")).removeprefix("models/")
                    if model_id:
                        found[model_id] = google_record(model_id, model)
                page = str(data.get("nextPageToken") or "")
                if not page:
                    break
        return {name: found[name] for name in names if name in found}

    async def verify(self) -> None:
        # Every one of these services wants the key to list its models: a bad key fails here
        try:
            await self.list_models()
        except LlmError as error:
            if error.status in (401, 403):
                raise PermissionError(str(error)) from None
            raise
