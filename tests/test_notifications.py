"""Notifications: sent by Clara, by a client or by the server itself, to one person's clients."""

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import FakeBackend, call, fake_providers, say
from fastapi.testclient import TestClient

from clara.agent import Agent, ChatRequest
from clara.commands import CommandContext, registry
from clara.notifications import RATE_LIMIT, NotificationError, Notifier, clean_targets
from clara.prompt import SystemPrompt
from clara.server import create_app
from clara.tools import NOTIFY_PER_TURN, ToolContext, default_toolbox

AUTH = {"Authorization": "Bearer secret-cli"}
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def notifier(memory):
    return Notifier(memory, lambda: NOW)


@pytest.fixture
def erwan(memory):
    return memory.resolve("cli", "erwan", "Erwan")


def notifications(memory) -> list:
    return [event for event in memory.reminder_events_after(0) if event.kind == "notification"]


# --- the notifier -------------------------------------------------------------------------


def test_targets_are_cleaned_and_checked():
    assert clean_targets(["App", "app", " discord "]) == ("app", "discord")
    assert clean_targets("app, cli") == ("app", "cli")
    assert clean_targets(None) == ()
    with pytest.raises(NotificationError):
        clean_targets(["two words"])


def test_a_notification_is_stored_for_its_person(notifier, memory, erwan):
    event = notifier.notify(erwan.id, "  Done!  ", "Build", ["app"], "terminal", "cli:erwan")
    assert (event.kind, event.text, event.title, event.targets, event.source) == (
        "notification", "Done!", "Build", ("app",), "terminal"
    )
    assert (event.person_id, event.conversation) == (erwan.id, "cli:erwan")


@pytest.mark.parametrize(("text", "title"), [("", ""), ("x" * 2001, ""), ("ok", "t" * 101)])
def test_what_is_invalid_is_refused(notifier, erwan, text, title):
    with pytest.raises(NotificationError):
        notifier.notify(erwan.id, text, title)


def test_a_person_cannot_be_flooded_but_the_server_is_not_counted(notifier, erwan):
    for number in range(RATE_LIMIT):
        notifier.notify(erwan.id, f"n{number}")
    with pytest.raises(NotificationError, match="Too many"):
        notifier.notify(erwan.id, "one more")
    notifier.notify(erwan.id, "from the server", limited=False)  # still goes


async def test_a_listener_gets_its_persons_notifications_on_its_surface(notifier, memory, erwan):
    memory.link_account("app", "pc", erwan)
    memory.set_reminder_cursor("terminal/app:pc", 0)
    memory.set_reminder_cursor("terminal/cli:erwan", 0)
    bob = memory.resolve("cli", "bob", "Bob")
    notifier.notify(erwan.id, "for the app only", targets=["app"])
    notifier.notify(bob.id, "for bob")
    notifier.broadcast("for everybody")

    async def texts(surface, user_id):
        stream = notifier.events("terminal", surface, user_id)
        found = []
        async for event in stream:
            if event["type"] == "server":
                break  # what was waiting comes first, then the state of the server
            found.append(event["text"])
        await stream.aclose()
        return found

    assert await texts("app", "pc") == ["for the app only", "for everybody"]
    assert await texts("cli", "erwan") == ["for everybody"]


# --- the model's tool ----------------------------------------------------------------------


def run_notify(memory, notifier, person, counts=None, **arguments) -> str:
    counts = {} if counts is None else counts
    context = ToolContext(person, memory, notifier=notifier, conversation="cli:erwan", counts=counts)
    return default_toolbox().run("notify", context, arguments)


def test_clara_can_notify_the_person_she_talks_to(memory, notifier, erwan):
    answer = run_notify(memory, notifier, erwan, text="Your export is ready", targets=["cli"])
    assert answer.startswith("Notification") and "on cli" in answer
    [event] = notifications(memory)
    assert (event.person_id, event.source, event.text) == (erwan.id, "clara", "Your export is ready")


def test_clara_is_warned_about_a_surface_the_person_does_not_use(memory, notifier, erwan):
    answer = run_notify(memory, notifier, erwan, text="Hi", targets="app")
    assert "no account on app" in answer


def test_clara_cannot_notify_without_limit_in_one_answer(memory, notifier, erwan):
    counts: dict = {}
    for _ in range(NOTIFY_PER_TURN):
        assert run_notify(memory, notifier, erwan, counts, text="ping").startswith("Notification")
    assert run_notify(memory, notifier, erwan, counts, text="ping").startswith("Error: At most")


def test_the_remind_tool_takes_targets(memory, erwan):
    from clara.reminders import ReminderService

    service = ReminderService(memory, lambda: NOW)
    context = ToolContext(erwan, memory, service, "Europe/Paris")
    answer = default_toolbox().run("remind", context, {"text": "Tea", "when": "2026-10-05T09:00", "targets": ["cli"]})
    assert "shown on cli" in answer
    assert service.upcoming(erwan)[0].targets == ("cli",)
    assert "on cli: Tea" in default_toolbox().run("list_reminders", context, {})


