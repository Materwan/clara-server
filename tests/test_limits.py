"""Daily token limits: who has one, how tokens are counted, what happens at the limit, how an administrator sets it."""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import FakeBackend, fake_providers, say
from fastapi.testclient import TestClient

from clara.agent import Agent, ChatRequest
from clara.commands import CommandContext, registry
from clara.limits import LIMIT_MARK, UsageLimitReached, UsageLimits, parse_limit
from clara.prompt import SystemPrompt
from clara.server import create_app
from clara.tools import default_toolbox
from clara.users import Users

PASSWORD = "correct horse battery"
TURN = 13  # what say() reports: 10 prompt tokens and 3 completion tokens


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def users(memory):
    return Users(memory)


@pytest.fixture
def limits(memory, clock):
    return UsageLimits(memory, clock=clock)


# --- the limits ------------------------------------------------------------------------------------------


def test_a_limit_is_written_the_way_an_operator_would():
    assert [parse_limit(text) for text in ("500000", "500k", "1.1k", "2M", "1,000", "off", "none", "0")] == [
        500_000, 500_000, 1_100, 2_000_000, 1_000, 0, 0, 0,
    ]
    for bad in ("", "abc", "-5", "0.5", "nan", "1e400"):
        with pytest.raises(ValueError):
            parse_limit(bad)


def test_nobody_is_limited_until_a_default_or_a_limit_is_set(memory, users, limits):
    user = users.create("erwan", PASSWORD)
    assert limits.limit_of(user.person_id) is None
    limits.set_default(1000)
    assert limits.limit_of(user.person_id) == 1000
    users.set_token_limit("erwan", 50)
    assert limits.limit_of(user.person_id) == 50
    users.set_token_limit("erwan", 0)  # 0 is "no limit", even when the default is one
    assert limits.limit_of(user.person_id) is None
    users.set_token_limit("erwan", None)  # back to following the default
    assert limits.limit_of(user.person_id) == 1000


def test_the_default_comes_from_the_settings_until_an_operator_changes_it(memory, clock):
    limits = UsageLimits(memory, default=700, clock=clock)
    assert limits.default() == 700
    limits.set_default(0)
    assert limits.default() == 0  # an explicit "no limit" beats the environment


def test_an_administrator_has_no_limit_whatever_is_set(memory, users, limits):
    admin = users.create("root", PASSWORD, admin=True)
    limits.set_default(10)
    users.set_token_limit("root", 5)
    assert limits.limit_of(admin.person_id) is None
    users.create("second", PASSWORD, admin=True)  # the last administrator cannot stop being one
    users.set_admin("root", False)
    assert limits.limit_of(admin.person_id) == 5  # only while they are an administrator


def test_a_person_without_a_user_follows_the_default(memory, limits):
    person = memory.resolve("cli", "somebody", "Somebody")
    assert limits.limit_of(person.id) is None
    limits.set_default(300)
    assert limits.limit_of(person.id) == 300


def test_tokens_are_counted_per_day_and_the_day_starts_again_at_midnight_utc(memory, limits, clock):
    person = memory.resolve("cli", "erwan", "Erwan")
    limits.set_default(100)
    limits.record(person.id, 60)
    limits.record(person.id, 30)
    assert limits.quota(person.id).used == 90
    limits.record(person.id, 20)
    with pytest.raises(UsageLimitReached) as stopped:
        limits.check(person.id)
    assert LIMIT_MARK in str(stopped.value) and "12 h 00 min" in str(stopped.value)
    assert stopped.value.retry_after == 12 * 3600
    clock.now += timedelta(hours=12)  # midnight
    assert limits.quota(person.id).used == 0
    assert limits.check(person.id).remaining == 100


def test_old_days_are_forgotten(memory, limits, clock):
    person = memory.resolve("cli", "erwan", "Erwan")
    limits.record(person.id, 5)
    clock.now += timedelta(days=401)
    limits.record(person.id, 5)
    assert memory.database.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 1


def test_erasing_a_person_forgets_their_usage_and_a_merge_adds_it_up(memory, limits):
    first = memory.resolve("cli", "erwan", "Erwan")
    second = memory.resolve("discord", "42", "Erwan on Discord")
    limits.record(first.id, 10)
    limits.record(second.id, 5)
    memory.link_account("discord", "42", first, force=True)
    assert limits.used(first.id) == 15
    memory.delete_person(first.id)
    assert memory.database.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0


# --- the agent -------------------------------------------------------------------------------------------


def make_agent(memory, tmp_path: Path, backend, limits, **options) -> Agent:
    return Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"), limits=limits, **options)


async def run(agent: Agent, **fields) -> list[dict]:
    request = ChatRequest(**{"surface": "cli", "user_id": "erwan", "user_name": "Erwan", "message": "hi", **fields})
    return [event async for event in agent.turn(request)]


async def test_every_round_is_counted_and_the_answer_says_where_the_person_stands(memory, tmp_path, limits):
    limits.set_default(100)
    agent = make_agent(memory, tmp_path, FakeBackend(say("one"), say("two")), limits)
    first = (await run(agent))[-1]
    assert first["quota"]["used"] == TURN and first["quota"]["limit"] == 100 and first["quota"]["remaining"] == 87
    second = (await run(agent))[-1]
    assert second["quota"]["used"] == 2 * TURN


