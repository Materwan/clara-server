"""What happens to a request for permission after the model has gone on with something else.

    asked          the request waits in its conversation (the client shows it there)
    60 s later     nobody answered: it is pushed, as an `approval` event, to the person's other surfaces
                   (the app, Discord, the web site) where they can answer it
    answered       approve: the server runs the action itself, as it was frozen; deny: nothing is done.
                   Either way the person's other surfaces are told it is settled, and a short follow-up turn
                   tells the model how it ended so it can go on
    24 h later     still nobody: it expires; the model is told at its next turn

Only an authenticated person can answer (`decide`, called from the API): there is no tool for the model to approve
its own request.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from ..memory import Memory
from ..notifications import SERVER, NotificationError, Notifier
from .broker import Broker, outcome_line
from .permissions import ALLOW
from .store import APPROVED, DENIED, DONE, EXPIRED, FAILED, PENDING, Approval, IntegrationStore, StoreError

log = logging.getLogger(__name__)

SWEEP_SECONDS = 10.0
FOLLOWUP_DELAY = 3.0  # answers given within this long make one follow-up turn, not several
PUSH_SURFACES = ("app", "discord", "web")  # where a request can be answered, besides its own conversation
REMEMBER = ("conversation", "resource")
KEEP_DAYS = 30  # how long a request that ended is kept (the log of what was done is kept longer)

FollowUp = Callable[[Approval, int | None, str], Awaitable[None]]  # (a request, its project, the message)


class AlreadyAnswered(StoreError):
    """The request is not waiting any more: someone answered first, or it expired."""


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


class Approvals:
    def __init__(
        self, broker: Broker, store: IntegrationStore, memory: Memory, notifier: Notifier, notify_after: int = 60,
        expire_after: int = 86_400, clock: Callable[[], datetime] | None = None,
    ):
        self.broker = broker
        self.store = store
        self.memory = memory
        self.notifier = notifier
        self.default_notify_after = notify_after
        self.expire_after = expire_after
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.followup: FollowUp | None = None  # runs the follow-up turn (set by the server, which has the agent)
        self.followup_delay = FOLLOWUP_DELAY
        self._scheduled: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # The person answers
    # ------------------------------------------------------------------
    async def decide(self, person_id: int, approval_id: int, approve: bool, surface: str = "", remember: str = "") -> Approval:
        """Approve (the action runs now) or deny. StoreError: not theirs; AlreadyAnswered: too late."""
        approval = self.store.approval(approval_id)
        if approval is None or approval.person_id != person_id:
            raise StoreError("No such request of yours.")
        if remember and remember not in REMEMBER:
            raise StoreError(f"remember must be one of: {', '.join(REMEMBER)}.")
        if approval.status != PENDING:
            raise AlreadyAnswered(f"This request is already {approval.status}.")
        if not self.store.decide(approval_id, APPROVED if approve else DENIED, surface):
            raise AlreadyAnswered("This request was answered a moment ago.")
        if approve:
            if remember:
                self._remember(approval, remember)
            status, result = await self.broker.execute(self.store.approval(approval_id))  # type: ignore[arg-type]
            self.store.finish(approval_id, status, result)
        self.store.log(
            person_id, approval.conversation, self._label(approval), approval.op, approval.level, approval.summary,
            "approved" if approve else DENIED, approval_id,
        )
        settled = self.store.approval(approval_id)
        assert settled is not None
        self._settled(settled)
        self.followup_soon(settled)
        return settled

    def _label(self, approval: Approval) -> str:
        resource = self.store.resource(approval.resource_id) if approval.resource_id else None
        return resource.label if resource else "(removed)"

    def _remember(self, approval: Approval, scope: str) -> None:
        """"Approve and do not ask again" for this kind of action, here or on the whole resource. (The
        administrator's ceiling still applies when it is read.)"""
        resource = self.store.resource(approval.resource_id) if approval.resource_id else None
        if resource is None:
            return
        if scope == "resource":
            self.store.update_resource(resource.id, levels={**resource.levels, approval.level: ALLOW})
            return
        mine = [a for a in self.store.attachments_of(conversation=approval.conversation) if a.resource.id == resource.id]
        self.store.attach(
            resource.id, conversation=approval.conversation,
            levels={**(mine[0].levels if mine else {}), approval.level: ALLOW},
        )

    # ------------------------------------------------------------------
    # Telling the other surfaces
    # ------------------------------------------------------------------
    def _targets(self, approval: Approval) -> tuple[str, ...]:
        return tuple(surface for surface in PUSH_SURFACES if surface != approval.surface)

    def _payload(self, approval: Approval) -> dict:
        return {
            "approval": approval.id, "summary": approval.summary, "resource": self._label(approval), "op": approval.op,
            "level": approval.level, "reason": approval.reason, "status": approval.status,
        }

    def _push(self, approval: Approval, kind: str, text: str, title: str, targets: tuple[str, ...]) -> None:
        try:
            self.notifier.notify(
                approval.person_id, text, title, targets, SERVER, approval.conversation, limited=False, kind=kind,
                payload=self._payload(approval),
            )
        except NotificationError as error:
            log.warning("could not push approval %s: %s", approval.id, error)

    def _settled(self, approval: Approval) -> None:
        """Tell every surface it was answered, so that the others stop showing it."""
        outcome = {DONE: "approved", FAILED: "approved but failed", DENIED: "denied", EXPIRED: "expired"}.get(
            approval.status, approval.status
        )
        self._push(approval, "approval_resolved", f"{approval.summary}: {outcome}", "Request settled", ())

    def threshold(self, person_id: int) -> int:
        own = self.memory.approval_notify_after(person_id)
        return self.default_notify_after if own is None else own

    async def sweep(self) -> None:
        """One pass: push requests nobody answered in time, expire those that waited too long."""
        now = self.clock()
        self.store.prune_approvals((now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds"))
        self.store.expire_jobs(
            (now - timedelta(minutes=10)).isoformat(timespec="seconds"), (now - timedelta(days=1)).isoformat(timespec="seconds")
        )
        for approval in self.store.unnotified():
            wait = self.threshold(approval.person_id)
            if wait == 0:
                self.store.mark_notified(approval.id)  # this person asked never to be pushed
                continue
            if _parse(approval.created_at) + timedelta(seconds=wait) <= now:
                self.store.mark_notified(approval.id)
                self._push(
                    approval, "approval", f"{approval.summary}\nin {approval.conversation}", "Clara needs your permission",
                    self._targets(approval),
                )
        for approval in self.store.pending_older_than((now - timedelta(seconds=self.expire_after)).isoformat(timespec="seconds")):
            if self.store.decide(approval.id, EXPIRED):
                self.store.log(
                    approval.person_id, approval.conversation, self._label(approval), approval.op, approval.level,
                    approval.summary, EXPIRED, approval.id,
                )
                self._settled(self.store.approval(approval.id))  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Telling the model
    # ------------------------------------------------------------------
    def followup_soon(self, approval: Approval) -> None:
        """Start a turn that tells the model how its held requests ended: after a moment, so that several answers
        make one turn."""
        if self.followup is None or not approval.surface or not approval.user_id:
            return
        if approval.conversation in self._scheduled:
            return
        self._scheduled.add(approval.conversation)
        task = asyncio.ensure_future(self._followup(approval))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _followup(self, approval: Approval) -> None:
        await asyncio.sleep(self.followup_delay)
        self._scheduled.discard(approval.conversation)
        ended = self.store.untold(approval.conversation)
        if not ended or self.followup is None:
            return
        self.store.mark_told([a.id for a in ended])
        info = self.memory.conversation_info(approval.conversation)
        try:
            await self.followup(approval, info.project_id if info else None, followup_message(ended))
        except Exception as error:  # the model was down, the person is out of credits...: it is told at the next turn
            log.warning("follow-up of %s failed: %s", approval.conversation, error)
            self.store.mark_untold([a.id for a in ended])

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------
    def recover(self) -> None:
        """At start: requests approved but not finished when the server stopped are not run again (they may have
        been done): they are marked failed, with a note saying to check."""
        for approval in self.store.interrupted():
            self.store.finish(
                approval.id, FAILED, "The server stopped before this finished: it may or may not have been done. Check."
            )

    async def run(self) -> None:
        self.recover()
        while True:
            try:
                await self.sweep()
            except Exception:
                log.exception("approval sweep failed")
            await asyncio.sleep(SWEEP_SECONDS)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task


def followup_message(ended: list[Approval]) -> str:
    """What the follow-up turn says to the model (stored in the conversation, like a reminder's announcement)."""
    lines = ["[Integration update] The person answered requests you had put on hold:"]
    lines += [outcome_line(item) for item in ended]
    lines.append("Go on with the task using this, and tell the person briefly how it ended.")
    return "\n".join(lines)
