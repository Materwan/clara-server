"""Stand-ins for Discord objects (only what the bot reads) and for the Clara server."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import discord
import httpx
import pytest

from clara.discord_bot.accounts import Accounts
from clara.discord_bot.remote import RemoteBackend


@dataclass(eq=False)
class FakeSent:
    """A private message the bot sent: what it says and its buttons, until it is edited."""

    content: str
    view: object = None

    async def edit(self, content: str | None = None, view: object = None, **options) -> None:
        self.content = content if content is not None else self.content
        self.view = view


@dataclass(eq=False)
class FakeUser:
    id: int
    name: str
    display_name: str = ""
    bot: bool = False
    global_name: str | None = None
    sent: list[str] = field(default_factory=list)
    messages: list[FakeSent] = field(default_factory=list)

    def __post_init__(self):
        self.display_name = self.display_name or self.name

    async def send(self, text: str, view: object = None, **options) -> FakeSent:
        self.sent.append(text)
        message = FakeSent(text, view)
        self.messages.append(message)
        return message


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@dataclass(eq=False)
class FakeChannel:
    id: int
    name: str = "general"
    sent: list[str] = field(default_factory=list)

    def typing(self) -> FakeTyping:
        return FakeTyping()

    async def send(self, text: str) -> None:
        self.sent.append(text)


@dataclass(eq=False)
class FakeGuild:
    id: int
    name: str
    members: list[FakeUser]
    me: FakeUser
    preferred_locale: str = "en-US"

    def get_member(self, user_id: int):
        return next((m for m in self.members if m.id == user_id), None)


@dataclass(eq=False)
class FakeMessage:
    id: int
    content: str
    author: FakeUser
    channel: FakeChannel
    guild: FakeGuild | None = None
    mentions: list[FakeUser] = field(default_factory=list)
    role_mentions: list = field(default_factory=list)
    channel_mentions: list = field(default_factory=list)
    reference: Any = None
    type: discord.MessageType = discord.MessageType.default
    replies: list[str] = field(default_factory=list)

    async def reply(self, text: str, **options) -> None:
        self.replies.append(text)


class FakeBot:
    def __init__(self, me: FakeUser):
        self.user = me
        self.users: dict[int, FakeUser] = {}

    def get_user(self, user_id: int):
        return self.users.get(user_id)

    async def fetch_user(self, user_id: int):
        return self.users[user_id]


class FakeServer:
    """A Clara server: records the requests, answers with what a test sets."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.signed_in: dict[str, str] = {}
        self.reply = "Hello!"
        self.chat_status = 200
        self.chat_detail = ""
        self.done_extra: dict = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/accounts/signed-in":
            accounts = [{"user_id": uid, "user": user, "name": user} for uid, user in self.signed_in.items()]
            return httpx.Response(200, json={"accounts": accounts})
        if path == "/v1/chat":
            if self.chat_status != 200:
                return httpx.Response(self.chat_status, json={"detail": self.chat_detail})
            body = json.loads(request.content)
            return httpx.Response(200, json={"reply": self.reply, "conversation": body.get("conversation", ""), **self.done_extra})
        return httpx.Response(404, json={"detail": "not here"})

    def bodies(self, path: str) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path == path and r.content]


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
async def api(server):
    client = RemoteBackend("http://clara.test", "token", transport=httpx.MockTransport(server.handler))
    yield client
    await client.close()


@pytest.fixture
def accounts(api):
    return Accounts(api)
