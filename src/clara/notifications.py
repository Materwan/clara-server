"""Notifications: what the server pushes to a person's clients, the moment it happens.

Anything can send one to a person:

    Clara       her `notify` tool, e.g. when a long task is done
    a client    POST /v1/notifications (a script, the console after a long job...)
    the server  a long turn finished, a conversation was summarised, a reminder could not be written,
                the provider or the model changed (that one is for everybody)

Reminders that come due (reminders.py) travel the same way. Every event is for **one person** (or for
everybody: a change of the whole server) and for some of their *surfaces* (`app`, `cli`, `console`,
`discord`...), or for all of them. A client listens *as an account* (`surface` + `user_id`): it gets
the events of the person behind that account that are meant for its surface. Accounts linked to the
same person (see linking.py) all get them.

The same stream also tells every client what the server is doing (`server` events: running, stopping,
stopped), so that they can say "Clara is not running" instead of just failing.

Events are stored for a week, and each listener (a token + an account) has a cursor: how far it has
read. A listener that connects after something fired, because it was off or offline at that moment, is
sent what it missed; a listener the server has never seen starts from now. Two connections of the same
listener both get what fires while they are open, and share the cursor.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable, Iterable

from .memory import Memory, ReminderEvent

SURFACE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
MAX_TARGETS = 10
MAX_TEXT = 2000
MAX_TITLE = 100
RATE_LIMIT = 10  # notifications a person may be sent per minute (the server's own are not counted)
RATE_WINDOW = 60.0  # seconds
EVENT_RETENTION = timedelta(days=7)  # a client offline longer than this misses the event
BATCH = 100  # events read from the database at a time

SERVER_MESSAGES = {
    "running": "Clara is running",
    "stopping": "Clara is stopping: she finishes what is running and takes nothing new",
    "stopped": "Clara is not running",
}

# Who sent a notification, when it was not a client (a client is named by its token)
CLARA = "clara"
SERVER = "server"


class NotificationError(ValueError):
    """The notification cannot be sent as asked (the message says why)."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def clean_targets(targets: Iterable[str] | str | None) -> tuple[str, ...]:
    """Surface names, checked and without duplicates; empty: every surface. A text may list them with
    commas or spaces ("app, discord")."""
    if targets is None:
        return ()
    if isinstance(targets, str):
        targets = re.split(r"[\s,|]+", targets)
    names: list[str] = []
    for target in targets:
        name = str(target).strip().lower()
        if not name:
            continue
        if not SURFACE_RE.match(name):
            raise NotificationError(f"Not a surface name: {target!r} (a-z, 0-9, _ and -, like app or discord).")
        if name not in names:
            names.append(name)
    if len(names) > MAX_TARGETS:
        raise NotificationError(f"At most {MAX_TARGETS} surfaces.")
    return tuple(names)


def for_surface(event: ReminderEvent, surface: str | None) -> bool:
    """Is the event meant for a client of that surface (None: a client that did not say who it is)?"""
    if not event.targets:
        return True
    return surface is not None and surface in event.targets


def event_payload(event: ReminderEvent) -> dict[str, Any]:
    if event.kind in ("approval", "approval_resolved", "job"):
        # for a client to act on, not to show: an approval to answer, the end of one, a job for the app
        return {
            "type": event.kind,
            "id": event.id,
            "text": event.text,
            "title": event.title,
            "sent_at": event.fired_at,
            "targets": list(event.targets),
            "conversation": event.conversation or None,
            **(event.payload or {}),
        }
    if event.kind == "notification":
        return {
            "type": "notification",
            "id": event.id,
            "title": event.title,
            "text": event.text,
            "sent_at": event.fired_at,
            "source": event.source,  # "clara", "server" or the name of the client that sent it
            "targets": list(event.targets),
            "conversation": event.conversation or None,  # what it is about, when it is about one
            "everyone": event.person_id is None,
        }
    return {
        "type": "reminder",
        "id": event.id,
        "text": event.text,
        "due_at": event.due_at,
        "fired_at": event.fired_at,
        "from": event.author,
        "message": event.message,  # what Clara wrote; None: show `text`
        "targets": list(event.targets),
    }


