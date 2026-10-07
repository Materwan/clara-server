"""The loop of the services that act at set times: reminders (reminders.py), the reminders of tasks (tasks.py) and
the scheduled prompts (schedule.py). Each one says what is due and when the next thing is; the loop sleeps until
then, or until something changes (`_wake`)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

MAX_SLEEP = 30.0  # seconds: the scheduler looks again at least this often (clock changes, safety net)


class DueLoop:
    """A service that fires what comes due. A subclass sets `_wake`, `stopping` and `_clock`, and gives
    `fire_due()` (it may be a coroutine) and `next_due()`."""

    _wake: asyncio.Event  # something changed: the loop looks again at once
    stopping: bool  # the server is stopping: what comes due waits for the next start
    _clock: Callable[[], datetime]

    def fire_due(self) -> int | Awaitable[int]:
        raise NotImplementedError

    def next_due(self) -> datetime | None:
        raise NotImplementedError

    async def run(self) -> None:
        """The scheduler loop; runs for the life of the server."""
        log = logging.getLogger(type(self).__module__)
        while True:
            self._wake.clear()  # before looking: a change made meanwhile wakes the next wait at once
            try:
                if not self.stopping:
                    fired = self.fire_due()
                    if inspect.isawaitable(fired):
                        await fired
            except Exception:
                log.exception("%s: could not fire the due ones", type(self).__name__)
            upcoming = self.next_due()
            delay = MAX_SLEEP if upcoming is None or self.stopping else (upcoming - self._clock()).total_seconds()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), max(0.0, min(delay, MAX_SLEEP)))
