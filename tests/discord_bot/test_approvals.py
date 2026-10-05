"""Requests for permission on Discord: the private message, its buttons, and answering from it."""

import json
import re

import discord
import httpx
import pytest

from clara.discord_bot.accounts import Accounts
from clara.discord_bot.approvals import TEMPLATE, ApprovalButton, approval_view, ask_text, outcome_text
from clara.discord_bot.backend import ClaraError
from clara.discord_bot.events import EventRelay
from clara.discord_bot.remote import RemoteBackend
from clara.discord_bot.texts import ENGLISH, FRENCH

from .conftest import FakeBot, FakeSent, FakeUser

ASK = {
    "type": "approval", "approval": 12, "summary": "overwrite notes.md in Docs (14 characters)", "resource": "Docs",
    "level": "destructive", "reason": "you asked me to tidy up", "conversation": "web:erwan:talk", "accounts": ["5"],
}


def test_the_question_names_what_where_and_how_far():
    english = ask_text(ASK, ENGLISH)
    assert "I need your permission" in english and "overwrite notes.md in Docs" in english
    assert "On Docs · replace or delete" in english and "I say: “you asked me to tidy up”" in english
    assert "Nothing is done until you approve" in english
    french = ask_text({**ASK, "reason": ""}, FRENCH)
    assert "besoin de ta permission" in french and "remplacement ou suppression" in french and "Je dis" not in french


def test_how_a_request_ended_is_told_in_the_message():
    done = {"status": "done", "summary": "write a.txt", "result": "Created a.txt."}
    assert outcome_text(done, ENGLISH) == "✅ Approved and done: write a.txt\nCreated a.txt."
    assert outcome_text({**done, "status": "denied"}, ENGLISH) == "⛔ Denied: write a.txt. Nothing was done."
    assert outcome_text({**done, "status": "expired"}, FRENCH).startswith("⌛ Expiré")
    assert "failed" in outcome_text({**done, "status": "failed", "result": "Error: no"}, ENGLISH)
    assert outcome_text({"status": "weird"}, ENGLISH) == "This request was already answered."


async def test_the_relay_sends_the_request_with_two_buttons_that_survive_a_restart(api, accounts):
    erwan = FakeUser(5, "erwan")
    bot = FakeBot(FakeUser(1, "clara", bot=True))
    bot.users = {5: erwan}
    accounts.remember_language(5, FRENCH)
    relay = EventRelay(bot, api, accounts)
    await relay.deliver(ASK)
    (message,) = erwan.messages
    assert "besoin de ta permission" in message.content
    ids = [item.custom_id for item in message.view.children]
    assert ids == ["clara-approval:12:approve", "clara-approval:12:deny"]
    assert all(re.fullmatch(TEMPLATE, custom_id) for custom_id in ids)
    assert [item.item.label for item in message.view.children] == ["Approuver", "Refuser"]


async def test_a_request_settled_elsewhere_loses_its_buttons(api, accounts):
    erwan = FakeUser(5, "erwan")
    bot = FakeBot(FakeUser(1, "clara", bot=True))
    bot.users = {5: erwan}
    relay = EventRelay(bot, api, accounts)
    await relay.deliver(ASK)
    await relay.deliver({"type": "approval_resolved", "approval": 12, "status": "denied", "summary": "overwrite notes.md", "accounts": ["5"]})
    message = erwan.messages[0]
    assert message.view is None and message.content == "⛔ Denied: overwrite notes.md. Nothing was done."
    await relay.deliver({"type": "approval_resolved", "approval": 12, "status": "denied", "summary": "x", "accounts": ["5"]})  # nothing left to edit


async def test_a_request_without_a_number_or_for_a_stranger_sends_nothing(api, accounts):
    erwan = FakeUser(5, "erwan")
    bot = FakeBot(FakeUser(1, "clara", bot=True))
    bot.users = {5: erwan}
    relay = EventRelay(bot, api, accounts)
    await relay.deliver({**ASK, "approval": None})
    await relay.deliver({**ASK, "accounts": ["not-a-number"]})
    assert erwan.messages == []


# --- clicking ------------------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self):
        self.deferred = False

    async def defer(self, **options):
        self.deferred = True


class FakeFollowup:
    def __init__(self):
        self.sent: list[tuple[str, bool]] = []

    async def send(self, text, ephemeral=False, **options):
        self.sent.append((text, ephemeral))


