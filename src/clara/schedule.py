"""Scheduled tasks: a prompt (with documents) that Clara runs by herself at a set time, once or as a routine.

    ScheduleService.create()     a person plans one (the web site's Schedule tab, schedulesapi.py)
    ScheduleService.run()        the scheduler: when one is due, Clara runs it in its own conversation
    ScheduleService.run_now()    the person does not wait for the time

Every run of a schedule is a turn in the same conversation (`conversation`), as the person's account: it is in their
history, the integrations attached to it are what Clara can reach, and a request for permission is pushed to their
surfaces like in any chat (the run does not wait for the answer). When the run is over the person gets a private
Discord message with a summary: the first paragraph of Clara's final answer. A run missed because the server was off
for more than LATE_SECONDS is skipped, and said so.
"""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .agent import Agent, ChatRequest, ServerStopping
from .announce import _iana
from .dueloop import DueLoop
from .memory import Memory, Person
from .notifications import SERVER, NotificationError, Notifier
from .reminders import ReminderError, _tzinfo, parse_moment

log = logging.getLogger(__name__)

REPEATS = ("daily", "weekly", "monthly")
MAX_NAME = 100
MAX_PROMPT = 20_000
MAX_DOCUMENTS_CHARS = 150_000  # the same limit as a message of the web chat
MAX_PER_PERSON = 50
LATE_SECONDS = 3600  # a run later than this (the server was off) is skipped
MAX_SUMMARY = 700
OWNER = "schedule"
NOTIFY_TARGETS = ("discord",)

INSTRUCTIONS = (
    "This is a scheduled task: the person asked you to do it at this time and is most likely not looking. Do the "
    "work with your tools. Nobody can answer a question now, so ask none: take the most reasonable reading. If an "
    "action waits for the person's permission, carry on with the rest and say what is waiting. Your final answer is "
    "also sent to the person as a notification: begin it with a plain summary of what you did and found, in at most "
    "three sentences, without heading or list; details may follow after a blank line."
)


class ScheduleError(ValueError):
    """The schedule cannot be made or changed as asked (the message says why)."""


@dataclass(frozen=True)
class Schedule:
    id: int
    person_id: int
    name: str
    prompt: str
    documents: tuple[dict, ...]  # {"name", "kind", "text"}
    surface: str
    user_id: str
    conversation: str
    project_id: int | None
    repeat: str  # "" (once), daily, weekly, monthly
    days: tuple[int, ...]  # weekly: the weekdays, 0 is Monday
    start_at: datetime
    timezone: str
    next_at: datetime | None
    enabled: bool
    runs: int
    last_at: datetime | None
    last_status: str  # "", ok, failed, missed
    last_summary: str
    created_at: datetime


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat(timespec="seconds") if moment else None


