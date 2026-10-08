"""The usage log: one row per answer with tokens in and out, Discord told apart, and the administration's view."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FakeBackend, call, fake_providers, say
from fastapi.testclient import TestClient

from clara.agent import Agent, ChatRequest
from clara.llm import LlmChunk
from clara.prompt import SystemPrompt
from clara.server import create_app
from clara.tools import default_toolbox
from clara.usagelog import UsageLog

PASSWORD = "correct horse battery"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def usage_log(memory, clock):
    return UsageLog(memory, clock)


def make_agent(memory, tmp_path: Path, backend, usage_log, **options) -> Agent:
    return Agent(
        memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"), usage_log=usage_log, **options
    )


async def run(agent: Agent, owner: str = "", **fields) -> list[dict]:
    request = ChatRequest(**{"surface": "cli", "user_id": "erwan", "user_name": "Erwan", "message": "hi", **fields})
    return [event async for event in agent.turn(request, owner)]


# --- the log ---------------------------------------------------------------------------------------------


async def test_an_answer_is_one_row_with_the_tokens_in_and_out(memory, tmp_path, usage_log):
    agent = make_agent(memory, tmp_path, FakeBackend(say("one", prompt_tokens=100, completion_tokens=7)), usage_log)
    await run(agent, conversation="cli:thread")
    [call] = usage_log.history()["calls"]
    assert (call["prompt_tokens"], call["completion_tokens"], call["rounds"]) == (100, 7, 1)
    assert (call["kind"], call["surface"], call["group"], call["conversation"]) == ("message", "cli", "other", "cli:thread")
    assert call["person"]["name"] == "Erwan" and call["model"] == "fake" and call["estimated"] is False


async def test_the_rounds_of_a_tool_loop_are_added_up_in_one_row(memory, tmp_path, usage_log):
    backend = FakeBackend(
        [*call("recall_facts", query="tea"), LlmChunk(prompt_tokens=50, completion_tokens=5)],
        say("done", prompt_tokens=70, completion_tokens=2),
    )
    agent = make_agent(memory, tmp_path, backend, usage_log)
    await run(agent)
    [row] = usage_log.history()["calls"]
    assert (row["prompt_tokens"], row["completion_tokens"], row["rounds"]) == (120, 7, 2)


async def test_a_model_that_reports_nothing_is_estimated_and_says_so(memory, tmp_path, usage_log):
    agent = make_agent(memory, tmp_path, FakeBackend(say("a long enough answer", prompt_tokens=0, completion_tokens=0)), usage_log)
    await run(agent)
    [call] = usage_log.history()["calls"]
    assert call["estimated"] is True and call["prompt_tokens"] > 0 and call["completion_tokens"] > 0


async def test_a_model_that_fails_logs_nothing(memory, tmp_path, usage_log):
    class Broken(FakeBackend):
        async def stream(self, messages, tools):
            raise RuntimeError("the model is down")
            yield  # pragma: no cover

    with pytest.raises(RuntimeError):
        await run(make_agent(memory, tmp_path, Broken(), usage_log))
    assert usage_log.history()["calls"] == []


async def test_what_the_model_did_is_logged_when_the_client_leaves(memory, tmp_path, usage_log):
    agent = make_agent(memory, tmp_path, FakeBackend(say("a long answer", "and more", "and more")), usage_log)
    stream = agent.turn(ChatRequest("cli", "erwan", "Erwan", "hi"))
    async for event in stream:
        if event["type"] == "token":
            break
    await stream.aclose()
    assert len(usage_log.history()["calls"]) == 1


async def test_work_the_server_starts_itself_is_tagged_scheduled(memory, tmp_path, usage_log):
    agent = make_agent(memory, tmp_path, FakeBackend(say("a"), say("b")), usage_log)
    await run(agent)
    await run(agent, owner="reminders")
    assert [c["kind"] for c in usage_log.history()["calls"]] == ["scheduled", "message"]  # newest first


async def test_a_title_is_logged_for_the_one_person_in_the_conversation_and_costs_no_credit(memory, tmp_path, usage_log):
    erwan = memory.resolve("app", "pc", "Erwan")
    memory.add_exchange("app:pc:1", erwan.id, "How do pointers work in C?", "They hold addresses.")
    agent = make_agent(memory, tmp_path, FakeBackend(say("Pointers", prompt_tokens=30, completion_tokens=2)), usage_log)
    await agent.title("app:pc:1")
    [call] = usage_log.history()["calls"]
    assert (call["kind"], call["surface"], call["person"]["id"], call["credits"]) == ("title", "app", erwan.id, 0)
    assert (call["prompt_tokens"], call["completion_tokens"]) == (30, 2)


async def test_nothing_is_logged_without_a_log(memory, tmp_path):
    agent = Agent(memory, FakeBackend(say("a")), default_toolbox(), SystemPrompt(tmp_path / "none.md"))
    assert (await run(agent))[-1]["reply"] == "a"


# --- the reading -----------------------------------------------------------------------------------------


def fill(memory, usage_log):
    erwan, alice = memory.resolve("cli", "erwan", "Erwan"), memory.resolve("discord", "9", "Alice")
    rows = [
        (erwan.id, "message", "cli", "local:small", 100, 10), (erwan.id, "message", "cli", "local:small", 50, 5),
        (erwan.id, "message", "discord", "cloud:big", 1000, 100), (erwan.id, "compaction", "cli", "local:small", 400, 40),
        (alice.id, "message", "discord", "cloud:big", 10, 1),
    ]
    for person, kind, surface, ref, prompt, completion in rows:
        usage_log.record(person, kind, surface, f"{surface}:x", ref, ref.partition(":")[2], ref.partition(":")[0], prompt, completion)
    return erwan, alice


def test_each_user_has_their_tokens_discord_apart_and_their_preferred_models(memory, usage_log):
    fill(memory, usage_log)
    erwan, alice = usage_log.per_user()
    assert erwan["person"]["name"] == "Erwan" and alice["person"]["name"] == "Alice"  # most tokens first
    assert (erwan["prompt_tokens"], erwan["completion_tokens"], erwan["answers"]) == (1550, 155, 3)
    assert erwan["discord"] == {"answers": 1, "prompt_tokens": 1000, "completion_tokens": 100}
    assert erwan["other"] == {"answers": 2, "prompt_tokens": 550, "completion_tokens": 55}  # the compaction counts in tokens
    assert set(erwan["surfaces"]) == {"cli", "discord"}
    assert [(m["model"], m["answers"]) for m in erwan["models"]] == [("local:small", 2), ("cloud:big", 1)]
    assert alice["other"]["prompt_tokens"] == 0 and alice["discord"]["answers"] == 1


def test_the_totals_keep_discord_apart(memory, usage_log):
    fill(memory, usage_log)
    totals = usage_log.totals()
    assert totals["discord"]["prompt_tokens"] == 1010 and totals["other"]["prompt_tokens"] == 550


def test_the_period_keeps_only_recent_rows(memory, usage_log, clock):
    erwan = memory.resolve("cli", "erwan", "Erwan")
    usage_log.record(erwan.id, "message", "cli", "c", "", "m", "p", 10, 1)
    clock.now += timedelta(days=40)
    usage_log.record(erwan.id, "message", "cli", "c", "", "m", "p", 20, 2)
    assert usage_log.per_user()[0]["prompt_tokens"] == 30
    assert usage_log.per_user(days=7)[0]["prompt_tokens"] == 20
    assert usage_log.history(days=7)["totals"]["calls"] == 1


def test_the_history_is_filtered_and_paged(memory, usage_log):
    erwan, alice = fill(memory, usage_log)
    assert [c["surface"] for c in usage_log.history(group="discord")["calls"]] == ["discord", "discord"]
    assert len(usage_log.history(group="other")["calls"]) == 3
    assert usage_log.history(person_id=alice.id)["totals"] == {"calls": 1, "prompt_tokens": 10, "completion_tokens": 1}
    assert len(usage_log.history(kind="compaction")["calls"]) == 1
    assert len(usage_log.history(model="cloud:big")["calls"]) == 2
    first = usage_log.history(limit=2)
    assert len(first["calls"]) == 2 and first["next"] is not None and first["totals"]["calls"] == 5
    second = usage_log.history(limit=2, before=first["next"])
    third = usage_log.history(limit=2, before=second["next"])
    ids = [c["id"] for c in first["calls"] + second["calls"] + third["calls"]]
    assert ids == sorted(ids, reverse=True) and len(ids) == 5 and third["next"] is None


def test_erasing_a_person_erases_their_log_and_merging_keeps_it(memory, usage_log):
    erwan, alice = fill(memory, usage_log)
    memory.delete_person(alice.id)
    assert [c["person"]["name"] for c in usage_log.history()["calls"]] == ["Erwan"] * 4
    other = memory.resolve("web", "e2", "Erwan2")
    usage_log.record(other.id, "message", "web", "c", "", "m", "p", 5, 1)
    memory._merge(other.id, erwan.id)  # what linking two accounts does
    assert usage_log.history(person_id=erwan.id)["totals"]["calls"] == 5


# --- the API ---------------------------------------------------------------------------------------------


@pytest.fixture
def http(settings):
    backend = FakeBackend(*[say("ok", prompt_tokens=20, completion_tokens=4) for _ in range(40)])
    with TestClient(create_app(settings, fake_providers(settings, backend))) as client:
        users = client.app.state.users
        users.create("root", PASSWORD, admin=True)
        users.create("erwan", PASSWORD)
        yield client


def bearer(http, name="erwan", surface="app") -> dict:
    answer = http.post("/v1/auth/login", json={"username": name, "password": PASSWORD, "surface": surface})
    return {"Authorization": f"Bearer {answer.json()['token']}"}


def test_only_administrators_read_the_usage(http):
    erwan = bearer(http)
    assert http.get("/v1/admin/usage", headers=erwan).status_code == 403
    assert http.get("/v1/admin/usage/history", headers=erwan).status_code == 403
    assert http.get("/v1/admin/usage").status_code in (401, 403)


def test_the_administration_sees_each_call_and_the_sums(http):
    admin, erwan = bearer(http, "root"), bearer(http)
    app = http.app.state
    app.memory.link_account("discord", "erwan", app.users.person_of(app.users.get("erwan")))  # what signing in does
    bot = {"Authorization": "Bearer secret-discord"}  # the Discord bot speaks for its own surface
    assert http.post("/v1/chat", json={"surface": "app", "user_id": "erwan", "message": "hi"}, headers=erwan).status_code == 200
    assert http.post("/v1/chat", json={"surface": "discord", "user_id": "erwan", "message": "hi"}, headers=bot).status_code == 200
    usage = http.get("/v1/admin/usage", headers=admin).json()
    [entry] = [u for u in usage["users"] if u["user"] == "erwan"]
    assert (entry["prompt_tokens"], entry["completion_tokens"], entry["answers"]) == (40, 8, 2)
    assert entry["discord"]["prompt_tokens"] == 20 and entry["other"]["prompt_tokens"] == 20
    assert usage["totals"]["discord"]["answers"] == 1
    history = http.get("/v1/admin/usage/history", params={"group": "discord", "person": entry["person"]["id"]}, headers=admin).json()
    assert [(c["surface"], c["prompt_tokens"]) for c in history["calls"]] == [("discord", 20)]
    assert http.get("/v1/admin/usage/history", params={"group": "nope"}, headers=admin).status_code == 422


def test_a_user_sees_only_their_own_usage(http):
    users, memory = http.app.state.users, http.app.state.memory
    users.create("alice", PASSWORD)
    admin, erwan, alice = bearer(http, "root"), bearer(http), bearer(http, "alice")
    bot = {"Authorization": "Bearer secret-discord"}
    memory.link_account("discord", "erwan", users.person_of(users.get("erwan")))
    body = {"surface": "app", "user_id": "erwan", "message": "hi"}
    assert http.post("/v1/chat", json=body, headers=erwan).status_code == 200
    assert http.post("/v1/chat", json=body | {"surface": "discord"}, headers=bot).status_code == 200
    assert http.post("/v1/chat", json=body | {"user_id": "alice"}, headers=alice).status_code == 200

    mine = http.get("/v1/me/usage", headers=erwan).json()
    assert (mine["usage"]["prompt_tokens"], mine["usage"]["answers"]) == (40, 2)
    assert mine["usage"]["discord"]["prompt_tokens"] == 20 and mine["usage"]["other"]["prompt_tokens"] == 20
    assert "used" in mine["quota"]
    history = http.get("/v1/me/usage/history", headers=erwan).json()
    assert {c["person"]["name"] for c in history["calls"]} == {mine["usage"]["person"]["name"]}
    assert history["totals"]["calls"] == 2
    only = http.get("/v1/me/usage/history", params={"group": "discord"}, headers=erwan).json()
    assert [c["surface"] for c in only["calls"]] == ["discord"]
    # somebody who never talked has nothing, and cannot ask for another person's calls
    assert http.get("/v1/me/usage", headers=admin).json()["usage"] is None
    assert http.get("/v1/me/usage/history", params={"person": 1}, headers=admin).json()["totals"]["calls"] == 0
    assert http.get("/v1/me/usage").status_code in (401, 403)
    assert http.get("/v1/me/usage", headers=bot).status_code == 403  # a client token is nobody in particular
