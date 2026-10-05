"""A prompt that does not fit the model's window is shrunk or refused, never silently truncated."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import FakeBackend, call, fake_providers, say, untimed
from fastapi.testclient import TestClient

from clara.agent import Agent, ChatRequest, PromptTooLarge
from clara.compaction import estimate_prompt_tokens, estimate_tokens
from clara.prompt import SystemPrompt
from clara.server import create_app
from clara.tools import Toolbox, default_toolbox

CHAT = {"Authorization": "Bearer secret-cli"}
READ_FILE = {
    "type": "function",
    "function": {"name": "read_file", "description": "Read a file.", "parameters": {"type": "object", "properties": {}}},
}


def make_agent(memory, tmp_path: Path, backend, **options) -> Agent:
    toolbox = options.pop("toolbox", None) or default_toolbox()
    return Agent(memory, backend, toolbox, SystemPrompt(tmp_path / "none.md"), **options)


def ask(message: str, **fields) -> ChatRequest:
    return ChatRequest("cli", "erwan", "Erwan", message, **fields)


async def events_of(agent: Agent, request: ChatRequest) -> list[dict]:
    return [event async for event in agent.turn(request, "client")]


def test_estimate_counts_messages_tool_calls_and_schemas():
    plain = estimate_prompt_tokens([{"role": "user", "content": "x" * 350}])
    assert plain == 100
    calls = [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "f", "arguments": {"a": "y" * 350}}}]}]
    assert estimate_prompt_tokens(calls) > 100
    assert estimate_prompt_tokens([], [READ_FILE]) > 0


async def test_a_message_that_cannot_fit_is_refused_before_the_model_is_asked(memory, tmp_path):
    backend = FakeBackend(say("never"))
    agent = make_agent(memory, tmp_path, backend, context_window=1_000)
    with pytest.raises(PromptTooLarge, match="too long"):
        await events_of(agent, ask("x" * 2_000))  # about 570 tokens: over half the window
    assert backend.calls == [] and memory.messages_after("cli:erwan") == []


def test_http_answers_413_and_the_stream_an_error_event(settings):
    small = replace(settings, local_context_window=1_000)
    body = {"surface": "cli", "user_id": "erwan", "message": "x" * 2_000}
    with TestClient(create_app(small, fake_providers(small, FakeBackend(say("never"))))) as client:
        assert client.post("/v1/chat", json=body, headers=CHAT).status_code == 413
        with client.stream("POST", "/v1/chat/stream", json=body, headers=CHAT) as response:
            events = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]
    assert events[-1]["type"] == "error" and "too long" in events[-1]["message"]


async def test_a_history_larger_than_the_window_is_compacted_first(memory, tmp_path):
    long = "w" * 1_300  # about 370 tokens
    backend = FakeBackend(*[say(f"a{i}") for i in range(1, 5)], say("summary of the early turns"), say("a5"))
    # no tools: the sizes below are about the conversation, not about the tool schemas
    agent = make_agent(
        memory, tmp_path, backend, toolbox=Toolbox([]), context_window=2_000, compact_percent=0, keep_recent_turns=1
    )
    for i in range(1, 5):
        await events_of(agent, ask(f"q{i} {long}"))

    events = await events_of(agent, ask(f"q5 {long}"))  # the 5th would need more than 95% of 2000 tokens

    assert "compacted" in [e["type"] for e in events]
    sent = backend.calls[-1][0]
    assert "summary of the early turns" in sent[0]["content"]
    assert estimate_prompt_tokens(sent, backend.calls[-1][1]) <= 0.95 * 2_600
    assert untimed(sent[-1]["content"]).startswith("q5")


class FailingSummary(FakeBackend):
    async def stream(self, messages, tools):
        if messages[0]["content"].startswith("You are summarising"):
            raise RuntimeError("model unavailable")
        async for chunk in super().stream(messages, tools):
            yield chunk


async def test_when_compaction_fails_the_oldest_turns_are_left_out_with_a_warning(memory, tmp_path):
    long = "w" * 1_200
    backend = FailingSummary(*[say(f"a{i}") for i in range(1, 6)])
    agent = make_agent(memory, tmp_path, backend, context_window=2_600, compact_percent=0)
    for i in range(1, 5):
        await events_of(agent, ask(f"q{i} {long}"))

    events = await events_of(agent, ask(f"q5 {long}"))

    warnings = [e["message"] for e in events if e["type"] == "warning"]
    assert any("Could not compact" in w for w in warnings) and any("left out" in w for w in warnings)
    sent = backend.calls[-1][0]
    assert estimate_prompt_tokens(sent, backend.calls[-1][1]) <= 0.95 * 2_600
    users = [untimed(m["content"])[:2] for m in sent if m["role"] == "user"]
    assert users[-1] == "q5" and "q1" not in users  # the oldest went first, the new message stayed
    assert events[-1]["reply"] == "a5"


async def test_the_context_is_the_larger_of_what_the_model_reported_and_the_estimate(memory, tmp_path):
    backend = FakeBackend(say("ok", prompt_tokens=5, completion_tokens=1))  # a cache made it report almost nothing
    agent = make_agent(memory, tmp_path, backend, context_window=10_000)
    done = (await events_of(agent, ask("w" * 7_000)))[-1]  # about 2000 tokens
    assert done["context"]["tokens"] >= estimate_tokens("w" * 7_000)
    assert agent.context("cli:erwan")["tokens"] == done["context"]["tokens"]


async def test_a_tool_result_too_large_for_the_window_is_refused(memory, tmp_path):
    backend = FakeBackend(call("read_file"), say("never"))
    agent = make_agent(memory, tmp_path, backend, context_window=4_000)
    stream = agent.turn(ask("read it", tools=(READ_FILE,)), "client")
    with pytest.raises(PromptTooLarge, match="too large"):
        async for event in stream:
            if event["type"] == "tool_requests":
                call_id = event["calls"][0]["id"]
                agent.submit_results(event["turn"], "client", {call_id: "z" * 20_000})
    assert agent._pending == {}
