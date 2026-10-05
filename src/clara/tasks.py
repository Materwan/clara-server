"""Tasks: each person's to-do list, every task with the reminders that are still to come.

    create()    a client, or the model through a tool, adds a task; without reminders Clara picks them
    update()    its title, description, deadline, reminders; complete() / reopen() / delete()
    run()       the scheduler: when a reminder of a task comes due, the person is notified and Clara
                decides what the next reminders are

A task has a title, a description, an optional deadline (`due_at`), a queue of reminders (`next`) and a count of
the reminders already sent. When a reminder is due, Clara (see taskai.py) is given the task and that count: she
writes the notification, and may replace the queue (move the next reminders, add some, or stop reminding).
Whatever she cannot do (the model is down, she answers garbage), plain rules do: see `default_reminders` and
`default_follow_up`. Reminders go through the notification stream (see notifications.py), to the person
only, on the surfaces the task names (or all of theirs).

Reminding stops when the task is done, when its queue is empty, or after `max_reminders` reminders (then
the person decides: done, or remind me again).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Awaitable, Callable, Iterable

from .memory import Memory, Person
from .notifications import MAX_TITLE as MAX_NOTIFICATION_TITLE
from .notifications import NotificationError, Notifier, clean_targets
from .reminders import MAX_SLEEP, AnnounceFailed, ReminderError, _offset_name, _tzinfo, parse_moment
from .taskstore import DONE, OPEN, STATUSES, Task, TaskStore

log = logging.getLogger(__name__)

MAX_TITLE = 200
MAX_DESCRIPTION = 2000
MAX_OPEN = 100  # open tasks per person
MAX_TASKS = 500  # tasks per person, done ones included
MAX_QUEUE = 10  # reminders queued for one task
MAX_REMINDERS = 10  # reminders sent for one task (the default of the server's setting)
HORIZON = timedelta(days=366)  # no reminder is set further than this
MORNING = 9  # the hour (the person's own) of a reminder Clara or the rules set for a day, not a time
PLAN_TIMEOUT = 30.0  # seconds Clara has to pick the reminders of a new task before the rules do
NOTIFICATION_TEXT = 1000  # characters kept of a notification

TASKS = "tasks"  # who sends the reminders (not "server": Discord leaves the server's notes about its own conversations out)
NO_DUE = object()  # update(): the deadline is not touched


class TaskError(ValueError):
    """The task cannot be created or changed as asked (the message says why)."""


@dataclass(frozen=True)
class Followup:
    """What Clara decided when a reminder of a task came due."""

    message: str | None  # the notification (None: the rules write it)
    next: list[datetime] | None  # the new queue; [] stops reminding; None: keep the queue as it is


# Pick the first reminders of a new task: the moments, or None to let the rules do it. AnnounceFailed when she
# should have and could not.
Planner = Callable[[Task, datetime], Awaitable["list[datetime] | None"]]
# Decide what follows a reminder that came due, for the task as it was when it did
Follower = Callable[[Task, datetime], Awaitable["Followup | None"]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def clock_of(zone: str) -> tzinfo:
    """The clock a task shows its times in: its timezone, or the server's own."""
    if zone:
        return _tzinfo(zone)
    return datetime.now().astimezone().tzinfo or timezone.utc


def local_text(moment: datetime, zone: str) -> str:
    """`2026-10-05T09:00+02:00`: a moment on the person's clock, for the model and the clients to read."""
    return moment.astimezone(clock_of(zone)).isoformat(timespec="minutes")


def _one_line(text: str | None) -> str:
    return " ".join((text or "").split())


def _morning(local: datetime) -> datetime:
    return local.replace(hour=MORNING, minute=0, second=0, microsecond=0)


def default_reminders(due: datetime | None, now: datetime, zone: str) -> list[datetime]:
    """The first reminders of a task when nobody (not even Clara) picked them: a day before its deadline, an
    hour before, and at it; without a deadline, tomorrow morning."""
    soon = now + timedelta(minutes=1)
    if due is not None and due > soon:
        return [t for t in (due - timedelta(days=1), due - timedelta(hours=1), due) if t > soon][:MAX_QUEUE]
    local = now.astimezone(clock_of(zone))
    morning = _morning(local)
    if morning <= local + timedelta(hours=2):
        morning = _morning(local + timedelta(days=1))
    return [morning.astimezone(timezone.utc)]


