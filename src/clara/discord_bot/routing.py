"""What to do with a Discord message: the rules, without Discord.

In a private conversation every message is for Clara. On a server a message is for her when it mentions her or
replies to one of her messages; any other message of a signed-in member is sent too, so that she follows the
conversation: as `maybe` (she answers only if she has something to add, where an administrator allows it; the
server decides) or, while she is already busy in that channel, as `observe` (only kept as context).
Bots are ignored, and so are people who are not signed in (they are only told how to sign in, when they talk
to her).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Action(str, Enum):
    IGNORE = "ignore"
    HINT = "hint"  # tell them to /register (they talked to Clara without an account)
    SLASH = "slash"  # "@Clara /register": tell them commands are slash commands
    ANSWER = "answer"
    MAYBE = "maybe"
    OBSERVE = "observe"


@dataclass(frozen=True)
class Incoming:
    from_bot: bool
    is_self: bool
    private: bool  # a direct message
    addressed: bool  # mentions Clara, or replies to her
    signed_in: bool
    text: str  # as Clara would read it (mentions turned into names)
    channel_busy: bool = False  # Clara is already working on a message of this channel


def route(message: Incoming) -> Action:
    if message.is_self or message.from_bot:
        return Action.IGNORE
    to_clara = message.private or message.addressed
    if not message.signed_in:
        return Action.HINT if to_clara else Action.IGNORE
    if not message.text:
        return Action.IGNORE
    if to_clara:
        return Action.SLASH if message.text.startswith("/") else Action.ANSWER
    return Action.OBSERVE if message.channel_busy else Action.MAYBE


def conversation_of(channel_id: int, private: bool) -> str | None:
    """The server's conversation: one per server channel (or thread), and the person's own for a direct
    message (None: the server's default, `discord:<user id>`)."""
    return None if private else f"discord:channel:{channel_id}"


def space_of(guild_id: int) -> str:
    return f"discord:guild:{guild_id}"
