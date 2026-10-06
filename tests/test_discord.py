"""What the Discord bot needs from the server: accounts signed in (or registered) by the bot, group spaces with
their members, messages kept as context or answered only when Clara has something to add, the relationship, and
one event stream for the whole surface."""

import asyncio
from dataclasses import replace

import pytest
from conftest import FakeBackend, call, fake_providers, say, untimed
from fastapi.testclient import TestClient

from clara.agent import ChatRequest, passed
from clara.commands import registry
from clara.llm import LlmChunk, ToolCall
from clara.memory import Person
from clara.notifications import Notifier
from clara.server import create_app

BOT = {"Authorization": "Bearer secret-discord"}
ADMIN = {"Authorization": "Bearer secret-admin"}
PASSWORD = "correct horse battery"
GUILD = "discord:guild:1"
CHANNEL = "discord:channel:10"


@pytest.fixture
def discord_settings(settings):
    return replace(settings, login_surfaces=frozenset({"discord"}))


@pytest.fixture
def make_client(discord_settings):
    clients = []

    def build(*rounds):
        backend = FakeBackend(*rounds)
        client = TestClient(create_app(discord_settings, fake_providers(discord_settings, backend)))
        client.__enter__()
        client.backend = backend
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)


def account(user_id="111", name="Erwan"):
    return {"surface": "discord", "user_id": user_id, "user_name": name}


def register(client, user_id="111", username="erwan", name="Erwan", password=PASSWORD):
    return client.post(
        "/v1/accounts/register", json={**account(user_id, name), "username": username, "password": password}, headers=BOT
    )


def chat(client, user_id="111", message="hi", **extra):
    return client.post("/v1/chat", json={"surface": "discord", "user_id": user_id, "message": message, **extra}, headers=BOT)


# --- signing in ------------------------------------------------------------------------------------------


def test_an_account_must_register_before_talking(make_client):
    client = make_client(say("Hello"))
    refused = chat(client)
    assert refused.status_code == 403 and "not signed in" in refused.json()["detail"]
    assert client.get("/v1/accounts/me", params={"surface": "discord", "user_id": "111"}, headers=BOT).json() == {
        "signed_in": False
    }

    made = register(client)
    assert made.status_code == 201
    assert made.json()["user"] == "erwan" and made.json()["person"]["name"] == "Erwan"
    assert "discord:111" in made.json()["accounts"]
    assert chat(client).json()["reply"] == "Hello"

    me = client.get("/v1/accounts/me", params={"surface": "discord", "user_id": "111"}, headers=BOT).json()
    assert me["signed_in"] and me["user"] == "erwan" and me["relation"] is None and me["facts"] == []
    listed = client.get("/v1/accounts/signed-in", params={"surface": "discord"}, headers=BOT).json()
    assert listed == {"accounts": [{"user_id": "111", "user": "erwan", "name": "Erwan"}]}


def test_the_registered_user_signs_in_on_the_web_with_the_same_memories(make_client):
    client = make_client()
    register(client)
    person = client.app.state.memory.find_person("discord", "111")
    client.app.state.memory.add_fact(person.id, "Likes jazz")
    login = client.post("/v1/auth/login", json={"username": "erwan", "password": PASSWORD, "surface": "web"})
    assert login.status_code == 200
    token = {"Authorization": f"Bearer {login.json()['token']}"}
    facts = client.get("/v1/memory/facts", params={"surface": "web", "user_id": "erwan"}, headers=token).json()
    assert [f["text"] for f in facts["facts"]] == ["Likes jazz"]


def test_registering_checks_the_name_and_password_and_leaves_nothing_behind(make_client):
    client = make_client()
    people = len(client.app.state.memory.summaries())
    assert register(client, password="short").status_code == 422
    assert register(client, username="Not valid!").status_code == 422
    assert len(client.app.state.memory.summaries()) == people  # no person left without a user
    assert register(client).status_code == 201
    assert register(client, user_id="222", username="erwan").status_code == 409  # name taken
    assert register(client).status_code == 409  # this account is already signed in