def _moment(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def next_run(start: datetime, zone: str, repeat: str, days: tuple[int, ...], after: datetime) -> datetime:
    """The first moment of a routine strictly after `after` (UTC), at the time of day of `start` in `zone`. Monthly
    keeps the day of the month of `start`, the last day of a shorter month."""
    tz = _tzinfo(zone)
    first = start.astimezone(tz)
    day = max(first.date(), after.astimezone(tz).date())
    for _ in range(800):  # a monthly one is never more than 62 days away
        if (
            repeat == "daily"
            or (repeat == "weekly" and day.weekday() in days)
            or (repeat == "monthly" and day.day == min(first.day, calendar.monthrange(day.year, day.month)[1]))
        ):
            moment = datetime.combine(day, first.time(), tz).astimezone(UTC)
            if moment > after:
                return moment
        day += timedelta(days=1)
    raise ScheduleError("No next run found.")  # unreachable for a valid routine


def _document(doc: dict) -> str:
    """A document as the web chat puts it in a message (documents.js, `forModel`)."""
    if doc["kind"] == "pdf":
        body = doc["text"]
    else:
        longest = max([0, *(len(run) for run in re.findall(r"`+", doc["text"]))])
        fence = "`" * max(3, longest + 1)
        body = f"{fence}{doc['kind']}\n{doc['text'].rstrip()}\n{fence}"
    name = doc["name"].replace('"', "'").replace("\n", "'")
    return f'<document name="{name}" type="{doc["kind"] or "text"}">\n{body}\n</document>'


def message_of(schedule: Schedule) -> str:
    """What Clara is sent: the prompt, then the documents (as `compose` does in documents.js)."""
    parts = [schedule.prompt.strip()] if schedule.prompt.strip() else []
    docs = schedule.documents
    if docs:
        names = ", ".join(doc["name"] for doc in docs)
        parts.append(f"(Attached: {names})" if parts else f"Here {'is' if len(docs) == 1 else 'are'}: {names}.")
        parts += [_document(doc) for doc in docs]
    return "\n\n".join(parts)


def summary_of(reply: str) -> str:
    """The first paragraph of the answer, on one line."""
    text = " ".join((reply.strip().split("\n\n", 1)[0]).split())
    if not text:
        return "Clara finished, without writing anything."
    return text if len(text) <= MAX_SUMMARY else text[: MAX_SUMMARY - 1] + "…"


class ScheduleStore:
    """The `schedules` table (memory.py)."""

    def __init__(self, memory: Memory):
        self._memory = memory

    @property
    def _db(self) -> sqlite3.Connection:
        return self._memory.database

    @property
    def _lock(self):
        return self._memory.lock

    @staticmethod
    def _schedule(row: sqlite3.Row) -> Schedule:
        return Schedule(
            row["id"], row["person_id"], row["name"], row["prompt"], tuple(json.loads(row["documents"])),
            row["surface"], row["user_id"], row["conversation"], row["project_id"], row["repeat"],
            tuple(int(day) for day in row["days"].split(",") if day), datetime.fromisoformat(row["start_at"]),
            row["timezone"], _moment(row["next_at"]), bool(row["enabled"]), row["runs"], _moment(row["last_at"]),
            row["last_status"], row["last_summary"], datetime.fromisoformat(row["created_at"]),
        )

    def _find(self, sql: str, *values: Any) -> list[Schedule]:
        with self._lock:
            return [self._schedule(row) for row in self._db.execute(sql, values).fetchall()]

    # The columns add() and set() may write: their names go into the SQL, so only these
    _COLUMNS = frozenset({
        "person_id", "name", "prompt", "documents", "surface", "user_id", "conversation", "project_id", "repeat",
        "days", "start_at", "timezone", "next_at", "enabled", "runs", "last_at", "last_status", "last_summary",
        "created_at",
    })

    @classmethod
    def _checked(cls, columns: dict[str, Any]) -> None:
        unknown = set(columns) - cls._COLUMNS
        if unknown:
            raise ValueError(f"Unknown schedule columns: {', '.join(sorted(unknown))}")

    def add(self, **columns: Any) -> int:
        self._checked(columns)
        marks = ",".join("?" * len(columns))
        with self._lock, self._db:
            cursor = self._db.execute(
                f"INSERT INTO schedules ({','.join(columns)}) VALUES ({marks})", list(columns.values())
            )
        return cursor.lastrowid  # type: ignore[return-value]

    def get(self, person_id: int, schedule_id: int) -> Schedule | None:
        found = self._find("SELECT * FROM schedules WHERE id = ? AND person_id = ?", schedule_id, person_id)
        return found[0] if found else None

    def of(self, person_id: int) -> list[Schedule]:
        return self._find("SELECT * FROM schedules WHERE person_id = ? ORDER BY id", person_id)

    def count(self, person_id: int) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM schedules WHERE person_id = ?", (person_id,)).fetchone()[0]

    def due(self, now: datetime) -> list[Schedule]:
        return self._find(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_at IS NOT NULL AND next_at <= ? ORDER BY next_at",
            _stamp(now),
        )

    def next_due(self) -> datetime | None:
        with self._lock:
            row = self._db.execute(
                "SELECT MIN(next_at) FROM schedules WHERE enabled = 1 AND next_at IS NOT NULL"
            ).fetchone()
        return _moment(row[0])

    def set(self, schedule_id: int, **columns: Any) -> None:
        self._checked(columns)
        sets = ", ".join(f"{name} = ?" for name in columns)
        with self._lock, self._db:
            self._db.execute(f"UPDATE schedules SET {sets} WHERE id = ?", [*columns.values(), schedule_id])

    def record(self, schedule_id: int, at: datetime, status: str, summary: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE schedules SET last_at = ?, last_status = ?, last_summary = ?,"
                " runs = runs + (? != 'missed') WHERE id = ?",
                (_stamp(at), status, summary, status, schedule_id),
            )

    def delete(self, person_id: int, schedule_id: int) -> bool:
        with self._lock, self._db:
            return self._db.execute(
                "DELETE FROM schedules WHERE id = ? AND person_id = ?", (schedule_id, person_id)
            ).rowcount > 0


class ScheduleService(DueLoop):
    def __init__(self, memory: Memory, notifier: Notifier, clock: Callable[[], datetime] = _utc_now):
        self.memory = memory
        self.notifier = notifier
        self.store = ScheduleStore(memory)
        self._clock = clock
        self._wake = asyncio.Event()
        self._running: dict[int, asyncio.Task] = {}
        self.agent: Agent | None = None  # who runs them
        self.waiting: Callable[[str], int] = lambda conversation: 0  # requests for permission waiting in a conversation
        self.stopping = False  # the server is stopping: what comes due waits for the next start

    @property
    def firing(self) -> bool:
        return bool(self._running)

    # -- planning ------------------------------------------------------------------------------ #

    @staticmethod
    def _name(name: str) -> str:
        name = " ".join((name or "").split())
        if not name:
            raise ScheduleError("A scheduled task needs a name.")
        if len(name) > MAX_NAME:
            raise ScheduleError(f"A name is at most {MAX_NAME} characters long.")
        return name

    @staticmethod
    def _prompt(prompt: str, documents: list[dict]) -> str:
        if len(prompt) > MAX_PROMPT:
            raise ScheduleError(f"A prompt is at most {MAX_PROMPT} characters long.")
        if not prompt.strip() and not documents:
            raise ScheduleError("A scheduled task needs a prompt.")
        return prompt

    @staticmethod
    def _documents(given: list[dict], kept: tuple[dict, ...] = ()) -> list[dict]:
        """The documents to store: those given, where one without text keeps the text of the one of that name."""
        old = {doc["name"]: doc for doc in kept}
        found = []
        for item in given:
            doc = old.get(item["name"]) if item.get("text") is None else item
            if doc is None:
                raise ScheduleError(f"The document {item['name']} has no text.")
            found.append({"name": item["name"], "kind": doc.get("kind") or "", "text": doc["text"]})
        if sum(len(doc["text"]) for doc in found) > MAX_DOCUMENTS_CHARS:
            raise ScheduleError(f"The documents are too long: at most {MAX_DOCUMENTS_CHARS:,} characters in all.")
        return found

    def _timing(
        self, at: str | None, zone: str | None, repeat: str, days: list[int], old: Schedule | None
    ) -> tuple[datetime, str, str, tuple[int, ...]]:
        """(first moment, clock, repeat, weekdays) validated; the old ones stay where nothing is given."""
        try:
            start, clock = parse_moment(at, zone) if at else (old.start_at, old.timezone)  # type: ignore[union-attr]
        except ReminderError as error:
            raise ScheduleError(str(error)) from None
        repeat = (repeat or "").strip().lower()
        if repeat and repeat not in REPEATS:
            raise ScheduleError(f"repeat must be one of: {', '.join(REPEATS)} (or nothing).")
        if any(not isinstance(day, int) or not 0 <= day <= 6 for day in days):
            raise ScheduleError("A weekday is a number from 0 (Monday) to 6 (Sunday).")
        weekdays = tuple(sorted(set(days))) or (start.astimezone(_tzinfo(clock)).weekday(),)
        return start, clock, repeat, weekdays if repeat == "weekly" else ()

    def _first(self, start: datetime, clock: str, repeat: str, days: tuple[int, ...]) -> datetime:
        now = self._clock()
        if not repeat:
            if start <= now:
                raise ScheduleError("That moment is already past.")
            return start
        return next_run(start, clock, repeat, days, max(now, start - timedelta(seconds=1)))

    def create(
        self, person: Person, surface: str, user_id: str, name: str, prompt: str, documents: list[dict], at: str,
        zone: str | None = None, repeat: str = "", days: list[int] | None = None, project_id: int | None = None,
        conversation: str = "",
    ) -> Schedule:
        """Plan one. `conversation` is where its runs happen (default: a new one of the account)."""
        if self.store.count(person.id) >= MAX_PER_PERSON:
            raise ScheduleError(f"At most {MAX_PER_PERSON} scheduled tasks: delete some first.")
        docs = self._documents(documents)
        start, clock, repeat, weekdays = self._timing(at, zone, repeat, days or [], None)
        schedule_id = self.store.add(
            person_id=person.id, name=self._name(name), prompt=self._prompt(prompt, docs),
            documents=json.dumps(docs), surface=surface, user_id=user_id,
            conversation=conversation or f"{surface}:{user_id}:schedule-{secrets.token_hex(5)}",
            project_id=project_id, repeat=repeat, days=",".join(map(str, weekdays)), start_at=_stamp(start),
            timezone=clock, next_at=_stamp(self._first(start, clock, repeat, weekdays)), created_at=_stamp(self._clock()),
        )
        self._wake.set()
        return self.get(person, schedule_id)

    def get(self, person: Person, schedule_id: int) -> Schedule:
        found = self.store.get(person.id, schedule_id)
        if found is None:
            raise ScheduleError("No such scheduled task of yours.")
        return found

    def update(self, person: Person, schedule_id: int, **fields: Any) -> Schedule:
        """Change what is given (`name`, `prompt`, `documents`, `project_id`, `at`, `timezone`, `repeat`, `days`,
        `enabled`). A change of the time, or turning it on, works out the next run again."""
        old = self.get(person, schedule_id)
        columns: dict[str, Any] = {}
        if "name" in fields:
            columns["name"] = self._name(fields["name"])
        if "prompt" in fields or "documents" in fields:
            docs = old.documents if fields.get("documents") is None else self._documents(fields["documents"], old.documents)
            columns["documents"] = json.dumps(docs)
            columns["prompt"] = self._prompt(fields["prompt"] if "prompt" in fields else old.prompt, list(docs))
        if "project_id" in fields:
            columns["project_id"] = fields["project_id"]
        enabled = fields.get("enabled", old.enabled)
        timing = {"at", "timezone", "repeat", "days"} & fields.keys()
        if timing or enabled != old.enabled:
            start, clock, repeat, weekdays = self._timing(
                fields.get("at"), fields.get("timezone"), fields.get("repeat", old.repeat),
                fields["days"] if "days" in fields else list(old.days), old,
            )
            columns.update(
                start_at=_stamp(start), timezone=clock, repeat=repeat, days=",".join(map(str, weekdays)),
                enabled=int(enabled), next_at=_stamp(self._first(start, clock, repeat, weekdays)) if enabled else None,
            )
        if columns:
            self.store.set(schedule_id, **columns)
            self._wake.set()
        return self.get(person, schedule_id)

    def delete(self, person: Person, schedule_id: int) -> bool:
        deleted = self.store.delete(person.id, schedule_id)
        if deleted:
            self._wake.set()
        return deleted

    def describe(self, schedule: Schedule) -> dict[str, Any]:
        return {
            "id": schedule.id,
            "name": schedule.name,
            "prompt": schedule.prompt,
            "documents": [{"name": d["name"], "kind": d["kind"], "chars": len(d["text"])} for d in schedule.documents],
            "conversation": schedule.conversation,
            "project": schedule.project_id,
            "repeat": schedule.repeat,
            "days": list(schedule.days),
            "start_at": _stamp(schedule.start_at),
            "timezone": schedule.timezone,
            "next_at": _stamp(schedule.next_at),
            "enabled": schedule.enabled,
            "running": schedule.id in self._running,
            "runs": schedule.runs,
            "last_at": _stamp(schedule.last_at),
            "last_status": schedule.last_status,
            "last_summary": schedule.last_summary,
        }

    # -- running -------------------------------------------------------------------------------- #

    def run_now(self, person: Person, schedule_id: int) -> Schedule:
        """Run it now, besides its times."""
        schedule = self.get(person, schedule_id)
        if schedule.id in self._running:
            raise ScheduleError("It is running already.")
        if self.stopping:
            raise ScheduleError("The server is stopping.")
        self._start(schedule, None)
        return schedule

    def _start(self, schedule: Schedule, due: datetime | None) -> None:
        task = asyncio.create_task(self._execute(schedule, due))
        self._running[schedule.id] = task
        task.add_done_callback(lambda _: self._running.pop(schedule.id, None))

    def fire_due(self) -> int:
        """Start what is due; the next run is set before, so that nothing runs twice. Returns how many started."""
        now = self._clock()
        started = 0
        for schedule in self.store.due(now):
            assert schedule.next_at is not None
            following = next_run(schedule.start_at, schedule.timezone, schedule.repeat, schedule.days, now) if schedule.repeat else None
            self.store.set(schedule.id, next_at=_stamp(following))
            if (now - schedule.next_at).total_seconds() > LATE_SECONDS:
                self._finish(schedule, now, "missed", f"The server was not running at the time ({schedule.next_at:%Y-%m-%d %H:%M} UTC): this run was skipped.")
            elif schedule.id in self._running:
                self._finish(schedule, now, "missed", "The previous run was still going: this one was skipped.")
            else:
                self._start(schedule, schedule.next_at)
                started += 1
        return started

    async def wait(self) -> None:
        """Until the runs started are over."""
        await asyncio.gather(*self._running.values(), return_exceptions=True)

    async def _turn(self, schedule: Schedule) -> str:
        if self.agent is None:
            raise ScheduleError("Clara cannot answer now.")
        request = ChatRequest(
            surface=schedule.surface, user_id=schedule.user_id, user_name=None, message=message_of(schedule),
            conversation=schedule.conversation, instructions=INSTRUCTIONS, prefix=f"[Scheduled task “{schedule.name}”, run {schedule.runs + 1}]",
            timezone=_iana(schedule.timezone), quiet=True, project=schedule.project_id,
        )
        reply = ""
        async for event in self.agent.turn(request, OWNER):
            if event["type"] == "done":
                reply = event["reply"]
        self.memory.title_if_untitled(schedule.conversation, schedule.name)
        return reply

    async def _execute(self, schedule: Schedule, due: datetime | None) -> None:
        started = self._clock()
        try:
            status, summary = "ok", summary_of(await self._turn(schedule))
        except ServerStopping:
            if due is not None:
                self.store.set(schedule.id, next_at=_stamp(due))  # the next start runs it
            return
        except Exception as error:
            log.warning("schedule %s: the run failed (%s: %s)", schedule.id, type(error).__name__, error)
            status, summary = "failed", f"Clara could not run it: {error or type(error).__name__}"
        self._finish(schedule, started, status, summary)

    def _finish(self, schedule: Schedule, at: datetime, status: str, summary: str) -> None:
        self.store.record(schedule.id, at, status, summary)
        text = summary
        waiting = self.waiting(schedule.conversation)
        if waiting:
            text += f"\n{waiting} request{'s' if waiting > 1 else ''} wait for your permission."
        try:
            self.notifier.notify(
                schedule.person_id, text[:1990], f"Scheduled task: {schedule.name}"[:100], NOTIFY_TARGETS, SERVER,
                schedule.conversation, limited=False,
            )
        except NotificationError:
            log.exception("schedule %s: could not tell the person", schedule.id)

    def next_due(self) -> datetime | None:
        return self.store.next_due()
