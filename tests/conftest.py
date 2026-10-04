from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import AsyncIterator

import pytest
import uvicorn

from clara.llm import LlmChunk, ToolCall
from clara.memory import Memory
from clara.providers import ProviderManager
from clara.server import create_app
from clara.settings import Settings


class FakeBackend:
    """Plays scripted rounds: each round is a list of LlmChunk. Records what it was sent."""

    def __init__(self, *rounds: list[LlmChunk], model: str = "fake"):
        self.model = model
        self.rounds = list(rounds)
        self.calls: list[tuple[list[dict], list[dict] | None]] = []

    async def list_models(self) -> list[str]:
        return ["fake", "fake-big", "other-model"]

    async def verify(self) -> None:
        await self.list_models()

    async def stream(self, messages, tools) -> AsyncIterator[LlmChunk]:
        self.calls.append(([dict(m) for m in messages], tools))
        for chunk in self.rounds.pop(0):
            yield chunk


def untimed(text: str) -> str:
    """A user message without the `[time: HH:MM]` line the agent puts on the newest one."""
    return re.sub(r"^\[time: \d\d:\d\d\]\n\n", "", text)


def say(*pieces: str, prompt_tokens: int = 10, completion_tokens: int = 3) -> list[LlmChunk]:
    return [LlmChunk(text=piece) for piece in pieces] + [
        LlmChunk(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    ]


def call(name: str, **arguments) -> list[LlmChunk]:
    return [LlmChunk(tool_calls=[ToolCall(name, arguments)])]


@pytest.fixture
def memory(tmp_path: Path):
    mem = Memory(tmp_path / "clara.sqlite")
    yield mem
    mem.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.from_env(
        {
            "CLARA_TOKENS": "terminal:secret-cli,discord:secret-discord",
            "CLARA_ADMIN_TOKENS": "ops:secret-admin",
            "OLLAMA_API_KEY": "key-123",
            "CLARA_LOCAL_MODEL": "fake",
            "CLARA_CLOUD_MODEL": "fake-big",
            "CLARA_DATA_DIR": str(tmp_path / "data"),
            "CLARA_SYSTEM_PROMPT_FILE": str(tmp_path / "missing.md"),
            # discord accounts talk without signing in here; test_discord.py turns signing in on
            "CLARA_LOGIN_SURFACES": "none",
        }
    )


def fake_providers(settings: Settings, backend: FakeBackend | None = None) -> ProviderManager:
    """A ProviderManager whose backends are fakes (the given one, or one per provider/model)."""
    return ProviderManager.from_settings(
        settings, factory=lambda config, model: backend or FakeBackend(model=model)
    )


@pytest.fixture
def live(settings):
    """A real server on a free port: TestClient buffers a response, so it cannot read an endless stream."""
    app = create_app(settings, fake_providers(settings, FakeBackend()))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield app, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)