def test_registering_is_limited_per_account(make_client):
    client = make_client()
    for number in range(3):
        assert register(client, username=f"user{number}").status_code == 201
        client.post("/v1/accounts/logout", json={"surface": "discord", "user_id": "111"}, headers=BOT)
    assert register(client, username="user9").status_code == 429


def test_login_needs_the_password_and_logout_ends_it(make_client):
    client = make_client(say("ok"))
    client.app.state.users.create("erwan", PASSWORD)
    body = {**account(), "username": "erwan", "password": "wrong password"}
    assert client.post("/v1/accounts/login", json=body, headers=BOT).status_code == 401
    body["password"] = PASSWORD
    assert client.post("/v1/accounts/login", json=body, headers=BOT).json()["user"] == "erwan"
    assert chat(client).status_code == 200
    out = client.post("/v1/accounts/logout", json={"surface": "discord", "user_id": "111"}, headers=BOT)
    assert out.json() == {"signed_out": True}
    assert chat(client).status_code == 403


def test_wrong_passwords_are_limited_per_account(make_client, discord_settings):
    client = make_client()
    client.app.state.users.create("erwan", PASSWORD)
    body = {**account(), "username": "erwan", "password": "wrong password"}
    for _ in range(discord_settings.auth_max_failures):
        client.post("/v1/accounts/login", json=body, headers=BOT)
    body["password"] = PASSWORD
    assert client.post("/v1/accounts/login", json=body, headers=BOT).status_code == 429
    # another account of the same bot is not blocked
    other = {**account("222"), "username": "erwan", "password": PASSWORD}
    assert client.post("/v1/accounts/login", json=other, headers=BOT).status_code == 200


def test_signing_in_as_another_user_moves_the_account_without_merging_anybody(make_client):
    client = make_client()
    memory = client.app.state.memory
    register(client, username="alice", name="Alice")
    alice = memory.find_person("discord", "111")
    memory.add_fact(alice.id, "Alice likes tea")
    client.post("/v1/accounts/logout", json={"surface": "discord", "user_id": "111"}, headers=BOT)

    client.app.state.users.create("bob", PASSWORD)
    body = {**account(), "username": "bob", "password": PASSWORD}
    assert client.post("/v1/accounts/login", json=body, headers=BOT).status_code == 200
    bob = memory.find_person("discord", "111")
    assert bob.id != alice.id and memory.person_by_id(alice.id) is not None
    assert [f.text for f in memory.facts(alice.id)] == ["Alice likes tea"]
    assert memory.facts(bob.id) == []


def test_a_new_password_or_a_disabled_user_signs_the_account_out(make_client):
    client = make_client()
    users = client.app.state.users
    register(client)
    assert users.account_user("discord", "111").name == "erwan"
    users.set_password("erwan", "another long password")
    assert users.account_user("discord", "111") is None

    client.post("/v1/accounts/login", json={**account(), "username": "erwan", "password": "another long password"}, headers=BOT)
    users.create("root", PASSWORD, admin=True)
    users.set_disabled("erwan", True)
    assert chat(client).status_code == 403
    users.set_disabled("erwan", False)
    assert users.account_user("discord", "111") is None  # disabling signed it out for good


def test_erasing_a_person_signs_their_accounts_out(make_client):
    client = make_client()
    register(client)
    memory = client.app.state.memory
    memory.delete_person(memory.find_person("discord", "111").id)
    assert client.app.state.users.signed_in_accounts("discord") == {}


def test_only_the_login_surfaces_sign_in_and_users_cannot(make_client):
    client = make_client()
    body = {"surface": "cli", "user_id": "x", "username": "erwan", "password": PASSWORD}
    assert client.post("/v1/accounts/register", json=body, headers={"Authorization": "Bearer secret-cli"}).status_code == 403


# --- spaces, members, chime in ---------------------------------------------------------------------------