async def test_no_limits_means_no_quota_in_the_answer(memory, tmp_path):
    agent = Agent(memory, FakeBackend(say("one")), default_toolbox(), SystemPrompt(tmp_path / "none.md"))
    assert (await run(agent))[-1]["quota"] is None


async def test_the_answer_that_crosses_the_limit_is_finished_and_the_next_one_is_refused(memory, tmp_path, limits):
    limits.set_default(20)
    backend = FakeBackend(say("one"), say("two"), say("never asked"))
    agent = make_agent(memory, tmp_path, backend, limits)
    await run(agent)  # 13 of 20
    assert (await run(agent))[-1]["reply"] == "two"  # 26 of 20: this one was allowed to start
    with pytest.raises(UsageLimitReached):
        await run(agent)
    assert len(backend.calls) == 2  # the model was not asked, and nothing was stored for the refused message
    assert [m.content for m in memory.history("cli:erwan", 10)] == ["hi", "one", "hi", "two"]


async def test_a_refusal_of_one_person_does_not_touch_the_others(memory, tmp_path, limits):
    limits.set_default(5)
    agent = make_agent(memory, tmp_path, FakeBackend(say("a"), say("b")), limits)
    await run(agent)
    with pytest.raises(UsageLimitReached):
        await run(agent)
    assert (await run(agent, user_id="alice", user_name="Alice"))[-1]["reply"] == "b"


async def test_a_message_that_was_not_for_clara_is_only_kept_when_the_person_is_out_of_tokens(memory, tmp_path, limits):
    limits.set_default(5)
    backend = FakeBackend(say("a"), say("never"))
    agent = make_agent(memory, tmp_path, backend, limits)
    await run(agent)
    chimed = await run(agent, mode="maybe", message="thinking aloud")
    assert chimed[-1]["observed"] is True and chimed[-1]["reply"] == ""
    observed = await run(agent, mode="observe", message="just saying")
    assert observed[-1]["observed"] is True
    assert len(backend.calls) == 1
    assert "thinking aloud" in [m.content for m in memory.history("cli:erwan", 10)]


async def test_an_administrator_is_never_refused(memory, tmp_path, limits, users):
    root = users.create("root", PASSWORD, admin=True)
    memory.link_account("app", "root", users.person_of(root))  # what logging in does
    limits.set_default(1)
    agent = make_agent(memory, tmp_path, FakeBackend(say("a"), say("b"), say("c")), limits)
    for _ in range(3):
        assert (await run(agent, surface="app", user_id="root"))[-1]["quota"]["limit"] is None


async def test_what_the_model_did_is_counted_when_the_client_leaves_but_not_when_the_model_fails(memory, tmp_path, limits):
    limits.set_default(10_000)
    agent = make_agent(memory, tmp_path, FakeBackend(say("a long answer", "and more", "and more")), limits)
    request = ChatRequest("cli", "erwan", "Erwan", "hi")
    stream = agent.turn(request)
    async for event in stream:
        if event["type"] == "token":
            break  # the client goes away in the middle of the answer
    await stream.aclose()
    person = memory.find_person("cli", "erwan")
    assert limits.used(person.id) > 0  # estimated: the model had not said how much yet

    class Broken(FakeBackend):
        async def stream(self, messages, tools):
            raise RuntimeError("the model is down")
            yield  # pragma: no cover

    before = limits.used(person.id)
    broken = make_agent(memory, tmp_path, Broken(), limits)
    with pytest.raises(RuntimeError):
        await run(broken)
    assert limits.used(person.id) == before


# --- the API ---------------------------------------------------------------------------------------------


@pytest.fixture
def http(settings):
    backend = FakeBackend(*[say("ok") for _ in range(40)])
    with TestClient(create_app(settings, fake_providers(settings, backend))) as client:
        users = client.app.state.users
        users.create("root", PASSWORD, admin=True)
        users.create("erwan", PASSWORD)
        users.create("alice", PASSWORD)
        yield client


def bearer(http, name="erwan", surface="app") -> dict:
    answer = http.post("/v1/auth/login", json={"username": name, "password": PASSWORD, "surface": surface})
    return {"Authorization": f"Bearer {answer.json()['token']}"}


def chat(http, headers, user="erwan", **fields):
    return http.post("/v1/chat", json={"surface": "app", "user_id": user, "message": "hi", **fields}, headers=headers)


def patch(http, name, headers, **body):
    return http.patch(f"/v1/admin/users/{name}", json=body, headers=headers)


def test_an_administrator_limits_a_user_and_the_user_is_refused_when_it_is_used(http):
    admin, erwan = bearer(http, "root"), bearer(http)
    assert patch(http, "erwan", admin, token_limit=20).json()["user"]["usage"]["limit"] == 20
    assert chat(http, erwan).json()["quota"]["used"] == TURN
    assert chat(http, erwan).status_code == 200  # 26 of 20: allowed to start
    refused = chat(http, erwan)
    assert refused.status_code == 429
    assert LIMIT_MARK in refused.json()["detail"] and int(refused.headers["retry-after"]) > 0
    assert chat(http, bearer(http, "alice"), user="alice").status_code == 200  # somebody else's day is not his
    assert patch(http, "erwan", admin, token_limit=0).status_code == 200  # no limit: back in
    assert chat(http, erwan).status_code == 200


