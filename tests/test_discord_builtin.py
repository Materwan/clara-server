"""The Discord bot built into the server: its direct calls to Clara (LocalBackend), starting and stopping it
(console, web site, AUTO_START_DISCORD_BOT), and the Discord page's routes. Discord itself is faked."""

import asyncio
import time
from dataclasses import replace

import pytest
from conftest import FakeBackend, fake_providers, say
from fastapi.testclient import TestClient

from clara.commands import registry
from clara.discord_bot.backend import ClaraError
from clara.discord_bot.local import LocalBackend
from clara.discord_bot.service import DiscordService, application_id
from clara.server import create_app
from clara.settings import Settings

PASSWORD = "correct horse battery"
ADMIN = {"Authorization": "Bearer secret-admin"}
TOKEN = "MTIz.fake.token"  # the first part is base64 of "123", the bot's id


class LoginFailure(Exception):
    """Named like discord.py's."""


class FakeUser:
    def __init__(self, user_id, name):
        self.id, self.name = user_id, name

    def __str__(self):
        return self.name


class FakeAccounts:
    def __init__(self):
        self.forgotten = []

    def signed_out(self, user_id):
        self.forgotten.append(user_id)


class FakeDiscord:
    """A Discord client that connects at once (or is refused, with the token "bad")."""

    made: list["FakeDiscord"] = []

    def __init__(self, api):
        self.api = api
        self.ready = self.closed = False
        self.user = FakeUser(123, "Clara#0001")
        self.guilds = ["a", "b"]
        self.latency = 0.042
        self.accounts = FakeAccounts()
        self._stop = asyncio.Event()
        FakeDiscord.made.append(self)

    def is_ready(self):
        return self.ready

    def is_closed(self):
        return self.closed

    def get_user(self, user_id):
        return FakeUser(user_id, "erwan_d") if user_id == 111 else None

    async def start(self, token):
        if token == "bad":
            raise LoginFailure("Improper token has been passed.")
        self.ready = True
        await self._stop.wait()

    async def close(self):
        self.closed = True
        self.ready = False
        self._stop.set()


@pytest.fixture
def discord_settings(settings):
    return replace(settings, login_surfaces=frozenset({"discord"}), discord_token=TOKEN)


@pytest.fixture
def app(discord_settings):
    backend = FakeBackend(say("Hi Erwan!"), say("<pass>"), say("Chess is great."), say("ok"))
    application = create_app(discord_settings, fake_providers(discord_settings, backend))
    application.state.fake_model = backend
    application.state.discord.bot_factory = FakeDiscord
    return application


# --- the settings ---------------------------------------------------------------------------------------


def test_the_settings_of_the_built_in_bot(tmp_path):
    base = {"CLARA_TOKENS": "discord:secret-discord", "CLARA_DATA_DIR": str(tmp_path)}
    off = Settings.from_env(base)
    assert (off.discord_token, off.discord_auto_start, off.discord_invite_url) == (None, False, "")
    on = Settings.from_env({**base, "DISCORD_BOT_TOKEN": TOKEN, "AUTO_START_DISCORD_BOT": "true",
                            "DISCORD_BOT_INVIT_URL": "https://discord.com/invite/x"})
    assert (on.discord_token, on.discord_auto_start, on.discord_invite_url) == (TOKEN, True, "https://discord.com/invite/x")
    assert TOKEN not in repr(on)


def test_the_bot_id_is_read_from_the_token():
    assert application_id(TOKEN) == "123"
    assert application_id("not-base64!") is None


# --- direct calls ---------------------------------------------------------------------------------------


async def test_the_local_backend_follows_the_same_rules_as_the_http_api(app):
    backend = LocalBackend(app)
    memory = app.state.memory
    with pytest.raises(ClaraError) as caught:
        await backend.chat(111, "Erwan", "hi")
    assert caught.value.not_signed_in

    assert (await backend.register(111, "Erwan", "erwan", PASSWORD))["user"] == "erwan"
    with pytest.raises(ClaraError) as taken:
        await backend.register(222, "Bob", "erwan", PASSWORD)
    assert taken.value.status == 409
    await backend.register(222, "Bob", "bob", PASSWORD)
    assert await backend.signed_in() == {111: "erwan", 222: "bob"}

    reply = await backend.chat(111, "Erwan", "hello")
    assert reply.answered and reply.text == "Hi Erwan!"

    await backend.sync_spaces([("discord:guild:5", "Home")])
    roster = [{"user_id": "111", "name": "Erwan"}, {"user_id": "222", "name": "Bob"}]
    common = {"conversation": "discord:channel:10", "space": "discord:guild:5", "roster": roster, "focus": [222]}
    observed = await backend.chat(111, "Erwan", "anyone for chess?", mode="maybe", **common)
    assert not observed.answered and len(app.state.fake_model.calls) == 1  # chime in is off: no model call
    memory.set_space_chime("discord:guild:5", True)
    assert not (await backend.chat(222, "Bob", "me maybe", mode="maybe", **common)).answered
    answered = await backend.chat(111, "Erwan", "what do you think?", mode="maybe", **common)
    assert answered.text == "Chess is great."
    assert "@Erwan, @Bob" in app.state.fake_model.calls[-1][0][0]["content"]

    stored = await backend.add_fact(111, "Plays chess on Sundays")
    assert [f["text"] for f in (await backend.me(111))["facts"]] == ["Plays chess on Sundays"]
    await backend.delete_fact(111, stored["id"])
    with pytest.raises(ClaraError) as gone:
        await backend.delete_fact(111, stored["id"])
    assert gone.value.status == 404

    assert await backend.clear_conversation("discord:channel:10") == 4
    assert await backend.logout(111) is True
    with pytest.raises(ClaraError) as out:
        await backend.add_fact(111, "no")
    assert out.value.not_signed_in


