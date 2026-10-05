"""Stopping the server: clients are told, new questions are refused, running replies and agents finish."""

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from conftest import FakeBackend, call, fake_providers, say

from clara.agent import Agent, ChatRequest, ServerStopping
from clara.lifecycle import Lifecycle
from clara.prompt import SystemPrompt
from clara.reminders import ReminderService
from clara.server import ClaraServer, create_app
from clara.tools import default_toolbox

ADMIN = {"Authorization": "Bearer secret-admin"}
CHAT = {"Authorization": "Bearer secret-cli"}
READ_FILE = {
    "type": "function",
    "function": {"name": "read_file", "description": "Read.", "parameters": {"type": "object", "properties": {}}},
}


class GatedBackend(FakeBackend):
    """Answers only when `gate` is opened: a reply that takes as long as the test wants."""

    def __init__(self, *rounds):
        super().__init__(*rounds)
        self.gate = asyncio.Event()

    async def stream(self, messages, tools):
        await self.gate.wait()
        async for chunk in super().stream(messages, tools):
            yield chunk


def make(memory, tmp_path: Path, backend):
    agent = Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"))
    reminders = ReminderService(memory)
    lifecycle = Lifecycle(agent, reminders, poll=0.01, flush=0.01)
    exits = []
    lifecycle.on_exit = exits.append
    return agent, reminders, lifecycle, exits


def ask(message="hi", **fields) -> ChatRequest:
    return ChatRequest("cli", "erwan", "Erwan", message, **fields)


async def run_turn(agent, request=None):
    return [event async for event in agent.turn(request or ask(), "cli")]


async def until(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.005)


async def test_with_nothing_running_it_stops_at_once(memory, tmp_path):
    agent, _, lifecycle, exits = make(memory, tmp_path, FakeBackend())
    assert not lifecycle.stopping and lifecycle.state == "running"

    message = lifecycle.request_stop()

    assert "nothing is running" in message and lifecycle.state == "stopping"
    await until(lambda: exits)
    assert exits == [False]
    assert agent.accepting is False


async def test_a_running_reply_is_awaited_and_new_questions_are_refused(memory, tmp_path):
    backend = GatedBackend(say("Done."))
    agent, _, lifecycle, exits = make(memory, tmp_path, backend)
    running = asyncio.ensure_future(run_turn(agent))
    await until(lambda: agent.busy)

    message = lifecycle.request_stop()

    assert "waiting for 1 running" in message
    with pytest.raises(ServerStopping):
        await run_turn(agent, ask("a new one"))
    await asyncio.sleep(0.1)
    assert exits == []  # the reply is still being written
    backend.gate.set()
    events = await running
    assert events[-1]["type"] == "done" and events[-1]["reply"] == "Done."  # it was not cut
    await until(lambda: exits)
    assert exits == [False]


async def test_an_agent_waiting_for_its_clients_tools_is_awaited_too(memory, tmp_path):
    backend = FakeBackend(call("read_file"), say("It says hello."))
    agent, _, lifecycle, exits = make(memory, tmp_path, backend)
    asked = []

    async def agent_turn():
        async for event in agent.turn(ask(tools=(READ_FILE,)), "cli"):
            if event["type"] == "tool_requests":
                asked.append(event)  # the client is now busy running its tool
        return event

    running = asyncio.ensure_future(agent_turn())
    await until(lambda: asked)

    lifecycle.request_stop()
    await asyncio.sleep(0.1)
    assert exits == []

    agent.submit_results(asked[0]["turn"], "cli", {asked[0]["calls"][0]["id"]: "contents"})  # still accepted
    assert (await running)["reply"] == "It says hello."
    await until(lambda: exits)


async def test_stop_now_does_not_wait(memory, tmp_path):
    backend = GatedBackend(say("never"))
    agent, _, lifecycle, exits = make(memory, tmp_path, backend)
    running = asyncio.ensure_future(run_turn(agent))
    await until(lambda: agent.busy)
    lifecycle.request_stop()
    await asyncio.sleep(0.05)
    assert exits == []

    assert "Stopping now" in lifecycle.request_stop(force=True)

    await until(lambda: exits)
    assert exits == [True]
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)


async def test_asking_twice_says_so(memory, tmp_path):
    backend = GatedBackend(say("x"))
    agent, _, lifecycle, _ = make(memory, tmp_path, backend)
    running = asyncio.ensure_future(run_turn(agent))
    await until(lambda: agent.busy)
    lifecycle.request_stop()
    assert "Already stopping: waiting for 1" in lifecycle.request_stop()
    lifecycle.request_stop(force=True)
    assert "Already stopping now" in lifecycle.request_stop(force=True)
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)


