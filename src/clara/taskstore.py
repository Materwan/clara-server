"""The to-do list of each person, in the memory's SQLite file (tables `tasks` and `task_reminders`, see memory.py).

Only storage lives here: what a task is, which reminders are queued for it, how many were sent. The rules
(what is a valid task, when the next reminder is, who writes what) are in tasks.py.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .memory import Memory, join_targets, split_targets

OPEN = "open"
DONE = "done"
STATUSES = (OPEN, DONE)


@dataclass(frozen=True)
class Task:
    id: int
    person_id: int
    title: str
    description: str
    due_at: datetime | None  # the deadline (UTC), if there is one
    status: str  # "open" or "done"
    reminders_sent: int  # how many reminders were announced so far
    timezone: str  # the person's clock when it was set: an IANA name or "+02:00"
    created_at: datetime
    updated_at: datetime
    done_at: datetime | None
    surface: str = ""  # where it was set: its follow-ups are written by the model of that surface
    user_id: str = ""
    conversation: str = ""
    targets: tuple[str, ...] = ()  # surfaces the reminders are shown on; empty: every surface of the person
    parent_id: int | None = None  # the task it is a sub task of (None: a main task)
    next: tuple[datetime, ...] = ()  # the reminders still to come, soonest first (UTC)

    @property
    def next_reminder(self) -> datetime | None:
        return self.next[0] if self.next else None


def _stamp(moment: datetime) -> str:
    """ISO text in UTC: these strings sort in time order."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _moment(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