class FakeInteraction:
    def __init__(self, bot, user_id=5, content="the question"):
        self.client = bot
        self.user = FakeUser(user_id, "erwan")
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.message = FakeSent(content, view="buttons")


class Bot:
    def __init__(self, api):
        self.api = api
        self.accounts = Accounts(api)


@pytest.fixture
def clara(server):
    server.decisions = []
    server.answer = (200, {"id": 12, "status": "done", "summary": "overwrite notes.md", "result": "Replaced notes.md."})
    original = server.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/approvals/12/decide":
            server.requests.append(request)
            server.decisions.append(json.loads(request.content))
            status, body = server.answer
            return httpx.Response(status, json=body)
        return original(request)

    return RemoteBackend("http://clara.test", "token", transport=httpx.MockTransport(handler))


async def test_approving_is_sent_as_that_discord_account_and_the_message_says_how_it_ended(server, clara):
    interaction = FakeInteraction(Bot(clara))
    await ApprovalButton(12, "approve").callback(interaction)
    assert server.decisions == [{"surface": "discord", "user_id": "5", "approve": True}]
    assert interaction.response.deferred
    assert interaction.message.view is None and interaction.message.content == "✅ Approved and done: overwrite notes.md\nReplaced notes.md."
    await clara.close()


async def test_denying_is_sent_as_a_refusal(server, clara):
    server.answer = (200, {"id": 12, "status": "denied", "summary": "overwrite notes.md", "result": ""})
    interaction = FakeInteraction(Bot(clara))
    await ApprovalButton(12, "deny").callback(interaction)
    assert server.decisions[0]["approve"] is False
    assert "Denied" in interaction.message.content
    await clara.close()


@pytest.mark.parametrize(
    ("status", "words"), [(409, "already answered"), (404, "cannot find"), (502, "could not process")]
)
async def test_a_refused_answer_is_explained_to_the_person_alone(server, clara, status, words):
    server.answer = (status, {"detail": "no"})
    interaction = FakeInteraction(Bot(clara), content="the question")
    await ApprovalButton(12, "approve").callback(interaction)
    (text, private) = interaction.followup.sent[0]
    assert words in text and private is True
    # an answered request has no use for its buttons; one that failed for another reason keeps them to try again
    assert (interaction.message.view is None) == (status == 409)
    await clara.close()


async def test_the_buttons_are_found_again_from_their_id_after_a_restart():
    match = re.fullmatch(TEMPLATE, "clara-approval:31:deny")
    button = await ApprovalButton.from_custom_id(None, None, match)
    assert (button.approval_id, button.action) == (31, "deny")
    view = approval_view(31, ENGLISH)
    assert view.timeout is None  # no expiry: the server decides when a request lapses


# --- the built-in bot, in the server ------------------------------------------------------------------


async def test_the_bot_inside_the_server_answers_for_its_own_discord_account(settings, tmp_path):
    from conftest import FakeBackend, fake_providers

    from clara.discord_bot.local import LocalBackend
    from clara.server import create_app

    app = create_app(settings, fake_providers(settings, FakeBackend()))
    service = app.state.integrations
    root = tmp_path / "docs"
    root.mkdir()
    service.store.set_policy({**service.store.policy(), "enabled": {"server": True}, "roots": [str(tmp_path)]})
    person = app.state.memory.resolve("discord", "5", "Erwan")
    resource = service.store.add_resource(person.id, None, "server_path", "docs", {"path": str(root)})
    approval, _ = service.store.add_approval(
        person.id, "discord:dm:5", resource.id, "write", "write", {"path": "a.txt", "content": "hi", "mode": "create"}, "create a.txt"
    )
    backend = LocalBackend(app)
    stranger = app.state.memory.resolve("discord", "6", "Zoe")
    assert stranger.id != person.id
    with pytest.raises(ClaraError) as refused:
        await backend.decide_approval(6, approval.id, True)
    assert refused.value.status == 404 and not (root / "a.txt").exists()
    done = await backend.decide_approval(5, approval.id, True)
    assert done["status"] == "done" and (root / "a.txt").read_text() == "hi"
    with pytest.raises(ClaraError) as again:
        await backend.decide_approval(5, approval.id, True)
    assert again.value.status == 409
    _ = discord  # (the module is imported for its button classes)
