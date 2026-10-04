"""From a Discord message to the request sent to the server, and the answer posted."""

import pytest

from clara.discord_bot.handler import MessageHandler

from .conftest import FakeBot, FakeChannel, FakeGuild, FakeMessage, FakeUser

CLARA = FakeUser(1, "clara", "Clara", bot=True)
ERWAN = FakeUser(111, "erwan", "Erwan")
BOB = FakeUser(222, "bob", "Bob")
EVE = FakeUser(333, "eve", "Eve")  # no account


@pytest.fixture
def guild():
    return FakeGuild(5, "Home", [CLARA, ERWAN, BOB, EVE], me=CLARA)


@pytest.fixture
async def handler(api, accounts, server):
    server.signed_in = {"111": "erwan", "222": "bob"}
    await accounts.refresh()
    return MessageHandler(FakeBot(CLARA), api, accounts, timezone="Europe/Paris")


def message(content, author=ERWAN, guild=None, mentions=(), channel=None):
    return FakeMessage(1000, content, author, channel or FakeChannel(10), guild, list(mentions))


async def test_a_mention_is_answered_with_the_space_roster_and_focus(handler, server, guild):
    msg = message("<@1> what does <@222> like?", guild=guild, mentions=[CLARA, BOB])
    server.reply = "@Bob likes chess."
    await handler.handle(msg)

    body = server.bodies("/v1/chat")[0]
    assert body["message"] == "what does @Bob like?" and body["mode"] == "answer" and body["quiet"] is True
    assert body["conversation"] == "discord:channel:10" and body["space"] == "discord:guild:5"
    assert body["roster"] == [{"user_id": "222", "name": "Bob"}, {"user_id": "111", "name": "Erwan"}]  # sorted
    assert body["focus"] == ["222"] and body["timezone"] == "Europe/Paris"
    assert "Home" in body["instructions"] and "#general" in body["instructions"]
    assert msg.replies == ["<@222> likes chess."]


async def test_other_messages_go_as_maybe_and_nothing_is_posted_when_she_passes(handler, server, guild):
    server.reply, server.done_extra = "", {"passed": True}
    msg = message("anyone up for chess?", guild=guild)
    await handler.handle(msg)
    assert server.bodies("/v1/chat")[0]["mode"] == "maybe"
    assert msg.replies == []


async def test_a_chime_in_answer_is_posted(handler, server, guild):
    server.reply, server.done_extra = "Me! Well, sort of.", {"passed": False}
    msg = message("anyone up for chess?", guild=guild)
    await handler.handle(msg)
    assert msg.replies == ["Me! Well, sort of."]


async def test_while_she_is_busy_in_a_channel_other_messages_are_only_observed(handler, server, guild):
    handler._busy[10] = 1
    await handler.handle(message("by the way", guild=guild))
    assert server.bodies("/v1/chat")[0]["mode"] == "observe"


async def test_a_private_message_uses_the_persons_own_conversation(handler, server):
    msg = message("hi")
    await handler.handle(msg)
    body = server.bodies("/v1/chat")[0]
    assert "conversation" not in body and "space" not in body and body["mode"] == "answer"
    assert "private conversation" in body["instructions"]
    assert msg.replies == ["Hello!"]


async def test_someone_without_an_account_is_told_once_how_to_sign_in(handler, server, guild):
    first = message("<@1> hello", author=EVE, guild=guild, mentions=[CLARA])
    await handler.handle(first)
    assert "/register" in first.replies[0]
    second = message("<@1> hello?", author=EVE, guild=guild, mentions=[CLARA])
    await handler.handle(second)
    assert second.replies == []  # not again so soon
    await handler.handle(message("just chatting", author=EVE, guild=guild))
    assert server.bodies("/v1/chat") == []  # nothing of theirs reaches the server


async def test_an_account_the_server_signed_out_is_forgotten_and_hinted(handler, server, accounts):
    server.chat_status, server.chat_detail = 403, "The account discord:111 is not signed in: sign in"
    msg = message("hi")
    await handler.handle(msg)
    assert not accounts.signed_in(111) and "/register" in msg.replies[0]


@pytest.mark.parametrize(
    ("status", "words"), [(503, "shutting down"), (413, "too long"), (429, "Too many"), (502, "could not answer")]
)
async def test_errors_are_told_only_to_someone_who_asked(handler, server, guild, status, words):
    server.chat_status, server.chat_detail = status, "nope"
    asked = message("<@1> hi", guild=guild, mentions=[CLARA])
    await handler.handle(asked)
    assert words in asked.replies[0]
    chatter = message("hi all", guild=guild)
    await handler.handle(chatter)
    assert chatter.replies == []


async def test_bots_are_ignored(handler, server, guild):
    await handler.handle(message("<@1> hi", author=FakeUser(9, "otherbot", bot=True), guild=guild, mentions=[CLARA]))
    assert server.requests == [r for r in server.requests if r.url.path != "/v1/chat"]


async def test_a_long_answer_is_split(handler, server):
    server.reply = "word " * 1000
    msg = message("tell me a story")
    await handler.handle(msg)
    assert len(msg.replies) == 1 and len(msg.channel.sent) >= 2
