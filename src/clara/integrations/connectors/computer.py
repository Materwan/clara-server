"""Folders on the person's own computer, worked on by the Clara desktop app.

The server cannot reach a computer, so an operation becomes a *job*: stored, announced to the app (a `job` event on the
notification stream, which the app answers by fetching its jobs), done by the app, and its result handed back. What the
app does is limited by what it was told: it only touches the folders its owner added there, whatever a job says, so even
a server that was taken over cannot read the rest of the disk.

If the app is not running, a read is refused at once ("offline": Clara carries on without it) and a change is queued:
it is done when the app comes back.
"""

from __future__ import annotations

import asyncio
import time

from ...notifications import SERVER, Notifier
from ..permissions import COMPUTER, DESTRUCTIVE, WRITE
from ..store import IntegrationStore
from .base import Connector, ConnectorError, Target, base_level, check_mode, check_text

SURFACE = "app"
READ_WAIT = 30.0  # seconds a read waits for the app
WRITE_WAIT = 45.0  # seconds a change waits for the app before it is told "queued"
POLL = 0.25
CHANGES = ("write", "delete", "move")


class ComputerFolders(Connector):
    type = COMPUTER
    ops = frozenset({"list", "read", "search", "write", "delete", "move"})

    def __init__(self, store: IntegrationStore, notifier: Notifier, read_wait: float = READ_WAIT, write_wait: float = WRITE_WAIT):
        self.store = store
        self.notifier = notifier
        self.read_wait = read_wait
        self.write_wait = write_wait

    async def level(self, op: str, target: Target, args: dict) -> str:
        """The server cannot see the disk, so it goes by what was asked: replacing is destructive (even when the file
        turns out not to exist), creating and appending are writes (the app refuses to create over a file)."""
        if op != "write":
            return base_level(op)
        mode = check_mode(args.get("mode"))
        check_text(args.get("content"))
        return DESTRUCTIVE if mode == "overwrite" else WRITE

    async def run(self, op: str, target: Target, args: dict) -> str:
        if op not in self.ops:
            raise ConnectorError(f"{target.label} does not support {op}.")
        device = str(target.locator.get("device", ""))
        alias = str(target.locator.get("alias", ""))
        if not device or not alias:
            raise ConnectorError(f"{target.label}: not a folder of a computer.")
        job = self.store.add_job(target.person_id, device, op, {"alias": alias, "args": args})
        online = self.notifier.connected(target.person_id, SURFACE)
        self.notifier.notify(  # wakes the app (it fetches its jobs); kept for a week, so an app that is off hears it later
            target.person_id, f"{op} on {target.label}", "", (SURFACE,), SERVER, "", limited=False, kind="job",
            payload={"job": job.id, "device": device},
        )
        if not online and op not in CHANGES:
            self.store.set_job(job.id, "failed", "offline")
            raise ConnectorError(
                f"{target.label} is on a computer where the Clara app is not running, so it cannot be reached now. "
                "Carry on without it, and tell the person if you need it."
            )
        if not online:
            return f"Queued: the Clara app is not running on the computer of {target.label}; it will be done when it is back."
        waited = self.read_wait if op not in CHANGES else self.write_wait
        end = time.monotonic() + waited
        while time.monotonic() < end:
            found = self.store.job(job.id)
            if found is not None and found.status in ("done", "failed"):
                if found.status == "failed":
                    raise ConnectorError(found.result or "The computer could not do it.")
                return found.result
            await asyncio.sleep(POLL)
        if op in CHANGES:
            return f"Sent to the computer of {target.label}: no answer yet, it will be done when the app picks it up."
        self.store.set_job(job.id, "failed", "no answer")
        raise ConnectorError(f"The computer of {target.label} did not answer in {waited:g} seconds.")
