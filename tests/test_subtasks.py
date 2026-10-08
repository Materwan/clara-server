"""Sub tasks: a task divided into tasks of its own, none of them later than the deadline of the tasks it is part of."""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeBackend, fake_providers, say
from fastapi.testclient import TestClient

from clara.notifications import Notifier
from clara.server import create_app
from clara.tasks import MAX_SUBTASKS, TaskError, TaskService
from clara.taskstore import DONE, OPEN
from clara.tools import ToolContext, default_toolbox

AUTH = {"Authorization": "Bearer secret-cli"}
ME = {"surface": "cli", "user_id": "erwan"}
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def at(**delta) -> str:
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


async def main(service, erwan, **kwargs):
    return await service.create(erwan, "Move house", due=at(days=10), reminders=[at(days=1)], **kwargs)


async def test_a_sub_task_has_its_own_description_deadline_and_reminders(service, erwan):
    parent = await main(service, erwan, description="The whole move")
    sub = await service.create(erwan, "Pack the books", "Boxes in the garage", at(days=5), [at(days=2), at(days=4)], parent_id=parent.id)
    assert sub.parent_id == parent.id and sub.description == "Boxes in the garage" and sub.due_at == NOW + timedelta(days=5)
    assert sub.next == (NOW + timedelta(days=2), NOW + timedelta(days=4))
    assert service.get(erwan, parent.id).description == "The whole move" and service.get(erwan, parent.id).parent_id is None


async def test_a_sub_task_cannot_be_due_after_its_main_task(service, erwan):
    parent = await main(service, erwan)
    with pytest.raises(TaskError, match="cannot be due after.*Move house"):
        await service.create(erwan, "Late", due=at(days=11), parent_id=parent.id)
    with pytest.raises(TaskError, match="reminder of a sub task cannot be after"):
        await service.create(erwan, "Late reminder", reminders=[at(days=11)], parent_id=parent.id)
    ok = await service.create(erwan, "On time", due=at(days=10), reminders=[at(days=10)], parent_id=parent.id)  # at it is fine
    assert ok.due_at == parent.due_at


async def test_the_limit_is_the_earliest_deadline_up_the_chain(service, erwan):
    top = await main(service, erwan)
    middle = await service.create(erwan, "Middle", due=at(days=6), parent_id=top.id)
    inner = await service.create(erwan, "Inner", parent_id=middle.id)  # no deadline of its own
    with pytest.raises(TaskError, match="cannot be due after"):
        await service.create(erwan, "Too late", due=at(days=7), parent_id=inner.id)
    assert (await service.create(erwan, "Fine", due=at(days=6), parent_id=inner.id)).parent_id == inner.id
    no_deadline = await service.create(erwan, "Open ended")
    sub = await service.create(erwan, "Under open ended", due=at(days=400), parent_id=no_deadline.id)
    assert sub.due_at is not None  # a main task without a deadline sets no limit
    assert service.limit_for(service.get(erwan, inner.id))[0] == middle.due_at


async def test_the_reminders_the_rules_choose_stay_inside_the_limit(service, erwan):
    parent = await service.create(erwan, "Soon", due=at(hours=30), reminders=[at(hours=2)])
    sub = await service.create(erwan, "Part", parent_id=parent.id)  # tomorrow morning would be fine, later would not
    assert sub.next and all(moment <= parent.due_at for moment in sub.next)
    tight = await service.create(erwan, "Tight", due=at(minutes=90), reminders=[at(minutes=30)])
    part = await service.create(erwan, "Quick part", parent_id=tight.id)
    assert part.next == (tight.due_at,)  # the rules' morning is after the limit: the limit itself is used


async def test_clara_cannot_pick_reminders_after_the_limit(service, erwan):
    parent = await main(service, erwan)

    async def planner(task, now):
        return [NOW + timedelta(days=3), NOW + timedelta(days=30)]

    service.planner = planner
    sub = await service.create(erwan, "Part", parent_id=parent.id)
    assert sub.next == (NOW + timedelta(days=3),)


