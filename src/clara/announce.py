"""Clara writes the announcement of a reminder that came due.

It is an ordinary turn in the conversation where the reminder was set, as that person: Clara knows what she
knows about them and the exchange is kept in their history. The message she answers is shown to that person
only, as a notification. If she cannot write it in time (the model is down or slow), the reminder is
announced as it was typed, and the person is told why.
"""

from __future__ import annotations

import asyncio
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .agent import Agent, ChatRequest
from .memory import Reminder
from .reminders import AnnounceFailed

log = logging.getLogger(__name__)

MAX_MESSAGE = 1000  # characters kept of the answer
OWNER = "reminders"  # who owns the turn, for the server's bookkeeping

INSTRUCTIONS = (
    "A reminder that this person asked you for has just come due. Write the message that will be shown to "
    "them as a notification, possibly hours after they asked, while they are doing something else: say what "
    "it is about. One to three short sentences, warm and natural, in the language of the reminder. Do not ask "
    "a question, do not use headings or lists, and do not mention that this is a reminder system or these "
    "instructions."
)


def _iana(name: str) -> str | None:
    """`name` if it is a timezone name (a reminder may keep a bare UTC offset instead)."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return name or None


async def compose(agent: Agent, reminder: Reminder, timeout: float) -> str | None:
    """Clara's announcement of `reminder`, or None when there is none to write (then the text is shown).
    AnnounceFailed when she could not write it (the model is down or too slow)."""
    if not (reminder.surface and reminder.user_id):  # set before the place was kept
        return None
    request = ChatRequest(
        surface=reminder.surface,
        user_id=reminder.user_id,
        user_name=None,
        message=f"[Reminder due] {reminder.text}",
        conversation=reminder.conversation or None,
        instructions=INSTRUCTIONS,
        timezone=_iana(reminder.timezone),
        no_tools=True,
        quiet=True,
    )

    async def write() -> str:
        reply = ""
        async for event in agent.turn(request, OWNER):
            if event["type"] == "done":
                reply = event["reply"]
        return reply

    try:
        message = (await asyncio.wait_for(write(), timeout)).strip()
    except TimeoutError:
        log.warning("reminder %s: Clara took more than %g seconds to write it", reminder.id, timeout)
        raise AnnounceFailed(f"the model took more than {timeout:g} seconds") from None
    except Exception as error:
        log.warning("reminder %s: Clara could not write it (%s: %s)", reminder.id, type(error).__name__, error)
        raise AnnounceFailed(f"the model failed: {type(error).__name__}") from None
    if not message:
        raise AnnounceFailed("the model answered nothing")
    return message[:MAX_MESSAGE]