# --- what the server tells by itself ------------------------------------------------------------


def make_agent(memory, tmp_path: Path, backend, notifier, **options) -> Agent:
    return Agent(
        memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"), notifier=notifier, **options
    )


async def talk(agent: Agent, message: str, **fields) -> dict:
    final = {}
    async for event in agent.turn(ChatRequest("cli", "erwan", "Erwan", message, **fields)):
        final = event
    return final


async def test_a_long_turn_notifies_its_person_when_done(memory, tmp_path, notifier):
    agent = make_agent(memory, tmp_path, FakeBackend(say("All " * 100 + "done")), notifier, long_turn_seconds=1)
    agent_turn = agent._turn

    async def slow(request, owner):  # the turn takes a while
        await asyncio.sleep(1.05)
        async for event in agent_turn(request, owner):
            yield event

    agent._turn = slow
    await talk(agent, "Do the long thing")
    [event] = notifications(memory)
    assert (event.title, event.source, event.conversation) == ("Answer ready", "server", "cli:erwan")
    assert "finished answering in cli:erwan" in event.text and event.text.endswith("…")


async def test_a_quick_turn_or_a_quiet_one_notifies_nobody(memory, tmp_path, notifier):
    agent = make_agent(memory, tmp_path, FakeBackend(say("Hi"), say("Hi")), notifier, long_turn_seconds=0)
    await talk(agent, "hello")  # 0: never
    agent.long_turn_seconds = 3600
    await talk(agent, "hello")  # not long enough
    assert notifications(memory) == []


async def slow_turn(agent: Agent, seconds: float) -> None:
    """Make every turn of the agent take `seconds` more."""
    agent_turn = agent._turn

    async def slow(request, owner):
        await asyncio.sleep(seconds)
        async for event in agent_turn(request, owner):
            yield event

    agent._turn = slow


async def test_a_person_s_own_delay_replaces_the_servers(memory, tmp_path, notifier, erwan):
    agent = make_agent(memory, tmp_path, FakeBackend(say("Hi"), say("Hi")), notifier, long_turn_seconds=3600)
    await slow_turn(agent, 1.05)
    await talk(agent, "hello")  # the server would wait an hour
    assert notifications(memory) == []
    memory.set_notify_after(erwan.id, 1)
    await talk(agent, "hello")
    assert [event.title for event in notifications(memory)] == ["Answer ready"]


async def test_a_person_can_turn_the_notification_off(memory, tmp_path, notifier, erwan):
    agent = make_agent(memory, tmp_path, FakeBackend(say("Hi")), notifier, long_turn_seconds=1)
    await slow_turn(agent, 1.05)
    memory.set_notify_after(erwan.id, 0)  # never, whatever the server says
    await talk(agent, "hello")
    assert notifications(memory) == []


# --- the delay a person sets ----------------------------------------------------------------------


def test_the_delay_is_the_servers_until_a_person_sets_one(memory, erwan):
    assert memory.notify_after(erwan.id) is None
    assert memory.set_notify_after(erwan.id, 90) == 90
    assert memory.notify_after(erwan.id) == 90
    assert memory.set_notify_after(erwan.id, 0) == 0  # never is not "not set"
    assert memory.notify_after(erwan.id) == 0
    memory.set_notify_after(erwan.id, None)
    assert memory.notify_after(erwan.id) is None


@pytest.mark.parametrize("seconds", [-1, 7 * 86400 + 1])
def test_a_delay_out_of_range_is_refused(memory, erwan, seconds):
    with pytest.raises(ValueError):
        memory.set_notify_after(erwan.id, seconds)


def test_merging_two_people_keeps_the_delay_that_was_set(memory):
    kept = memory.resolve("cli", "erwan", "Erwan")
    other = memory.resolve("discord", "1", "Erwan")
    memory.set_notify_after(other.id, 30)
    memory.link_account("discord", "1", kept)
    assert memory.notify_after(kept.id) == 30


async def test_a_summarised_conversation_notifies_its_person(memory, tmp_path, notifier):
    backend = FakeBackend(say("one"), say("two"), say("A summary."))
    agent = make_agent(memory, tmp_path, backend, notifier, compact_percent=0, keep_recent_turns=1)
    await talk(agent, "first")
    await talk(agent, "second")
    await agent.compact("cli:erwan")
    [event] = notifications(memory)
    assert (event.title, event.targets, event.conversation) == ("Conversation summarised", ("cli",), "cli:erwan")


async def test_a_shared_conversation_summarised_notifies_nobody(memory, tmp_path, notifier):
    backend = FakeBackend(say("one"), say("two"), say("A summary."))
    agent = make_agent(memory, tmp_path, backend, notifier, compact_percent=0, keep_recent_turns=1)
    await talk(agent, "first", conversation="discord:channel:1")
    async for _ in agent.turn(ChatRequest("discord", "bob", "Bob", "second", "discord:channel:1")):
        pass
    await agent.compact("discord:channel:1")
    assert notifications(memory) == []


