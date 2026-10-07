"""Reminders: a text and a moment, announced to the person who set it when the moment comes.

    create()   a client (or the model, through a tool) sets one, for some of the person's surfaces
    run()      the scheduler: when one is due it becomes an *event*, and a repeating one is moved on
    events()   one listener's stream of events, oldest first (see notifications.py)

A reminder is shown only to the person who set it, on the surfaces it was set for (`targets`: `app`,
`discord`...), or on every client of theirs when it names none.

When one comes due, Clara writes the announcement herself (see announce.py) and that message is what the
clients show; if she cannot, they show the reminder's own text, and the person is told why by a
notification.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .dueloop import DueLoop
from .memory import Memory, Person, Reminder
from .notifications import SERVER, NotificationError, Notifier, clean_targets

log = logging.getLogger(__name__)

REPEATS = ("daily", "weekly", "monthly")
MAX_TEXT = 500
MAX_PER_PERSON = 100

# Writes the announcement of a reminder that came due: its text, or None to announce the reminder as it is.
# It raises AnnounceFailed when it should have written one and could not.
Composer = Callable[[Reminder], Awaitable["str | None"]]

_OFFSET = re.compile(r"^([+-])(\d\d):(\d\d)$")


class ReminderError(ValueError):
    """The reminder cannot be created as asked (the message says why)."""


class AnnounceFailed(Exception):
    """Clara could not write the announcement of a reminder (the message says why)."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _tzinfo(spec: str) -> tzinfo:
    """A clock from its name: an IANA zone ("Europe/Paris"), an offset ("+02:00"), or UTC for ""."""
    if not spec:
        return UTC
    match = _OFFSET.match(spec)
    if match:
        delta = timedelta(hours=int(match[2]), minutes=int(match[3]))
        return timezone(delta if match[1] == "+" else -delta)
    try:
        return ZoneInfo(spec)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ReminderError(f"Unknown timezone: {spec}") from None


def _offset_name(moment: datetime) -> str:
    seconds = int(moment.utcoffset().total_seconds())  # type: ignore[union-attr]
    sign = "-" if seconds < 0 else "+"
    minutes = abs(seconds) // 60
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def parse_moment(at: str, zone: str | None) -> tuple[datetime, str]:
    """`(UTC moment, the clock a repeat keeps)` from an ISO 8601 text such as `2026-10-05T09:00`
    or `2026-10-05T09:00+02:00`. Without an offset the time is read in `zone` (an IANA name), or
    in the server's own timezone. The clock is `zone` if given, else the offset of the time."""
    try:
        moment = datetime.fromisoformat(at.strip())
    except ValueError:
        raise ReminderError(
            f"Cannot read the time {at!r}: use ISO 8601, like 2026-10-05T09:00 or 2026-10-05T09:00+02:00."
        ) from None
    if zone:
        _tzinfo(zone)  # validates the name
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_tzinfo(zone)) if zone else moment.astimezone()
    return moment.astimezone(UTC), zone or _offset_name(moment)


def _shift(local: datetime, repeat: str, count: int) -> datetime:
    """`local` moved `count` periods on, keeping the wall-clock time (and the day of the month,
    clamped to the length of the month)."""
    if repeat == "daily":
        return local + timedelta(days=count)
    if repeat == "weekly":
        return local + timedelta(weeks=count)
    years, month = divmod(local.month - 1 + count, 12)
    year, month = local.year + years, month + 1
    return local.replace(year=year, month=month, day=min(local.day, calendar.monthrange(year, month)[1]))


def next_occurrence(reminder: Reminder, after: datetime) -> datetime:
    """The first moment of a repeating reminder strictly after `after`. Occurrences are counted from
    the first one, so "monthly on the 31st" is the 28th in February and the 31st again in March."""
    zone = _tzinfo(reminder.timezone)
    anchor = reminder.anchor_at.astimezone(zone)
    count = 0
    while True:
        count += 1
        candidate = _shift(anchor, reminder.repeat, count).astimezone(UTC)
        if candidate > after:
            return candidate


def describe(reminder: Reminder) -> dict[str, Any]:
    return {
        "id": reminder.id,
        "text": reminder.text,
        "due_at": reminder.due_at.isoformat(timespec="seconds"),
        "repeat": reminder.repeat,
        "targets": list(reminder.targets),  # empty: every surface of the person
    }


