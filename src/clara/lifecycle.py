"""Stopping the server without cutting anybody off.

    running ──request_stop()──► stopping ──everything finished (or forced)──► stopped

While *stopping* the server tells every connected client (the `server` events of the reminder stream),
refuses new questions (HTTP 503), and waits for what is running: replies being written, agents waiting for
their client's tools, summaries, announcements of reminders. Nothing is waited for beyond that; a second
request, `/stop now`, a second Ctrl+C, forces the stop. Then clients are told it is *stopped*, and the
process exits.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from .agent import Agent
from .reminders import ReminderService
from .schedule import ScheduleService
from .tasks import TaskService

log = logging.getLogger(__name__)

POLL_SECONDS = 0.1
REPORT_SECONDS = 30.0  # the log says what is awaited this often
FLUSH_SECONDS = 0.5  # clients get "stopped" before the connections are closed


class Lifecycle:
    def __init__(
        self,
        agent: Agent,
        reminders: ReminderService,
        poll: float = POLL_SECONDS,
        flush: float = FLUSH_SECONDS,
        tasks: TaskService | None = None,
        schedules: ScheduleService | None = None,
    ):
        self.agent = agent
        self.reminders = reminders
        self.tasks = tasks  # its reminders are written by Clara too: waited for like the others'
        self.schedules = schedules  # the runs that are going on are waited for too

        self.on_exit: Callable[[bool], None] | None = None  # called once, with "was it forced"
        self.loop: asyncio.AbstractEventLoop | None = None  # set when the server starts: signals come from outside it
        self._poll, self._flush = poll, flush
        self._force = False
        self._task: asyncio.Task | None = None
        self.exited = False

    @property
    def stopping(self) -> bool:
        return self._task is not None

    @property
    def state(self) -> str:
        return "stopping" if self.stopping else "running"

    @property
    def waiting_for(self) -> int:
        """How many replies / agent turns are still running."""
        return self.agent.stats.active

    def request_stop(self, force: bool = False) -> str:
        """Start stopping, or (with `force`) stop now. Returns what to tell the operator. Call it on the
        server's event loop."""
        if self._task is None:
            self.agent.accepting = False
            self.reminders.stopping = True
            if self.tasks is not None:
                self.tasks.stopping = True
            if self.schedules is not None:
                self.schedules.stopping = True
            self.reminders.announce_server("stopping")
            self._force = force
            self._task = asyncio.ensure_future(self._drain())
            if force:
                return "Stopping now."
            running = self.waiting_for
            if not self._busy():
                return "Stopping: nothing is running."
            return (
                f"Stopping: clients are told, new questions are refused, waiting for {running} running "
                f"turn(s) to end. /stop now forces it."
            )
        if force and not self._force:
            self._force = True
            return "Stopping now, whatever is running."
        if self._force:
            return "Already stopping now."
        return f"Already stopping: waiting for {self.waiting_for} running turn(s). /stop now forces it."

    def request_stop_threadsafe(self, force: bool = False) -> None:
        """For signal handlers, which do not run on the loop."""
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.request_stop, force)

    def _busy(self) -> bool:
        return (
            self.agent.busy
            or self.reminders.firing
            or (self.tasks is not None and self.tasks.firing)
            or (self.schedules is not None and self.schedules.firing)
        )

    async def _drain(self) -> None:
        started = last_report = time.monotonic()
        while self._busy() and not self._force:
            await asyncio.sleep(self._poll)
            if time.monotonic() - last_report >= REPORT_SECONDS:
                last_report = time.monotonic()
                log.info(
                    "stopping: still waiting for %d running turn(s), %d s so far (Ctrl+C or /stop now forces)",
                    self.waiting_for,
                    last_report - started,
                )
        forced = self._force and self._busy()
        if forced:
            log.warning("stopping now: %d running turn(s) are cut", self.waiting_for)
        self.reminders.announce_server("stopped")
        await asyncio.sleep(self._flush)
        self.exited = True
        log.info("stopped")
        if self.on_exit is not None:
            self.on_exit(forced)