def default_follow_up(task: Task, now: datetime) -> list[datetime]:
    """The next reminder of a task whose queue ran dry: halfway to its deadline, then the deadline; once it is
    past (or if it has none), a morning further away each time (1, 2, 4, then 7 days)."""
    if task.due_at is not None and task.due_at - now > timedelta(seconds=60):
        left = task.due_at - now
        if left <= timedelta(hours=2):
            return [task.due_at]
        return [(now + left / 2).replace(second=0, microsecond=0)]
    overdue = task.due_at is not None
    days = 1 if overdue else min(2 ** max(task.reminders_sent, 0), 7)
    local = now.astimezone(clock_of(task.timezone))
    return [_morning(local + timedelta(days=days)).astimezone(timezone.utc)]


def valid_reminders(moments: Iterable[datetime], now: datetime) -> list[datetime]:
    """The moments that can be reminders: in the future, not too far, each once, soonest first, at most MAX_QUEUE."""
    kept = {m.astimezone(timezone.utc) for m in moments if now + timedelta(seconds=30) < m <= now + HORIZON}
    return sorted(kept)[:MAX_QUEUE]


def describe(task: Task, max_reminders: int = MAX_REMINDERS) -> dict[str, Any]:
    def when(moment: datetime | None) -> str | None:
        return moment.isoformat(timespec="seconds") if moment else None

    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "due_at": when(task.due_at),
        "reminders_sent": task.reminders_sent,
        "max_reminders": max_reminders,
        "next_reminder": when(task.next_reminder),
        "reminders": [when(at) for at in task.next],
        "timezone": task.timezone,
        "targets": list(task.targets),  # empty: every surface of the person
        "created_at": when(task.created_at),
        "updated_at": when(task.updated_at),
        "done_at": when(task.done_at),
    }


def task_line(task: Task) -> str:
    """One line for a list: its number, title, status, reminders sent and next reminder."""
    parts = [f"[{task.id}] {task.title}", task.status]
    if task.due_at:
        parts.append(f"due {local_text(task.due_at, task.timezone)}")
    sent = f"{task.reminders_sent} reminder{'s' if task.reminders_sent != 1 else ''} sent"
    parts.append(sent)
    if task.status == OPEN:
        parts.append(f"next reminder {local_text(task.next_reminder, task.timezone)}" if task.next else "no reminder to come")
    return " | ".join(parts)


def task_detail(task: Task) -> str:
    lines = [task_line(task), f"Description: {task.description or '(none)'}"]
    if len(task.next) > 1:
        lines.append("Reminders to come: " + ", ".join(local_text(at, task.timezone) for at in task.next))
    if task.targets:
        lines.append("Shown on: " + ", ".join(task.targets))
    return "\n".join(lines)


