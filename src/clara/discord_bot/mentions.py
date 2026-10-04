"""Discord text in both directions: a message -> what Clara reads, her answer -> what Discord shows.

Clara never sees raw ids: `<@123>` becomes `@Paul`. In her answers, `@Paul` becomes a ping of the member of that
server called Paul (display name or user name; the longest name that matches wins). Roles, @everyone and @here
are never pinged (the client's allowed mentions refuse them too).
"""

from __future__ import annotations

import re
from typing import Iterable, Protocol

DISCORD_LIMIT = 2000
MAX_PINGS_PER_REPLY = 10  # beyond this, "@Name" stays plain text (no mass pings)
MAX_NAME_WORDS = 3

# "@Paul" or "@Jean Pierre Dupont" (up to 3 words) not already inside "<@...>"
_PING_RE = re.compile(r"(?<![<\w])@(\w[\w'.-]*(?: \w[\w'.-]*){0,%d})" % (MAX_NAME_WORDS - 1))
_USER_RE = re.compile(r"<@!?(\d+)>")
_ROLE_RE = re.compile(r"<@&(\d+)>")
_CHANNEL_RE = re.compile(r"<#(\d+)>")


class Named(Protocol):
    id: int
    name: str
    display_name: str


def readable(
    content: str,
    users: Iterable[Named],
    bot_id: int,
    bot_name: str,
    roles: dict[int, str] | None = None,
    channels: dict[int, str] | None = None,
) -> str:
    """The text of a message as Clara reads it: mentions as names, without the mention that called her."""
    names = {user.id: user.display_name for user in users}
    text = content.strip()
    lead = _USER_RE.match(text)
    if lead and int(lead.group(1)) == bot_id:  # "@Clara what time is it?" -> "what time is it?"
        text = text[lead.end():].lstrip(" ,:")

    def user(match: re.Match) -> str:
        user_id = int(match.group(1))
        if user_id == bot_id:
            return f"@{bot_name}"
        return f"@{names[user_id]}" if user_id in names else "@someone"

    text = _USER_RE.sub(user, text)
    text = _ROLE_RE.sub(lambda m: f"@{(roles or {}).get(int(m.group(1)), 'role')}", text)
    text = _CHANNEL_RE.sub(lambda m: f"#{(channels or {}).get(int(m.group(1)), 'channel')}", text)
    return text.strip()


def add_pings(text: str, members: Iterable[Named], bot_id: int, max_pings: int = MAX_PINGS_PER_REPLY) -> str:
    """"@Paul" -> "<@id>" for the members named so (display name, global name or user name, any case)."""
    ids: dict[str, int] = {}
    for member in members:
        for name in (member.display_name, getattr(member, "global_name", None), member.name):
            if name:
                ids.setdefault(name.lower(), member.id)
    pinged: set[int] = set()

    def replace(match: re.Match) -> str:
        words = match.group(1).split(" ")
        for count in range(len(words), 0, -1):
            name = " ".join(words[:count])
            user_id = ids.get(name.lower()) or ids.get(name.rstrip(".").lower())
            if user_id is None or user_id == bot_id:
                continue
            if user_id not in pinged and len(pinged) >= max_pings:
                break
            pinged.add(user_id)
            rest = match.group(1)[len(name):]
            return f"<@{user_id}>{rest}"
        return match.group(0)  # unknown name, or too many pings: left as text

    return _PING_RE.sub(replace, text)


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Pieces of at most `limit` characters, cut at a line break or a space when possible; a code block cut in
    two is closed and opened again."""
    chunks: list[str] = []
    fence = ""
    while len(fence + text) > limit:
        text = fence + text
        room = limit - 4  # for a closing ``` if the piece ends inside a code block
        cut = text.rfind("\n", 0, room)
        if cut <= 0:
            cut = text.rfind(" ", 0, room)
        if cut <= 0:
            cut = room
        piece, text = text[:cut], text[cut:].lstrip("\n ") if text[cut:cut + 1] in "\n " else text[cut:]
        fence = ""
        opened = re.findall(r"^```(\w*)", piece, flags=re.MULTILINE)
        if len(opened) % 2:  # inside a code block: close it here, open it again in the next piece
            piece += "\n```"
            fence = f"```{opened[-1]}\n"
        chunks.append(piece)
    chunks.append(fence + text)
    return [chunk for chunk in chunks if chunk.strip()]
