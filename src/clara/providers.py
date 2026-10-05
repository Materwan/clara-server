"""Where the model runs, switchable while the server is running.

    local     "Local host"     Ollama on this machine (or any host in OLLAMA_HOST)
    cloud     "Ollama API key" ollama.com, authenticated with OLLAMA_API_KEY
    gemini    "Google Gemini"  GEMINI_API_KEY      } through their OpenAI-compatible API
    deepseek  "DeepSeek"       DEEPSEEK_API_KEY    } (llm.OpenAIBackend)
    mistral   "Mistral"        MISTRAL_API_KEY     }

`ProviderManager` is itself an `LlmBackend`: the agent talks to it, and each
model round goes to whichever provider is active at that moment. The choice
(and the model picked for each provider) is saved in `runtime.json` so it
survives a restart. The API keys are never saved or shown; they only ever come
from the environment. A provider without its key is listed, but cannot be chosen.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, TypeVar

from .llm import LlmBackend, LlmChunk, OllamaBackend, OpenAIBackend, OpenAIFlavor
from .settings import Settings
from .traffic import TrafficLog

log = logging.getLogger(__name__)

T = TypeVar("T")

CHECK_TIMEOUT = 8.0


class ProviderError(Exception):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    label: str
    host: str
    default_model: str
    api_key: str | None
    needs_key: bool
    context_window: int = 32_768  # tokens: what the percentages of the context are relative to
    request_context: bool = False  # ask the server for exactly that window (local Ollama only)
    key_name: str = "OLLAMA_API_KEY"  # where the key comes from, to say what is missing
    flavor: OpenAIFlavor | None = None  # None: Ollama; else an OpenAI-compatible service

    @property
    def usable(self) -> bool:
        return bool(self.api_key) or not self.needs_key


BackendFactory = Callable[[ProviderConfig, str], LlmBackend]


def make_ref(provider: str, model: str) -> str:
    """How a model is named across providers: `local:llama3.2`, `cloud:gpt-oss:120b` (the model keeps its own colons)."""
    return f"{provider}:{model}"


def split_ref(ref: str) -> tuple[str, str]:
    provider, separator, model = ref.strip().partition(":")
    if not separator or not provider or not model.strip():
        raise ProviderError(f"{ref!r} is not a model: write it provider:model, e.g. cloud:gpt-oss:120b.")
    return provider, model.strip()

_ALIASES = {
    "local": "local",
    "localhost": "local",
    "local-host": "local",
    "cloud": "cloud",
    "apikey": "cloud",
    "api-key": "cloud",
    "ollama-api-key": "cloud",
    "gemini": "gemini",
    "google": "gemini",
    "deepseek": "deepseek",
    "mistral": "mistral",
}

LABELS = {"gemini": "Google Gemini", "deepseek": "DeepSeek", "mistral": "Mistral"}


def flavor_of(provider: str, settings: Settings) -> OpenAIFlavor:
    """How each OpenAI-compatible service differs from the others."""
    if provider == "gemini":
        return OpenAIFlavor(signatures=True)
    if provider == "deepseek":
        extra = () if settings.deepseek_thinking else (("thinking", {"type": "disabled"}),)
        return OpenAIFlavor(reasoning_back=True, extra_body=extra)
    if provider == "mistral":
        return OpenAIFlavor(stream_usage=False, tool_names=True)  # Mistral gives the counts at the end anyway
    return OpenAIFlavor()


def default_factory(config: ProviderConfig, model: str) -> LlmBackend:
    if config.flavor is not None:
        return OpenAIBackend(model, config.host, config.api_key, config.flavor, config.label)
    return OllamaBackend(
        model,
        host=config.host,
        api_key=config.api_key,
        num_ctx=config.context_window if config.request_context else None,
    )


def configs_from_settings(settings: Settings) -> dict[str, ProviderConfig]:
    return {
        "local": ProviderConfig(
            "local",
            "Local host",
            settings.local_host,
            settings.local_model,
            None,
            False,
            settings.local_context_window,
            request_context=True,
        ),
        "cloud": ProviderConfig(
            "cloud",
            "Ollama API key",
            settings.cloud_host,
            settings.cloud_model,
            settings.ollama_api_key,
            True,
            settings.cloud_context_window,
        ),
        **{
            name: ProviderConfig(
                name,
                LABELS[name],
                api.host,
                api.model,
                api.api_key,
                True,
                api.context_window,
                key_name=api.key_name,
                flavor=flavor_of(name, settings),
            )
            for name, api in settings.api_providers.items()
        },
    }


class ProviderManager:
    def __init__(
        self,
        configs: dict[str, ProviderConfig],
        default: str,
        state_path: Path | None = None,
        factory: BackendFactory = default_factory,
    ):
        self.configs = configs
        self.state_path = state_path
        self._factory = factory
        self.traffic: TrafficLog | None = None  # where the calls to the provider are logged
        self._models = {name: config.default_model for name, config in configs.items()}
        self.active = default
        self._load_state()
        self._backend = self._factory(self.config, self.model)
        self._others: dict[str, LlmBackend] = {}  # the models somebody chose that are not the server's own

    @classmethod
    def from_settings(
        cls, settings: Settings, factory: BackendFactory = default_factory
    ) -> ProviderManager:
        return cls(
            configs_from_settings(settings),
            settings.default_provider,
            settings.runtime_state_file,
            factory,
        )

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    @property
    def config(self) -> ProviderConfig:
        return self.configs[self.active]

    @property
    def model(self) -> str:
        return self._models[self.active]

    @property
    def context_window(self) -> int:
        return self.config.context_window

    def model_of(self, provider: str) -> str:
        return self._models[provider]

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            for name, model in (state.get("models") or {}).items():
                if name in self._models and isinstance(model, str) and model.strip():
                    self._models[name] = model.strip()
            saved = state.get("provider")
            if saved in self.configs:
                if self.configs[saved].usable:
                    self.active = saved
                else:
                    log.warning("Saved provider %r has no API key any more; using %r", saved, self.active)
        except (OSError, ValueError, AttributeError):
            log.warning("Ignoring unreadable %s", self.state_path)

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        state = {"provider": self.active, "models": self._models}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(temporary, self.state_path)  # atomic: never a half-written file

    # ------------------------------------------------------------------
    # Changing provider or model
    # ------------------------------------------------------------------
    def resolve(self, reference: str) -> str:
        provider = _ALIASES.get(reference.strip().lower())
        if provider is None:
            raise ProviderError(f"Unknown provider {reference!r}. Choose: {', '.join(self.configs)}.")
        return provider

    def switch(self, reference: str) -> ProviderConfig:
        provider = self.resolve(reference)
        config = self.configs[provider]
        if not config.usable:
            raise ProviderError(f"{config.label} needs {config.key_name} in the server environment.")
        self.active = provider
        self._rebuild()
        return config

    def set_model(self, model: str) -> None:
        model = model.strip()
        if not model:
            raise ProviderError("The model name is empty.")
        self._models[self.active] = model
        self._rebuild()

    def _rebuild(self) -> None:
        self._backend = self._factory(self.config, self.model)
        self._save_state()

    # ------------------------------------------------------------------
    # LlmBackend: delegate to the active provider
    # ------------------------------------------------------------------
    @property
    def peer(self) -> str:
        """The provider, as named in the traffic log."""
        return f"ollama:{self.active}" if self.config.flavor is None else self.active

    def stream(self, messages: list[dict], tools: list[dict] | None) -> AsyncIterator[LlmChunk]:
        stream = self._backend.stream(messages, tools)
        if self.traffic is None:
            return stream
        return self.traffic.model_stream(stream, self.peer, self.config.host, self.model, messages, tools)

    # ------------------------------------------------------------------
    # Any model of any provider, at the same time (what each person chose)
    # ------------------------------------------------------------------
    @property
    def default_ref(self) -> str:
        return make_ref(self.active, self.model)

    def usable_ref(self, ref: str) -> bool:
        """Is the provider of this model there (known, and its key set)?"""
        try:
            config = self.configs.get(split_ref(ref)[0])
        except ProviderError:
            return False
        return config is not None and config.usable

    def window_of(self, ref: str) -> int:
        """The context window of the provider of this model."""
        config = self.configs.get(split_ref(ref)[0])
        return config.context_window if config is not None else self.context_window

    def _backend_for(self, ref: str) -> LlmBackend:
        if ref == self.default_ref:
            return self._backend
        provider, model = split_ref(ref)
        config = self.configs.get(provider)
        if config is None:
            raise ProviderError(f"Unknown provider {provider!r}. Choose: {', '.join(self.configs)}.")
        if not config.usable:
            raise ProviderError(f"{config.label} needs {config.key_name} in the server environment.")
        if ref not in self._others:
            self._others[ref] = self._factory(config, model)
        return self._others[ref]

    def stream_ref(self, ref: str, messages: list[dict], tools: list[dict] | None) -> AsyncIterator[LlmChunk]:
        """`stream`, for the model `ref` instead of the server's own."""
        backend = self._backend_for(ref)
        stream = backend.stream(messages, tools)
        if self.traffic is None:
            return stream
        provider, model = split_ref(ref)
        config = self.configs[provider]
        return self.traffic.model_stream(stream, self._peer_of(config), config.host, model, messages, tools)

    async def list_models_of(self, provider: str) -> list[str]:
        """What one provider offers (not only the active one)."""
        config = self.configs[provider]
        backend = self._backend_for(make_ref(provider, self._models[provider]))
        return await asyncio.wait_for(
            self._logged("list_models", backend.list_models(), config), CHECK_TIMEOUT
        )

    async def sizes_of(self, provider: str) -> dict[str, str]:
        """The sizes ("7.2B") a provider says of its models, for those that say it (Ollama's)."""
        config = self.configs[provider]
        backend = self._backend_for(make_ref(provider, self._models[provider]))
        sizes = getattr(backend, "model_sizes", None)
        if sizes is None:
            return {}
        return await asyncio.wait_for(self._logged("model_sizes", sizes(), config), CHECK_TIMEOUT)

    @staticmethod
    def _peer_of(config: ProviderConfig) -> str:
        return f"ollama:{config.id}" if config.flavor is None else config.id

    def _logged(self, operation: str, call: Awaitable[T], config: ProviderConfig | None = None) -> Awaitable[T]:
        if self.traffic is None:
            return call
        config = config or self.config
        return self.traffic.model_call(operation, self._peer_of(config), config.host, call)

    async def list_models(self) -> list[str]:
        return await asyncio.wait_for(self._logged("list_models", self._backend.list_models()), CHECK_TIMEOUT)

    async def check(self) -> str | None:
        """None if the active provider is usable (reachable, key accepted), else a short reason."""
        try:
            await asyncio.wait_for(self._logged("verify", self._backend.verify()), CHECK_TIMEOUT)
        except asyncio.TimeoutError:
            return "no answer"
        except Exception as error:
            return f"{type(error).__name__}: {str(error)[:200]}"
        return None