class Notifier:
    def __init__(self, memory: Memory, clock: Callable[[], datetime] = _utc_now):
        self.memory = memory
        self._clock = clock
        self._listeners: set[asyncio.Event] = set()  # one per open client stream
        self._sent: dict[int | None, deque[float]] = {}  # recent notifications per person, for the rate limit
        self._present: dict[tuple[int, str], int] = {}  # open streams of (person, surface): who is listening now
        self.server_state = "running"

    def connected(self, person_id: int, surface: str) -> bool:
        """Is a client of that surface listening for this person right now (the desktop app is running)?"""
        return self._present.get((person_id, surface), 0) > 0

    def wake(self) -> None:
        """Events were stored: every open stream looks for them."""
        for listener in self._listeners:
            listener.set()

    def stored(self) -> None:
        """Events were stored by someone else (the reminders): forget the old ones, wake the streams."""
        self.memory.prune_reminder_events(self._clock() - EVENT_RETENTION)
        self.wake()

    # -- sending -------------------------------------------------------------------------------- #

    def notify(
        self,
        person_id: int | None,
        text: str,
        title: str = "",
        targets: Iterable[str] | str | None = (),
        source: str = "",
        conversation: str = "",
        limited: bool = True,
        kind: str = "notification",
        payload: dict | None = None,
    ) -> ReminderEvent:
        """Send a notification to one person (None: everybody) on some of their surfaces (empty: all).
        `limited`: counts towards the rate limit (the server's own notifications do not). `kind` and `payload`: an
        event a client acts on (an approval to answer) rather than shows. Raises :class:`NotificationError` when it
        is invalid or the person was sent too many."""
        text = (text or "").strip()
        title = " ".join((title or "").split())
        if not text:
            raise NotificationError("A notification needs a text.")
        if len(text) > MAX_TEXT:
            raise NotificationError(f"A notification is at most {MAX_TEXT} characters long.")
        if len(title) > MAX_TITLE:
            raise NotificationError(f"A title is at most {MAX_TITLE} characters long.")
        surfaces = clean_targets(targets)
        if limited:
            self._count(person_id)
        event = self.memory.add_notification(
            person_id, text, self._clock(), title, surfaces, source, conversation, kind, payload
        )
        self.stored()
        return event

    def broadcast(self, text: str, title: str = "", source: str = SERVER) -> ReminderEvent:
        """A notification for everybody (a change of the whole server)."""
        return self.notify(None, text, title, (), source, limited=False)

    def _count(self, person_id: int | None) -> None:
        now = time.monotonic()
        recent = self._sent.setdefault(person_id, deque())
        while recent and now - recent[0] > RATE_WINDOW:
            recent.popleft()
        if len(recent) >= RATE_LIMIT:
            raise NotificationError(f"Too many notifications: at most {RATE_LIMIT} a minute.")
        recent.append(now)

    def announce_server(self, state: str) -> None:
        """Tell every connected client what the server is doing ("stopping", "stopped")."""
        self.server_state = state
        self.wake()

    # -- one listener's stream ------------------------------------------------------------------ #

    @staticmethod
    def listener(client: str, surface: str | None = None, user_id: str | None = None) -> str:
        """The name of a listener's cursor: the token, plus the account it listens as."""
        return f"{client}/{surface}:{user_id}" if surface and user_id else client

    async def events(
        self, client: str, surface: str | None = None, user_id: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """What fires from now on for the account `surface:user_id`, preceded by what fired since this
        listener last connected, and the state of the server whenever it changes (first of all when the
        client connects). Without an account, only what is for everybody."""
        wake = asyncio.Event()
        self._listeners.add(wake)
        told = ""
        name = self.listener(client, surface, user_id)
        here = self.memory.find_person(surface, user_id) if surface and user_id else None
        mark = (here.id, surface) if here is not None and surface else None
        if mark is not None:
            self._present[mark] = self._present.get(mark, 0) + 1
        try:
            cursor = self.memory.reminder_cursor(name)
            if cursor is None:  # first time: no backlog
                cursor = self.memory.last_reminder_event()
                self.memory.set_reminder_cursor(name, cursor)
            while True:
                wake.clear()  # before reading: an event stored meanwhile is not lost
                # Looked up each time: the account may have been linked to someone else meanwhile
                person = self.memory.find_person(surface, user_id) if surface and user_id else None
                batch = self.memory.reminder_events_after(cursor, BATCH, person.id if person else None)
                for event in batch:
                    if for_surface(event, surface):
                        yield event_payload(event)
                    cursor = event.id
                    self.memory.set_reminder_cursor(name, cursor)
                if len(batch) == BATCH:
                    continue
                state = self.server_state
                if state != told:
                    told = state
                    yield {"type": "server", "state": state, "message": SERVER_MESSAGES[state]}
                if state == "stopped":
                    return  # everything was delivered: the connection can close
                await wake.wait()
        finally:
            self._listeners.discard(wake)
            if mark is not None:
                self._present[mark] -= 1
                if self._present[mark] <= 0:
                    del self._present[mark]

    async def surface_events(
        self, client: str, surface: str, recipients: Callable[[int], list[str]]
    ) -> AsyncIterator[dict[str, Any]]:
        """For a client that speaks for every account of a surface (the Discord bot): the reminders and
        notifications of every person meant for that surface, each with `accounts`, the ids on that surface
        to deliver it to (`recipients(person_id)`; an event nobody there can receive is skipped). What is for
        everybody (a change of model) is left out: it would be sent to each one. Plus the state of the server."""
        wake = asyncio.Event()
        self._listeners.add(wake)
        told = ""
        name = f"{client}/{surface}:*"
        try:
            cursor = self.memory.reminder_cursor(name)
            if cursor is None:  # first time: no backlog
                cursor = self.memory.last_reminder_event()
                self.memory.set_reminder_cursor(name, cursor)
            while True:
                wake.clear()
                batch = self.memory.reminder_events_after(cursor, BATCH)
                for event in batch:
                    if event.person_id is not None and for_surface(event, surface):
                        accounts = recipients(event.person_id)
                        if accounts:
                            yield {**event_payload(event), "accounts": accounts}
                    cursor = event.id
                    self.memory.set_reminder_cursor(name, cursor)
                if len(batch) == BATCH:
                    continue
                state = self.server_state
                if state != told:
                    told = state
                    yield {"type": "server", "state": state, "message": SERVER_MESSAGES[state]}
                if state == "stopped":
                    return
                await wake.wait()
        finally:
            self._listeners.discard(wake)
