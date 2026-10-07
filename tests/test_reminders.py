"""Reminders: set by a person, announced to that person's clients (on the surfaces chosen), kept for the
ones that were away."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import FakeBackend, fake_providers
from fastapi.testclient import TestClient

from clara.reminders import (
    MAX_PER_PERSON,
    ReminderError,
    ReminderService,
    next_occurrence,
    parse_moment,
)
from clara.server import create_app
from clara.tools import ToolContext, default_toolbox

AUTH = {"Authorization": "Bearer secret-cli"}
ME = {"surface": "cli", "user_id": "erwan"}
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(memory, clock):
    return ReminderService(memory, clock)


@pytest.fixture
def erwan(memory):
    return memory.resolve("cli", "erwan", "Erwan")


def at(**delta) -> str:
    """An ISO time (UTC) `delta` after NOW."""
    return (NOW + timedelta(**delta)).isoformat()


# --- reading a moment ----------------------------------------------------------------


def test_a_time_with_an_offset_is_converted_to_utc():
    moment, clock = parse_moment("2026-10-05T09:00+02:00", None)
    assert moment == datetime(2026, 10, 5, 7, 0, tzinfo=UTC)
    assert clock == "+02:00"


def test_a_time_without_offset_is_read_in_the_given_zone():
    moment, clock = parse_moment("2026-10-05T09:00", "Europe/Paris")  # summer time: UTC+2
    assert moment == datetime(2026, 10, 5, 7, 0, tzinfo=UTC)
    assert clock == "Europe/Paris"


def test_a_time_without_offset_or_zone_is_read_in_the_servers_zone():
    moment, clock = parse_moment("2026-10-05T09:00", None)
    assert moment == datetime(2026, 10, 5, 9, 0).astimezone(UTC)
    assert clock[0] in "+-"


@pytest.mark.parametrize("text", ["tomorrow", "2026-13-01T00:00", ""])
def test_a_time_that_is_not_iso_is_refused(text):
    with pytest.raises(ReminderError, match="ISO 8601"):
        parse_moment(text, None)


def test_an_unknown_zone_is_refused():
    with pytest.raises(ReminderError, match="Unknown timezone"):
        parse_moment("2026-10-05T09:00", "Mars/Olympus")


# --- creating -------------------------------------------------------------------------


def test_create_stores_a_reminder(service, erwan):
    reminder = service.create(erwan, "  Dentist\n at 9 ", at(hours=2))
    assert reminder.text == "Dentist at 9"
    assert reminder.due_at == NOW + timedelta(hours=2)
    assert [r.id for r in service.upcoming(erwan)] == [reminder.id]


@pytest.mark.parametrize(
    ("text", "when", "repeat", "message"),
    [
        ("", "+1h", "", "needs a text"),
        ("x" * 501, "+1h", "", "at most"),
        ("ok", "+1h", "yearly", "repeat must be"),
        ("ok", "past", "", "already past"),
    ],
)
def test_create_refuses_what_is_invalid(service, erwan, text, when, repeat, message):
    when = at(hours=1) if when == "+1h" else at(seconds=-1) if when == "past" else when
    with pytest.raises(ReminderError, match=message):
        service.create(erwan, text, when, repeat)
    assert service.upcoming(erwan) == []


def test_a_person_cannot_pile_up_reminders_without_limit(service, erwan):
    for _ in range(MAX_PER_PERSON):
        service.create(erwan, "again", at(days=1))
    with pytest.raises(ReminderError, match="At most"):
        service.create(erwan, "one more", at(days=1))


def test_only_the_author_can_cancel(service, memory, erwan):
    reminder = service.create(erwan, "mine", at(hours=1))
    other = memory.resolve("discord", "42", "Alice")
    assert service.cancel(other, reminder.id) is False
    assert service.cancel(erwan, reminder.id) is True
    assert service.upcoming(erwan) == []


# --- firing ---------------------------------------------------------------------------


async def test_a_one_off_fires_once_then_is_gone(service, memory, clock, erwan):
    service.create(erwan, "Dentist", at(minutes=30))
    assert await service.fire_due() == 0
    clock.advance(minutes=30)
    assert await service.fire_due() == 1
    assert await service.fire_due() == 0
    assert service.upcoming(erwan) == []
    [event] = memory.reminder_events_after(0)
    assert (event.text, event.author) == ("Dentist", "Erwan")


async def test_a_daily_reminder_comes_back_at_the_same_time(service, clock, erwan):
    service.create(erwan, "Stand-up", at(hours=1), "daily")
    clock.advance(hours=1)
    await service.fire_due()
    [again] = service.upcoming(erwan)
    assert again.due_at == NOW + timedelta(days=1, hours=1)


async def test_a_repeating_reminder_that_missed_several_fires_once(service, memory, clock, erwan):
    service.create(erwan, "Stand-up", at(hours=1), "daily")
    clock.advance(days=3, hours=2)  # the server was down
    assert await service.fire_due() == 1
    assert len(memory.reminder_events_after(0)) == 1
    [again] = service.upcoming(erwan)
    assert again.due_at == NOW + timedelta(days=4, hours=1)


async def test_a_weekly_reminder_waits_a_week(service, clock, erwan):
    service.create(erwan, "Bins", at(hours=1), "weekly")
    clock.advance(hours=1)
    await service.fire_due()
    assert service.upcoming(erwan)[0].due_at == NOW + timedelta(weeks=1, hours=1)


def test_monthly_on_the_31st_is_the_28th_in_february_and_the_31st_again_after(service, erwan):
    reminder = service.create(erwan, "Rent", "2027-01-31T08:00+00:00", "monthly")
    expected = ["2027-02-28", "2027-03-31", "2027-04-30", "2027-05-31"]
    after = reminder.due_at
    for day in expected:
        after = next_occurrence(reminder, after)
        assert after.date().isoformat() == day


def test_a_repeat_keeps_the_wall_clock_across_daylight_saving(service, erwan):
    # Paris leaves summer time on 2026-10-25: 09:00 stays 09:00 (07:00 UTC, then 08:00 UTC)
    reminder = service.create(erwan, "Stand-up", "2026-10-24T09:00", "daily", "Europe/Paris")
    assert reminder.due_at == datetime(2026, 10, 24, 7, 0, tzinfo=UTC)
    after = next_occurrence(reminder, reminder.due_at)
    assert after == datetime(2026, 10, 25, 8, 0, tzinfo=UTC)


# --- delivery to the clients ----------------------------------------------------------


async def next_event(stream, kind="reminder"):
    """The next event of that kind (a stream also says what the server is doing: those are skipped)."""
    async def find():
        while True:
            event = await anext(stream)
            if event["type"] == kind:
                return event

    return await asyncio.wait_for(find(), 2)


async def nothing_more(stream, wait=0.1):
    """No reminder comes (asking is also what records the previous one as sent)."""
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(next_event(stream), wait)


CLI = ("terminal", "cli", "erwan")  # a listener: the token's client, and the account it listens as


async def test_a_new_client_gets_what_fires_while_it_is_connected(service, clock, erwan):
    stream = service.events(*CLI)
    waiting = asyncio.ensure_future(next_event(stream))
    await asyncio.sleep(0)
    service.create(erwan, "Dentist", at(minutes=5))
    clock.advance(minutes=5)
    await service.fire_due()
    event = await waiting
    assert event["type"] == "reminder"
    assert event["text"] == "Dentist"
    assert event["from"] == "Erwan"
    assert event["targets"] == []
    assert event["due_at"] == (NOW + timedelta(minutes=5)).isoformat(timespec="seconds")
    await stream.aclose()


class Reader:
    """Reads a stream in the background: what it was sent so far, the state of the server left out."""

    def __init__(self, stream):
        self.stream = stream
        self.events: list[dict] = []
        self.task = asyncio.ensure_future(self._read())

    async def _read(self):
        async for event in self.stream:
            if event["type"] != "server":
                self.events.append(event)

    @property
    def texts(self) -> list[str]:
        return [event["text"] for event in self.events]

    async def close(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0.01)


async def test_every_client_of_the_person_gets_it_and_nobody_else(service, memory, clock, erwan):
    memory.link_account("app", "erwan-pc", erwan)  # the same person, on the desktop app
    memory.resolve("cli", "bob", "Bob")
    listeners = [CLI, ("terminal", "app", "erwan-pc"), ("terminal", "cli", "bob"), ("discord",)]
    readers = [Reader(service.events(*listener)) for listener in listeners]
    await settle()
    service.create(erwan, "Meeting", at(minutes=1))
    clock.advance(minutes=1)
    await service.fire_due()
    await settle()
    assert [reader.texts for reader in readers] == [["Meeting"], ["Meeting"], [], []]
    for reader in readers:
        await reader.close()


async def test_targets_choose_the_surfaces(service, memory, clock, erwan):
    memory.link_account("app", "erwan-pc", erwan)
    on_cli, on_app = Reader(service.events(*CLI)), Reader(service.events("terminal", "app", "erwan-pc"))
    await settle()
    reminder = service.create(erwan, "On the desktop", at(minutes=1), targets=["APP", "app"])
    assert reminder.targets == ("app",)
    clock.advance(minutes=1)
    await service.fire_due()
    await settle()
    assert [(e["text"], e["targets"]) for e in on_app.events] == [("On the desktop", ["app"])]
    assert on_cli.events == []
    for reader in (on_cli, on_app):
        await reader.close()


def test_bad_targets_are_refused(service, erwan):
    with pytest.raises(ReminderError, match="surface"):
        service.create(erwan, "Nope", at(minutes=1), targets=["not a surface!"])


async def test_an_account_linked_later_gets_what_comes_after(service, memory, clock, erwan):
    on_app = Reader(service.events("terminal", "app", "erwan-pc"))  # not linked yet: nobody's
    await settle()
    memory.link_account("app", "erwan-pc", erwan)
    service.create(erwan, "Linked", at(minutes=1))
    clock.advance(minutes=1)
    await service.fire_due()
    await settle()
    assert on_app.texts == ["Linked"]
    await on_app.close()


async def test_a_client_that_was_away_gets_what_it_missed_once(service, clock, erwan):
    first = service.events(*CLI)
    waiting = asyncio.ensure_future(next_event(first))
    await asyncio.sleep(0)  # connected: the cursor exists
    waiting.cancel()  # disconnected
    await asyncio.gather(waiting, return_exceptions=True)

    for name in ("one", "two"):
        service.create(erwan, name, at(minutes=1))
    clock.advance(minutes=1)
    await service.fire_due()  # nobody is connected

    back = service.events(*CLI)
    assert sorted([(await next_event(back))["text"], (await next_event(back))["text"]]) == ["one", "two"]
    await nothing_more(back)

    again = service.events(*CLI)  # it already has them
    await nothing_more(again)
    await again.aclose()


async def test_a_client_seeing_the_server_for_the_first_time_gets_no_backlog(service, clock, erwan):
    service.create(erwan, "Old news", at(minutes=1))
    clock.advance(minutes=1)
    await service.fire_due()

    stream = service.events("brand-new", "cli", "erwan")
    await nothing_more(stream)
    await stream.aclose()


async def test_the_scheduler_fires_a_reminder_set_while_it_sleeps(memory, erwan):
    real = ReminderService(memory)  # the real clock
    runner = asyncio.create_task(real.run())
    stream = real.events(*CLI)
    waiting = asyncio.ensure_future(next_event(stream))
    await asyncio.sleep(0.05)
    soon = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    real.create(erwan, "Now-ish", soon)
    try:
        assert (await asyncio.wait_for(waiting, 5))["text"] == "Now-ish"
    finally:
        runner.cancel()
        await stream.aclose()


async def test_old_events_are_forgotten(service, memory, clock, erwan):
    service.create(erwan, "Ancient", at(minutes=1))
    clock.advance(minutes=1)
    await service.fire_due()
    clock.advance(days=8)
    service.create(erwan, "Recent", (clock.now + timedelta(minutes=1)).isoformat())
    clock.advance(minutes=1)
    await service.fire_due()
    assert [e.text for e in memory.reminder_events_after(0)] == ["Recent"]


# --- privacy ----------------------------------------------------------------------------


async def test_erasing_a_person_erases_their_reminders_and_announcements(service, memory, clock, erwan):
    service.create(erwan, "Pending", at(days=1))
    service.create(erwan, "Fired", at(minutes=1))
    clock.advance(minutes=1)
    await service.fire_due()
    memory.delete_person(erwan.id)
    assert memory.reminder_events_after(0) == []
    assert memory.next_reminder_due() is None


def test_linking_accounts_keeps_the_reminders(service, memory, erwan):
    other = memory.resolve("discord", "42", "Erwan on Discord")
    service.create(other, "From discord", at(days=1))
    memory.link_account("discord", "42", erwan)
    assert [r.text for r in service.upcoming(erwan)] == ["From discord"]


# --- the model's tools --------------------------------------------------------------------


def run_tool(name, service, person, memory, **arguments):
    return default_toolbox().run(name, ToolContext(person, memory, service, "Europe/Paris"), arguments)


def test_the_model_can_set_list_and_cancel_a_reminder(service, memory, erwan):
    answer = run_tool("remind", service, erwan, memory, text="Call mum", when="2026-10-05T09:00")
    assert "set for 2026-10-05T07:00:00+00:00" in answer  # read in the person's clock (Paris, UTC+2)
    listing = run_tool("list_reminders", service, erwan, memory)
    assert "Call mum" in listing
    reminder_id = service.upcoming(erwan)[0].id
    assert run_tool("cancel_reminder", service, erwan, memory, reminder_id=reminder_id) == "Cancelled."
    assert run_tool("list_reminders", service, erwan, memory) == "No reminder set."


def test_the_model_is_told_what_went_wrong(service, memory, erwan):
    answer = run_tool("remind", service, erwan, memory, text="Late", when="2020-01-01T09:00")
    assert answer == "Error: That moment is already past."


# --- HTTP ---------------------------------------------------------------------------------


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, fake_providers(settings, FakeBackend()))) as http:
        yield http


def body(**fields) -> dict:
    return {**ME, "user_name": "Erwan", "text": "Dentist", "at": at(days=1), **fields}


def future() -> str:
    return (datetime.now(UTC) + timedelta(days=1)).isoformat()


def test_http_set_list_and_cancel(client):
    created = client.post("/v1/reminders", json=body(at=future(), repeat="weekly"), headers=AUTH)
    assert created.status_code == 201
    assert created.json()["repeat"] == "weekly"

    listed = client.get("/v1/reminders", params=ME, headers=AUTH).json()["reminders"]
    assert [r["text"] for r in listed] == ["Dentist"]

    assert client.delete(f"/v1/reminders/{listed[0]['id']}", params=ME, headers=AUTH).status_code == 200
    assert client.get("/v1/reminders", params=ME, headers=AUTH).json() == {"reminders": []}
    assert client.delete(f"/v1/reminders/{listed[0]['id']}", params=ME, headers=AUTH).status_code == 404


def test_http_refuses_a_past_time_and_a_bad_time(client):
    assert client.post("/v1/reminders", json=body(at="2020-01-01T00:00+00:00"), headers=AUTH).status_code == 422
    assert client.post("/v1/reminders", json=body(at="soon"), headers=AUTH).status_code == 422


def test_http_needs_a_token_and_respects_the_surface_limits(settings):
    limited = replace(settings, client_surfaces={"terminal": frozenset({"cli"})})
    with TestClient(create_app(limited, fake_providers(limited, FakeBackend()))) as http:
        assert http.post("/v1/reminders", json=body(at=future())).status_code == 401
        assert http.get("/v1/reminders/stream").status_code == 401
        discord = {**body(at=future()), "surface": "discord"}
        assert http.post("/v1/reminders", json=discord, headers=AUTH).status_code == 403


def first_event(url: str, headers: dict, params: dict | None = None, path: str = "/v1/reminders/stream") -> dict:
    """The first event of a stream that is not the state of the server."""
    with httpx.stream("GET", f"{url}{path}", headers=headers, params=params, timeout=10) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                if event["type"] != "server":
                    return event
    raise AssertionError("the stream ended without an event")


def server_state(url: str, headers: dict) -> dict:
    with httpx.stream("GET", f"{url}/v1/notifications/stream", headers=headers, timeout=10) as response:
        for line in response.iter_lines():
            if line.startswith("data: "):
                return json.loads(line[6:])
    raise AssertionError("the stream ended without an event")


def test_http_stream_sends_what_a_client_missed(live):
    app, url = live
    person = app.state.memory.resolve("cli", "erwan", "Erwan")
    app.state.memory.set_reminder_cursor("terminal/cli:erwan", 0)  # this listener has connected before
    app.state.memory.add_reminder(person.id, "Missed", NOW)  # already due
    asyncio.run(app.state.reminders.fire_due())

    event = first_event(url, AUTH, ME)

    assert (event["type"], event["text"], event["from"]) == ("reminder", "Missed", "Erwan")


def test_http_stream_is_for_the_person_who_set_it(live):
    app, url = live
    memory = app.state.memory
    person = memory.resolve("cli", "erwan", "Erwan")
    memory.resolve("cli", "bob", "Bob")
    for listener in ("terminal/cli:erwan", "discord/cli:bob"):
        memory.set_reminder_cursor(listener, 0)
    memory.add_reminder(person.id, "Only mine", NOW)
    memory.add_notification(None, "For everybody", NOW)
    asyncio.run(app.state.reminders.fire_due())

    mine = first_event(url, AUTH, ME, "/v1/notifications/stream")
    bobs = first_event(url, {"Authorization": "Bearer secret-discord"}, {"surface": "cli", "user_id": "bob"})

    assert (mine["type"], mine["text"]) == ("notification", "For everybody")  # older: it comes first
    assert (bobs["type"], bobs["text"]) == ("notification", "For everybody")  # not erwan's reminder


def test_http_stream_without_an_account_only_says_how_the_server_is(live):
    _, url = live
    assert server_state(url, AUTH) == {"type": "server", "state": "running", "message": "Clara is running"}


def test_http_stream_needs_both_parts_of_the_account(client):
    response = client.get("/v1/notifications/stream", params={"surface": "cli"}, headers=AUTH)
    assert response.status_code == 422