def test_the_roster_and_the_people_mentioned_reach_the_prompt(make_client):
    client = make_client(say("Hi all"))
    memory = client.app.state.memory
    register(client)
    register(client, user_id="222", username="bob", name="Bob")
    memory.add_fact(memory.find_person("discord", "222").id, "Bob plays chess")
    memory.resolve("discord", "333", "Ghost")  # known, but not signed in: left out
    roster = [{"user_id": "111", "name": "Erwan"}, {"user_id": "222", "name": "Bobby"}, {"user_id": "333", "name": "Ghost"}]
    reply = chat(client, message="hello", conversation=CHANNEL, space=GUILD, roster=roster, focus=["222", "333"])
    assert reply.status_code == 200
    messages, tools = client.backend.calls[0]
    system = messages[0]["content"]
    assert "@Erwan, @Bobby" in system and "Ghost" not in system
    assert "What you remember about Bobby (mentioned)" in system and "Bob plays chess" in system
    assert untimed(messages[-1]["content"]) == "Erwan: hello"  # in a group, everybody's messages carry a name
    assert "about_person" in [tool["function"]["name"] for tool in tools]


def test_the_instructions_of_a_server_replace_the_personality_there_only(make_client):
    client = make_client(say("a"), say("b"), say("c"))
    register(client)
    client.put("/v1/spaces", json={"surface": "discord", "spaces": [{"id": GUILD, "name": "Guild"}]}, headers=BOT)
    items = [{"text": "You are a  pirate.", "enabled": True}, {"text": "Never speak of cats.", "enabled": False}]
    done = client.patch(f"/v1/admin/spaces/{GUILD}", json={"instructions": items}, headers=ADMIN).json()
    assert [i["text"] for i in done["instructions"]] == ["You are a pirate.", "Never speak of cats."] and done["chime"] is None
    chat(client, message="hello", conversation=CHANNEL, space=GUILD)
    system = client.backend.calls[0][0][0]["content"]
    assert "- You are a pirate." in system and "cats" not in system and "helpful personal AI assistant" not in system
    chat(client, message="in private")  # no space: the usual personality
    assert "pirate" not in client.backend.calls[1][0][0]["content"]
    client.patch(f"/v1/admin/spaces/{GUILD}", json={"instructions": []}, headers=ADMIN)  # nothing defined: the default
    chat(client, message="again", conversation=CHANNEL, space=GUILD)
    assert "pirate" not in client.backend.calls[2][0][0]["content"]
    assert client.patch("/v1/admin/spaces/discord:guild:404", json={"instructions": []}, headers=ADMIN).status_code == 404


def test_about_person_is_only_offered_with_other_people(make_client):
    client = make_client(say("hi"))
    register(client)
    chat(client)
    assert "about_person" not in [tool["function"]["name"] for tool in client.backend.calls[0][1]]


def test_about_person_reads_a_member_of_the_roster_only(make_client):
    client = make_client([LlmChunk(tool_calls=[ToolCall("about_person", {"name": "bob"})])], say("He plays chess"))
    memory = client.app.state.memory
    register(client)
    register(client, user_id="222", username="bob", name="Bob")
    memory.add_fact(memory.find_person("discord", "222").id, "Bob plays chess")
    roster = [{"user_id": "111", "name": "Erwan"}, {"user_id": "222", "name": "Bob"}]
    chat(client, message="what about bob?", conversation=CHANNEL, space=GUILD, roster=roster)
    result = client.backend.calls[1][0][-1]
    assert result["role"] == "tool" and "Bob plays chess" in result["content"]


def test_a_space_must_be_of_the_surface(make_client):
    client = make_client()
    register(client)
    assert chat(client, conversation=CHANNEL, space="cli:guild:1").status_code == 403
    assert chat(client, roster=[{"user_id": "111"}]).status_code == 422  # a roster needs a space


def test_an_observed_message_is_stored_without_the_model(make_client):
    client = make_client()
    register(client)
    done = chat(client, message="talking to Bob", conversation=CHANNEL, space=GUILD, mode="observe").json()
    assert done["reply"] == "" and done["observed"] is True
    assert client.backend.calls == []
    assert [m.content for m in client.app.state.memory.history(CHANNEL, 10)] == ["talking to Bob"]


