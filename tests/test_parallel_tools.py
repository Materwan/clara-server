"""The server's tools of one round: those that only read over the network run at the same time, the others in
the order the model asked."""

import asyncio

from conftest import FakeBackend, say

from clara.agent import Agent, ChatRequest
from clara.llm import LlmChunk, ToolCall
from clara.prompt import SystemPrompt
from clara.tools import Tool, Toolbox


def calls(*names: str) -> list[LlmChunk]:
    return [LlmChunk(tool_calls=[ToolCall(name, {"label": f"{name}-{index}"}) for index, name in enumerate(names)])]


def toolbox(log: list[str], running: list[int], most: list[int]) -> Toolbox:
    async def fetch(context, label: str) -> str:
        running[0] += 1
        most[0] = max(most[0], running[0])
        log.append(f"start {label}")
        await asyncio.sleep(0.05)
        log.append(f"end {label}")
        running[0] -= 1
        return f"page {label}"

    def write(context, label: str) -> str:
        log.append(f"write {label}")
        return "written"

    return Toolbox([
        Tool("fetch", "Read a page.", fetch, {"label": {"type": "string"}}, parallel=True),
        Tool("write", "Write a file.", write, {"label": {"type": "string"}}),
    ])


async def run(memory, tmp_path, *names: str) -> tuple[list[dict], list[str], int]:
    log: list[str] = []
    running, most = [0], [0]
    backend = FakeBackend(calls(*names), say("done"))
    agent = Agent(memory, backend, toolbox(log, running, most), SystemPrompt(tmp_path / "none.md"))
    request = ChatRequest(surface="cli", user_id="erwan", user_name="Erwan", message="go")
    events = [event async for event in agent.turn(request)]
    return events, log, most[0]


async def test_reads_over_the_network_run_at_the_same_time(memory, tmp_path):
    events, log, most = await run(memory, tmp_path, "fetch", "fetch", "fetch")
    assert most == 3
    tools = [event for event in events if event["type"] == "tool"]
    assert [event["result"] for event in tools] == ["page fetch-0", "page fetch-1", "page fetch-2"]  # in order
    assert events[-1]["reply"] == "done"


async def test_a_write_waits_for_the_reads_before_it_and_holds_back_those_after(memory, tmp_path):
    events, log, most = await run(memory, tmp_path, "fetch", "fetch", "write", "fetch")
    assert most == 2
    assert log.index("write write-2") > max(log.index("end fetch-0"), log.index("end fetch-1"))
    assert log.index("start fetch-3") > log.index("write write-2")
    kinds = [event["type"] for event in events if event["type"] in ("tool_start", "tool")]
    assert kinds == ["tool_start", "tool_start", "tool", "tool", "tool_start", "tool", "tool_start", "tool"]
