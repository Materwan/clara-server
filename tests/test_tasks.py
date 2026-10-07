"""The to-do list: tasks with reminders, the follow-up Clara does at each one, the tools, the routes."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeBackend, fake_providers, say
from fastapi.testclient import TestClient

from clara.agent import Agent
from clara.memory import Memory
from clara.notifications import Notifier
from clara.prompt import SystemPrompt
from clara.reminders import AnnounceFailed
from clara.server import create_app
from clara.settings import Settings, SettingsError
from clara.taskai import follow, plan
from clara.tasks import (
    MAX_OPEN,
    NO_DUE,
    Followup,
    TaskError,
    TaskService,
    default_follow_up,
    default_reminders,
    describe,
    task_line,
)
from clara.taskstore import DONE, OPEN
from clara.tools import ToolContext, default_toolbox

AUTH = {"Authorization": "Bearer secret-cli"}
ME = {"surface": "cli", "user_id": "erwan"}
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)  # a Friday
ORIGIN = ("cli", "erwan", "cli:erwan")


class Clock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


def at(**delta) -> str:
    """An ISO time (UTC) `delta` after NOW."""
    return (NOW + timedelta(**delta)).isoformat()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(memory, clock):
    return TaskService(memory, Notifier(memory, clock), clock)


@pytest.fixture
def erwan(memory):
    return memory.resolve("cli", "erwan", "Erwan")


def sent(memory):
    """The notifications that were sent, oldest first."""
    return memory.reminder_events_after(0)


# --- the rules that pick reminders ---------------------------------------------------------------


def test_a_task_with_a_deadline_is_reminded_a_day_before_an_hour_before_and_at_it():
    due = NOW + timedelta(days=3)
    assert default_reminders(due, NOW, "") == [due - timedelta(days=1), due - timedelta(hours=1), due]


def test_a_near_deadline_leaves_out_the_reminders_already_past():
    due = NOW + timedelta(hours=3)
    assert default_reminders(due, NOW, "") == [due - timedelta(hours=1), due]
    assert default_reminders(NOW + timedelta(minutes=30), NOW, "") == [NOW + timedelta(minutes=30)]


def test_a_task_without_a_deadline_is_reminded_tomorrow_morning_on_the_persons_clock():
    # 14:00 in Paris: nine o'clock today is behind us (or too close), so tomorrow at 09:00 Paris time (07:00 UTC)
    assert default_reminders(None, NOW, "Europe/Paris") == [datetime(2026, 10, 3, 7, 0, tzinfo=UTC)]
    early = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)  # 05:00 in Paris: this morning is still to come
    assert default_reminders(None, early, "Europe/Paris") == [datetime(2026, 10, 2, 7, 0, tzinfo=UTC)]


def test_a_deadline_already_past_is_ignored_by_the_default_rule():
    assert default_reminders(NOW - timedelta(days=1), NOW, "+00:00") == [datetime(2026, 10, 3, 9, 0, tzinfo=UTC)]


def _task(**changes):
    from clara.taskstore import Task

    base = Task(1, 1, "t", "", None, OPEN, 0, "+00:00", NOW, NOW, None)
    return replace(base, **changes)


def test_the_rules_go_halfway_to_the_deadline_then_to_the_deadline():
    far = _task(due_at=NOW + timedelta(hours=10), reminders_sent=1)
    assert default_follow_up(far, NOW) == [NOW + timedelta(hours=5)]
    near = _task(due_at=NOW + timedelta(hours=2), reminders_sent=2)
    assert default_follow_up(near, NOW) == [NOW + timedelta(hours=2)]


def test_the_rules_come_back_each_morning_once_the_deadline_is_past():
    late = _task(due_at=NOW - timedelta(hours=1), reminders_sent=3)
    assert default_follow_up(late, NOW) == [datetime(2026, 10, 3, 9, 0, tzinfo=UTC)]


def test_the_rules_wait_longer_each_time_when_there_is_no_deadline():
    days = [default_follow_up(_task(reminders_sent=n), NOW)[0] - NOW for n in (1, 2, 3, 6)]
    assert [round(d.total_seconds() / 86400) for d in days] == [2, 4, 7, 7]  # 2, 4, then 7 (the longest) days, to 09:00


# --- creating ---------------------------------------------------------------------------------


async def test_create_stores_the_task_with_its_given_reminders(service, erwan):
    task = await service.create(
        erwan, "  Buy\n milk ", "  two litres ", at(days=2), [at(hours=5), at(hours=1)], origin=ORIGIN
    )
    assert (task.title, task.description, task.status, task.reminders_sent) == ("Buy milk", "two litres", OPEN, 0)
    assert task.due_at == NOW + timedelta(days=2)
    assert task.next == (NOW + timedelta(hours=1), NOW + timedelta(hours=5))  # soonest first
    assert (task.surface, task.user_id, task.conversation) == ORIGIN
    assert [t.id for t in service.tasks(erwan)] == [task.id]


async def test_without_reminders_the_rules_choose_when_clara_cannot(service, erwan):
    task = await service.create(erwan, "Call mum", due=at(days=3))
    due = NOW + timedelta(days=3)
    assert task.next == (due - timedelta(days=1), due - timedelta(hours=1), due)


async def test_clara_picks_the_reminders_when_nobody_did(service, erwan):
    asked = []

    async def planner(task, now):
        asked.append((task.title, task.due_at, now))
        return [NOW + timedelta(days=1, hours=3), NOW + timedelta(days=1, hours=3), NOW - timedelta(days=1), NOW + timedelta(days=500)]

    service.planner = planner
    task = await service.create(erwan, "Write the report", due=at(days=3))
    assert asked == [("Write the report", NOW + timedelta(days=3), NOW)]
    assert task.next == (NOW + timedelta(days=1, hours=3),)  # twice the same, one past, one beyond a year: dropped


async def test_the_reminders_a_person_gives_are_not_second_guessed(service, erwan):
    async def planner(task, now):
        raise AssertionError("Clara is not asked when the person said when")

    service.planner = planner
    task = await service.create(erwan, "Dentist", reminders=[at(hours=2)])
    assert task.next == (NOW + timedelta(hours=2),)


@pytest.mark.parametrize("failure", [AnnounceFailed("the model is down"), RuntimeError("boom"), "slow", "nothing", "past"])
async def test_the_rules_stay_when_clara_cannot_pick(service, erwan, failure):
    async def planner(task, now):
        if failure == "slow":
            await asyncio.sleep(5)
        if failure == "nothing":
            return None
        if failure == "past":
            return [NOW - timedelta(hours=1)]
        raise failure

    service.planner, service.plan_timeout = planner, 0.1
    task = await service.create(erwan, "Water the plants", zone="Europe/Paris")
    assert task.next == (datetime(2026, 10, 3, 7, 0, tzinfo=UTC),)  # tomorrow 09:00 in Paris: the rules'


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"title": "  "}, "needs a title"),
        ({"title": "x" * 201}, "at most 200"),
        ({"title": "ok", "description": "x" * 2001}, "at most 2000"),
        ({"title": "ok", "due": "tomorrow"}, "ISO 8601"),
        ({"title": "ok", "due": at(seconds=-1)}, "deadline is already past"),
        ({"title": "ok", "reminders": [at(seconds=-1)]}, "already past"),
        ({"title": "ok", "reminders": [at(minutes=n) for n in range(1, 12)]}, "At most 10 reminders"),
        ({"title": "ok", "targets": ["not a surface!"]}, "Not a surface"),
    ],
)
async def test_create_refuses_what_is_invalid(service, erwan, kwargs, message):
    with pytest.raises(TaskError, match=message):
        await service.create(erwan, **kwargs)
    assert service.tasks(erwan, None) == []


async def test_a_person_cannot_open_tasks_without_limit(service, erwan):
    for _ in range(MAX_OPEN):
        await service.create(erwan, "again", reminders=[at(days=1)])
    with pytest.raises(TaskError, match=f"At most {MAX_OPEN} open"):
        await service.create(erwan, "one too many")
    first = service.tasks(erwan)[0]
    service.complete(erwan, first.id)  # a done task does not count
    await service.create(erwan, "now there is room", reminders=[at(days=1)])


async def test_a_time_without_offset_is_read_on_the_persons_clock(service, erwan):
    task = await service.create(erwan, "Meeting", due="2026-10-05T18:00", zone="Europe/Paris")
    assert task.due_at == datetime(2026, 10, 5, 16, 0, tzinfo=UTC)
    assert task.timezone == "Europe/Paris"
    assert "due 2026-10-05T18:00+02:00" in task_line(task)


# --- listing and describing ------------------------------------------------------------------------


async def test_the_list_shows_title_reminders_sent_and_next_reminder_soonest_first(service, erwan):
    later = await service.create(erwan, "Later", reminders=[at(days=2)])
    soon = await service.create(erwan, "Soon", reminders=[at(hours=1)])
    none = await service.create(erwan, "No reminder", reminders=[at(hours=3)])
    service.update(erwan, none.id, reminders=[])
    assert [t.title for t in service.tasks(erwan)] == ["Soon", "Later", "No reminder"]
    line = task_line(soon)
    assert line == f"[{soon.id}] Soon | open | 0 reminders sent | next reminder 2026-10-02T13:00+00:00"
    assert "no reminder to come" in task_line(service.get(erwan, none.id))
    assert "next reminder 2026-10-04T12:00+00:00" in task_line(later)


async def test_the_description_of_a_task_is_asked_for_by_its_number(service, erwan, memory):
    task = await service.create(erwan, "Taxes", "Gather the 2025 papers", reminders=[at(hours=1), at(hours=2)])
    shown = service.describe(service.get(erwan, task.id))
    assert shown["description"] == "Gather the 2025 papers" and len(shown["reminders"]) == 2
    assert shown["next_reminder"] == shown["reminders"][0] and shown["max_reminders"] == 10
    other = memory.resolve("cli", "alice", "Alice")
    with pytest.raises(TaskError, match="No such task"):
        service.get(other, task.id)  # never somebody else's
    assert service.tasks(other) == []


async def test_done_tasks_are_listed_apart(service, erwan):
    one = await service.create(erwan, "One", reminders=[at(hours=1)])
    await service.create(erwan, "Two", reminders=[at(hours=1)])
    service.complete(erwan, one.id)
    assert [t.title for t in service.tasks(erwan)] == ["Two"]
    assert [t.title for t in service.tasks(erwan, DONE)] == ["One"]
    assert sorted(t.title for t in service.tasks(erwan, None)) == ["One", "Two"]
    with pytest.raises(TaskError, match="status must be"):
        service.tasks(erwan, "later")


# --- changing -----------------------------------------------------------------------------------


async def test_update_changes_only_what_is_given(service, erwan):
    task = await service.create(erwan, "Old", "details", at(days=2), [at(hours=1)])
    changed = service.update(erwan, task.id, title="New")
    assert (changed.title, changed.description, changed.due_at, changed.next) == (
        "New", "details", task.due_at, task.next,
    )
    changed = service.update(erwan, task.id, description="", due="", reminders=[at(hours=4), at(hours=2)])
    assert (changed.description, changed.due_at) == ("", None)
    assert changed.next == (NOW + timedelta(hours=2), NOW + timedelta(hours=4))
    changed = service.update(erwan, task.id, due=at(days=9), targets=["app", "discord"])
    assert changed.due_at == NOW + timedelta(days=9) and changed.targets == ("app", "discord")
    assert service.update(erwan, task.id, due=NO_DUE).due_at == NOW + timedelta(days=9)


async def test_update_refuses_what_is_invalid(service, erwan):
    task = await service.create(erwan, "Task", reminders=[at(hours=1)])
    with pytest.raises(TaskError, match="needs a title"):
        service.update(erwan, task.id, title=" ")
    with pytest.raises(TaskError, match="already past"):
        service.update(erwan, task.id, reminders=[at(seconds=-5)])
    with pytest.raises(TaskError, match="deadline is already past"):
        service.update(erwan, task.id, due=at(seconds=-5))
    with pytest.raises(TaskError, match="No such task"):
        service.update(erwan, task.id + 99, title="x")
    assert service.get(erwan, task.id).title == "Task"


async def test_a_done_task_is_not_reminded_and_can_be_reopened(service, erwan, clock, memory):
    task = await service.create(erwan, "Pay rent", reminders=[at(hours=1)])
    done = service.complete(erwan, task.id)
    assert (done.status, done.next, done.done_at) == (DONE, (), NOW)
    with pytest.raises(TaskError, match="reopen it"):
        service.update(erwan, task.id, reminders=[at(hours=2)])
    clock.advance(hours=2)
    assert await service.fire_due() == 0 and sent(memory) == []
    reopened = await service.reopen(erwan, task.id, [at(hours=5)])
    assert (reopened.status, reopened.done_at, reopened.next) == (OPEN, None, (NOW + timedelta(hours=5),))
    assert (await service.reopen(erwan, task.id)).status == OPEN  # already open: nothing happens


async def test_reopening_without_reminders_picks_them_again(service, erwan):
    task = await service.create(erwan, "Pay rent", reminders=[at(hours=1)])
    service.complete(erwan, task.id)
    reopened = await service.reopen(erwan, task.id)
    assert len(reopened.next) == 1 and reopened.next[0] > NOW


async def test_delete_removes_the_task_and_its_reminders(service, erwan, memory):
    task = await service.create(erwan, "Gone", reminders=[at(hours=1)])
    assert service.delete(erwan, task.id) and not service.delete(erwan, task.id)
    assert memory.database.execute("SELECT COUNT(*) FROM task_reminders").fetchone()[0] == 0


# --- when a reminder comes due ------------------------------------------------------------------


async def test_a_due_reminder_notifies_only_its_person_and_is_counted(service, erwan, clock, memory):
    task = await service.create(erwan, "Dentist", "Bring the card", reminders=[at(hours=1), at(hours=9)], targets=["app"])
    clock.advance(hours=1)
    assert await service.fire_due() == 1
    [event] = sent(memory)
    assert (event.kind, event.person_id, event.targets, event.source) == ("notification", erwan.id, ("app",), "tasks")
    assert event.title == "Task: Dentist"
    assert "Reminder 1" in event.text and "Dentist" in event.text and "Bring the card" in event.text
    after = service.get(erwan, task.id)
    assert after.reminders_sent == 1 and after.next == (NOW + timedelta(hours=9),)  # the next one stays
    assert await service.fire_due() == 0  # nothing more is due


async def test_a_task_is_reminded_once_however_many_reminders_it_missed(service, erwan, clock, memory):
    task = await service.create(erwan, "Missed", reminders=[at(hours=1), at(hours=2), at(days=2)])
    clock.advance(hours=5)  # the server was off
    assert await service.fire_due() == 1
    assert len(sent(memory)) == 1
    after = service.get(erwan, task.id)
    assert after.reminders_sent == 1 and after.next == (NOW + timedelta(days=2),)


async def test_when_the_queue_runs_dry_the_rules_queue_the_next_reminder(service, erwan, clock):
    task = await service.create(erwan, "Chore", reminders=[at(hours=1)])
    clock.advance(hours=1)
    await service.fire_due()
    after = service.get(erwan, task.id)
    assert after.reminders_sent == 1 and len(after.next) == 1 and after.next[0] > clock.now


async def test_clara_writes_the_notification_and_can_replace_the_next_reminders(service, erwan, clock, memory):
    task = await service.create(erwan, "Dentist", "Bring the card", at(days=2), [at(hours=1), at(hours=3)])
    clock.advance(hours=1)
    seen = []

    async def follower(task, now):
        seen.append((task.title, task.description, task.reminders_sent, task.next))
        return Followup("Your dentist visit is in two days: bring the card!", [clock.now + timedelta(days=1)])

    service.follower = follower
    await service.fire_due()

    assert seen == [("Dentist", "Bring the card", 0, (NOW + timedelta(hours=1), NOW + timedelta(hours=3)))]
    [event] = sent(memory)
    assert event.text == "Your dentist visit is in two days: bring the card!"
    after = service.get(erwan, task.id)
    assert after.reminders_sent == 1 and after.next == (clock.now + timedelta(days=1),)  # hers, not the queued ones


async def test_clara_can_stop_the_reminders_but_the_task_stays_open(service, erwan, clock):
    task = await service.create(erwan, "Someday", reminders=[at(hours=1), at(hours=2)])
    clock.advance(hours=1)

    async def follower(task, now):
        return Followup("Last nudge.", [])

    service.follower = follower
    await service.fire_due()
    after = service.get(erwan, task.id)
    assert (after.status, after.next, after.reminders_sent) == (OPEN, (), 1)
    assert "no reminder to come" in task_line(after)
    clock.advance(days=30)
    assert await service.fire_due() == 0


async def test_without_a_decision_about_the_next_ones_the_queue_is_kept(service, erwan, clock):
    task = await service.create(erwan, "Keep", reminders=[at(hours=1), at(hours=7)])
    clock.advance(hours=1)

    async def follower(task, now):
        return Followup("Hello", None)

    service.follower = follower
    await service.fire_due()
    assert service.get(erwan, task.id).next == (NOW + timedelta(hours=7),)


async def test_reminders_clara_gave_that_cannot_be_used_do_not_stop_it(service, erwan, clock):
    task = await service.create(erwan, "Keep", reminders=[at(hours=1), at(hours=7)])
    clock.advance(hours=1)

    async def follower(task, now):
        return Followup(None, [clock.now - timedelta(days=1)])  # in the past: unusable

    service.follower = follower
    await service.fire_due()
    assert service.get(erwan, task.id).next == (NOW + timedelta(hours=7),)


@pytest.mark.parametrize("failure", [AnnounceFailed("down"), RuntimeError("boom")])
async def test_if_clara_cannot_follow_up_the_plain_text_is_sent_and_the_rules_go_on(
    service, erwan, clock, memory, failure
):
    task = await service.create(erwan, "Chore", reminders=[at(hours=1)])
    clock.advance(hours=1)

    async def follower(task, now):
        raise failure

    service.follower = follower
    assert await service.fire_due() == 1
    [event] = sent(memory)
    assert "Reminder 1 for your task: Chore" in event.text
    assert len(service.get(erwan, task.id).next) == 1


async def test_a_task_is_left_alone_after_its_maximum_of_reminders(memory, erwan, clock):
    service = TaskService(memory, Notifier(memory, clock), clock, max_reminders=2)
    task = await service.create(erwan, "Nagging", reminders=[at(hours=1), at(hours=2), at(hours=3)])
    clock.advance(hours=1)
    await service.fire_due()
    assert service.get(erwan, task.id).next == (NOW + timedelta(hours=2), NOW + timedelta(hours=3))
    clock.advance(hours=1)
    await service.fire_due()
    last = service.get(erwan, task.id)
    assert (last.reminders_sent, last.next) == (2, ())  # the third one is dropped
    assert "last reminder of this task" in sent(memory)[-1].text
    assert service.describe(last)["max_reminders"] == 2


async def test_clara_cannot_add_reminders_past_the_maximum(memory, erwan, clock):
    service = TaskService(memory, Notifier(memory, clock), clock, max_reminders=1)
    task = await service.create(erwan, "Once", reminders=[at(hours=1)])
    clock.advance(hours=1)

    async def follower(task, now):
        return Followup("Hi", [clock.now + timedelta(days=1)])

    service.follower = follower
    await service.fire_due()
    assert service.get(erwan, task.id).next == ()


async def test_a_task_done_while_clara_writes_is_not_reminded(service, erwan, clock, memory):
    task = await service.create(erwan, "Quick", reminders=[at(hours=1)])
    clock.advance(hours=1)

    async def follower(task, now):
        service.complete(erwan, task.id)
        return Followup("too late", None)

    service.follower = follower
    assert await service.fire_due() == 0
    assert sent(memory) == [] and service.get(erwan, task.id).reminders_sent == 0


async def test_what_the_person_changed_while_clara_writes_wins(service, erwan, clock):
    task = await service.create(erwan, "Edited", reminders=[at(hours=1)])
    clock.advance(hours=1)

    async def follower(task, now):
        clock.advance(seconds=5)
        service.update(erwan, task.id, reminders=[clock.now + timedelta(days=3)])  # the person moves it meanwhile
        return Followup("hello", [clock.now + timedelta(days=9)])

    service.follower = follower
    await service.fire_due()
    assert service.get(erwan, task.id).next == (NOW + timedelta(hours=1, seconds=5, days=3),)  # not Clara's day 9


async def test_several_due_tasks_are_followed_up_together(service, erwan, clock, memory):
    for name in ("first", "second", "third"):
        await service.create(erwan, name, reminders=[at(hours=1)])
    clock.advance(hours=1)
    running = most = 0

    async def follower(task, now):
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.05)
        running -= 1
        return Followup(f"about {task.title}", None)

    service.follower = follower
    assert await service.fire_due() == 3
    assert most == 3
    assert sorted(e.text for e in sent(memory)) == ["about first", "about second", "about third"]


async def test_the_scheduler_fires_what_comes_due_and_stops_with_the_server(service, erwan, clock, memory):
    await service.create(erwan, "Soon", reminders=[at(minutes=1)])
    clock.advance(minutes=1)
    runner = asyncio.create_task(service.run())
    for _ in range(100):
        if sent(memory):
            break
        await asyncio.sleep(0.01)
    runner.cancel()
    assert len(sent(memory)) == 1


async def test_nothing_is_written_while_the_server_stops(service, erwan, clock):
    await service.create(erwan, "Wait", reminders=[at(minutes=1)])
    clock.advance(minutes=1)

    async def follower(task, now):
        raise AssertionError("not while stopping")

    service.follower, service.stopping = follower, True
    assert await service._follow(service.store.of(erwan.id)[0], clock.now) is None


# --- Clara's side: the model's answers ---------------------------------------------------------------


def agent_with(memory, tmp_path, backend):
    return Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"))


async def a_task(service, erwan, **kwargs):
    kwargs.setdefault("reminders", [at(hours=1)])
    return await service.create(erwan, "Dentist", "Bring the card", due=at(days=2), origin=ORIGIN, zone="Europe/Paris", **kwargs)


async def test_clara_reads_the_reminders_she_picks_as_local_times(memory, tmp_path, service, erwan):
    backend = FakeBackend(say('{"reminders": ["2026-10-03T09:00", "2026-10-04T08:00", "never"]}'))
    task = await a_task(service, erwan)
    found = await plan(agent_with(memory, tmp_path, backend), task, NOW, 5)
    # read on the clock of the task (Paris, UTC+2), what cannot be read left out
    assert found == [datetime(2026, 10, 3, 7, 0, tzinfo=UTC), datetime(2026, 10, 4, 6, 0, tzinfo=UTC)]
    messages, tools = backend.calls[0]
    text = "\n".join(m["content"] for m in messages)
    assert "Task: Dentist" in text and "Description: Bring the card" in text and "Deadline: 2026-10-04T14:00+02:00" in text
    assert "Current time: 2026-10-02T14:00+02:00" in text


async def test_she_may_wrap_the_json_in_a_fence_or_words(memory, tmp_path, service, erwan):
    task = await a_task(service, erwan)
    for answer in ('```json\n{"reminders": ["2026-10-03T09:00"]}\n```', 'Sure! {"reminders": ["2026-10-03T09:00"]} Done.'):
        found = await plan(agent_with(memory, tmp_path, FakeBackend(say(answer))), task, NOW, 5)
        assert found == [datetime(2026, 10, 3, 7, 0, tzinfo=UTC)]
    assert await plan(agent_with(memory, tmp_path, FakeBackend(say("no idea"))), task, NOW, 5) is None


async def test_the_follow_up_gives_clara_the_task_and_the_number_of_reminders_sent(memory, tmp_path, service, erwan):
    backend = FakeBackend(say('{"message": "Your dentist is in 2 days, bring the card!", "next": ["2026-10-03T18:00"]}'))
    task = await a_task(service, erwan, reminders=[at(hours=1), at(hours=6)])
    task = replace(task, reminders_sent=2)
    decision = await follow(agent_with(memory, tmp_path, backend), task, NOW + timedelta(hours=1), 5, 10)
    assert decision == Followup("Your dentist is in 2 days, bring the card!", [datetime(2026, 10, 3, 16, 0, tzinfo=UTC)])
    text = "\n".join(m["content"] for m in backend.calls[0][0])
    assert "This is reminder number 3 of at most 10." in text and "Reminders sent before this one: 2" in text
    assert "Task: Dentist" in text and "Description: Bring the card" in text
    assert "Reminders still queued: 2026-10-02T20:00+02:00" in text  # the one that came due is not "still queued"


async def test_the_last_reminder_is_called_the_last_one(memory, tmp_path, service, erwan):
    backend = FakeBackend(say('{"message": "Final call."}'))
    task = replace(await a_task(service, erwan), reminders_sent=1)
    await follow(agent_with(memory, tmp_path, backend), task, NOW, 5, 2)
    assert "(the last one)" in "\n".join(m["content"] for m in backend.calls[0][0])


async def test_without_next_the_queue_is_kept_and_an_empty_list_stops(memory, tmp_path, service, erwan):
    task = await a_task(service, erwan)
    kept = await follow(agent_with(memory, tmp_path, FakeBackend(say('{"message": "Hi"}'))), task, NOW, 5, 10)
    stop = await follow(agent_with(memory, tmp_path, FakeBackend(say('{"message": "Hi", "next": []}'))), task, NOW, 5, 10)
    unreadable = await follow(agent_with(memory, tmp_path, FakeBackend(say('{"message": "Hi", "next": ["soon"]}'))), task, NOW, 5, 10)
    assert (kept.message, kept.next) == ("Hi", None)
    assert (stop.message, stop.next) == ("Hi", [])
    assert (unreadable.message, unreadable.next) == ("Hi", None)  # a list she wrote that cannot be read: as if left out


async def test_a_plain_answer_is_the_notification_itself(memory, tmp_path, service, erwan):
    task = await a_task(service, erwan)
    found = await follow(agent_with(memory, tmp_path, FakeBackend(say("Time\nfor the dentist!"))), task, NOW, 5, 10)
    assert found == Followup("Time for the dentist!", None)


async def test_nothing_of_this_is_kept_in_the_conversation(memory, tmp_path, service, erwan):
    task = await a_task(service, erwan)
    await follow(agent_with(memory, tmp_path, FakeBackend(say('{"message": "Hi"}'))), task, NOW, 5, 10)
    assert memory.history("cli:erwan", 10) == []


class BrokenBackend(FakeBackend):
    async def stream(self, messages, tools):
        raise ConnectionError("Ollama is down")
        yield  # pragma: no cover


class SlowBackend(FakeBackend):
    async def stream(self, messages, tools):
        await asyncio.sleep(5)
        yield  # pragma: no cover


@pytest.mark.parametrize("backend", [BrokenBackend(), SlowBackend(), FakeBackend(say("   "))])
async def test_she_cannot_answer_so_the_rules_take_over(memory, tmp_path, service, erwan, backend):
    task = await a_task(service, erwan)
    with pytest.raises(AnnounceFailed):
        await follow(agent_with(memory, tmp_path, backend), task, NOW, 0.1, 10)
    with pytest.raises(AnnounceFailed):
        await plan(agent_with(memory, tmp_path, backend), task, NOW, 0.1)


async def test_a_task_whose_account_is_gone_is_not_asked_about(memory, tmp_path, service, erwan):
    backend = FakeBackend(say('{"message": "x"}'))
    task = replace(await a_task(service, erwan), user_id="nobody")
    with pytest.raises(AnnounceFailed, match="account"):
        await follow(agent_with(memory, tmp_path, backend), task, NOW, 5, 10)
    assert backend.calls == [] and memory.find_person("cli", "nobody") is None  # no person was created either


async def test_a_task_without_a_place_is_not_asked_about(memory, tmp_path, service, erwan):
    task = await service.create(erwan, "Old one", reminders=[at(hours=1)])  # no origin
    with pytest.raises(AnnounceFailed):
        await follow(agent_with(memory, tmp_path, FakeBackend(say("{}"))), task, NOW, 5, 10)


async def test_the_whole_follow_up_through_the_scheduler(memory, tmp_path, service, erwan, clock):
    backend = FakeBackend(say('{"message": "Dentist in two days!", "next": ["2026-10-03T10:00"]}'))
    agent = agent_with(memory, tmp_path, backend)
    service.follower = lambda task, now: follow(agent, task, now, 5, service.max_reminders)
    task = await a_task(service, erwan)
    clock.advance(hours=1)
    await service.fire_due()
    [event] = sent(memory)
    assert event.text == "Dentist in two days!" and event.title == "Task: Dentist"
    assert service.get(erwan, task.id).next == (datetime(2026, 10, 3, 8, 0, tzinfo=UTC),)


# --- the model's tools (natural language) -----------------------------------------------------------


@pytest.fixture
def toolbox():
    return default_toolbox()


def context(memory, service, person, timezone_name=None):
    return ToolContext(person, memory, None, timezone_name, "cli", "erwan", "cli:erwan", tasks=service)


async def test_the_model_adds_a_task_with_reminders_it_chose(memory, service, erwan, toolbox):
    ctx = context(memory, service, erwan, "Europe/Paris")
    out = await toolbox.arun(
        "add_task",
        ctx,
        {"title": "Send invoice", "description": "to ACME", "due": "2026-10-05T17:00", "reminders": ["2026-10-05T09:00", "2026-10-05T16:00"]},
    )
    assert out.startswith("Task added.") and "Send invoice" in out
    assert "next reminder 2026-10-05T09:00+02:00" in out
    [task] = service.tasks(erwan)
    assert (task.surface, task.conversation, task.timezone) == ("cli", "cli:erwan", "Europe/Paris")
    assert task.next == (datetime(2026, 10, 5, 7, 0, tzinfo=UTC), datetime(2026, 10, 5, 14, 0, tzinfo=UTC))


async def test_the_model_may_leave_the_reminders_to_the_server(memory, service, erwan, toolbox):
    out = await toolbox.arun("add_task", context(memory, service, erwan), {"title": "Tidy", "reminders": ""})
    assert "next reminder" in out and len(service.tasks(erwan)[0].next) == 1


async def test_the_model_reads_the_list_and_one_task(memory, service, erwan, toolbox):
    ctx = context(memory, service, erwan)
    assert await toolbox.arun("list_tasks", ctx, {}) == "No open task."
    await toolbox.arun("add_task", ctx, {"title": "Taxes", "description": "Gather papers", "reminders": [at(hours=2)]})
    listed = await toolbox.arun("list_tasks", ctx, {})
    assert "Taxes | open | 0 reminders sent | next reminder 2026-10-02T14:00" in listed
    shown = await toolbox.arun("list_tasks", ctx, {"task_id": service.tasks(erwan)[0].id})
    assert "Description: Gather papers" in shown
    assert (await toolbox.arun("list_tasks", ctx, {"task_id": 999})).startswith("Error: No such task")
    assert (await toolbox.arun("list_tasks", ctx, {"task_id": "abc"})).startswith("Error: task_id must be")
    assert await toolbox.arun("list_tasks", ctx, {"status": "done"}) == "No done task."
    assert (await toolbox.arun("list_tasks", ctx, {"status": "later"})).startswith("Error: status must be")


async def test_the_model_changes_finishes_and_deletes(memory, service, erwan, toolbox):
    ctx = context(memory, service, erwan)
    await toolbox.arun("add_task", ctx, {"title": "Taxes", "due": at(days=2), "reminders": [at(hours=2)]})
    number = service.tasks(erwan)[0].id
    out = await toolbox.arun("update_task", ctx, {"task_id": number, "title": "Taxes 2025", "due": "", "reminders": []})
    task = service.get(erwan, number)
    assert out.startswith("Task updated.") and (task.title, task.due_at, task.next) == ("Taxes 2025", None, ())
    assert (await toolbox.arun("update_task", ctx, {"task_id": number, "status": "done"})).startswith("Task updated.")
    assert (service.get(erwan, number).status, service.get(erwan, number).next) == (DONE, ())
    refused = await toolbox.arun("update_task", ctx, {"task_id": number, "status": "done", "reminders": [at(hours=3)]})
    assert refused.startswith("Error: A task that is done")
    reopened = await toolbox.arun("update_task", ctx, {"task_id": number, "status": "open", "reminders": [at(hours=3)]})
    assert reopened.startswith("Task updated.") and "next reminder 2026-10-02T15:00" in reopened
    assert service.get(erwan, number).status == OPEN
    assert (await toolbox.arun("update_task", ctx, {"task_id": number, "status": "paused"})).startswith("Error: status")
    assert await toolbox.arun("delete_task", ctx, {"task_id": number}) == "Deleted."
    assert await toolbox.arun("delete_task", ctx, {"task_id": number}) == "No such task of yours."


async def test_the_tools_say_when_tasks_are_not_available(memory, erwan, toolbox):
    ctx = ToolContext(erwan, memory)
    assert await toolbox.arun("add_task", ctx, {"title": "x"}) == "Error: Tasks are not available."
    assert toolbox.run("list_tasks", ctx, {}) == "Error: Tasks are not available."


async def test_the_model_is_told_about_a_surface_the_person_does_not_use(memory, service, erwan, toolbox):
    out = await toolbox.arun("add_task", context(memory, service, erwan), {"title": "x", "reminders": [at(hours=1)], "targets": ["discord"]})
    assert "Warning" in out and "discord" in out


# --- the routes -------------------------------------------------------------------------------------


PLANNED = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT09:00")  # within a year, on the real clock


@pytest.fixture
def api(settings):
    backend = FakeBackend(*[say('{"reminders": ["' + PLANNED + '"]}')] * 5)
    with TestClient(create_app(settings, fake_providers(settings, backend))) as client:
        yield client


def test_a_task_is_added_listed_shown_changed_and_deleted_over_http(api):
    created = api.post(
        "/v1/tasks",
        json={**ME, "user_name": "Erwan", "title": "Dentist", "description": "Bring the card",
              "due": "2030-01-03T10:00", "reminders": ["2030-01-02T09:00"], "timezone": "Europe/Paris"},  # winter: UTC+1
        headers=AUTH,
    )
    assert created.status_code == 201
    task = created.json()
    assert task["title"] == "Dentist" and task["status"] == "open" and task["reminders_sent"] == 0
    assert task["reminders"] == ["2030-01-02T08:00:00+00:00"] == [task["next_reminder"]]
    assert task["due_at"] == "2030-01-03T09:00:00+00:00" and task["max_reminders"] == 10 and task["timezone"] == "Europe/Paris"

    listed = api.get("/v1/tasks", params=ME, headers=AUTH).json()
    assert [t["id"] for t in listed["tasks"]] == [task["id"]]
    assert api.get(f"/v1/tasks/{task['id']}", params=ME, headers=AUTH).json()["description"] == "Bring the card"

    patched = api.patch(f"/v1/tasks/{task['id']}", json={**ME, "title": "Dentist!", "due": None, "reminders": []}, headers=AUTH)
    assert patched.status_code == 200
    assert (patched.json()["title"], patched.json()["due_at"], patched.json()["reminders"]) == ("Dentist!", None, [])

    done = api.patch(f"/v1/tasks/{task['id']}", json={**ME, "status": "done"}, headers=AUTH).json()
    assert done["status"] == "done" and done["done_at"]
    assert api.get("/v1/tasks", params=ME, headers=AUTH).json()["tasks"] == []
    assert len(api.get("/v1/tasks", params={**ME, "status": "all"}, headers=AUTH).json()["tasks"]) == 1
    reopened = api.patch(f"/v1/tasks/{task['id']}", json={**ME, "status": "open", "reminders": ["2030-02-01T09:00"]}, headers=AUTH).json()
    assert reopened["status"] == "open" and reopened["reminders"] == ["2030-02-01T08:00:00+00:00"]  # read on the task's clock

    assert api.delete(f"/v1/tasks/{task['id']}", params=ME, headers=AUTH).json() == {"deleted": task["id"]}
    assert api.delete(f"/v1/tasks/{task['id']}", params=ME, headers=AUTH).status_code == 404


def test_without_reminders_clara_picks_them(api):
    created = api.post(
        "/v1/tasks", json={**ME, "title": "Write", "due": "2030-01-05T10:00", "timezone": "UTC"}, headers=AUTH
    ).json()
    assert created["reminders"] == [PLANNED + ":00+00:00"]  # what the (fake) model answered, not the rules' three


def test_the_routes_refuse_what_is_invalid_and_what_is_not_theirs(api):
    assert api.post("/v1/tasks", json={**ME, "title": ""}, headers=AUTH).status_code == 422
    bad = api.post("/v1/tasks", json={**ME, "title": "x", "due": "2001-01-01T00:00"}, headers=AUTH)
    assert bad.status_code == 422 and "already past" in bad.json()["detail"]
    assert api.post("/v1/tasks", json={**ME, "title": "x"}).status_code in (401, 403)  # no token
    mine = api.post("/v1/tasks", json={**ME, "title": "Mine", "reminders": ["2030-01-02T09:00"]}, headers=AUTH).json()
    alice = {"surface": "cli", "user_id": "alice"}
    api.post("/v1/tasks", json={**alice, "user_name": "Alice", "title": "Hers", "reminders": ["2030-01-02T09:00"]}, headers=AUTH)
    assert api.get(f"/v1/tasks/{mine['id']}", params=alice, headers=AUTH).status_code == 404
    assert api.patch(f"/v1/tasks/{mine['id']}", json={**alice, "title": "mine now"}, headers=AUTH).status_code == 404
    assert api.delete(f"/v1/tasks/{mine['id']}", params=alice, headers=AUTH).status_code == 404
    assert [t["title"] for t in api.get("/v1/tasks", params=alice, headers=AUTH).json()["tasks"]] == ["Hers"]
    assert api.get("/v1/tasks", params={"surface": "cli", "user_id": "nobody"}, headers=AUTH).json()["tasks"] == []
    assert api.get("/v1/tasks/1", params={"surface": "cli", "user_id": "nobody"}, headers=AUTH).status_code == 404


def test_a_done_task_cannot_be_given_reminders(api):
    task = api.post("/v1/tasks", json={**ME, "title": "x", "reminders": ["2030-01-02T09:00"]}, headers=AUTH).json()
    refused = api.patch(f"/v1/tasks/{task['id']}", json={**ME, "status": "done", "reminders": ["2030-01-03T09:00"]}, headers=AUTH)
    assert refused.status_code == 422


# --- the server --------------------------------------------------------------------------------------


def test_the_server_lets_clara_plan_and_follow_up_unless_told_not_to(settings):
    on = create_app(settings, fake_providers(settings, FakeBackend()))
    off = create_app(replace(settings, reminder_ai_timeout=0), fake_providers(settings, FakeBackend()))
    assert on.state.tasks.planner is not None and on.state.tasks.follower is not None
    assert off.state.tasks.planner is None and off.state.tasks.follower is None


def test_the_maximum_of_reminders_comes_from_the_environment():
    base = {"CLARA_TOKENS": "terminal:secret-cli"}
    assert Settings.from_env(base).task_max_reminders == 10
    assert Settings.from_env({**base, "CLARA_TASK_MAX_REMINDERS": "3"}).task_max_reminders == 3
    with pytest.raises(SettingsError):
        Settings.from_env({**base, "CLARA_TASK_MAX_REMINDERS": "0"})


def test_a_task_remembers_where_it_was_set_over_http(api):
    api.post("/v1/tasks", json={**ME, "title": "x", "reminders": ["2030-01-02T09:00"], "conversation": "cli:work"}, headers=AUTH)
    person = api.app.state.memory.find_person("cli", "erwan")
    [task] = api.app.state.tasks.tasks(person)
    assert (task.surface, task.user_id, task.conversation) == ("cli", "erwan", "cli:work")


# --- a person leaves, two people become one -----------------------------------------------------------


async def test_erasing_a_person_erases_their_tasks(memory, service, erwan):
    await service.create(erwan, "Mine", reminders=[at(hours=1)])
    other = memory.resolve("cli", "alice", "Alice")
    await service.create(other, "Hers", reminders=[at(hours=1)])
    memory.delete_person(erwan.id)
    rows = memory.database
    assert [r["title"] for r in rows.execute("SELECT title FROM tasks")] == ["Hers"]
    assert rows.execute("SELECT COUNT(*) FROM task_reminders").fetchone()[0] == 1


async def test_merging_two_people_keeps_the_tasks_of_both(memory, service, erwan):
    other = memory.resolve("discord", "42", "Erwan on Discord")
    await service.create(erwan, "From the terminal", reminders=[at(hours=1)])
    await service.create(other, "From Discord", reminders=[at(hours=2)])
    memory.link_account("discord", "42", erwan, force=True)
    assert sorted(t.title for t in service.tasks(erwan)) == ["From Discord", "From the terminal"]


def test_a_database_from_before_tasks_gets_the_tables(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.executescript("CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created_at TEXT NOT NULL);")
    old.commit()
    old.close()
    memory = Memory(path)
    try:
        assert memory.database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        memory.close()


def test_describe_gives_what_a_client_shows():
    task = _task(due_at=NOW + timedelta(days=1), reminders_sent=2, next=(NOW + timedelta(hours=1), NOW + timedelta(hours=5)))
    shown = describe(task, 7)
    assert shown["next_reminder"] == "2026-10-02T13:00:00+00:00" and shown["reminders_sent"] == 2
    assert shown["max_reminders"] == 7 and shown["targets"] == [] and shown["done_at"] is None


def test_the_web_site_has_a_tasks_page(api):
    page = api.get("/tasks.js")
    assert page.status_code == 200 and "export function mountTasks" in page.text
    assert 'from "./tasks.js"' in api.get("/app.js").text and 'tasks: ["Tasks", "tasks"]' in api.get("/app.js").text


# --- through a real turn ----------------------------------------------------------------------------


async def test_the_model_adds_then_lists_tasks_during_a_conversation(memory, tmp_path, service):
    from conftest import call

    backend = FakeBackend(
        call("add_task", title="Send the invoice", due=at(days=2), reminders=[at(days=1)]),
        say("Done: I will remind you tomorrow."),
        call("list_tasks"),
        say("You have one task."),
    )
    agent = Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"), tasks=service)
    from clara.agent import ChatRequest

    async def ask(message):
        return [e async for e in agent.turn(ChatRequest("cli", "erwan", "Erwan", message), "test")]

    first = await ask("Add a task: send the invoice by Friday")
    assert [e["name"] for e in first if e["type"] == "tool"] == ["add_task"]
    person = memory.find_person("cli", "erwan")
    [task] = service.tasks(person)
    assert (task.title, task.next) == ("Send the invoice", (NOW + timedelta(days=1),))
    assert (task.surface, task.user_id, task.conversation) == ("cli", "erwan", "cli:erwan")  # her follow-ups go there

    second = await ask("What is on my list?")
    assert [e["name"] for e in second if e["type"] == "tool"] == ["list_tasks"]
    tool_answer = [m for m in backend.calls[-1][0] if m["role"] == "tool"][-1]["content"]
    assert "[1] Send the invoice | open | due 2026-10-04T12:00+00:00 | 0 reminders sent | next reminder 2026-10-03T12:00+00:00" in tool_answer