class ReminderService(DueLoop):
    def __init__(
        self, memory: Memory, clock: Callable[[], datetime] = _utc_now, notifier: Notifier | None = None
    ):
        self.memory = memory
        self._clock = clock
        self.notifier = notifier or Notifier(memory, clock)  # delivers what fires
        self._wake = asyncio.Event()  # something changed: the scheduler looks again
        self.composer: Composer | None = None  # who writes the announcements (None: the text is announced)
        self.stopping = False  # the server is stopping: what comes due waits for the next start
        self.firing = False  # announcements are being written

    @property
    def server_state(self) -> str:
        return self.notifier.server_state

    # -- setting and cancelling ------------------------------------------------------------- #

    def create(
        self,
        person: Person,
        text: str,
        at: str,
        repeat: str = "",
        zone: str | None = None,
        origin: tuple[str, str, str] = ("", "", ""),
        targets: Iterable[str] | str | None = (),
    ) -> Reminder:
        """Set a reminder. `origin` is (surface, user_id, conversation) of where it was asked: Clara writes
        the announcement there. `targets`: the surfaces of the person it is shown on (empty: all of them).
        Raises :class:`ReminderError` when it is invalid or in the past."""
        try:
            surfaces = clean_targets(targets)
        except NotificationError as error:
            raise ReminderError(str(error)) from None
        text = " ".join((text or "").split())
        if not text:
            raise ReminderError("A reminder needs a text.")
        if len(text) > MAX_TEXT:
            raise ReminderError(f"A reminder is at most {MAX_TEXT} characters long.")
        repeat = (repeat or "").strip().lower()
        if repeat and repeat not in REPEATS:
            raise ReminderError(f"repeat must be one of: {', '.join(REPEATS)} (or nothing).")
        due, clock = parse_moment(at, zone)
        if due <= self._clock():
            raise ReminderError("That moment is already past.")
        if self.memory.reminder_count(person.id) >= MAX_PER_PERSON:
            raise ReminderError(f"At most {MAX_PER_PERSON} reminders at a time: cancel some first.")
        reminder = self.memory.add_reminder(person.id, text, due, repeat, clock, origin, surfaces)
        self._wake.set()
        return reminder

    def upcoming(self, person: Person) -> list[Reminder]:
        return self.memory.reminders_of(person.id)

    def cancel(self, person: Person, reminder_id: int) -> bool:
        cancelled = self.memory.delete_reminder(person.id, reminder_id)
        if cancelled:
            self._wake.set()
        return cancelled

    # -- the scheduler ------------------------------------------------------------------------ #

    async def fire_due(self) -> int:
        """Announce every reminder that is due: Clara writes each announcement (all at once), then it is
        stored and sent. A repeating reminder fires once however many occurrences it missed (the server
        was down), then waits for its next one. Returns how many fired."""
        due = self.memory.due_reminders(self._clock())
        if not due:
            return 0
        self.firing = True
        try:
            written = await asyncio.gather(*(self._compose(reminder) for reminder in due))
            now = self._clock()  # after the writing: that is when the clients get it
            fired = 0
            for reminder, (message, failure) in zip(due, written):
                if not self.memory.reminder_exists(reminder.id):  # cancelled while Clara was writing
                    continue
                following = next_occurrence(reminder, now) if reminder.repeat else None
                self.memory.fire_reminder(reminder, following, now, message)
                fired += 1
                if failure:
                    self._tell_failure(reminder, failure)
            if fired:
                self.notifier.stored()
            return fired
        finally:
            self.firing = False

    async def _compose(self, reminder: Reminder) -> tuple[str | None, str]:
        """(Clara's announcement or None, why she could not write it or "")."""
        if self.composer is None or self.stopping:
            return None, ""
        try:
            return await self.composer(reminder), ""
        except AnnounceFailed as error:
            return None, str(error)
        except Exception:
            log.exception("reminders: could not write the announcement of reminder %s", reminder.id)
            return None, "an unexpected error"

    def _tell_failure(self, reminder: Reminder, reason: str) -> None:
        """The person gets the reminder's own text: say why, on the same surfaces."""
        try:
            self.notifier.notify(
                reminder.person_id,
                f"Clara could not write the announcement of your reminder ({reason}), so it was shown "
                f"as you typed it: {reminder.text}",
                "Reminder",
                reminder.targets,
                SERVER,
                reminder.conversation,
                limited=False,
            )
        except NotificationError:
            log.exception("reminders: could not tell why reminder %s was announced as typed", reminder.id)

    def announce_server(self, state: str) -> None:
        """Tell every connected client what the server is doing ("stopping", "stopped")."""
        self.notifier.announce_server(state)

    def next_due(self) -> datetime | None:
        return self.memory.next_reminder_due()

    # -- one listener's stream ----------------------------------------------------------------- #

    def events(
        self, client: str, surface: str | None = None, user_id: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """See :meth:`Notifier.events`: reminders and notifications go through the same stream."""
        return self.notifier.events(client, surface, user_id)