class TaskService:
    def __init__(
        self,
        memory: Memory,
        notifier: Notifier,
        clock: Callable[[], datetime] = _utc_now,
        max_reminders: int = MAX_REMINDERS,
        plan_timeout: float = PLAN_TIMEOUT,
    ):
        self.store = TaskStore(memory)
        self.memory = memory
        self.notifier = notifier
        self._clock = clock
        self.max_reminders = max_reminders
        self.plan_timeout = plan_timeout
        self.planner: Planner | None = None  # Clara picks the reminders of a task given none (None: the rules do)
        self.follower: Follower | None = None  # Clara follows up a reminder that came due (None: the rules do)
        self._wake = asyncio.Event()
        self.stopping = False  # the server is stopping: what comes due waits for the next start
        self.firing = False  # reminders are being written

    # -- reading ------------------------------------------------------------------------------ #

    def tasks(self, person: Person, status: str | None = OPEN) -> list[Task]:
        if status is not None and status not in STATUSES:
            raise TaskError(f"status must be one of: {', '.join(STATUSES)}, or all.")
        return self.store.of(person.id, status)

    def get(self, person: Person, task_id: int) -> Task:
        task = self.store.get(person.id, task_id)
        if task is None:
            raise TaskError("No such task of yours.")
        return task

    def describe(self, task: Task) -> dict[str, Any]:
        return describe(task, self.max_reminders)

    # -- changing ----------------------------------------------------------------------------- #

    @staticmethod
    def _title(title: str | None) -> str:
        title = _one_line(title)
        if not title:
            raise TaskError("A task needs a title.")
        if len(title) > MAX_TITLE:
            raise TaskError(f"A title is at most {MAX_TITLE} characters long.")
        return title

    @staticmethod
    def _description(description: str | None) -> str:
        description = (description or "").strip()
        if len(description) > MAX_DESCRIPTION:
            raise TaskError(f"A description is at most {MAX_DESCRIPTION} characters long.")
        return description

    @staticmethod
    def _targets(targets: Iterable[str] | str | None) -> tuple[str, ...]:
        try:
            return clean_targets(targets)
        except NotificationError as error:
            raise TaskError(str(error)) from None

    @staticmethod
    def _moment(text: str, zone: str | None) -> tuple[datetime, str]:
        try:
            return parse_moment(text, zone)
        except ReminderError as error:
            raise TaskError(str(error)) from None

    def _given(self, reminders: Iterable[str], zone: str | None, now: datetime) -> tuple[list[datetime], str]:
        """The reminders a person gave (texts): their moments, and the clock the first one was read in."""
        moments: list[datetime] = []
        clock = ""
        for text in reminders:
            moment, kept = self._moment(str(text), zone)
            if moment <= now:
                raise TaskError(f"The reminder {text!r} is already past.")
            moments.append(moment)
            clock = clock or kept
        moments = sorted(set(moments))
        if len(moments) > MAX_QUEUE:
            raise TaskError(f"At most {MAX_QUEUE} reminders for one task.")
        return moments, clock

    async def create(
        self,
        person: Person,
        title: str,
        description: str = "",
        due: str | None = None,
        reminders: Iterable[str] | None = None,
        zone: str | None = None,
        origin: tuple[str, str, str] = ("", "", ""),
        targets: Iterable[str] | str | None = (),
    ) -> Task:
        """Add a task. `due`: its deadline (ISO 8601, a time without offset is read in `zone`, else the server's
        clock). `reminders`: when to remind, as the person said; none given: Clara picks them (and the rules do
        if she cannot). Raises :class:`TaskError`."""
        title, description, surfaces = self._title(title), self._description(description), self._targets(targets)
        now = self._clock()
        clock = zone or ""
        due_at = None
        if due:
            due_at, clock = self._moment(str(due), zone)
            if due_at <= now:
                raise TaskError("That deadline is already past.")
        given, given_clock = self._given(list(reminders or ()), zone, now)
        clock = clock or given_clock or _offset_name(now.astimezone())
        if self.store.count_open(person.id) >= MAX_OPEN:
            raise TaskError(f"At most {MAX_OPEN} open tasks at a time: finish or delete some first.")
        if self.store.count(person.id) >= MAX_TASKS:
            raise TaskError(f"At most {MAX_TASKS} tasks, done ones included: delete some first.")
        queue = given or default_reminders(due_at, now, clock)  # always something: Clara may only improve it
        task = self.store.add(person.id, title, description, due_at, clock, origin, surfaces, now, queue)
        self._wake.set()
        return task if given else await self._plan(task)

    def update(
        self,
        person: Person,
        task_id: int,
        title: str | None = None,
        description: str | None = None,
        due: Any = NO_DUE,
        reminders: Iterable[str] | None = None,
        targets: Iterable[str] | str | None = None,
        zone: str | None = None,
    ) -> Task:
        """Change a task: only what is given. `due`: a new deadline, or "" / None to remove it (left out: kept).
        `reminders` replaces the reminders to come ([]: stop reminding)."""
        task = self.get(person, task_id)
        now = self._clock()
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = self._title(title)
        if description is not None:
            fields["description"] = self._description(description)
        if targets is not None:
            fields["targets"] = self._targets(targets)
        clock = task.timezone
        if due is not NO_DUE:
            if due:
                due_at, _ = self._moment(str(due), zone or task.timezone or None)
                if due_at <= now:
                    raise TaskError("That deadline is already past.")
                fields["due_at"] = due_at
            else:
                fields["due_at"] = None
        queue: list[datetime] | None = None
        if reminders is not None:
            if task.status != OPEN:
                raise TaskError("The task is done: reopen it before setting reminders.")
            queue, _ = self._given(list(reminders), zone or clock or None, now)
        if fields:
            self.store.update(task.id, now, **fields)
        if queue is not None:
            self.store.set_reminders(task.id, queue, now)
        self._wake.set()
        return self.get(person, task_id)

    def complete(self, person: Person, task_id: int) -> Task:
        """Mark a task done: it is not reminded any more."""
        task = self.get(person, task_id)
        if task.status != DONE:
            self.store.set_status(task.id, DONE, self._clock())
            self._wake.set()
        return self.get(person, task_id)

    async def reopen(
        self, person: Person, task_id: int, reminders: Iterable[str] | None = None, zone: str | None = None
    ) -> Task:
        """A done task is open again, with reminders as given or as Clara picks them."""
        task = self.get(person, task_id)
        if task.status == OPEN:
            return task
        now = self._clock()
        given, _ = self._given(list(reminders or ()), zone or task.timezone or None, now)
        if self.store.count_open(person.id) >= MAX_OPEN:
            raise TaskError(f"At most {MAX_OPEN} open tasks at a time: finish or delete some first.")
        self.store.set_status(task.id, OPEN, now)
        self.store.set_reminders(task.id, given or default_reminders(task.due_at, now, task.timezone), now)
        self._wake.set()
        task = self.get(person, task_id)
        return task if given else await self._plan(task)

    def delete(self, person: Person, task_id: int) -> bool:
        deleted = self.store.delete(person.id, task_id)
        if deleted:
            self._wake.set()
        return deleted

    async def _plan(self, task: Task) -> Task:
        """Clara picks the reminders of a task nobody gave any for; the rules' ones stay if she cannot."""
        if self.planner is None or self.stopping:
            return task
        now = self._clock()
        try:
            planned = await asyncio.wait_for(self.planner(task, now), self.plan_timeout)
        except AnnounceFailed as error:
            log.warning("task %s: Clara could not pick the reminders (%s)", task.id, error)
            return task
        except asyncio.TimeoutError:
            log.warning("task %s: Clara took more than %g seconds to pick the reminders", task.id, self.plan_timeout)
            return task
        except Exception:
            log.exception("task %s: could not pick the reminders", task.id)
            return task
        picked = valid_reminders(planned or (), now)
        if not picked or self.store.get_any(task.id) is None:
            return task
        self.store.set_reminders(task.id, picked, now)
        self._wake.set()
        return self.store.get_any(task.id) or task

    # -- the scheduler ------------------------------------------------------------------------ #

    async def fire_due(self) -> int:
        """Remind of every task that has a reminder due: Clara writes each notification and decides what
        follows (all at once), then it is sent. A task fires once however many of its reminders were missed
        (the server was down). Returns how many fired."""
        started = self._clock()
        due = self.store.due(started)
        if not due:
            return 0
        self.firing = True
        try:
            decided = await asyncio.gather(*(self._follow(task, started) for task in due))
            now = self._clock()  # after the writing: that is when the clients get it
            fired = 0
            for task, follow in zip(due, decided):
                current = self.store.get_any(task.id)
                if current is None or current.status != OPEN:  # done or deleted while Clara was writing
                    continue
                self._send(current, follow, now, started, edited=current.updated_at > task.updated_at)
                fired += 1
            return fired
        finally:
            self.firing = False

    async def _follow(self, task: Task, now: datetime) -> Followup | None:
        if self.follower is None or self.stopping:
            return None
        try:
            return await self.follower(task, now)
        except AnnounceFailed as error:
            log.warning("task %s: Clara could not follow it up (%s)", task.id, error)
        except Exception:
            log.exception("task %s: could not follow it up", task.id)
        return None

    def _send(self, task: Task, follow: Followup | None, now: datetime, started: datetime, edited: bool) -> None:
        """Tell the person, then queue what comes next and count the reminder."""
        sent = task.reminders_sent + 1
        last = sent >= self.max_reminders
        decided = None
        if follow is not None and follow.next is not None and not edited:
            decided = valid_reminders(follow.next, now) if follow.next else []  # [] stops reminding
            if follow.next and not decided:
                decided = None  # nothing she said can be used: as if she had not said
        kept = [at for at in task.next if at > started]
        if last:
            remaining: list[datetime] = []
        elif decided is not None:
            remaining = decided
        elif kept:
            remaining = kept
        else:
            remaining = default_follow_up(replace(task, reminders_sent=sent), now)
        text = follow.message if follow and follow.message else self._plain_text(task, sent)
        if last and not (follow and follow.message):
            text += " This was the last reminder of this task: mark it done, or ask me to remind you again."
        self.store.fired(task.id, now, remaining)
        try:
            self.notifier.notify(
                task.person_id, text[:NOTIFICATION_TEXT], f"Task: {task.title}"[:MAX_NOTIFICATION_TITLE], task.targets,
                TASKS, task.conversation, limited=False,
            )
        except NotificationError:
            log.exception("tasks: could not remind of task %s", task.id)

    @staticmethod
    def _plain_text(task: Task, sent: int) -> str:
        text = f"Reminder {sent} for your task: {task.title}."
        if task.due_at:
            text += f" Due {local_text(task.due_at, task.timezone)}."
        if task.description:
            text += f"\n{task.description}"
        return text

    async def run(self) -> None:
        """The scheduler loop; runs for the life of the server."""
        while True:
            self._wake.clear()  # before looking: a task changed meanwhile wakes the next wait at once
            try:
                if not self.stopping:
                    await self.fire_due()
            except Exception:
                log.exception("tasks: could not fire the due ones")
            upcoming = self.store.next_due()
            delay = MAX_SLEEP if upcoming is None or self.stopping else (upcoming - self._clock()).total_seconds()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), max(0.0, min(delay, MAX_SLEEP)))

