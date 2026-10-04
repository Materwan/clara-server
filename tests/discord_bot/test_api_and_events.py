import json

import httpx
import pytest

from clara.discord_bot.backend import ClaraError
from clara.discord_bot.commands import FrenchTranslator, me_embed
from clara.discord_bot.events import EventRelay, text_of
from clara.discord_bot.remote import RemoteBackend
from clara.discord_bot.standalone import Settings, SettingsError
from clara.discord_bot.texts import ENGLISH, FRENCH

from .conftest import FakeBot, FakeUser


async def test_the_api_sends_the_account_and_reads_errors(api, server):
    server.chat_status, server.chat_detail = 403, "The account discord:5 is not signed in: sign in or register first"
    with pytest.raises(ClaraError) as caught:
        await api.chat(5, "Five", "hi")
    assert caught.value.not_signed_in and caught.value.status == 403
    sent = server.bodies("/v1/chat")[0]
    assert sent["surface"] == "discord" and sent["user_id"] == "5" and sent["user_name"] == "Five"
    assert server.requests[0].headers["authorization"] == "Bearer token"


async def test_observed_and_passed_messages_are_not_answers(api, server):
    server.done_extra = {"observed": True}
    reply = await api.chat(5, "Five", "hi", conversation="discord:channel:1", space="discord:guild:1", mode="observe")
    assert not reply.answered and reply.text == ""


async def test_an_unreachable_server():
    def down(request):
        raise httpx.ConnectError("refused")

    api = RemoteBackend("http://clara.test", "t", transport=httpx.MockTransport(down))
    with pytest.raises(ClaraError) as caught:
        await api.me(1)
    assert caught.value.unreachable
    await api.close()


async def test_the_event_stream_is_read_as_server_sent_events():
    events = [{"type": "server", "state": "running"}, {"type": "reminder", "text": "tea", "accounts": ["5"]}]
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events) + ": keepalive\n\n"

    def stream(request):
        assert request.url.params["all"] == "true" and request.url.params["surface"] == "discord"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    api = RemoteBackend("http://clara.test", "t", transport=httpx.MockTransport(stream))
    assert [event async for event in api.events()] == events
    await api.close()


def test_what_is_sent_for_each_event():
    assert text_of({"type": "reminder", "text": "tea", "message": "Time for tea, Erwan!"}, ENGLISH) == (
        "⏰ **Reminder**: Time for tea, Erwan!"
    )
    assert text_of({"type": "reminder", "text": "thé", "message": None}, FRENCH) == "⏰ **Rappel** : thé"
    assert text_of({"type": "notification", "title": "Done", "text": "It works", "source": "clara"}, ENGLISH) == (
        "🔔 **Done**\nIt works"
    )
    about_discord = {"type": "notification", "text": "Answer ready", "source": "server", "conversation": "discord:channel:1"}
    assert text_of(about_discord, ENGLISH) is None
    assert text_of({"type": "server", "state": "stopping"}, ENGLISH) is None


async def test_events_reach_each_account_privately(api, accounts):
    erwan, other = FakeUser(5, "erwan"), FakeUser(6, "other")
    bot = FakeBot(FakeUser(1, "clara", bot=True))
    bot.users = {5: erwan, 6: other}
    accounts.remember_language(6, FRENCH)
    relay = EventRelay(bot, api, accounts)
    await relay.deliver({"type": "reminder", "text": "tea", "message": None, "accounts": ["5", "6", "x"]})
    assert erwan.sent == ["⏰ **Reminder**: tea"] and other.sent == ["⏰ **Rappel** : tea"]


def test_settings_need_both_tokens():
    with pytest.raises(SettingsError, match="DISCORD_BOT_TOKEN"):
        Settings.from_env({"CLARA_TOKEN": "x"})
    with pytest.raises(SettingsError, match="CLARA_TOKEN"):
        Settings.from_env({"DISCORD_BOT_TOKEN": "x"})
    with pytest.raises(SettingsError, match="timezone"):
        Settings.from_env({"DISCORD_BOT_TOKEN": "x", "CLARA_TOKEN": "y", "CLARA_TIMEZONE": "Mars/Base"})
    settings = Settings.from_env(
        {"DISCORD_BOT_TOKEN": "secret-discord", "CLARA_TOKEN": "secret-clara", "CLARA_URL": "https://box.ts.net/"}
    )
    assert settings.clara_url == "https://box.ts.net" and "secret" not in repr(settings)


def test_the_me_card_lists_the_newest_facts_first_and_fits():
    facts = [{"id": i, "text": f"fact number {i} " + "x" * 40} for i in range(100)]
    embed = me_embed(ENGLISH, {"user": "erwan", "relation": 70, "relation_label": "good", "accounts": ["discord:5"], "facts": facts})
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Relationship"] == "70/100 (good)"
    assert fields["What I remember about you"].startswith("`99`") and len(fields["What I remember about you"]) <= 1024
    assert "more" in fields["What I remember about you"]


async def test_command_descriptions_are_translated_for_french_discords():
    import discord
    from discord import app_commands

    translator = FrenchTranslator()
    text = app_commands.locale_str("Create your Clara account")
    assert await translator.translate(text, discord.Locale.french, None) == "Créer ton compte Clara"
    assert await translator.translate(text, discord.Locale.german, None) is None
