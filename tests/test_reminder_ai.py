"""When a reminder comes due, Clara writes the announcement, in the conversation where it was set."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FakeBackend, fake_providers, say
from fastapi.testclient import TestClient

from clara.agent import Agent
from clara.announce import compose
from clara.memory import Memory
from clara.prompt import SystemPrompt
from clara.reminders import ReminderService
from clara.server import create_app
from clara.tools import ToolContext, default_toolbox

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
ORIGIN = ("cli", "erwan", "cli:erwan")


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class BrokenBackend(FakeBackend):
    async def stream(self, messages, tools):
        raise ConnectionError("Ollama is down")
        yield  # pragma: no cover


class SlowBackend(FakeBackend):
    async def stream(self, messages, tools):
        await asyncio.sleep(5)
        yield  # pragma: no cover


def setup(memory, tmp_path: Path, backend, timeout: float = 5.0):
    agent = Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"))
    clock = Clock()
    service = ReminderService(memory, clock)
    service.composer = lambda reminder: compose(agent, reminder, timeout)
    return agent, service, clock


def due_reminder(memory, service, clock, text="Dentist at 9", origin=ORIGIN):
    person = memory.resolve("cli", "erwan", "Erwan")
    service.create(person, text, (clock.now + timedelta(minutes=5)).isoformat(), origin=origin)
    clock.now += timedelta(minutes=5)
    return person


async def test_the_announcement_is_what_clara_wrote(memory, tmp_path):
    backend = FakeBackend(say("Erwan, your dentist is waiting for you at 9!"))
    _, service, clock = setup(memory, tmp_path, backend)
    due_reminder(memory, service, clock)

    assert await service.fire_due() == 1

    [event] = memory.reminder_events_after(0)
    assert event.message == "Erwan, your dentist is waiting for you at 9!"
    assert event.text == "Dentist at 9"  # the reminder itself is kept too
    memory.set_reminder_cursor("terminal/cli:erwan", 0)  # a client that has connected before
    stream = service.events("terminal", "cli", "erwan")
    payload = await asyncio.wait_for(anext(stream), 2)
    while payload["type"] != "reminder":  # (it also says the server is running)
        payload = await asyncio.wait_for(anext(stream), 2)
    assert payload["message"] == event.message and payload["text"] == "Dentist at 9"
    await stream.aclose()


async def test_she_writes_as_the_author_with_what_she_knows_about_them(memory, tmp_path):
    backend = FakeBackend(say("Time for the dentist!"))
    _, service, clock = setup(memory, tmp_path, backend)
    person = due_reminder(memory, service, clock)
    memory.add_fact(person.id, "Erwan likes jazz")

    await service.fire_due()

    messages, tools = backend.calls[0]
    system = messages[0]["content"]
    assert "Erwan likes jazz" in system and "shown to them as a notification" in system  # facts, and who reads it
    assert "[Reminder due] Dentist at 9" in messages[-1]["content"]
    assert not tools  # she can only write: no tool to call (no reminder from a reminder)


async def test_the_exchange_is_kept_in_the_conversation_where_it_was_set(memory, tmp_path):
    backend = FakeBackend(say("Time for the dentist!"))
    _, service, clock = setup(memory, tmp_path, backend)
    due_reminder(memory, service, clock, origin=("discord", "42", "discord:channel:7"))

    await service.fire_due()

    history = [(m.role, m.content) for m in memory.history("discord:channel:7", 10)]
    assert history == [("user", "[Reminder due] Dentist at 9"), ("assistant", "Time for the dentist!")]
    assert memory.history("cli:erwan", 10) == []


@pytest.mark.parametrize("backend", [BrokenBackend(), SlowBackend()])
async def test_if_she_cannot_write_it_the_text_is_announced(memory, tmp_path, backend):
    _, service, clock = setup(memory, tmp_path, backend, timeout=0.1)
    due_reminder(memory, service, clock)

    assert await service.fire_due() == 1  # fired all the same

    event, why = memory.reminder_events_after(0)
    assert event.message is None and event.text == "Dentist at 9"
    assert service.upcoming(memory.resolve("cli", "erwan", "Erwan")) == []
    # ...and the person is told why it is shown as typed
    assert (why.kind, why.source, why.person_id) == ("notification", "server", event.person_id)
    assert "could not write the announcement" in why.text and "Dentist at 9" in why.text


async def test_a_reminder_set_before_the_place_was_kept_is_announced_as_it_is(memory, tmp_path):
    backend = FakeBackend(say("never used"))
    _, service, clock = setup(memory, tmp_path, backend)
    due_reminder(memory, service, clock, origin=("", "", ""))

    await service.fire_due()

    assert backend.calls == [] and memory.reminder_events_after(0)[0].message is None


async def test_without_a_composer_the_text_is_announced(memory):
    clock = Clock()
    service = ReminderService(memory, clock)
    due_reminder(memory, service, clock)
    await service.fire_due()
    assert memory.reminder_events_after(0)[0].message is None


async def test_a_reminder_cancelled_while_clara_writes_is_not_announced(memory, tmp_path):
    clock = Clock()
    service = ReminderService(memory, clock)
    person = due_reminder(memory, service, clock)

    async def composer(reminder):
        service.cancel(person, reminder.id)
        return "too late"

    service.composer = composer

    assert await service.fire_due() == 0
    assert memory.reminder_events_after(0) == []


async def test_several_due_reminders_are_written_together_and_announced_in_order(memory, tmp_path):
    clock = Clock()
    service = ReminderService(memory, clock)
    person = memory.resolve("cli", "erwan", "Erwan")
    for name in ("first", "second", "third"):
        service.create(person, name, (clock.now + timedelta(minutes=5)).isoformat(), origin=ORIGIN)
    clock.now += timedelta(minutes=5)
    running = 0
    most = 0

    async def composer(reminder):
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.05)
        running -= 1
        return f"about {reminder.text}"

    service.composer = composer

    assert await service.fire_due() == 3
    assert most == 3  # not one after the other
    assert [e.message for e in memory.reminder_events_after(0)] == ["about first", "about second", "about third"]


async def test_a_repeating_reminder_is_announced_each_time(memory, tmp_path):
    backend = FakeBackend(say("Stand-up time!"), say("Stand-up time again!"))
    _, service, clock = setup(memory, tmp_path, backend)
    person = memory.resolve("cli", "erwan", "Erwan")
    service.create(person, "Stand-up", (clock.now + timedelta(minutes=5)).isoformat(), "daily", origin=ORIGIN)
    clock.now += timedelta(minutes=5)
    await service.fire_due()
    clock.now += timedelta(days=1)
    await service.fire_due()
    assert [e.message for e in memory.reminder_events_after(0)] == ["Stand-up time!", "Stand-up time again!"]


# --- where a reminder remembers it was set ----------------------------------------------------------


def test_the_model_tool_remembers_the_conversation(memory):
    service = ReminderService(memory, Clock())
    person = memory.resolve("discord", "42", "Alice")
    context = ToolContext(person, memory, service, None, "discord", "42", "discord:channel:7")
    default_toolbox().run("remind", context, {"text": "Call mum", "when": (NOW + timedelta(days=400)).isoformat()})
    [reminder] = service.upcoming(person)
    assert (reminder.surface, reminder.user_id, reminder.conversation) == ("discord", "42", "discord:channel:7")


def test_the_http_route_remembers_where_it_was_set(settings):
    with TestClient(create_app(settings, fake_providers(settings, FakeBackend()))) as client:
        when = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        headers = {"Authorization": "Bearer secret-cli"}
        body = {"surface": "cli", "user_id": "erwan", "user_name": "Erwan", "text": "Dentist", "at": when}
        assert client.post("/v1/reminders", json=body, headers=headers).status_code == 201
        assert client.post("/v1/reminders", json={**body, "conversation": "cli:work"}, headers=headers).status_code == 201
        memory = client.app.state.memory
        person = memory.find_person("cli", "erwan")
        assert [(r.surface, r.user_id, r.conversation) for r in memory.reminders_of(person.id)] == [
            ("cli", "erwan", "cli:erwan"),
            ("cli", "erwan", "cli:work"),
        ]


def test_the_server_writes_announcements_unless_told_not_to(settings):
    from dataclasses import replace

    on = create_app(settings, fake_providers(settings, FakeBackend()))
    off = create_app(replace(settings, reminder_ai_timeout=0), fake_providers(settings, FakeBackend()))
    assert on.state.reminders.composer is not None and off.state.reminders.composer is None


def test_the_timeout_comes_from_the_environment():
    from clara.settings import Settings, SettingsError

    base = {"CLARA_TOKENS": "terminal:secret-cli"}
    assert Settings.from_env(base).reminder_ai_timeout == 60
    assert Settings.from_env({**base, "CLARA_REMINDER_AI_TIMEOUT": "0"}).reminder_ai_timeout == 0
    with pytest.raises(SettingsError):
        Settings.from_env({**base, "CLARA_REMINDER_AI_TIMEOUT": "-1"})


# --- databases from before ----------------------------------------------------------------------------


def test_a_database_from_the_previous_version_gets_the_new_columns(tmp_path):
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO people (name, created_at) VALUES ('Erwan', 'x');
        CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, person_id INTEGER NOT NULL, text TEXT NOT NULL,
            due_at TEXT NOT NULL, anchor_at TEXT NOT NULL, repeat TEXT NOT NULL DEFAULT '',
            timezone TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        INSERT INTO reminders (person_id, text, due_at, anchor_at, created_at)
            VALUES (1, 'Old one', '2030-01-01T00:00:00+00:00', '2030-01-01T00:00:00+00:00', 'x');
        CREATE TABLE reminder_events (id INTEGER PRIMARY KEY AUTOINCREMENT, person_id INTEGER, text TEXT NOT NULL,
            due_at TEXT NOT NULL, fired_at TEXT NOT NULL);
        INSERT INTO reminder_events (person_id, text, due_at, fired_at) VALUES (1, 'Old event', 'a', 'b');
        """
    )
    old.commit()
    old.close()

    memory = Memory(path)
    try:
        [reminder] = memory.reminders_of(1)
        assert (reminder.text, reminder.surface, reminder.conversation) == ("Old one", "", "")
        [event] = memory.reminder_events_after(0)
        assert (event.text, event.message) == ("Old event", None)
    finally:
        memory.close()
