import asyncio
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from conftest import FakeBackend, call, say, untimed

from clara.agent import Agent, ChatRequest
from clara.prompt import SystemPrompt
from clara.tools import default_toolbox


def make_agent(memory, tmp_path: Path, backend, **options) -> Agent:
    return Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"), **options)


async def run(agent: Agent, **fields) -> list[dict]:
    request = ChatRequest(**{"surface": "cli", "user_id": "erwan", "user_name": "Erwan", **fields})
    return [event async for event in agent.turn(request)]


async def test_streams_tokens_then_done_and_stores_the_exchange(memory, tmp_path):
    agent = make_agent(memory, tmp_path, FakeBackend(say("Hel", "lo!")))
    events = await run(agent, message="hi")

    assert [e["type"] for e in events] == ["turn", "token", "token", "usage", "done"]
    done = events[-1]
    assert done["reply"] == "Hello!"
    assert done["conversation"] == "cli:erwan"
    assert done["usage"] == {"prompt_tokens": 10, "completion_tokens": 3}
    assert [m.content for m in memory.history("cli:erwan", 10)] == ["hi", "Hello!"]


async def test_tools_run_and_memory_reaches_the_next_prompt(memory, tmp_path):
    backend = FakeBackend(
        call("remember", fact="Has a cat named Miso"),
        say("Noted."),
        say("Miso, your cat!"),
    )
    agent = make_agent(memory, tmp_path, backend)

    first = await run(agent, message="I have a cat named Miso")
    assert [e["type"] for e in first] == ["turn", "usage", "tool_start", "tool", "token", "usage", "done"]
    assert first[-1]["tools"] == ["remember"]

    # Another surface, same person once linked: the fact is in the system prompt
    person = memory.find_person("cli", "erwan")
    memory.link_account("discord", "1234", person)
    await run(agent, surface="discord", user_id="1234", message="what is my pet called?")

    system_prompt = backend.calls[-1][0][0]["content"]
    assert "Has a cat named Miso" in system_prompt
    assert "Erwan" in system_prompt


async def test_tool_result_is_sent_back_to_the_model(memory, tmp_path):
    backend = FakeBackend(call("remember", fact="Likes tea"), say("ok"))
    agent = make_agent(memory, tmp_path, backend)
    await run(agent, message="I like tea")

    second_round = backend.calls[1][0]
    assert second_round[-2]["role"] == "assistant" and second_round[-2]["tool_calls"]
    assert second_round[-1]["role"] == "tool"
    assert second_round[-1]["content"].startswith("Remembered")


async def test_a_bad_tool_call_does_not_crash_the_turn(memory, tmp_path):
    backend = FakeBackend(call("forget", fact_id="abc"), call("nope"), say("fine"))
    agent = make_agent(memory, tmp_path, backend)
    events = await run(agent, message="x")
    assert events[-1]["reply"] == "fine"


async def test_last_round_offers_no_tools(memory, tmp_path):
    backend = FakeBackend(call("remember", fact="a"), call("remember", fact="b"), say("done"))
    agent = make_agent(memory, tmp_path, backend, max_tool_rounds=2)
    await run(agent, message="x")
    assert [tools is not None for _, tools in backend.calls] == [True, True, False]


async def test_empty_answers_are_not_stored(memory, tmp_path):
    agent = make_agent(memory, tmp_path, FakeBackend(say()))
    events = await run(agent, message="hi")
    assert events[-1]["reply"] == ""
    assert memory.history("cli:erwan", 10) == []


async def test_messages_from_other_people_are_labelled(memory, tmp_path):
    backend = FakeBackend(say("a"), say("b"))
    agent = make_agent(memory, tmp_path, backend)
    await run(agent, user_id="alice", user_name="Alice", conversation="room", message="hello")
    await run(agent, user_id="bob", user_name="Bob", conversation="room", message="and me?")

    history = backend.calls[1][0][1:-1]
    assert history[0] == {"role": "user", "content": "Alice: hello"}
    assert history[1] == {"role": "assistant", "content": "a"}
    last = backend.calls[1][0][-1]
    assert last["role"] == "user" and untimed(last["content"]) == "and me?"


async def test_turns_of_one_conversation_never_overlap(memory, tmp_path):
    active = peak = 0

    class SlowBackend(FakeBackend):
        async def stream(self, messages, tools):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            for chunk in say("ok"):
                yield chunk

    agent = make_agent(memory, tmp_path, SlowBackend())
    await asyncio.gather(*(run(agent, conversation="same", message=f"m{i}") for i in range(3)))
    assert peak == 1

    peak = 0
    await asyncio.gather(*(run(agent, conversation=f"c{i}", message="m") for i in range(2)))
    assert peak == 2