def test_the_administrator_never_has_one_and_sees_everybody_s_usage(http):
    admin, erwan = bearer(http, "root"), bearer(http)
    http.app.state.limits.set_default(1)
    for _ in range(3):
        assert chat(http, admin, user="root").status_code == 200
    assert chat(http, erwan).status_code == 200
    assert chat(http, erwan).status_code == 429
    listed = {u["name"]: u for u in http.get("/v1/admin/users", headers=admin).json()["users"]}
    assert listed["root"]["usage"]["limit"] is None and listed["root"]["usage"]["used"] == 3 * TURN
    assert listed["erwan"]["usage"] | {"resets_at": ""} == {
        "used": TURN, "limit": 1, "remaining": 0, "resets_at": "", "own_limit": None, "default_limit": 1,
    }
    assert listed["alice"]["usage"]["used"] == 0


def test_a_user_sees_their_own_usage_and_cannot_change_it(http):
    erwan = bearer(http)
    patch_admin = bearer(http, "root")
    patch(http, "erwan", patch_admin, token_limit=100)
    chat(http, erwan)
    me = http.get("/v1/auth/me", headers=erwan).json()
    assert (me["usage"]["used"], me["usage"]["limit"], me["usage"]["remaining"]) == (TURN, 100, 100 - TURN)
    assert patch(http, "erwan", erwan, token_limit=0).status_code == 403
    assert http.put("/v1/admin/limits/default", json={"tokens": 0}, headers=erwan).status_code == 403
    assert http.get("/v1/admin/limits", headers=erwan).status_code == 403


def test_the_default_is_changed_by_an_administrator_and_a_user_can_follow_it_or_not(http):
    admin, erwan = bearer(http, "root"), bearer(http)
    assert http.get("/v1/admin/limits", headers=admin).json() == {"default": None}
    assert http.put("/v1/admin/limits/default", json={"tokens": 15}, headers=admin).json() == {"default": 15}
    assert patch(http, "alice", admin, token_limit=1000).json()["user"]["usage"]["limit"] == 1000
    assert chat(http, erwan).status_code == 200 and chat(http, erwan).status_code == 200
    assert chat(http, erwan).status_code == 429  # the default applies to erwan, not to alice
    assert chat(http, bearer(http, "alice"), user="alice").status_code == 200
    assert patch(http, "alice", admin, follow_default_limit=True).json()["user"]["usage"]["own_limit"] is None
    assert http.put("/v1/admin/limits/default", json={"tokens": 0}, headers=admin).json() == {"default": None}
    assert chat(http, erwan).status_code == 200  # the day's tokens stay counted, the limit is gone
    assert http.put("/v1/admin/limits/default", json={"tokens": -1}, headers=admin).status_code == 422
    assert patch(http, "alice", admin, token_limit=5, follow_default_limit=True).status_code == 422
    assert patch(http, "alice", admin, token_limit=-5).status_code == 422


def test_a_stream_says_why_it_stopped(http):
    admin, erwan = bearer(http, "root"), bearer(http)
    patch(http, "erwan", admin, token_limit=1)
    chat(http, erwan)
    body = {"surface": "app", "user_id": "erwan", "message": "hi"}
    answer = http.post("/v1/chat/stream", json=body, headers=erwan)
    assert answer.status_code == 200
    assert '"reason": "usage_limit"' in answer.text and LIMIT_MARK in answer.text


async def test_the_console_command_shows_and_sets_the_limits(settings, memory, tmp_path):
    users, limits = Users(memory), UsageLimits(memory)
    users.create("root", PASSWORD, admin=True)
    erwan = users.create("erwan", PASSWORD)
    providers = fake_providers(settings, FakeBackend())
    agent = Agent(memory, providers, default_toolbox(), SystemPrompt(tmp_path / "none.md"), limits=limits)
    ctx = CommandContext(settings, memory, agent, providers, time.monotonic(), "127.0.0.1:8765", users=users, limits=limits)

    async def command(line: str) -> str:
        return (await registry.execute(line, ctx)).output

    assert "Default: no limit" in await command("/limit")
    assert "now 2,000,000 tokens a day" in await command("/limit default 2m")
    assert "erwan: 500,000 tokens a day" in await command("/limit erwan 500k")
    assert limits.limit_of(erwan.person_id) == 500_000
    shown = await command("/limit")
    assert "own" in shown and "admin" in shown and "500,000" in shown
    assert "follows the default (2,000,000" in await command("/limit erwan default")
    assert "erwan: no limit" in await command("/limit erwan off")
    assert "administrator" in await command("/limit root 5")
    assert (await command("/limit nobody")).startswith("!")
    assert (await command("/limit erwan lots")).startswith("!")
    assert "Change it" in await command("/limit default")