async def test_the_main_deadline_cannot_move_before_a_sub_task(service, erwan):
    parent = await main(service, erwan)
    sub = await service.create(erwan, "Part", due=at(days=8), parent_id=parent.id)
    with pytest.raises(TaskError, match=rf"\[{sub.id}\].*change it first"):
        service.update(erwan, parent.id, due=at(days=7))
    with pytest.raises(TaskError, match="change it first"):  # its reminders (a day before, at it) are late too
        service.update(erwan, parent.id, due=at(days=7))
    service.update(erwan, sub.id, due=at(days=3), reminders=[at(days=2)])
    assert service.update(erwan, parent.id, due=at(days=7)).due_at == NOW + timedelta(days=7)
    reminded = await service.create(erwan, "Reminded", reminders=[at(days=6, hours=12)], parent_id=parent.id)
    with pytest.raises(TaskError, match="change it first"):
        service.update(erwan, parent.id, due=at(days=6))
    assert reminded.id  # the reminders count too


async def test_a_sub_task_cannot_be_moved_after_its_main_task(service, erwan):
    parent = await main(service, erwan)
    sub = await service.create(erwan, "Part", parent_id=parent.id)
    with pytest.raises(TaskError, match="cannot be due after"):
        service.update(erwan, sub.id, due=at(days=12))
    with pytest.raises(TaskError, match="reminder of a sub task"):
        service.update(erwan, sub.id, reminders=[at(days=12)])
    assert service.update(erwan, sub.id, due=at(days=9), reminders=[at(days=8)]).next == (NOW + timedelta(days=8),)
    assert service.update(erwan, parent.id, due="").due_at is None  # the main task may lose its deadline


async def test_finishing_the_last_sub_task_finishes_the_main_task(service, erwan):
    parent = await main(service, erwan)
    first = await service.create(erwan, "One", parent_id=parent.id)
    second = await service.create(erwan, "Two", parent_id=parent.id)
    service.complete(erwan, first.id)
    assert service.get(erwan, parent.id).status == OPEN
    service.complete(erwan, second.id)
    done = service.get(erwan, parent.id)
    assert done.status == DONE and done.next == ()


async def test_finishing_a_task_finishes_what_it_is_made_of_and_so_on_up(service, erwan):
    top = await main(service, erwan)
    middle = await service.create(erwan, "Middle", parent_id=top.id)
    leaf = await service.create(erwan, "Leaf", parent_id=middle.id)
    other = await service.create(erwan, "Other", parent_id=top.id)
    service.complete(erwan, middle.id)
    assert (service.get(erwan, leaf.id).status, service.get(erwan, leaf.id).next) == (DONE, ())
    assert service.get(erwan, top.id).status == OPEN  # "Other" is not done
    service.complete(erwan, other.id)
    assert service.get(erwan, top.id).status == DONE
    service.complete(erwan, top.id)  # nothing to do twice


async def test_reopening_a_sub_task_reopens_the_tasks_it_is_part_of(service, erwan):
    parent = await main(service, erwan)
    sub = await service.create(erwan, "Part", parent_id=parent.id)
    service.complete(erwan, parent.id)
    assert service.get(erwan, sub.id).status == DONE
    await service.reopen(erwan, sub.id, [at(days=2)])
    assert service.get(erwan, sub.id).status == OPEN
    reopened = service.get(erwan, parent.id)
    assert reopened.status == OPEN and reopened.next  # with reminders of its own
    await service.reopen(erwan, parent.id)  # already open: nothing happens


async def test_a_done_task_takes_no_new_sub_task(service, erwan):
    parent = await main(service, erwan)
    service.complete(erwan, parent.id)
    with pytest.raises(TaskError, match="reopen it before adding a sub task"):
        await service.create(erwan, "Late part", parent_id=parent.id)
    with pytest.raises(TaskError, match="does not exist"):
        await service.create(erwan, "Orphan", parent_id=999)


async def test_deleting_a_task_deletes_its_sub_tasks_and_may_finish_the_main_task(service, erwan, memory):
    parent = await main(service, erwan)
    done = await service.create(erwan, "Done", parent_id=parent.id)
    doomed = await service.create(erwan, "Doomed", parent_id=parent.id)
    deep = await service.create(erwan, "Deep", parent_id=doomed.id)
    service.complete(erwan, done.id)
    assert service.delete(erwan, doomed.id)
    assert service.store.get(erwan.id, deep.id) is None  # went with it
    assert service.get(erwan, parent.id).status == DONE  # what is left of it is done
    assert memory.database.execute("SELECT COUNT(*) FROM task_reminders WHERE task_id = ?", (deep.id,)).fetchone()[0] == 0