async def test_a_compaction_counts_as_running(memory, tmp_path):
    backend = GatedBackend(say("a1"), say("summary"))
    agent, _, lifecycle, exits = make(memory, tmp_path, backend)
    backend.gate.set()
    await run_turn(agent)
    backend.gate.clear()
    compacting = asyncio.ensure_future(agent.compact("cli:erwan"))
    await until(lambda: agent.busy)
    lifecycle.request_stop()
    await asyncio.sleep(0.1)
    assert exits == []
    backend.gate.set()
    await compacting
    await until(lambda: exits)


async def test_clients_are_told_it_is_stopping_then_stopped_and_the_stream_ends(memory, tmp_path):
    backend = GatedBackend(say("Done."))
    agent, reminders, lifecycle, exits = make(memory, tmp_path, backend)
    stream = reminders.events("terminal")
    seen = []

    async def listen():
        async for event in stream:
            seen.append(event)

    listening = asyncio.ensure_future(listen())
    await until(lambda: [e["state"] for e in seen] == ["running"])
    running = asyncio.ensure_future(run_turn(agent))
    await until(lambda: agent.busy)

    lifecycle.request_stop()

    await until(lambda: [e["state"] for e in seen] == ["running", "stopping"])
    assert "finishes what is running" in seen[-1]["message"]
    backend.gate.set()
    await running
    await asyncio.wait_for(listening, 2)  # the stream ended by itself
    assert [e["state"] for e in seen] == ["running", "stopping", "stopped"]
    assert seen[-1]["message"] == "Clara is not running"


async def test_a_client_that_connects_while_stopping_is_told_at_once(memory, tmp_path):
    backend = GatedBackend(say("x"))
    agent, reminders, lifecycle, _ = make(memory, tmp_path, backend)
    running = asyncio.ensure_future(run_turn(agent))
    await until(lambda: agent.busy)
    lifecycle.request_stop()

    first = await asyncio.wait_for(anext(reminders.events("late")), 2)

    assert (first["type"], first["state"]) == ("server", "stopping")
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)


async def test_reminders_that_come_due_while_stopping_wait_for_the_next_start(memory, tmp_path):
    agent, reminders, lifecycle, exits = make(memory, tmp_path, FakeBackend())
    person = memory.resolve("cli", "erwan", "Erwan")
    memory.add_reminder(person.id, "Later", reminders._clock())
    runner = asyncio.ensure_future(reminders.run())
    lifecycle.request_stop()
    await until(lambda: exits)
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)
    assert memory.reminder_events_after(0) == []  # still waiting in the database
    assert [r.text for r in reminders.upcoming(person)] == ["Later"]


async def test_a_reminder_being_announced_is_awaited(memory, tmp_path):
    agent, reminders, lifecycle, exits = make(memory, tmp_path, FakeBackend())
    person = memory.resolve("cli", "erwan", "Erwan")
    memory.add_reminder(person.id, "Dentist", reminders._clock(), origin=("cli", "erwan", "cli:erwan"))
    release = asyncio.Event()

    async def slow_composer(reminder):
        await release.wait()
        return "Your dentist is waiting."

    reminders.composer = slow_composer
    firing = asyncio.ensure_future(reminders.fire_due())
    await until(lambda: reminders.firing)

    lifecycle.request_stop()
    await asyncio.sleep(0.1)
    assert exits == []
    release.set()
    await firing
    await until(lambda: exits)
    assert [e.message for e in memory.reminder_events_after(0)] == ["Your dentist is waiting."]


async def test_a_task_reminder_being_written_is_awaited_and_none_start_while_stopping(memory, tmp_path):
    from datetime import timedelta

    from clara.notifications import Notifier
    from clara.tasks import Followup, TaskService

    agent, reminders, _, _ = make(memory, tmp_path, FakeBackend())
    tasks = TaskService(memory, Notifier(memory))
    lifecycle = Lifecycle(agent, reminders, poll=0.01, flush=0.01, tasks=tasks)
    exits = []
    lifecycle.on_exit = exits.append
    person = memory.resolve("cli", "erwan", "Erwan")
    soon = (tasks._clock() + timedelta(seconds=1)).isoformat()
    task = await tasks.create(person, "Dentist", reminders=[soon])
    tasks._clock = lambda: tasks.store.get_any(task.id).next[0] + timedelta(seconds=1)  # it is due
    release = asyncio.Event()

    async def slow_follower(task, now):
        await release.wait()
        return Followup("Your dentist is waiting.", None)

    tasks.follower = slow_follower
    firing = asyncio.ensure_future(tasks.fire_due())
    await until(lambda: tasks.firing)

    lifecycle.request_stop()
    assert tasks.stopping  # nothing new is written from now on
    await asyncio.sleep(0.1)
    assert exits == []  # the one being written is awaited
    release.set()
    await firing
    await until(lambda: exits)
    assert [e.text for e in memory.reminder_events_after(0)] == ["Your dentist is waiting."]