class TaskStore:
    def __init__(self, memory: Memory):
        self._memory = memory

    @property
    def _db(self) -> sqlite3.Connection:
        return self._memory.database

    @property
    def _lock(self):
        return self._memory.lock

    # -- reading ------------------------------------------------------------------------------ #

    def _tasks(self, rows: Iterable[sqlite3.Row]) -> list[Task]:
        rows = list(rows)
        queued: dict[int, list[datetime]] = {}
        if rows:
            marks = ",".join("?" * len(rows))
            for reminder in self._db.execute(
                f"SELECT task_id, at FROM task_reminders WHERE task_id IN ({marks}) ORDER BY at, id",
                [row["id"] for row in rows],
            ):
                queued.setdefault(reminder["task_id"], []).append(datetime.fromisoformat(reminder["at"]))
        return [
            Task(
                row["id"], row["person_id"], row["title"], row["description"], _moment(row["due_at"]), row["status"],
                row["reminders_sent"], row["timezone"], datetime.fromisoformat(row["created_at"]),
                datetime.fromisoformat(row["updated_at"]), _moment(row["done_at"]), row["surface"], row["user_id"],
                row["conversation"], split_targets(row["targets"]), row["parent_id"], tuple(queued.get(row["id"], ())),
            )
            for row in rows
        ]

    def get(self, person_id: int, task_id: int) -> Task | None:
        """One of the person's tasks (never someone else's)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM tasks WHERE id = ? AND person_id = ?", (task_id, person_id)
            ).fetchall()
            found = self._tasks(rows)
        return found[0] if found else None

    def get_any(self, task_id: int) -> Task | None:
        """A task whoever it belongs to (for the scheduler)."""
        with self._lock:
            found = self._tasks(self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchall())
        return found[0] if found else None

    def of(self, person_id: int, status: str | None = OPEN) -> list[Task]:
        """The person's tasks (`status`: only those; None: all). Those with the nearest reminder first, then
        those with a deadline, then the others, the newest last."""
        with self._lock:
            if status is None:
                rows = self._db.execute("SELECT * FROM tasks WHERE person_id = ?", (person_id,)).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM tasks WHERE person_id = ? AND status = ?", (person_id, status)
                ).fetchall()
            found = self._tasks(rows)
        far = datetime.max.replace(tzinfo=timezone.utc)
        return sorted(
            found,
            key=lambda t: (t.status != OPEN, t.next_reminder or t.due_at or far, t.due_at or far, t.id),
        )

    def children(self, task_id: int) -> list[Task]:
        """The sub tasks of a task, the oldest first."""
        with self._lock:
            return self._tasks(self._db.execute("SELECT * FROM tasks WHERE parent_id = ? ORDER BY id", (task_id,)).fetchall())

    def descendants(self, task_id: int) -> list[Task]:
        """Its sub tasks, their sub tasks, and so on (the oldest first)."""
        with self._lock:
            rows = self._db.execute(
                "WITH RECURSIVE sub(id) AS (SELECT id FROM tasks WHERE parent_id = ?"
                " UNION SELECT t.id FROM tasks t JOIN sub ON t.parent_id = sub.id)"
                " SELECT * FROM tasks WHERE id IN (SELECT id FROM sub) ORDER BY id",
                (task_id,),
            ).fetchall()
            return self._tasks(rows)

    def ancestors(self, task: Task) -> list[Task]:
        """The task it is a sub task of, then that one's, up to the main task."""
        found: list[Task] = []
        seen = {task.id}
        parent = task.parent_id
        while parent is not None and parent not in seen:
            up = self.get_any(parent)
            if up is None:
                break
            found.append(up)
            seen.add(up.id)
            parent = up.parent_id
        return found

    def count_children(self, task_id: int) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM tasks WHERE parent_id = ?", (task_id,)).fetchone()[0]

    def count_open(self, person_id: int) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM tasks WHERE person_id = ? AND status = ?", (person_id, OPEN)
            ).fetchone()[0]

    def count(self, person_id: int) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM tasks WHERE person_id = ?", (person_id,)).fetchone()[0]

    # -- writing ------------------------------------------------------------------------------ #

    def add(
        self,
        person_id: int,
        title: str,
        description: str,
        due_at: datetime | None,
        zone: str,
        origin: tuple[str, str, str],
        targets: tuple[str, ...],
        now: datetime,
        reminders: Iterable[datetime] = (),
        parent_id: int | None = None,
    ) -> Task:
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO tasks (person_id, title, description, due_at, timezone, surface, user_id, conversation,"
                " targets, created_at, updated_at, parent_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    person_id, title, description, _stamp(due_at) if due_at else None, zone, *origin,
                    join_targets(targets), _stamp(now), _stamp(now), parent_id,
                ),
            )
            task_id = cursor.lastrowid
            self._queue(task_id, reminders)
        found = self.get_any(task_id)
        assert found is not None
        return found

    def _queue(self, task_id: int, reminders: Iterable[datetime]) -> None:
        self._db.execute("DELETE FROM task_reminders WHERE task_id = ?", (task_id,))
        self._db.executemany(
            "INSERT INTO task_reminders (task_id, at) VALUES (?, ?)",
            [(task_id, _stamp(at)) for at in sorted(set(reminders))],
        )

    def set_reminders(self, task_id: int, reminders: Iterable[datetime], now: datetime) -> None:
        """Replace the reminders still to come."""
        with self._lock, self._db:
            self._queue(task_id, reminders)
            self._db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (_stamp(now), task_id))

    def update(self, task_id: int, now: datetime, **fields: object) -> None:
        """Change `title`, `description`, `due_at` (a datetime or None), `targets`, `timezone`."""
        columns, values = [], []
        for name, value in fields.items():
            if name == "due_at":
                value = _stamp(value) if value else None  # type: ignore[arg-type]
            elif name == "targets":
                value = join_targets(value)  # type: ignore[arg-type]
            elif name not in ("title", "description", "timezone"):
                raise ValueError(f"Unknown field: {name}")
            columns.append(f"{name} = ?")
            values.append(value)
        with self._lock, self._db:
            self._db.execute(
                f"UPDATE tasks SET {', '.join(columns)}, updated_at = ? WHERE id = ?", (*values, _stamp(now), task_id)
            )

    def set_status(self, task_id: int, status: str, now: datetime) -> None:
        """Mark a task done (its reminders are dropped) or open again."""
        with self._lock, self._db:
            if status == DONE:
                self._db.execute("DELETE FROM task_reminders WHERE task_id = ?", (task_id,))
            self._db.execute(
                "UPDATE tasks SET status = ?, done_at = ?, updated_at = ? WHERE id = ?",
                (status, _stamp(now) if status == DONE else None, _stamp(now), task_id),
            )

    def delete(self, person_id: int, task_id: int) -> bool:
        with self._lock, self._db:
            return self._db.execute(
                "DELETE FROM tasks WHERE id = ? AND person_id = ?", (task_id, person_id)
            ).rowcount > 0

    # -- the scheduler ------------------------------------------------------------------------ #

    def next_due(self) -> datetime | None:
        """The moment of the nearest queued reminder of an open task."""
        with self._lock:
            row = self._db.execute(
                "SELECT MIN(r.at) FROM task_reminders r JOIN tasks t ON t.id = r.task_id WHERE t.status = ?", (OPEN,)
            ).fetchone()
        return datetime.fromisoformat(row[0]) if row[0] else None

    def due(self, now: datetime) -> list[Task]:
        """The open tasks that have a reminder due (each once, the one that has waited longest first)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT t.* FROM tasks t JOIN task_reminders r ON r.task_id = t.id"
                " WHERE t.status = ? AND r.at <= ? GROUP BY t.id ORDER BY MIN(r.at), t.id",
                (OPEN, _stamp(now)),
            ).fetchall()
            return self._tasks(rows)

    def fired(self, task_id: int, now: datetime, remaining: Iterable[datetime]) -> None:
        """A reminder of the task was announced: count it and keep only the reminders `remaining`."""
        with self._lock, self._db:
            self._queue(task_id, remaining)
            self._db.execute(
                "UPDATE tasks SET reminders_sent = reminders_sent + 1, updated_at = ? WHERE id = ?",
                (_stamp(now), task_id),
            )