def test_maybe_is_only_observed_where_chime_in_is_off(make_client):
    client = make_client(say("<pass>"), say("Actually, it is 42."))
    memory = client.app.state.memory
    register(client)
    memory.sync_spaces("discord", [(GUILD, "Guild")])
    off = chat(client, message="m1", conversation=CHANNEL, space=GUILD, mode="maybe").json()
    assert off.get("observed") and client.backend.calls == []

    memory.set_space_chime(GUILD, True)
    declined = chat(client, message="m2", conversation=CHANNEL, space=GUILD, mode="maybe").json()
    assert declined["reply"] == "" and declined["passed"] is True
    assert "not addressed to you" in client.backend.calls[0][0][-1]["content"]
    answered = chat(client, message="what is 6x7?", conversation=CHANNEL, space=GUILD, mode="maybe").json()
    assert answered["reply"] == "Actually, it is 42." and answered["passed"] is False
    assert [m.content for m in memory.history(CHANNEL, 10)] == ["m1", "m2", "what is 6x7?", "Actually, it is 42."]


@pytest.mark.parametrize("reply", ["<pass>", " <PASS>. ", "", "pass", "`<pass>`"])
def test_what_counts_as_passing(reply):
    assert passed(reply)


def test_a_real_answer_does_not_pass():
    assert not passed("I pass the salt") and not passed("Passing by: hi!")


def test_spaces_are_synced_and_switched_by_an_administrator(make_client):
    client = make_client()
    body = {"surface": "discord", "spaces": [{"id": GUILD, "name": "Guild"}, {"id": "discord:guild:2", "name": "Two"}]}
    synced = client.put("/v1/spaces", json=body, headers=BOT).json()
    assert synced["default_chime"] is False and [s["id"] for s in synced["spaces"]] == [GUILD, "discord:guild:2"]
    assert client.put("/v1/spaces", json={"surface": "discord", "spaces": [{"id": "cli:x"}]}, headers=BOT).status_code == 422

    assert client.patch(f"/v1/admin/spaces/{GUILD}", json={"chime": True}, headers=ADMIN).json()["chime_effective"] is True
    assert client.patch("/v1/admin/spaces", json={"default_chime": True}, headers=ADMIN).status_code == 200
    listed = {s["id"]: s for s in client.get("/v1/admin/spaces", headers=ADMIN).json()["spaces"]}
    assert listed["discord:guild:2"]["chime"] is None and listed["discord:guild:2"]["chime_effective"] is True
    assert client.patch(f"/v1/admin/spaces/{GUILD}", json={"chime": True}, headers=BOT).status_code == 401

    client.put("/v1/spaces", json={"surface": "discord", "spaces": [{"id": GUILD, "name": "Renamed"}]}, headers=BOT)
    after = {s.id: s for s in client.app.state.memory.spaces()}
    assert after[GUILD].name == "Renamed" and after[GUILD].chime is True and not after["discord:guild:2"].present


async def test_the_chime_command(memory, discord_settings):
    from clara.commands import CommandContext

    ctx = CommandContext(discord_settings, memory, None, None, 0.0, "here")  # type: ignore[arg-type]
    memory.sync_spaces("discord", [(GUILD, "My Guild")])
    assert "Default: off" in (await registry.execute("/chime", ctx)).output
    assert "now on" in (await registry.execute("/chime my guild on", ctx)).output  # a name with spaces, any case
    await registry.execute(f"/chime {GUILD} off", ctx)
    assert not memory.chime_allowed(GUILD)
    await registry.execute(f"/chime {GUILD} on", ctx)
    assert memory.chime_allowed(GUILD)
    await registry.execute(f"/chime {GUILD} default", ctx)
    assert not memory.chime_allowed(GUILD)
    await registry.execute("/chime default on", ctx)
    assert memory.chime_allowed(GUILD) and memory.chime_allowed("discord:guild:unknown")
    assert "Usage" in (await registry.execute("/chime nope maybe", ctx)).output


# --- relationship ----------------------------------------------------------------------------------------