# --- over HTTP, with the real server class -------------------------------------------------


@pytest.fixture
def live(settings):
    """A real server (with Ctrl+C handling) on a free port, and its thread."""
    backend = GatedBackend(say("Hello."))
    app = create_app(settings, fake_providers(settings, backend))
    server = ClaraServer(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"), app.state.lifecycle)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield app, server, thread, f"http://127.0.0.1:{port}", backend
    server.should_exit = server.force_exit = True
    thread.join(10)


def read_states(url: str, into: list, ended: threading.Event) -> threading.Thread:
    def read():
        with httpx.stream("GET", f"{url}/v1/reminders/stream", headers=CHAT, timeout=20) as response:
            for line in response.iter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    if event["type"] == "server":
                        into.append(event["state"])
        ended.set()

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    return thread


def test_stop_from_the_admin_console_finishes_the_reply_then_ends_everything(live):
    app, server, thread, url, backend = live
    states, ended = [], threading.Event()
    read_states(url, states, ended)
    deadline = time.monotonic() + 5
    while states != ["running"] and time.monotonic() < deadline:
        time.sleep(0.01)

    answer = {}

    def chat():
        answer["response"] = httpx.post(
            f"{url}/v1/chat", json={"surface": "cli", "user_id": "erwan", "message": "hi"}, headers=CHAT, timeout=20
        )

    asking = threading.Thread(target=chat, daemon=True)
    asking.start()
    while not app.state.agent.busy:
        time.sleep(0.01)

    said = httpx.post(f"{url}/v1/admin/command", json={"line": "/stop"}, headers=ADMIN).json()["output"]
    assert "waiting for 1 running" in said

    refused = httpx.post(f"{url}/v1/chat", json={"surface": "cli", "user_id": "x", "message": "me too"}, headers=CHAT)
    assert refused.status_code == 503 and "stopping" in refused.json()["detail"]
    status = httpx.post(f"{url}/v1/admin/command", json={"line": "/status"}, headers=ADMIN).json()["output"]
    assert "STOPPING" in status
    assert thread.is_alive() and not ended.is_set()  # the reply is still being written

    server.lifecycle.loop.call_soon_threadsafe(backend.gate.set)  # the gate belongs to the server's loop
    asking.join(10)
    assert answer["response"].status_code == 200 and answer["response"].json()["reply"] == "Hello."
    thread.join(10)
    assert not thread.is_alive()  # the process would exit now
    assert ended.wait(5)
    assert states == ["running", "stopping", "stopped"]


def test_stop_is_for_admins_only(live):
    _, _, _, url, _ = live
    assert httpx.post(f"{url}/v1/admin/command", json={"line": "/stop"}, headers=CHAT).status_code == 401


def test_a_signal_stops_gracefully_and_a_second_one_forces(live):
    app, server, thread, url, backend = live
    def chat_and_be_cut():
        with pytest.raises(httpx.HTTPError):  # the forced stop cuts the connection
            httpx.post(
                f"{url}/v1/chat", json={"surface": "cli", "user_id": "erwan", "message": "hi"}, headers=CHAT, timeout=20
            )

    asking = threading.Thread(target=chat_and_be_cut, daemon=True)
    asking.start()
    while not app.state.agent.busy:
        time.sleep(0.01)

    server.handle_exit(2, None)  # Ctrl+C
    deadline = time.monotonic() + 5
    while not app.state.lifecycle.stopping and time.monotonic() < deadline:
        time.sleep(0.01)
    assert app.state.lifecycle.stopping
    time.sleep(0.3)
    assert thread.is_alive()  # it waits for the reply

    server.handle_exit(2, None)  # Ctrl+C again
    thread.join(10)
    assert not thread.is_alive()
    asking.join(5)
    assert not asking.is_alive()  # its request was cut
