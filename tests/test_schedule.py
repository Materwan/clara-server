"""Scheduled tasks: a prompt Clara runs by herself, once or as a routine, and tells the person on Discord."""

from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeBackend, fake_providers, say
from fastapi.testclient import TestClient

from clara.agent import Agent
from clara.notifications import Notifier
from clara.prompt import SystemPrompt
from clara.schedule import ScheduleError, ScheduleService, message_of, next_run, summary_of
from clara.server import create_app
from clara.tools import default_toolbox

AUTH = {"Authorization": "Bearer secret-cli"}
ME = {"surface": "cli", "user_id": "erwan"}
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)  # a Monday


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class BrokenBackend(FakeBackend):
    async def stream(self, messages, tools):
        raise ConnectionError("Ollama is down")
        yield  # pragma: no cover


def at(**delta) -> str:
    return (NOW + timedelta(**delta)).isoformat()


def build(memory, tmp_path, backend):
    clock = Clock()
    service = ScheduleService(memory, Notifier(memory, clock), clock)
    service.agent = Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"))
    person = memory.resolve("cli", "erwan", "Erwan")
    return service, clock, person


def plan(service, person, **given):
    args = {"name": "Backup report", "prompt": "Check the backups.", "documents": [], "at": at(minutes=5), **given}
    return service.create(person, "cli", "erwan", args.pop("name"), args.pop("prompt"), args.pop("documents"), args.pop("at"), "UTC", **args)


def test_a_routine_comes_back_at_the_same_time_of_day():
    start = NOW + timedelta(hours=1)
    daily = next_run(start, "UTC", "daily", (), start)
    assert daily == start + timedelta(days=1)
    assert next_run(start, "UTC", "daily", (), start - timedelta(seconds=1)) == start  # the first run is the start


def test_a_weekly_routine_runs_on_its_weekdays_and_a_monthly_one_clamps_to_the_month():
    friday_after = next_run(NOW, "UTC", "weekly", (0, 4), NOW)  # Monday and Friday: after Monday noon, Friday
    assert friday_after == NOW + timedelta(days=4)
    jan31 = datetime(2026, 1, 31, 9, 0, tzinfo=timezone.utc)
    assert next_run(jan31, "UTC", "monthly", (), jan31) == datetime(2026, 2, 28, 9, 0, tzinfo=timezone.utc)
    assert next_run(jan31, "UTC", "monthly", (), datetime(2026, 2, 28, 9, 0, tzinfo=timezone.utc)) == datetime(2026, 3, 31, 9, 0, tzinfo=timezone.utc)


def test_the_message_is_the_prompt_then_the_documents_like_in_the_chat(memory, tmp_path):
    service, _, person = build(memory, tmp_path, FakeBackend())
    schedule = plan(service, person, documents=[{"name": "notes.md", "kind": "markdown", "text": "# Title\n```py\nx\n```"}])
    assert message_of(schedule) == (
        'Check the backups.\n\n(Attached: notes.md)\n\n<document name="notes.md" type="markdown">\n'
        "````markdown\n# Title\n```py\nx\n```\n````\n</document>"
    )
    assert summary_of("Done: 3 files.\nStill one line.\n\nDetails follow.") == "Done: 3 files. Still one line."


def test_a_schedule_must_be_in_the_future_and_make_sense(memory, tmp_path):
    service, _, person = build(memory, tmp_path, FakeBackend())
    with pytest.raises(ScheduleError, match="past"):
        plan(service, person, at=at(minutes=-5))
    with pytest.raises(ScheduleError, match="repeat"):
        plan(service, person, repeat="hourly")
    with pytest.raises(ScheduleError, match="needs a prompt"):
        plan(service, person, prompt=" ")
    weekly = plan(service, person, repeat="weekly")  # no weekday given: the one of the first run
    assert weekly.days == (0,)


async def test_a_run_is_a_turn_in_its_conversation_and_the_person_is_told_on_discord(memory, tmp_path):
    backend = FakeBackend(say("Three backups are fresh, one is late.\n\nThe late one is db-2."))
    service, clock, person = build(memory, tmp_path, backend)
    schedule = plan(service, person)
    assert service.fire_due() == 0  # not yet
    clock.now += timedelta(minutes=5)
    assert service.fire_due() == 1
    await service.wait()

    done = service.get(person, schedule.id)
    assert (done.last_status, done.runs, done.next_at) == ("ok", 1, None)  # a unique one is over
    assert done.last_summary == "Three backups are fresh, one is late."
    [event] = memory.reminder_events_after(0, person_id=person.id)
    assert event.targets == ("discord",) and event.title == "Scheduled task: Backup report"
    assert event.text == "Three backups are fresh, one is late."
    assert "Check the backups." in [m.content for m in memory.history(schedule.conversation, 10)][0]
    assert "scheduled task" in backend.calls[0][0][0]["content"]  # Clara is told why she is running
    assert memory.conversation_info(schedule.conversation).title == "Backup report"


