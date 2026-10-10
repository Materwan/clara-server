"""Tools that run on the client's machine, and everything else a client can add to a turn."""

import asyncio
from pathlib import Path

import pytest
from conftest import FakeBackend, call, say, untimed

from clara.agent import Agent, ChatRequest, ClientToolTimeout, NothingToCompact
from clara.llm import LlmChunk
from clara.prompt import SystemPrompt
from clara.tools import Toolbox, default_toolbox

READ_FILE = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file on the user's computer.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
}


def make_agent(memory, tmp_path: Path, backend, **options) -> Agent:
    toolbox = options.pop("toolbox", None) or default_toolbox()
    return Agent(memory, backend, toolbox, SystemPrompt(tmp_path / "none.md"), **options)


def request(**fields) -> ChatRequest:
    return ChatRequest(**{"surface": "console", "user_id": "erwan", "user_name": "Erwan", "message": "hi", **fields})


async def drive(agent: Agent, req: ChatRequest, answer=lambda name, arguments: "ok", owner="console") -> list[dict]:
    """Run a turn like a client does: answer every `tool_requests` event with `answer`."""
    events = []
    async for event in agent.turn(req, owner):
        events.append(event)
        if event["type"] == "tool_requests":
            agent.submit_results(
                event["turn"],
                owner,
                {c["id"]: answer(c["name"], c["arguments"]) for c in event["calls"]},
            )
    return events


async def test_client_tool_round_trip(memory, tmp_path):
    backend = FakeBackend(call("read_file", path="a.txt"), say("It says hello."))
    agent = make_agent(memory, tmp_path, backend)

    events = await drive(agent, request(tools=(READ_FILE,)), lambda name, args: f"contents of {args['path']}")

    types = [e["type"] for e in events]
    assert types == ["turn", "usage", "tool_requests", "token", "usage", "done"]
    asked = next(e for e in events if e["type"] == "tool_requests")
    assert asked["calls"] == [{"id": "call_0_0", "name": "read_file", "arguments": {"path": "a.txt"}}]
    assert events[-1]["reply"] == "It says hello."
    assert events[-1]["tools"] == ["read_file"]

    # the model saw its own tool schema first, then the call and its result
    first_round_tools = backend.calls[0][1]
    assert READ_FILE in first_round_tools and {t["function"]["name"] for t in first_round_tools} >= {"remember"}
    second_round = backend.calls[1][0]
    assert second_round[-2]["tool_calls"] == [{"function": {"name": "read_file", "arguments": {"path": "a.txt"}}}]
    assert second_round[-1] == {"role": "tool", "tool_name": "read_file", "content": "contents of a.txt"}


async def test_server_and_client_tools_in_the_same_round(memory, tmp_path):
    from clara.llm import LlmChunk, ToolCall

    both = [LlmChunk(tool_calls=[ToolCall("remember", {"fact": "Likes tea"}), ToolCall("read_file", {"path": "x"})])]
    agent = make_agent(memory, tmp_path, FakeBackend(both, say("done")))

    events = await drive(agent, request(tools=(READ_FILE,)))

    assert [e["type"] for e in events if e["type"] in ("tool", "tool_requests")] == ["tool", "tool_requests"]
    assert [fact.text for fact in memory.facts(memory.find_person("console", "erwan").id)] == ["Likes tea"]
    stored = memory.messages_after("console:erwan")
    assert [(m.role, m.tool_name) for m in stored if m.role == "tool"] == [("tool", "remember"), ("tool", "read_file")]