async def test_switching_provider_or_model_tells_everybody(settings, memory, tmp_path, notifier):
    providers = fake_providers(settings)
    agent = Agent(memory, providers, default_toolbox(), SystemPrompt(tmp_path / "none.md"))
    ctx = CommandContext(settings, memory, agent, providers, time.monotonic(), "here", None, notifier)
    await registry.execute("/provider cloud", ctx)
    await registry.execute("/model other-model", ctx)
    texts = [(event.person_id, event.text) for event in notifications(memory)]
    assert texts == [
        (None, "Clara now runs on Ollama API key (cloud), with the model fake-big."),
        (None, "Clara now uses the model other-model (Ollama API key)."),
    ]


# --- HTTP -----------------------------------------------------------------------------------------


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, fake_providers(settings, FakeBackend()))) as http:
        yield http


def test_a_client_can_send_a_notification(client):
    body = {"surface": "cli", "user_id": "erwan", "text": "Backup done", "title": "Backup", "targets": ["app"]}
    response = client.post("/v1/notifications", json=body, headers=AUTH)
    assert response.status_code == 201
    assert response.json()["targets"] == ["app"]
    [event] = notifications(client.app.state.memory)
    assert (event.text, event.title, event.source) == ("Backup done", "Backup", "terminal")


def test_http_refuses_bad_notifications(client):
    me = {"surface": "cli", "user_id": "erwan"}
    assert client.post("/v1/notifications", json={**me, "text": "x"}).status_code == 401
    assert client.post("/v1/notifications", json={**me, "text": ""}, headers=AUTH).status_code == 422
    bad = {**me, "text": "x", "targets": ["Not A Surface"]}
    assert client.post("/v1/notifications", json=bad, headers=AUTH).status_code == 422
    for _ in range(RATE_LIMIT):
        client.post("/v1/notifications", json={**me, "text": "x"}, headers=AUTH)
    assert client.post("/v1/notifications", json={**me, "text": "x"}, headers=AUTH).status_code == 429


def test_a_person_reads_and_sets_their_delay_over_http(client):
    me = {"surface": "cli", "user_id": "erwan"}
    default = client.app.state.settings.notify_long_turn
    assert client.get("/v1/settings", params=me).status_code == 401
    first = client.get("/v1/settings", params=me, headers=AUTH).json()  # nobody known yet: the defaults
    assert first == {"notify_after": None, "notify_after_default": default, "notify_after_effective": default}
    saved = client.patch("/v1/settings", json={**me, "notify_after": 45}, headers=AUTH).json()
    assert (saved["notify_after"], saved["notify_after_effective"]) == (45, 45)
    assert client.get("/v1/settings", params=me, headers=AUTH).json()["notify_after"] == 45
    assert client.patch("/v1/settings", json={**me, "notify_after": 0}, headers=AUTH).json()["notify_after_effective"] == 0
    back = client.patch("/v1/settings", json={**me, "notify_after": None}, headers=AUTH).json()
    assert (back["notify_after"], back["notify_after_effective"]) == (None, default)


@pytest.mark.parametrize("value", [-1, 7 * 86400 + 1, "soon"])
def test_http_refuses_a_bad_delay(client, value):
    body = {"surface": "cli", "user_id": "erwan", "notify_after": value}
    assert client.patch("/v1/settings", json=body, headers=AUTH).status_code == 422
    assert client.patch("/v1/settings", json={"surface": "cli", "user_id": "erwan"}, headers=AUTH).status_code == 422


def test_http_settings_respect_the_surface_limits(settings):
    limited = replace(settings, client_surfaces={"terminal": frozenset({"cli"})})
    with TestClient(create_app(limited, fake_providers(limited, FakeBackend()))) as http:
        body = {"surface": "discord", "user_id": "1", "notify_after": 5}
        assert http.patch("/v1/settings", json=body, headers=AUTH).status_code == 403
        assert http.get("/v1/settings", params={"surface": "discord", "user_id": "1"}, headers=AUTH).status_code == 403


def test_http_notifications_respect_the_surface_limits(settings):
    limited = replace(settings, client_surfaces={"terminal": frozenset({"cli"})})
    with TestClient(create_app(limited, fake_providers(limited, FakeBackend()))) as http:
        body = {"surface": "discord", "user_id": "1", "text": "x"}
        assert http.post("/v1/notifications", json=body, headers=AUTH).status_code == 403
        stream = http.get("/v1/notifications/stream", params={"surface": "discord", "user_id": "1"}, headers=AUTH)
        assert stream.status_code == 403
        # but it may show a notification on any surface of a person of its own
        mine = {"surface": "cli", "user_id": "erwan", "text": "x", "targets": ["discord"]}
        assert http.post("/v1/notifications", json=mine, headers=AUTH).status_code == 201


async def test_the_model_notifies_through_a_turn(memory, tmp_path, notifier):
    backend = FakeBackend(call("notify", text="Ready!"), say("I told you."))
    agent = make_agent(memory, tmp_path, backend, notifier)
    final = await talk(agent, "Tell me when ready")
    assert final["tools"] == ["notify"]
    assert [event.text for event in notifications(memory)] == ["Ready!"]
