"""What keeps the prompt small and stable: its order, the tools a surface is sent, outputs cut once read, the
token trigger of a compaction, and the cached tokens the usage log keeps."""

from pathlib import Path

import pytest
from conftest import FakeBackend, call, say
from test_client_tools import READ_FILE, drive, make_agent, request

from clara.agent import CUT_IN_TURN, IN_TURN_RESULT_KEEP, Agent, ChatRequest
from clara.llm import LlmChunk, cached_tokens
from clara.memory import Fact, Person
from clara.prompt import SystemPrompt
from clara.settings import DEFAULT_HIDDEN_TOOLS, SettingsError, parse_hidden_tools
from clara.tools import Toolbox, default_toolbox
from clara.usagelog import UsageLog

# --- the prompt keeps what does not change first ------------------------------------------------------


def render(prompt: SystemPrompt, facts, summary="", relation=50) -> str:
    from datetime import datetime

    return prompt.render(
        Person(1, "Erwan"), "web", facts, datetime(2026, 10, 10), instructions="Use the tools.", summary=summary,
        relation=relation, project="## Project\nfiles", files="## Files\nnotes.md",
    )


def test_what_changes_during_a_conversation_comes_last(tmp_path):
    prompt = SystemPrompt(tmp_path / "none.md")
    text = render(prompt, [Fact(1, "likes tea")], summary="so far: tea")
    order = [text.index(h) for h in (
        "## Instructions from web", "## Project", "## Files", "## What you remember about Erwan",
        "## Your relationship with Erwan", "## Earlier in this conversation",
    )]
    assert order == sorted(order)


def test_a_new_fact_or_a_new_score_leaves_the_beginning_of_the_prompt_untouched(tmp_path):
    prompt = SystemPrompt(tmp_path / "none.md")
    before = render(prompt, [Fact(1, "likes tea")], relation=50)
    after = render(prompt, [Fact(1, "likes tea"), Fact(2, "has a cat")], relation=80)
    shared = len(__import__("os").path.commonprefix([before, after]))
    assert shared > before.index("## Files")  # the project, the files and the instructions are in the cached beginning


# --- tools a surface never needs ----------------------------------------------------------------------


def names(backend: FakeBackend) -> set[str]:
    return {schema["function"]["name"] for schema in backend.calls[-1][1] or []}


async def test_a_surface_is_not_sent_the_tools_it_hides(memory, tmp_path):
    backend = FakeBackend(say("a"), say("b"))
    agent = make_agent(memory, tmp_path, backend, hidden_tools={"discord": ["add_task", "remember"]})
    await drive(agent, request(surface="discord", user_id="1"))
    discord = names(backend)
    await drive(agent, request(surface="console"))
    console = names(backend)
    assert "list_tasks" in discord and not {"add_task", "remember"} & discord
    assert {"add_task", "remember"} <= console


def test_the_default_hidden_tools_are_for_discord_and_can_be_replaced():
    assert parse_hidden_tools(DEFAULT_HIDDEN_TOOLS)["discord"] >= {"add_task", "github_pr"}
    assert parse_hidden_tools("web=notify|forget, web=remind") == {"web": frozenset({"notify", "forget", "remind"})}
    assert parse_hidden_tools("") == {}
    with pytest.raises(SettingsError):
        parse_hidden_tools("discord")


# --- an output the model has read is cut in the next rounds -------------------------------------------


async def test_outputs_already_read_are_cut_in_the_prompt_but_kept_in_the_history(memory, tmp_path):
    backend = FakeBackend(*[call("read_file", path=str(i)) for i in range(3)], say("done"))
    agent = make_agent(memory, tmp_path, backend, toolbox=Toolbox([]), context_window=100_000)
    await drive(agent, request(tools=(READ_FILE,)), lambda n, a: a["path"] * 5_000)

    last = [m["content"] for m in backend.calls[-1][0] if m["role"] == "tool"]
    assert last[0] == "0" * IN_TURN_RESULT_KEEP + CUT_IN_TURN
    assert last[1] == "1" * IN_TURN_RESULT_KEEP + CUT_IN_TURN
    assert last[2] == "2" * 5_000  # the newest one has not been read yet
    assert [m.content for m in memory.messages_after("console:erwan") if m.role == "tool"][0] == "0" * 5_000


async def test_a_short_output_is_never_cut(memory, tmp_path):
    backend = FakeBackend(*[call("read_file", path=str(i)) for i in range(3)], say("done"))
    agent = make_agent(memory, tmp_path, backend, toolbox=Toolbox([]))
    await drive(agent, request(tools=(READ_FILE,)), lambda n, a: "short " + a["path"])
    assert [m["content"] for m in backend.calls[-1][0] if m["role"] == "tool"] == ["short 0", "short 1", "short 2"]


# --- a compaction when the messages weigh a lot, whatever the window -----------------------------------


async def talk(agent: Agent, message: str) -> list[dict]:
    return [e async for e in agent.turn(ChatRequest("cli", "erwan", "Erwan", message))]


async def test_a_conversation_is_summarised_when_its_messages_weigh_the_token_limit(memory, tmp_path):
    backend = FakeBackend(say("a1", prompt_tokens=3_000), say("a summary"))
    agent = make_agent(
        memory, tmp_path, backend, toolbox=Toolbox([]), compact_percent=0, compact_tokens=1_000, keep_recent_turns=0
    )
    events = await talk(agent, "q1")
    assert "compacted" in [e["type"] for e in events]
    assert memory.state("cli:erwan").summary == "a summary"


async def test_the_system_prompt_and_the_tools_do_not_count_for_the_token_limit(memory, tmp_path):
    backend = FakeBackend(say("a1", prompt_tokens=3_000), say("a summary"))
    agent = make_agent(  # 3,000 tokens in all, of which the prompt and the tools are most
        memory, tmp_path, backend, compact_percent=0, compact_tokens=2_900, keep_recent_turns=0
    )
    assert "compacted" not in [e["type"] for e in await talk(agent, "q1")]


async def test_no_token_limit_means_no_compaction(memory, tmp_path):
    backend = FakeBackend(say("a1", prompt_tokens=30_000))
    agent = make_agent(memory, tmp_path, backend, toolbox=Toolbox([]), compact_percent=0, compact_tokens=0)
    assert "compacted" not in [e["type"] for e in await talk(agent, "q1")]


# --- cached tokens ------------------------------------------------------------------------------------


def test_the_providers_ways_of_saying_what_came_from_their_cache():
    assert cached_tokens({"prompt_tokens_details": {"cached_tokens": 800}}) == 800
    assert cached_tokens({"prompt_cache_hit_tokens": 640}) == 640
    assert cached_tokens({"prompt_tokens": 900}) == 0
    assert cached_tokens({"prompt_tokens_details": None}) == 0


async def test_the_usage_log_keeps_the_cached_tokens_of_an_answer(memory, tmp_path):
    usage_log = UsageLog(memory)
    backend = FakeBackend([LlmChunk(text="a"), LlmChunk(prompt_tokens=1_000, completion_tokens=5, cached_tokens=700)])
    agent = Agent(memory, backend, default_toolbox(), SystemPrompt(Path(tmp_path) / "none.md"), usage_log=usage_log)
    await talk(agent, "hi")
    [row] = usage_log.history()["calls"]
    assert row["cached_tokens"] == 700 and row["prompt_tokens"] == 1_000
    assert usage_log.history()["totals"]["cached_tokens"] == 700