def test_clara_adjusts_the_relationship_once_per_answer(make_client):
    client = make_client(call("adjust_relation", change=99, reason="kind"), call("adjust_relation", change=4), say("Thanks!"))
    register(client)
    chat(client, message="you are great")
    person = client.app.state.memory.find_person("discord", "111")
    assert client.app.state.memory.relation(person.id) == 60  # 50 + at most 10
    second_result = client.backend.calls[2][0][-1]["content"]
    assert "already adjusted" in second_result


def test_the_relationship_sets_the_tone_in_the_prompt(make_client):
    client = make_client(say("a"), say("b"))
    register(client)
    chat(client)
    assert "None yet: be neutral and polite." in client.backend.calls[0][0][0]["content"]
    memory = client.app.state.memory
    memory.set_relation(memory.find_person("discord", "111").id, 20)
    chat(client)
    assert "20/100 (very bad)" in client.backend.calls[1][0][0]["content"]


async def test_the_relation_command_and_the_admin_route(make_client, memory, discord_settings):
    from clara.commands import CommandContext

    ctx = CommandContext(discord_settings, memory, None, None, 0.0, "here")  # type: ignore[arg-type]
    person = memory.resolve("cli", "erwan", "Erwan Le Gall")
    assert "none yet" in (await registry.execute("/relation Erwan Le Gall", ctx)).output
    assert "70/100" in (await registry.execute(f"/relation {person.id} 70", ctx)).output
    assert "60/100" in (await registry.execute(f"/relation {person.id} -10", ctx)).output
    assert "60/100" in (await registry.execute("/relation", ctx)).output
    await registry.execute(f"/relation {person.id} reset", ctx)
    assert memory.relation(person.id) is None
    assert "0 to 100" in (await registry.execute(f"/relation {person.id} 150", ctx)).output

    client = make_client()
    other = client.app.state.memory.resolve("cli", "zoe", "Zoe")
    done = client.patch(f"/v1/admin/people/{other.id}", json={"relation": 90}, headers=ADMIN).json()
    assert done["relation"] == 90 and done["relation_label"] == "excellent"
    people = client.get("/v1/admin/people", headers=ADMIN).json()["people"]
    assert next(p for p in people if p["id"] == other.id)["relation"] == 90
    assert client.patch(f"/v1/admin/people/{other.id}", json={"relation": 101}, headers=ADMIN).status_code == 422


def test_merging_people_keeps_a_relationship(memory):
    a, b = memory.resolve("cli", "a", "A"), memory.resolve("app", "b", "B")
    memory.set_relation(a.id, 80)
    memory.link_account("cli", "a", b)
    assert memory.relation(b.id) == 80


# --- the event stream of the whole surface ---------------------------------------------------------------


async def test_the_surface_stream_gives_each_event_with_its_accounts(memory):
    notifier = Notifier(memory)
    erwan = memory.resolve("discord", "111", "Erwan")
    memory.link_account("discord", "112", erwan)  # a second Discord account of the same person
    zoe = memory.resolve("app", "zoe", "Zoe")  # no Discord account

    def recipients(person_id):
        return [e for s, e in memory.accounts_of(person_id) if s == "discord"]

    stream = notifier.surface_events("bot", "discord", recipients)
    assert (await anext(stream))["type"] == "server"
    notifier.notify(zoe.id, "for zoe")
    notifier.notify(erwan.id, "only in the app", targets=["app"])
    notifier.broadcast("everybody")
    notifier.notify(erwan.id, "for erwan")
    event = await asyncio.wait_for(anext(stream), 2)
    assert event["text"] == "for erwan" and event["accounts"] == ["111", "112"]
    await stream.aclose()


def test_the_surface_stream_is_for_clients_of_that_surface(make_client):
    client = make_client()
    response = client.get(
        "/v1/notifications/stream", params={"surface": "discord", "user_id": "1", "all": "true"}, headers=BOT
    )
    assert response.status_code == 422


def test_a_group_request_in_the_agent_carries_roster_people():
    request = ChatRequest("discord", "1", None, "hi", space=GUILD, roster=(Person(1, "A"),))
    assert request.group and request.roster[0].name == "A"