async def test_a_task_has_a_number_of_sub_tasks_at_most(service, erwan):
    parent = await service.create(erwan, "Big", reminders=[at(hours=2)])
    for number in range(MAX_SUBTASKS):
        service.store.add(erwan.id, f"s{number}", "", None, "", ("", "", ""), (), NOW, (), parent.id)
    with pytest.raises(TaskError, match="At most 50 sub tasks"):
        await service.create(erwan, "One more", parent_id=parent.id)


async def test_a_reminder_that_comes_due_is_not_followed_by_one_after_the_limit(service, erwan, clock):
    parent = await service.create(erwan, "Soon", due=at(days=2), reminders=[at(hours=5)])
    sub = await service.create(erwan, "Part", reminders=[at(hours=1)], parent_id=parent.id)
    clock.now = NOW + timedelta(days=2, hours=1)  # the main deadline has gone by
    await service.fire_due()
    assert service.get(erwan, sub.id).reminders_sent == 1 and service.get(erwan, sub.id).next == ()


# --- the tools and the routes ----------------------------------------------------------------------


def context(memory, service, person):
    return ToolContext(person, memory, None, None, "cli", "erwan", "cli:erwan", tasks=service)


async def test_the_model_divides_a_task_and_reads_it_back(memory, service, erwan):
    toolbox, ctx = default_toolbox(), context(memory, service, erwan)
    await toolbox.arun("add_task", ctx, {"title": "Move house", "due": at(days=10), "reminders": [at(days=1)]})
    number = service.tasks(erwan)[0].id
    out = await toolbox.arun("add_task", ctx, {"title": "Pack", "description": "Books", "parent_id": number, "reminders": [at(days=2)]})
    assert out.startswith("Sub task added.") and f"sub task of [{number}]" in out and "Description: Books" in out
    assert "Nothing of it may be later than" in out
    late = await toolbox.arun("add_task", ctx, {"title": "Late", "parent_id": number, "due": at(days=11)})
    assert late.startswith("Error: A sub task cannot be due after")
    assert (await toolbox.arun("add_task", ctx, {"title": "x", "parent_id": "abc"})).startswith("Error: task_id must be")
    shown = await toolbox.arun("list_tasks", ctx, {"task_id": number})
    assert "0/1 sub tasks done" in shown and "Sub task: [" in shown and "Pack" in shown
    sub = [t for t in service.tasks(erwan) if t.parent_id][0]
    await toolbox.arun("update_task", ctx, {"task_id": sub.id, "status": "done"})
    assert service.get(erwan, number).status == DONE


@pytest.fixture
def api(settings):
    backend = FakeBackend(*[say('{"reminders": ["2030-01-02T09:00"]}')] * 8)
    with TestClient(create_app(settings, fake_providers(settings, backend))) as client:
        yield client


def test_sub_tasks_over_http(api):
    parent = api.post("/v1/tasks", json={**ME, "user_name": "Erwan", "title": "Trip", "due": "2030-02-01T10:00",
                                         "reminders": ["2030-01-02T09:00"], "timezone": "UTC"}, headers=AUTH).json()
    sub = api.post("/v1/tasks", json={**ME, "title": "Book hotel", "description": "Near the station",
                                      "due": "2030-01-20T10:00", "reminders": ["2030-01-10T09:00"], "timezone": "UTC",
                                      "parent_id": parent["id"]}, headers=AUTH)
    assert sub.status_code == 201 and sub.json()["parent_id"] == parent["id"]
    assert sub.json()["due_limit"] == "2030-02-01T10:00:00+00:00"
    late = api.post("/v1/tasks", json={**ME, "title": "Too late", "due": "2030-02-02T10:00", "timezone": "UTC",
                                       "parent_id": parent["id"]}, headers=AUTH)
    assert late.status_code == 422 and "cannot be due after" in late.json()["detail"]
    missing = api.post("/v1/tasks", json={**ME, "title": "Orphan", "reminders": ["2030-01-10T09:00"], "parent_id": 999}, headers=AUTH)
    assert missing.status_code == 422

    listed = {t["id"]: t for t in api.get("/v1/tasks", params={**ME, "status": "all"}, headers=AUTH).json()["tasks"]}
    assert listed[parent["id"]]["subtasks"] == {"total": 1, "done": 0} and listed[parent["id"]]["parent_id"] is None
    assert listed[sub.json()["id"]]["due_limit"] == "2030-02-01T10:00:00+00:00"

    moved = api.patch(f"/v1/tasks/{parent['id']}", json={**ME, "due": "2030-01-15T10:00"}, headers=AUTH)
    assert moved.status_code == 422 and "change it first" in moved.json()["detail"]

    api.patch(f"/v1/tasks/{sub.json()['id']}", json={**ME, "status": "done"}, headers=AUTH)
    assert api.get(f"/v1/tasks/{parent['id']}", params=ME, headers=AUTH).json()["status"] == "done"
    assert api.get(f"/v1/tasks/{parent['id']}", params=ME, headers=AUTH).json()["subtasks"] == {"total": 1, "done": 1}
    assert api.delete(f"/v1/tasks/{parent['id']}", params=ME, headers=AUTH).status_code == 200
    assert api.get("/v1/tasks", params={**ME, "status": "all"}, headers=AUTH).json()["tasks"] == []