async def test_the_local_backend_streams_the_events_of_discord_accounts(app):
    backend = LocalBackend(app)
    await backend.register(111, "Erwan", "erwan", PASSWORD)
    stream = backend.events()
    assert (await anext(stream))["type"] == "server"
    person = app.state.memory.find_person("discord", "111")
    app.state.notifier.notify(person.id, "Your tea is ready", "Tea")
    event = await asyncio.wait_for(anext(stream), 5)
    assert event["accounts"] == ["111"] and event["text"] == "Your tea is ready"
    await stream.aclose()


async def test_a_model_failure_is_a_502(app, discord_settings):
    app.state.fake_model.rounds.clear()  # the model has nothing to say: it fails
    backend = LocalBackend(app)
    await backend.register(111, "Erwan", "erwan", PASSWORD)
    with pytest.raises(ClaraError) as caught:
        await backend.chat(111, "Erwan", "hi")
    assert caught.value.status == 502


# --- starting and stopping -------------------------------------------------------------------------------


async def test_the_service_starts_and_stops_the_bot():
    service = DiscordService(TOKEN, lambda: "backend", bot_factory=FakeDiscord)
    assert service.state == "stopped" and service.status()["invite_url"].startswith(
        "https://discord.com/oauth2/authorize?client_id=123&"
    )
    assert "running as Clara#0001" in await service.start()
    status = service.status()
    assert status["state"] == "running" and status["guilds"] == 2 and status["latency_ms"] == 42
    assert FakeDiscord.made[-1].api == "backend"
    assert "already running" in await service.start()
    assert await service.stop() == "Discord bot: stopped."
    assert service.state == "stopped" and FakeDiscord.made[-1].closed
    assert "not running" in await service.stop()
    await service.restart()
    assert service.state == "running"
    await service.stop()


async def test_a_refused_token_is_explained():
    service = DiscordService("bad", lambda: None, bot_factory=FakeDiscord)
    await service.start(wait=1)
    assert service.state == "error" and "refused the token" in service.last_error
    assert "after an error" in service.describe()


async def test_without_a_token_or_discord_py_nothing_starts(monkeypatch):
    service = DiscordService(None, lambda: None, bot_factory=FakeDiscord)
    assert service.state == "no-token" and "DISCORD_BOT_TOKEN" in await service.start()
    monkeypatch.setattr("clara.discord_bot.service.discord_installed", lambda: False)
    plain = DiscordService(TOKEN, lambda: None)
    assert plain.state == "unavailable" and "pip install" in await plain.start()


async def test_the_console_command(app):
    ctx = app.state.commands
    assert "stopped" in (await registry.execute("/discord", ctx)).output
    assert "running" in (await registry.execute("/discord start", ctx)).output
    status = (await registry.execute("/discord status", ctx)).output
    assert "Starts with the server: no" in status and "client_id=123" in status
    assert "stopped" in (await registry.execute("/discord stop", ctx)).output
    assert "Usage" in (await registry.execute("/discord fly", ctx)).output


def test_auto_start_starts_the_bot_with_the_server_and_stops_it_with_it(discord_settings):
    settings = replace(discord_settings, discord_auto_start=True)
    app = create_app(settings, fake_providers(settings, FakeBackend()))
    app.state.discord.bot_factory = FakeDiscord
    with TestClient(app):
        for _ in range(50):
            if app.state.discord.state == "running":
                break
            time.sleep(0.05)
        assert app.state.discord.state == "running"
        bot = FakeDiscord.made[-1]
    assert bot.closed and app.state.discord.state == "stopped"


# --- the web site's Discord page -------------------------------------------------------------------------


def test_the_discord_page_routes(app):
    with TestClient(app) as client:
        assert client.get("/v1/admin/discord", headers={"Authorization": "Bearer secret-discord"}).status_code == 401
        page = client.get("/v1/admin/discord", headers=ADMIN).json()
        assert page["bot"]["state"] == "stopped" and page["bot"]["token_set"] and page["accounts"] == []
        assert "token" not in str(page["bot"]).lower().replace("token_set", "")

        started = client.post("/v1/admin/discord/start", headers=ADMIN).json()
        assert started["bot"]["state"] == "running" and "running" in started["output"]

        users = app.state.users
        user = users.create("erwan", PASSWORD)
        app.state.memory.resolve("discord", "111", "Erwan")
        users.sign_in_account(user, "discord", "111", "discord-bot")
        app.state.memory.sync_spaces("discord", [("discord:guild:5", "Home")])
        page = client.get("/v1/admin/discord", headers=ADMIN).json()
        assert page["accounts"] == [{"user_id": "111", "user": "erwan", "person": "Erwan", "discord_name": "erwan_d"}]
        assert [space["name"] for space in page["spaces"]] == ["Home"]

        assert client.delete("/v1/admin/discord/accounts/111", headers=ADMIN).json() == {"signed_out": True}
        assert FakeDiscord.made[-1].accounts.forgotten == [111]
        assert client.delete("/v1/admin/discord/accounts/111", headers=ADMIN).status_code == 404
        assert client.post("/v1/admin/discord/explode", headers=ADMIN).status_code == 404
        assert client.post("/v1/admin/discord/stop", headers=ADMIN).json()["bot"]["state"] == "stopped"
