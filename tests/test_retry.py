"""A model that is overloaded or unreachable is asked again, but only before it has said anything."""

import httpx
import pytest
from conftest import FakeBackend

from clara.agent import Agent, ChatRequest
from clara.llm import LlmChunk, LlmError
from clara.prompt import SystemPrompt
from clara.retry import MAX_WAIT, delay_before, retryable
from clara.tools import default_toolbox


class Flaky(FakeBackend):
    """Fails with `errors` one after the other (before saying anything), then answers; `after_text` says a word first."""

    def __init__(self, *errors, after_text: bool = False):
        super().__init__(model="flaky")
        self.errors = list(errors)
        self.after_text = after_text
        self.attempts = 0

    async def stream(self, messages, tools):
        self.attempts += 1
        if self.errors:
            if self.after_text:
                yield LlmChunk(text="Hel")
            raise self.errors.pop(0)
        yield LlmChunk(text="Hello")
        yield LlmChunk(prompt_tokens=5, completion_tokens=1)


def make_agent(memory, tmp_path, backend, **options) -> Agent:
    options.setdefault("retry_delay", 0.001)
    return Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"), **options)


async def events_of(agent):
    return [event async for event in agent.turn(ChatRequest("cli", "erwan", "Erwan", "hi"))]


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_busy_statuses_are_retried(status):
    assert retryable(LlmError("busy", status))


@pytest.mark.parametrize("status", [400, 401, 403, 404, None])
def test_the_other_refusals_are_not(status):
    assert not retryable(LlmError("no", status))


def test_a_connection_that_failed_is_retried_but_a_silent_model_is_not():
    assert retryable(httpx.ConnectError("down"))
    assert retryable(httpx.RemoteProtocolError("cut"))
    assert retryable(ConnectionError("refused"))
    assert not retryable(httpx.ReadTimeout("silent"))
    assert not retryable(ValueError("a bug"))


def test_the_wait_doubles_and_gives_way_to_retry_after():
    first, third = delay_before(RuntimeError(), 0, 2.0), delay_before(RuntimeError(), 2, 2.0)
    assert 1.5 <= first <= 2.5 and 6 <= third <= 10
    assert delay_before(LlmError("busy", 429, retry_after=7), 0, 2.0) == 7
    assert delay_before(LlmError("busy", 429, retry_after=MAX_WAIT + 1), 0, 2.0) is None


async def test_a_busy_model_is_asked_again_and_the_person_is_told(memory, tmp_path):
    backend = Flaky(LlmError("high demand", 503), LlmError("high demand", 503))
    events = await events_of(make_agent(memory, tmp_path, backend))
    assert backend.attempts == 3
    assert [e["message"] for e in events if e["type"] == "retrying"] == [
        "The model is busy, trying again (1/3)…", "The model is busy, trying again (2/3)…",
    ]
    assert events[-1]["type"] == "done" and events[-1]["reply"] == "Hello"


async def test_it_gives_up_after_the_retries(memory, tmp_path):
    backend = Flaky(*[LlmError("high demand", 503)] * 5)
    with pytest.raises(LlmError, match="high demand"):
        await events_of(make_agent(memory, tmp_path, backend, retries=2))
    assert backend.attempts == 3


async def test_a_refusal_is_not_retried(memory, tmp_path):
    backend = Flaky(LlmError("bad key", 401))
    with pytest.raises(LlmError):
        await events_of(make_agent(memory, tmp_path, backend))
    assert backend.attempts == 1


async def test_nothing_is_asked_again_once_the_model_has_started_to_answer(memory, tmp_path):
    backend = Flaky(LlmError("cut", 503), after_text=True)
    with pytest.raises(LlmError):
        await events_of(make_agent(memory, tmp_path, backend))
    assert backend.attempts == 1


async def test_no_retries_when_there_are_none(memory, tmp_path):
    backend = Flaky(LlmError("high demand", 503))
    with pytest.raises(LlmError):
        await events_of(make_agent(memory, tmp_path, backend, retries=0))
    assert backend.attempts == 1


async def test_the_slot_is_free_while_waiting(memory, tmp_path):
    backend = Flaky(LlmError("high demand", 503))
    agent = make_agent(memory, tmp_path, backend, max_concurrent_llm=1)
    await events_of(agent)
    assert not agent._llm_slots.locked()