async def test_the_turn_with_its_tool_calls_is_stored_and_replayed(memory, tmp_path):
    backend = FakeBackend(call("read_file", path="a"), say("first answer"), say("second answer"))
    agent = make_agent(memory, tmp_path, backend)
    await drive(agent, request(message="read a", tools=(READ_FILE,)), lambda n, a: "FILE A")
    await drive(agent, request(message="and then?", tools=(READ_FILE,)))

    replay = backend.calls[2][0]
    roles = [m["role"] for m in replay]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert replay[2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert replay[3]["content"] == "FILE A"
    assert untimed(replay[-1]["content"]) == "and then?"


async def test_old_tool_outputs_are_dropped_from_the_replay(memory, tmp_path):
    rounds = []
    for index in range(10):
        rounds += [call("read_file", path=str(index)), say(f"answer {index}")]
    rounds.append(say("last"))
    backend = FakeBackend(*rounds)
    agent = make_agent(memory, tmp_path, backend)
    for index in range(10):
        await drive(agent, request(message=f"q{index}", tools=(READ_FILE,)), lambda n, a: f"output {a['path']}")
    await drive(agent, request(message="again", tools=(READ_FILE,)))

    tool_messages = [m for m in backend.calls[-1][0] if m["role"] == "tool"]
    assert len(tool_messages) == 10
    assert [m["content"] for m in tool_messages[:2]] == ["[output omitted to save context]"] * 2
    assert tool_messages[-1]["content"] == "output 9"


async def test_history_keeps_only_the_last_turns(memory, tmp_path):
    backend = FakeBackend(*[say(f"a{i}") for i in range(4)])
    agent = make_agent(memory, tmp_path, backend, history_turns=2, compact_percent=0)  # plain truncation: compaction is off
    for index in range(3):
        await drive(agent, request(message=f"q{index}"))
    await drive(agent, request(message="q3"))
    sent = [m["content"] for m in backend.calls[-1][0] if m["role"] != "system"]
    assert [untimed(text) for text in sent] == ["q1", "a1", "q2", "a2", "q3"]


async def test_results_must_come_from_the_right_client_for_the_right_calls(memory, tmp_path):
    agent = make_agent(memory, tmp_path, FakeBackend(call("read_file", path="a"), say("ok")))
    seen = {}

    async for event in agent.turn(request(tools=(READ_FILE,)), "console"):
        if event["type"] == "tool_requests":
            turn = event["turn"]
            with pytest.raises(KeyError):
                agent.submit_results(turn, "someone-else", {"call_0_0": "x"})
            with pytest.raises(KeyError):
                agent.submit_results("nope", "console", {"call_0_0": "x"})
            with pytest.raises(ValueError):
                agent.submit_results(turn, "console", {"wrong": "x"})
            with pytest.raises(ValueError):
                agent.submit_results(turn, "console", {})
            agent.submit_results(turn, "console", {"call_0_0": "fine"})
            with pytest.raises(KeyError):  # already answered
                agent.submit_results(turn, "console", {"call_0_0": "again"})
            seen["answered"] = True
    assert seen == {"answered": True}


async def test_a_silent_client_times_out(memory, tmp_path):
    agent = make_agent(memory, tmp_path, FakeBackend(call("read_file", path="a")), tool_timeout=0.05)
    with pytest.raises(ClientToolTimeout):
        async for _ in agent.turn(request(tools=(READ_FILE,)), "console"):
            pass
    # what was done stays known: the call, a note instead of its result, and why the answer stopped
    stored = memory.messages_after("console:erwan")
    assert [(m.role, m.tool_name) for m in stored] == [("user", None), ("assistant", None), ("tool", "read_file"), ("assistant", None)]
    assert stored[2].content == "[not run: the answer was interrupted]"
    assert stored[3].content.startswith("[This answer was interrupted: The client did not return its tool results")
    assert agent._pending == {}


async def test_a_client_that_leaves_cleans_up(memory, tmp_path):
    agent = make_agent(memory, tmp_path, FakeBackend(call("read_file", path="a")))
    stream = agent.turn(request(tools=(READ_FILE,)), "console")
    async for event in stream:
        if event["type"] == "tool_requests":
            break
    await stream.aclose()
    assert agent._pending == {}
    assert agent.stats.active == 0
    stored = memory.messages_after("console:erwan")
    assert stored[-1].content == "[This answer was interrupted: the connection to the client was lost.]"
    # and the conversation is free again
    agent.backend.rounds.append(say("hello again"))
    events = await drive(agent, request())
    assert events[-1]["reply"] == "hello again"


async def test_waiting_for_a_client_does_not_hold_a_model_slot(memory, tmp_path):
    """With one slot, another conversation must still be answered while a client runs its tool."""
    backend = FakeBackend(call("read_file", path="a"), say("slow done"), say("fast done"))
    agent = make_agent(memory, tmp_path, backend, max_concurrent_llm=1)

    slow = agent.turn(request(conversation="slow", tools=(READ_FILE,)), "console")
    async for event in slow:
        if event["type"] == "tool_requests":
            break
    fast = await asyncio.wait_for(drive(agent, request(conversation="fast")), timeout=2)
    assert fast[-1]["reply"] == "slow done"  # (the fake plays rounds in order)
    await slow.aclose()


def test_tools_are_validated(memory, tmp_path):
    agent = make_agent(memory, tmp_path, FakeBackend())
    agent.validate(request(tools=(READ_FILE,)))
    taken = {"type": "function", "function": {"name": "remember"}}
    for bad in ((taken,), (READ_FILE, READ_FILE), ({"type": "function"},), ("nope",)):
        with pytest.raises(ValueError):
            agent.validate(request(tools=bad))
    agent.validate(request(tools=(taken,), ephemeral=True))  # server tools do not exist there


async def test_ephemeral_jobs_have_no_persona_memory_or_history(memory, tmp_path):
    backend = FakeBackend(call("read_file", path="a"), say("report"))
    agent = make_agent(memory, tmp_path, backend)
    person = memory.resolve("console", "erwan")
    memory.add_fact(person.id, "Secret fact")

    events = await drive(
        agent, request(ephemeral=True, instructions="You are a sub-agent.", message="find it", tools=(READ_FILE,))
    )

    assert events[-1]["reply"] == "report"
    system = backend.calls[0][0][0]
    assert system == {"role": "system", "content": "You are a sub-agent."}
    assert [t["function"]["name"] for t in backend.calls[0][1]] == ["read_file"]  # no remember/forget
    assert memory.messages_after("console:erwan") == []
    assert memory.state("console:erwan").context_tokens == 0


async def test_instructions_and_prefix(memory, tmp_path):
    backend = FakeBackend(say("a"), say("b"))
    agent = make_agent(memory, tmp_path, backend)
    await drive(agent, request(message="hello", prefix="[note: it is noon]", instructions="Be a coding agent."))
    await drive(agent, request(message="again", prefix="[note: it is one]", instructions="Be a coding agent."))

    first = backend.calls[0][0]
    assert "## Instructions from console\nBe a coding agent." in first[0]["content"]
    assert first[-1]["content"].endswith("\n\n[note: it is noon]\n\nhello")
    second = backend.calls[1][0]
    assert second[1]["content"] == "[note: it is noon]\n\nhello"  # history keeps the prefix, not the time
    stored = memory.messages_after("console:erwan")
    assert stored[0].content == "hello" and stored[0].prefix == "[note: it is noon]"


# --- compaction ---------------------------------------------------------------------


async def test_manual_compaction_replaces_older_messages_by_a_summary(memory, tmp_path):
    backend = FakeBackend(say("a1", prompt_tokens=700), say("a2", prompt_tokens=1500), say("The user asked q1 and q2."), say("a3"))
    agent = make_agent(memory, tmp_path, backend, context_window=8000, compact_percent=0)
    long = "x" * 7000  # about 2000 tokens
    await drive(agent, request(message="q1 " + long))
    await drive(agent, request(message="q2 " + long))
    full = agent.context("console:erwan")["percent"]
    assert full > 50  # the model reported less than the prompt weighs (about 2000 tokens of messages): the estimate wins

    before, after = await agent.compact("console:erwan", focus="the questions")
    assert before == pytest.approx(full, abs=0.1)
    assert after < full / 2  # only the summary and the fixed part (system prompt, tools: it grows with them) are left

    summary_call = backend.calls[2][0]
    assert "the questions" in summary_call[1]["content"] and "Erwan: q1" in summary_call[1]["content"]
    info = agent.context("console:erwan")
    assert info["summary"] == "The user asked q1 and q2." and info["messages"] == 0

    await drive(agent, request(message="q3"))
    prompt = backend.calls[3][0]
    assert "## Earlier in this conversation (summary)\nThe user asked q1 and q2." in prompt[0]["content"]
    assert [untimed(m["content"]) for m in prompt[1:]] == ["q3"]  # the compacted messages are not sent again


async def test_compacting_an_empty_conversation_or_failing_summary(memory, tmp_path):
    agent = make_agent(memory, tmp_path, FakeBackend(say("")))
    with pytest.raises(NothingToCompact):
        await agent.compact("nothing")
    memory.add_exchange("c", memory.resolve("cli", "x").id, "q", "a")
    with pytest.raises(RuntimeError, match="empty summary"):
        await agent.compact("c")


async def test_automatic_compaction_when_the_context_is_full(memory, tmp_path):
    backend = FakeBackend(say("big answer", prompt_tokens=1700), say("Summary."))
    # no server tools: the windows here are tiny, and what tools weigh would decide whether the prompt fits
    agent = make_agent(memory, tmp_path, backend, toolbox=Toolbox([]), context_window=2000, compact_percent=80)

    events = await drive(agent, request(message="q" * 1200))  # the model says the prompt took 1700 of 2000 tokens

    kinds = [e["type"] for e in events]
    assert kinds.index("compacted") < kinds.index("done")
    compacted = next(e for e in events if e["type"] == "compacted")
    assert compacted["before"] > 80 > compacted["after"]
    assert events[-1]["context"]["percent"] == pytest.approx(compacted["after"], abs=0.1)
    assert memory.state("console:erwan").summary == "Summary."


async def test_a_failing_automatic_compaction_is_a_warning_not_an_error(memory, tmp_path):
    backend = FakeBackend(say("answer", prompt_tokens=1800), say(""))
    agent = make_agent(memory, tmp_path, backend, toolbox=Toolbox([]), context_window=2000, compact_percent=80)
    events = await drive(agent, request(message="q"))
    assert [e["type"] for e in events][-2:] == ["warning", "done"]
    assert events[-1]["reply"] == "answer"


async def test_what_was_written_before_an_interruption_is_kept(memory, tmp_path):
    backend = FakeBackend([LlmChunk(text="I will read "), LlmChunk(text="the file")])
    agent = make_agent(memory, tmp_path, backend)
    stream = agent.turn(request(message="go"), "console")
    async for event in stream:
        if event["type"] == "token" and event["text"] == "the file":
            break
    await stream.aclose()
    stored = memory.messages_after("console:erwan")
    assert [m.content for m in stored] == [
        "go", "I will read the file\n\n[This answer was interrupted: the connection to the client was lost.]"
    ]


async def test_nothing_is_stored_when_nothing_happened(memory, tmp_path):
    agent = make_agent(memory, tmp_path, FakeBackend(say("never read")))
    stream = agent.turn(request(message="go"), "console")
    await anext(stream)  # the `turn` event: the model has not been asked yet
    await stream.aclose()
    assert memory.messages_after("console:erwan") == []


async def test_old_tool_outputs_are_kept_within_a_share_of_the_window(memory, tmp_path):
    rounds = []
    for index in range(3):
        rounds += [call("read_file", path=str(index)), say(f"answer {index}")]
    rounds.append(say("last"))
    backend = FakeBackend(*rounds)
    agent = make_agent(memory, tmp_path, backend, context_window=10_000)  # 25%: about 2,500 tokens of outputs
    for index in range(3):
        await drive(agent, request(message=f"q{index}", tools=(READ_FILE,)), lambda n, a: a["path"] * 6_000)
    await drive(agent, request(message="again", tools=(READ_FILE,)))

    contents = [m["content"] for m in backend.calls[-1][0] if m["role"] == "tool"]
    assert contents[:2] == ["[output omitted to save context]"] * 2 and contents[2] == "2" * 6_000


async def test_older_outputs_of_a_long_answer_are_left_out_to_fit(memory, tmp_path, monkeypatch):
    monkeypatch.setattr("clara.agent.IN_TURN_RESULT_KEEP", 10**9)  # the safety net, when no output was cut earlier
    backend = FakeBackend(*[call("read_file", path=str(i)) for i in range(3)], say("done"))
    agent = make_agent(memory, tmp_path, backend, toolbox=Toolbox([]), context_window=4_000)
    events = await drive(agent, request(tools=(READ_FILE,)), lambda n, a: a["path"] * 5_000)  # ~1,400 tokens each

    assert events[-1]["reply"] == "done"
    assert any(e["type"] == "warning" and "tool outputs of this answer" in e["message"] for e in events)
    last = [m["content"] for m in backend.calls[-1][0] if m["role"] == "tool"]
    assert last[0].startswith("[output omitted to fit the context") and last[-1] == "2" * 5_000
    # the history keeps the full outputs: only the prompt was trimmed
    assert [m.content for m in memory.messages_after("console:erwan") if m.role == "tool"][0] == "0" * 5_000


async def test_thinking_is_streamed_but_never_stored(memory, tmp_path):
    backend = FakeBackend([LlmChunk(thinking="Let me see..."), LlmChunk(text="Yes."), LlmChunk(prompt_tokens=5, completion_tokens=2)])
    agent = make_agent(memory, tmp_path, backend)
    events = await drive(agent, request())
    assert {"type": "thinking", "text": "Let me see..."} in events
    assert [m.content for m in memory.messages_after("console:erwan")] == ["hi", "Yes."]


async def test_a_server_tool_event_carries_its_arguments_and_result(memory, tmp_path):
    agent = make_agent(memory, tmp_path, FakeBackend(call("remember", fact="Likes tea"), say("ok")))
    events = await drive(agent, request())
    [tool] = [e for e in events if e["type"] == "tool"]
    assert tool["name"] == "remember" and tool["arguments"] == {"fact": "Likes tea"}
    assert tool["result"].startswith("Remembered")
