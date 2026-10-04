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

    def _logged(self, operation: str, call: Awaitable[T]) -> Awaitable[T]:
        if self.traffic is None:
            return call
        return self.traffic.model_call(operation, self.peer, self.config.host, call)

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