async def test_a_routine_is_set_for_its_next_time_before_it_runs_and_runs_again(memory, tmp_path):
    service, clock, person = build(memory, tmp_path, FakeBackend(say("First."), say("Second.")))
    schedule = plan(service, person, repeat="daily")
    clock.now += timedelta(minutes=5)
    service.fire_due()
    assert service.get(person, schedule.id).next_at == NOW + timedelta(days=1, minutes=5)
    await service.wait()
    clock.now += timedelta(days=1)
    assert service.fire_due() == 1
    await service.wait()
    done = service.get(person, schedule.id)
    assert (done.runs, done.last_summary, done.conversation) == (2, "Second.", schedule.conversation)


async def test_a_run_the_server_was_too_late_for_is_skipped_and_said(memory, tmp_path):
    service, clock, person = build(memory, tmp_path, FakeBackend())
    schedule = plan(service, person, repeat="daily")
    clock.now += timedelta(hours=3)  # the server was off
    assert service.fire_due() == 0
    done = service.get(person, schedule.id)
    assert (done.last_status, done.runs) == ("missed", 0)
    assert done.next_at == NOW + timedelta(days=1, minutes=5)
    [event] = memory.reminder_events_after(0, person_id=person.id)
    assert "skipped" in event.text


async def test_a_run_that_fails_says_why(memory, tmp_path):
    service, clock, person = build(memory, tmp_path, BrokenBackend())
    schedule = plan(service, person)
    clock.now += timedelta(minutes=5)
    service.fire_due()
    await service.wait()
    done = service.get(person, schedule.id)
    assert done.last_status == "failed" and "Clara could not run it" in done.last_summary


async def test_pausing_stops_it_and_resuming_works_out_the_next_run(memory, tmp_path):
    service, clock, person = build(memory, tmp_path, FakeBackend())
    schedule = plan(service, person, repeat="daily")
    paused = service.update(person, schedule.id, enabled=False)
    assert paused.next_at is None
    clock.now += timedelta(days=3)
    assert service.fire_due() == 0
    resumed = service.update(person, schedule.id, enabled=True)
    assert resumed.next_at == NOW + timedelta(days=3, minutes=5)  # today's time is still ahead


def test_the_web_api_plans_lists_changes_runs_and_deletes(settings, tmp_path):
    backend = FakeBackend(say("Done: all good."))
    with TestClient(create_app(settings, fake_providers(settings, backend))) as api:
        made = api.post("/v1/schedules", json={**ME, "name": "Nightly", "prompt": "Check.", "at": "2030-01-02T09:00",
                                               "timezone": "UTC", "repeat": "weekly", "days": [0, 2]}, headers=AUTH)
        assert made.status_code == 201, made.text
        schedule = made.json()
        assert (schedule["days"], schedule["next_at"], schedule["resources"]) == ([0, 2], "2030-01-02T09:00:00+00:00", [])
        assert api.post("/v1/schedules", json={**ME, "name": "x", "prompt": "y", "at": "2000-01-01T09:00"}, headers=AUTH).status_code == 422
        stranger = api.post("/v1/schedules", json={**ME, "name": "x", "prompt": "y", "at": "2030-01-02T09:00", "resources": [99]}, headers=AUTH)
        assert stranger.status_code == 404
        changed = api.patch(f"/v1/schedules/{schedule['id']}", json={**ME, "name": "Weekly", "enabled": False}, headers=AUTH).json()
        assert (changed["name"], changed["next_at"], changed["enabled"]) == ("Weekly", None, False)
        assert [s["name"] for s in api.get("/v1/schedules", params=ME, headers=AUTH).json()["schedules"]] == ["Weekly"]
        assert api.post(f"/v1/schedules/{schedule['id']}/run", json=ME, headers=AUTH).status_code == 202
        assert api.delete(f"/v1/schedules/{schedule['id']}", params=ME, headers=AUTH).status_code == 200
        assert api.get("/v1/schedules", params=ME, headers=AUTH).json()["schedules"] == []
