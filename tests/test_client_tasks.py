"""clara-chat: the to-do list (/tasks, /task)."""

from datetime import datetime, timedelta, timezone

import pytest

from clara.client import ClaraApi, command, describe_task, describe_task_detail, parse_task, parse_when_list, take_when

PARIS = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=PARIS)


def test_a_time_is_read_from_the_start_of_the_words():
    assert take_when("+2h Tea".split(), NOW) == (NOW + timedelta(hours=2), ["Tea"])
    assert take_when("tomorrow 09:30 Tea".split(), NOW) == (datetime(2026, 10, 3, 9, 30, tzinfo=PARIS), ["Tea"])
    assert take_when("18:30".split(), NOW) == (datetime(2026, 10, 2, 18, 30, tzinfo=PARIS), [])
    with pytest.raises(ValueError, match="Cannot read the time"):
        take_when("soon".split(), NOW)
    with pytest.raises(ValueError, match="Cannot read the time"):
        take_when([], NOW)


def test_a_task_has_a_title_a_description_a_deadline_and_reminders():
    title, description, due, reminders = parse_task(
        "due tomorrow 18:00 remind +1h remind tomorrow 09:00 Send the invoice | to ACME, with the PDF", NOW
    )
    assert (title, description) == ("Send the invoice", "to ACME, with the PDF")
    assert due == datetime(2026, 10, 3, 18, 0, tzinfo=PARIS)
    assert reminders == [NOW + timedelta(hours=1), datetime(2026, 10, 3, 9, 0, tzinfo=PARIS)]


def test_only_a_title_is_needed():
    assert parse_task("Buy milk", NOW) == ("Buy milk", "", None, [])
    assert parse_task("Fix the due date | in the form", NOW)[:2] == ("Fix the due date", "in the form")  # not leading


@pytest.mark.parametrize(
    ("argument", "message"),
    [("", "needs a title"), ("due", "needs a time"), ("due 18:00", "needs a title"), ("remind soon Tea", "Cannot read"), ("| only", "needs a title")],
)
def test_a_task_that_cannot_be_read_is_explained(argument, message):
    with pytest.raises(ValueError, match=message):
        parse_task(argument, NOW)


def test_several_reminders_are_separated_by_commas():
    assert parse_when_list("+1h, tomorrow 09:00", NOW) == [NOW + timedelta(hours=1), datetime(2026, 10, 3, 9, 0, tzinfo=PARIS)]
    assert parse_when_list("none", NOW) == []
    with pytest.raises(ValueError, match="separate several"):
        parse_when_list("+1h tomorrow", NOW)


def task(**fields) -> dict:
    return {
        "id": 4, "title": "Taxes", "description": "Gather papers", "status": "open", "due_at": "2026-10-07T16:00:00+00:00",
        "reminders_sent": 2, "max_reminders": 10, "next_reminder": "2026-10-05T07:00:00+00:00",
        "reminders": ["2026-10-05T07:00:00+00:00", "2026-10-06T07:00:00+00:00"], "targets": ["app"], **fields,
    }


def test_the_list_shows_title_reminders_sent_and_next_reminder():
    line = describe_task(task())
    assert line.startswith("[4] Taxes  ·  due ") and "2 reminders sent" in line and "next reminder 2026-10-05" in line
    assert "1 reminder sent" in describe_task(task(reminders_sent=1))
    assert "no reminder to come" in describe_task(task(next_reminder=None, reminders=[]))
    done = describe_task(task(status="done"))
    assert "done" in done and "next reminder" not in done


def test_a_task_in_full_has_its_description_and_every_reminder():
    shown = describe_task_detail(task())
    assert "Description: Gather papers" in shown and "Reminders to come: " in shown and "Shown on: app" in shown
    assert "Description: (none)" in describe_task_detail(task(description="", reminders=[], targets=[]))


def test_the_client_keeps_its_to_do_list_on_a_real_server(live, capsys):
    app, url = live
    api = ClaraApi(url, "secret-cli", "erwan", "Erwan", "cli:erwan")

    command(api, "/task add due +2d remind +1h remind +3h Send the invoice | to ACME")
    out = capsys.readouterr().out
    assert out.startswith("Task added.") and "Send the invoice" in out and "Description: to ACME" in out
    [added] = api.tasks()
    assert (added["title"], added["description"], len(added["reminders"]), added["status"]) == ("Send the invoice", "to ACME", 2, "open")
    number = added["id"]

    command(api, "/tasks")
    assert f"[{number}] Send the invoice" in capsys.readouterr().out
    command(api, f"/task {number}")
    assert "Reminders to come: " in capsys.readouterr().out

    command(api, f"/task set {number} title Send the new invoice")
    command(api, f"/task set {number} description to the new client")
    command(api, f"/task set {number} due none")
    command(api, f"/task set {number} remind +5h, +6h")
    capsys.readouterr()
    changed = api.task(number)
    assert (changed["title"], changed["description"], changed["due_at"], len(changed["reminders"])) == (
        "Send the new invoice", "to the new client", None, 2,
    )

    command(api, f"/task done {number}")
    assert "done" in capsys.readouterr().out
    command(api, "/tasks")
    assert capsys.readouterr().out.strip() == "(no task)"
    command(api, "/tasks done")
    assert "done" in capsys.readouterr().out
    command(api, f"/task reopen {number}")
    assert api.task(number)["status"] == "open"
    command(api, f"/task delete {number}")
    assert capsys.readouterr().out.strip().endswith("Deleted.") and api.tasks("all") == []


def test_the_client_explains_what_it_cannot_do(live, capsys):
    app, url = live
    app.state.memory.resolve("cli", "erwan", "Erwan")
    api = ClaraApi(url, "secret-cli", "erwan", "Erwan", "cli:erwan")
    command(api, "/task add")  # no title
    assert "needs a title" in capsys.readouterr().out
    command(api, "/task 999")
    assert "No such task" in capsys.readouterr().out
    command(api, "/task set 1 colour red")
    assert "Usage: /task set" in capsys.readouterr().out
    command(api, "/tasks soon")
    assert "Usage: /tasks" in capsys.readouterr().out
    command(api, "/task")  # nothing to do: the help
    assert "/task add" in capsys.readouterr().out