# --- moving a task under another, and ordering -------------------------------------------------------


async def test_a_task_moves_under_another_with_its_sub_tasks_and_back(service, erwan):
    a = await service.create(erwan, "A", reminders=[at(days=1)])
    b = await service.create(erwan, "B", reminders=[at(days=2)])
    part = await service.create(erwan, "Part of B", reminders=[at(days=2)], parent_id=b.id)
    moved = service.move(erwan, b.id, a.id)
    assert moved.parent_id == a.id and service.get(erwan, part.id).parent_id == b.id  # its own sub tasks stay with it
    assert [t.id for t in service.store.children(a.id)] == [b.id]
    assert service.move(erwan, b.id, None).parent_id is None


async def test_a_task_cannot_be_moved_into_itself_or_its_own_sub_tasks(service, erwan):
    a = await service.create(erwan, "A", reminders=[at(days=1)])
    deep = await service.create(erwan, "Deep", reminders=[at(days=1)], parent_id=a.id)
    for target in (a.id, deep.id):
        with pytest.raises(TaskError, match="part of itself"):
            service.move(erwan, a.id, target)


async def test_done_tasks_do_not_move_and_take_nothing(service, erwan):
    a = await service.create(erwan, "A", reminders=[at(days=1)])
    b = await service.create(erwan, "B", reminders=[at(days=1)])
    service.complete(erwan, a.id)
    with pytest.raises(TaskError, match="is done: reopen it before moving"):
        service.move(erwan, a.id, b.id)
    with pytest.raises(TaskError, match="reopen it before adding a sub task"):
        service.move(erwan, b.id, a.id)


async def test_a_task_cannot_move_under_a_deadline_it_is_later_than(service, erwan):
    soon = await service.create(erwan, "Soon", due=at(days=2), reminders=[at(days=1)])
    late_due = await service.create(erwan, "Late deadline", due=at(days=5), reminders=[at(days=1)])
    late_reminder = await service.create(erwan, "Late reminder", reminders=[at(days=4)])
    ok = await service.create(erwan, "Fits", due=at(days=1, hours=5), reminders=[at(days=1)])
    with pytest.raises(TaskError, match="cannot be due after"):
        service.move(erwan, late_due.id, soon.id)
    with pytest.raises(TaskError, match="reminder of a sub task cannot be after"):
        service.move(erwan, late_reminder.id, soon.id)
    assert service.move(erwan, ok.id, soon.id).parent_id == soon.id
    # what it holds counts too
    holder = await service.create(erwan, "Holder", reminders=[at(days=1)])
    await service.create(erwan, "Inside", due=at(days=6), reminders=[at(days=1)], parent_id=holder.id)
    with pytest.raises(TaskError, match="cannot be due after"):
        service.move(erwan, holder.id, soon.id)
    assert service.get(erwan, holder.id).parent_id is None  # nothing changed


async def test_a_task_moved_out_of_the_last_open_one_finishes_the_task_it_was_part_of(service, erwan):
    parent = await main(service, erwan)
    done = await service.create(erwan, "Done", parent_id=parent.id)
    leaving = await service.create(erwan, "Leaving", parent_id=parent.id)
    service.complete(erwan, done.id)
    service.move(erwan, leaving.id, None)
    assert service.get(erwan, parent.id).status == DONE


