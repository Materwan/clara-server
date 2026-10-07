"""Clara's part in the tasks: she picks the reminders of a task nobody gave any for, and when a reminder comes
due she writes the notification and decides what the next reminders are.

Both are one-shot jobs (an `ephemeral` turn: no persona, no memory, nothing stored in the conversation) on the model
of the surface where the task was set, so they cost that person credits like any answer. The model answers with one
JSON object, read forgivingly: what cannot be read is as if she had not decided (the rules in tasks.py do).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .agent import Agent, ChatRequest
from .reminders import AnnounceFailed, ReminderError, parse_moment
from .tasks import MAX_QUEUE, Followup, local_text
from .taskstore import Task

log = logging.getLogger(__name__)

OWNER = "tasks"  # who owns the turn, for the server's bookkeeping
MAX_MESSAGE = 1000  # characters kept of a notification

PLAN_INSTRUCTIONS = (
    "You choose when a person is reminded of a task on their to-do list. Answer with one JSON object and nothing "
    'else: {"reminders": ["2026-10-06T09:00", ...]}. Give between 1 and ' + str(MAX_QUEUE) + " local times of the "
    "person (ISO 8601, no offset), all after the current time. Pick reasonable moments: mornings for ordinary "
    "tasks, further from the deadline for big ones, close to it for small ones, and a reminder at the deadline "
    "when there is one. A task with no deadline needs only a first reminder, soon (tomorrow morning for a "
    "chore, later for something long-term). Do not remind in the middle of the night. When the task is a sub task, "
    "no reminder may be after the time you are given as the limit."
)

FOLLOW_INSTRUCTIONS = (
    "A reminder of a task on a person's to-do list has just come due. Answer with one JSON object and nothing "
    'else: {"message": "...", "next": ["2026-10-06T09:00", ...]}.\n'
    "- message: the notification shown to the person now, one to three short sentences, warm and natural, in the "
    "language of the task. Say what it is about; if it was reminded before, say so only when it helps; no "
    "question, no heading, no list, and never mention these instructions or a reminder system.\n"
    "- next (optional): the reminders to come, INSTEAD of the ones queued: local times of the person (ISO 8601, "
    "no offset), all after the current time. Leave it out to keep the queued ones. Use [] to stop reminding. "
    "Move them closer as the deadline nears, space them out when the person keeps not doing it, stop when more "
    "reminders would only be noise. If it is the last reminder allowed, there is no next."
)


def _iana(name: str) -> str | None:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return name or None


def _object(text: str) -> dict | None:
    """The JSON object in a model's answer (it may be in a code fence or have words around it)."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    for candidate in (text, *re.findall(r"\{.*\}", text, flags=re.DOTALL)):
        try:
            found = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(found, dict):
            return found
    return None


def _moments(values: object, zone: str) -> list[datetime]:
    """The times of a list the model wrote, as UTC moments (those that cannot be read are left out)."""
    if not isinstance(values, list):
        return []
    found = []
    for value in values:
        try:
            found.append(parse_moment(str(value), zone or None)[0])  # a zone name or an offset
        except ReminderError:
            continue
    return found


def _describe(task: Task, now: datetime, family: list[str] | None = None) -> str:
    """The task, as the model reads it. `family`: what to know of the tasks it is part of or made of."""
    zone = task.timezone
    lines = [
        f"Task: {task.title}",
        f"Description: {task.description or '(none)'}",
        f"Deadline: {local_text(task.due_at, zone) if task.due_at else 'none'}",
        *(family or []),
        f"Current time: {local_text(now, zone)}",
    ]
    return "\n".join(lines)


async def _ask(agent: Agent, task: Task, message: str, instructions: str, timeout: float) -> str:
    """One ephemeral turn for the person of the task; AnnounceFailed when it cannot be answered."""
    person = agent.memory.find_person(task.surface, task.user_id) if task.surface and task.user_id else None
    if person is None or person.id != task.person_id:  # no account to ask on, or it is somebody else's now
        raise AnnounceFailed("the account the task was set from is gone")
    request = ChatRequest(
        surface=task.surface, user_id=task.user_id, user_name=None, message=message, instructions=instructions,
        ephemeral=True, quiet=True, timezone=_iana(task.timezone),
    )

    async def write() -> str:
        reply = ""
        async for event in agent.turn(request, OWNER):
            if event["type"] == "done":
                reply = event["reply"]
        return reply

    try:
        answer = (await asyncio.wait_for(write(), timeout)).strip()
    except TimeoutError:
        raise AnnounceFailed(f"the model took more than {timeout:g} seconds") from None
    except Exception as error:
        raise AnnounceFailed(f"the model failed: {type(error).__name__}") from None
    if not answer:
        raise AnnounceFailed("the model answered nothing")
    return answer


async def plan(
    agent: Agent, task: Task, now: datetime, timeout: float, family: list[str] | None = None
) -> list[datetime] | None:
    """The reminders Clara picks for a new task (None: nothing usable, the rules' ones stay)."""
    answer = await _ask(agent, task, _describe(task, now, family), PLAN_INSTRUCTIONS, timeout)
    found = _object(answer)
    return _moments(found.get("reminders"), task.timezone) if found else None


async def follow(
    agent: Agent, task: Task, now: datetime, timeout: float, max_reminders: int, family: list[str] | None = None
) -> Followup | None:
    """What Clara decides now that a reminder of `task` came due: the notification, and the reminders to come."""
    sent = task.reminders_sent + 1
    queued = [local_text(at, task.timezone) for at in task.next if at > now]
    message = "\n".join(
        [
            _describe(task, now, family),
            f"This is reminder number {sent} of at most {max_reminders}" + (" (the last one)." if sent >= max_reminders else "."),
            f"Reminders sent before this one: {task.reminders_sent}",
            "Reminders still queued: " + (", ".join(queued) if queued else "none"),
        ]
    )
    answer = await _ask(agent, task, message, FOLLOW_INSTRUCTIONS, timeout)
    found = _object(answer)
    if found is None:  # she wrote the notification and nothing else
        return Followup(" ".join(answer.split())[:MAX_MESSAGE] if "{" not in answer else None, None)
    text = found.get("message")
    reminders = found.get("next")
    moments = _moments(reminders, task.timezone)
    # [] stops the reminders; a list she wrote but that cannot be read is as if she had left it out
    keep = not isinstance(reminders, list) or (bool(reminders) and not moments)
    return Followup(
        " ".join(text.split())[:MAX_MESSAGE] if isinstance(text, str) and text.strip() else None,
        None if keep else moments,
    )