def test_system_prompt_rereads_the_file_when_edited(tmp_path):
    path = tmp_path / "prompt.md"
    prompt = SystemPrompt(path)
    assert "Clara" in prompt.personality()  # default while the file is missing
    path.write_text("You are Test.", encoding="utf-8")
    assert prompt.personality() == "You are Test."
    path.write_text("You are Other.", encoding="utf-8")
    import os

    os.utime(path, (1, 1))  # force a different mtime
    assert prompt.personality() == "You are Other."
    rendered = prompt.render(
        __import__("clara.memory", fromlist=["Person"]).Person(1, "Zoe"), "cli", [], datetime.now()
    )
    assert "Zoe" in rendered and "(nothing yet)" in rendered
    assert "Date:" in rendered and not re.search(r"\d\d:\d\d", rendered)  # no time of day


async def test_consecutive_turns_share_the_system_prompt_and_the_replayed_history(memory, tmp_path):
    """Ollama can only reuse its cache of a prompt prefix that did not change."""
    clock_values = iter([datetime(2026, 10, 2, 9, 5).astimezone(), datetime(2026, 10, 2, 17, 40).astimezone()] * 3)
    backend = FakeBackend(say("a1"), say("a2"), say("a3"))
    agent = make_agent(memory, tmp_path, backend, clock=lambda tz: next(clock_values))
    for text in ("q1", "q2", "q3"):
        await run(agent, message=text)

    second, third = backend.calls[1][0], backend.calls[2][0]
    assert second[0] == third[0] == backend.calls[0][0][0]  # same system prompt, whatever the time
    assert third[:3] == [second[0], {"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    assert second[-1]["content"] == "[time: 17:40]\n\nq2"  # the time rides on the newest message...
    assert [m.content for m in memory.history("cli:erwan", 10)] == ["q1", "a1", "q2", "a2", "q3", "a3"]  # ...only


async def test_the_timezone_of_the_client_sets_the_date_and_time(memory, tmp_path):
    from datetime import timezone

    def clock(name):
        return datetime(2026, 10, 2, 23, 30, tzinfo=timezone.utc).astimezone(ZoneInfo(name)) if name else None

    backend = FakeBackend(say("ok"))
    agent = make_agent(memory, tmp_path, backend, clock=clock)
    await run(agent, message="hi", timezone="Asia/Tokyo")
    assert "2026-10-03" in backend.calls[0][0][0]["content"]  # already tomorrow there
    assert backend.calls[0][0][-1]["content"].startswith("[time: 08:30]")


def test_an_unknown_timezone_is_refused(memory, tmp_path):
    agent = make_agent(memory, tmp_path, FakeBackend())
    request = ChatRequest("cli", "erwan", None, "hi", timezone="Mars/Olympus")
    try:
        agent.validate(request)
    except ValueError as error:
        assert "Mars/Olympus" in str(error)
    else:
        raise AssertionError("expected ValueError")


async def test_texts_of_consecutive_model_rounds_are_separated(memory, tmp_path):
    from clara.llm import LlmChunk, ToolCall

    backend = FakeBackend(
        [LlmChunk(text="Let me look."), LlmChunk(tool_calls=[ToolCall("remember", {"fact": "Likes tea"})])],
        say("Here is ", "what I found."),
    )
    agent = make_agent(memory, tmp_path, backend)
    events = await run(agent, message="look")

    streamed = "".join(e["text"] for e in events if e["type"] == "token")
    assert streamed == "Let me look.\n\nHere is what I found."
    assert events[-1]["reply"] == streamed
    stored = [m.content for m in memory.history("cli:erwan", 10) if m.role == "assistant"]
    assert stored == ["Let me look.", "Here is what I found."]  # the stored rows stay as the model wrote them


async def test_no_separator_when_the_first_round_said_nothing(memory, tmp_path):
    from clara.llm import LlmChunk, ToolCall

    backend = FakeBackend(
        [LlmChunk(tool_calls=[ToolCall("remember", {"fact": "Likes tea"})])], say("Done."),
    )
    events = await run(make_agent(memory, tmp_path, backend), message="remember")
    assert events[-1]["reply"] == "Done."