async def test_the_number_of_sub_tasks_under_a_task_is_limited_when_moving_too(service, erwan):
    parent = await service.create(erwan, "Big", reminders=[at(hours=2)])
    for number in range(MAX_SUBTASKS):
        service.store.add(erwan.id, f"s{number}", "", None, "", ("", "", ""), (), NOW, (), parent.id)
    loose = await service.create(erwan, "Loose", reminders=[at(hours=3)])
    with pytest.raises(TaskError, match="At most 50 sub tasks"):
        service.move(erwan, loose.id, parent.id)


async def test_tasks_are_put_before_another_and_the_order_is_kept(service, erwan):
    parent = await main(service, erwan)
    kids = [await service.create(erwan, name, reminders=[at(days=1)], parent_id=parent.id) for name in "xyz"]
    assert [t.title for t in service.store.children(parent.id)] == ["x", "y", "z"]  # oldest first until one is chosen
    service.move(erwan, kids[2].id, before=kids[0].id)
    assert [t.title for t in service.store.children(parent.id)] == ["z", "x", "y"]
    service.move(erwan, kids[2].id, before=None)  # last
    assert [t.title for t in service.store.children(parent.id)] == ["x", "y", "z"]
    newest = await service.create(erwan, "w", reminders=[at(days=1)], parent_id=parent.id)
    assert [t.title for t in service.store.children(parent.id)] == ["x", "y", "z", "w"]  # a new one goes last
    service.move(erwan, newest.id, parent.id, before=kids[1].id)  # same parent: only the place changes
    assert [t.title for t in service.store.children(parent.id)] == ["x", "w", "y", "z"]
    assert service.move(erwan, kids[0].id, before=kids[0].id).id == kids[0].id  # before itself: nothing
    with pytest.raises(TaskError, match="not among the tasks"):
        service.move(erwan, kids[0].id, before=parent.id)


async def test_the_model_moves_a_task_and_the_web_page_asks_for_it_over_http(memory, service, erwan):
    toolbox, ctx = default_toolbox(), context(memory, service, erwan)
    a = await service.create(erwan, "A", reminders=[at(days=1)])
    b = await service.create(erwan, "B", reminders=[at(days=1)])
    out = await toolbox.arun("update_task", ctx, {"task_id": b.id, "parent_id": a.id})
    assert out.startswith("Task updated.") and f"sub task of [{a.id}]" in out
    assert service.get(erwan, b.id).parent_id == a.id
    await toolbox.arun("update_task", ctx, {"task_id": b.id, "parent_id": 0})
    assert service.get(erwan, b.id).parent_id is None
    assert (await toolbox.arun("update_task", ctx, {"task_id": a.id, "parent_id": a.id})).startswith("Error: A task cannot")


def test_moving_a_task_over_http(api):
    ids = [api.post("/v1/tasks", json={**ME, "user_name": "Erwan", "title": name, "reminders": ["2030-01-02T09:00"],
                                       "timezone": "UTC"}, headers=AUTH).json()["id"] for name in "abc"]
    a, b, c = ids
    under = api.patch(f"/v1/tasks/{b}", json={**ME, "parent_id": a}, headers=AUTH)
    assert under.status_code == 200 and under.json()["parent_id"] == a
    ahead = api.patch(f"/v1/tasks/{c}", json={**ME, "before_id": a}, headers=AUTH)  # main tasks: c before a
    assert ahead.status_code == 200 and ahead.json()["position"] == 1
    listed = {t["id"]: t for t in api.get("/v1/tasks", params={**ME, "status": "all"}, headers=AUTH).json()["tasks"]}
    assert listed[a]["position"] == 2 and listed[b]["parent_id"] == a
    out = api.patch(f"/v1/tasks/{b}", json={**ME, "parent_id": None}, headers=AUTH)
    assert out.status_code == 200 and out.json()["parent_id"] is None
    assert api.patch(f"/v1/tasks/{a}", json={**ME, "parent_id": a}, headers=AUTH).status_code == 422
    assert api.patch(f"/v1/tasks/{a}", json={**ME, "title": "A2"}, headers=AUTH).json()["parent_id"] is None  # not asked: kept
