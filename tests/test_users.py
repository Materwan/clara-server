"""Users with a password: tokens bound to one user and one surface, cookies for the web site, administration."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeBackend, fake_providers, say
from fastapi.testclient import TestClient

from clara.commands import registry
from clara.server import create_app
from clara.users import UserError, Users, hash_password, verify_password

PASSWORD = "correct horse battery"
WEB = {"X-Clara-Web": "1"}
CLI_TOKEN = {"Authorization": "Bearer secret-cli"}
ADMIN_TOKEN = {"Authorization": "Bearer secret-admin"}


# --- the store -------------------------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def users(memory, clock):
    return Users(memory, session_days=90, clock=clock)


def test_a_password_is_kept_salted_and_checked():
    stored = hash_password(PASSWORD)
    assert PASSWORD not in stored and stored != hash_password(PASSWORD)
    assert verify_password(PASSWORD, stored) and not verify_password("wrong password!", stored)
    assert not verify_password(PASSWORD, "garbage") and not verify_password(PASSWORD, "md5$1$2$3$4$5")


def test_a_user_is_created_with_rules(users):
    user = users.create("Erwan", PASSWORD, admin=True)
    assert (user.name, user.is_admin) == ("erwan", True)
    for bad in ("", "a b", "-x", "x" * 33, "é"):
        with pytest.raises(UserError):
            users.create(bad, PASSWORD)
    with pytest.raises(UserError, match="at least"):
        users.create("bob", "short")
    with pytest.raises(UserError, match="already"):
        users.create("ERWAN", PASSWORD)


def test_a_new_user_is_the_person_an_account_of_that_name_already_is(users, memory):
    known = memory.resolve("cli", "erwan", "Erwan")
    memory.add_fact(known.id, "Likes tea")
    assert users.create("erwan", PASSWORD).person_id == known.id
    assert users.create("fresh", PASSWORD).person_id != known.id


def test_authentication_needs_the_password_and_an_enabled_user(users):
    users.create("erwan", PASSWORD)
    assert users.authenticate("Erwan", PASSWORD).name == "erwan"
    assert users.authenticate("erwan", "wrong password") is None
    assert users.authenticate("nobody", PASSWORD) is None
    users.create("root", PASSWORD, admin=True)
    users.set_disabled("erwan", True)
    assert users.authenticate("erwan", PASSWORD) is None


def test_tokens_are_random_hashed_and_expire_after_90_idle_days(users, clock, memory):
    user = users.create("erwan", PASSWORD)
    token, session = users.open_session(user, "app", "laptop", "10.0.0.1")
    assert token.startswith("clu_")
    stored = memory.database.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert token not in stored and len(stored) == 64
    assert users.lookup(token)[1].surface == "app"
    clock.now += timedelta(days=89)
    assert users.lookup(token) is not None  # used: renewed
    clock.now += timedelta(days=89)
    assert users.lookup(token) is not None
    clock.now += timedelta(days=91)
    assert users.lookup(token) is None
    assert users.prune() == 1


def test_session_days_zero_never_expires(memory, clock):
    forever = Users(memory, session_days=0, clock=clock)
    token, _ = forever.open_session(forever.create("erwan", PASSWORD), "app")
    clock.now += timedelta(days=5000)
    assert forever.lookup(token) is not None


def test_a_token_stops_working_when_revoked_disabled_or_the_password_changes(users):
    user = users.create("erwan", PASSWORD)
    users.create("root", PASSWORD, admin=True)
    first, one = users.open_session(user, "app")
    second, two = users.open_session(user, "web")
    assert users.revoke_session("erwan", one.id) and users.lookup(first) is None
    assert users.set_password("erwan", "another password", keep_session=two.id) == 0 and users.lookup(second)
    assert users.set_password("erwan", "third password!!") == 1 and users.lookup(second) is None
    third, _ = users.open_session(user, "cli")
    users.set_disabled("erwan", True)
    assert users.lookup(third) is None
    users.set_disabled("erwan", False)
    assert users.lookup(third) is None  # disabling signed it out for good


def test_the_last_administrator_cannot_be_removed_demoted_or_disabled(users):
    users.create("root", PASSWORD, admin=True)
    for action in (lambda: users.delete("root"), lambda: users.set_admin("root", False), lambda: users.set_disabled("root", True)):
        with pytest.raises(UserError, match="last administrator"):
            action()
    users.create("second", PASSWORD, admin=True)
    users.set_admin("root", False)


def test_erasing_or_merging_a_person_keeps_the_users_consistent(users, memory):
    keep = memory.resolve("cli", "keep", "Keep")
    gone = memory.resolve("cli", "gone", "Gone")
    memory.add_fact(keep.id, "x")
    users.create("gone", PASSWORD)
    users.open_session(users.get("gone"), "app")
    memory.link_account("web", "extra", keep)
    memory.delete_person(gone.id)
    assert users.get("gone") is None and memory.database.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    other = memory.resolve("cli", "other", "Other")
    users.create("other", PASSWORD)
    memory.link_account("cli", "other", keep, force=True)  # merges `other` into `keep`
    assert users.get("other").person_id == keep.id and other.id != keep.id


# --- the API ---------------------------------------------------------------------------------------------


@pytest.fixture
def http(settings):
    backend = FakeBackend(*[say("ok") for _ in range(20)])
    with TestClient(create_app(settings, fake_providers(settings, backend))) as client:
        users = client.app.state.users
        users.create("root", PASSWORD, admin=True)
        users.create("erwan", PASSWORD)
        users.create("alice", PASSWORD)
        yield client


def login(http, name="erwan", surface="app", password=PASSWORD, **headers):
    return http.post("/v1/auth/login", json={"username": name, "password": password, "surface": surface}, headers=headers)


def bearer(http, name="erwan", surface="app") -> dict:
    return {"Authorization": f"Bearer {login(http, name, surface).json()['token']}"}


def chat(http, headers, **fields):
    body = {"surface": "app", "user_id": "erwan", "message": "hi", **fields}
    return http.post("/v1/chat", json=body, headers=headers)


def test_logging_in_gives_a_token_that_works_for_that_user_and_surface(http):
    answer = login(http)
    assert answer.status_code == 200
    body = answer.json()
    assert body["token"].startswith("clu_") and body["user"]["name"] == "erwan" and body["surface"] == "app"
    assert chat(http, {"Authorization": f"Bearer {body['token']}"}).json()["reply"] == "ok"


def test_a_wrong_password_or_name_is_a_plain_401(http):
    assert login(http, password="wrong password").status_code == 401
    assert login(http, name="nobody").status_code == 401
    assert login(http, surface="discord").status_code == 403  # not a surface users may log in on


def test_the_token_is_bound_to_its_user_and_surface(http):
    headers = bearer(http)
    assert chat(http, headers, user_id="alice").status_code == 403  # the client says it is somebody else
    assert chat(http, headers, surface="discord").status_code == 403
    assert chat(http, headers, conversation="app:alice").status_code == 403
    assert chat(http, headers, conversation="app:erwan:2").status_code == 200
    assert http.get("/v1/memory/facts", params={"surface": "app", "user_id": "alice"}, headers=headers).status_code == 403
    assert http.get("/v1/memory/facts", params={"surface": "app", "user_id": "erwan"}, headers=headers).status_code == 200
    assert http.get("/v1/conversations/app:alice", headers=headers).status_code == 403
    assert http.get("/v1/conversations", params={"surface": "cli", "user_id": "erwan"}, headers=headers).status_code == 403
    assert http.post("/v1/accounts/link-code", json={"surface": "web", "user_id": "erwan"}, headers=headers).status_code == 403


def test_users_cannot_read_each_others_conversations(http):
    mine, theirs = bearer(http, "erwan"), bearer(http, "alice")
    chat(http, mine, conversation="app:erwan:one")
    chat(http, theirs, user_id="alice", conversation="app:alice:one")
    listed = http.get("/v1/conversations", params={"surface": "app", "user_id": "alice"}, headers=theirs).json()
    assert [c["id"] for c in listed["conversations"]] == ["app:alice:one"]
    params = {"surface": "app", "user_id": "alice"}
    assert http.get("/v1/conversations/app:erwan:one/messages", params=params, headers=theirs).status_code == 403


def test_the_web_site_and_the_app_share_their_conversations(http):
    app, web = bearer(http, surface="app"), bearer(http, surface="web")
    chat(http, app, conversation="app:erwan:one")
    chat(http, web, surface="web", conversation="web:erwan:two")
    for headers, surface in ((app, "app"), (web, "web")):
        params = {"surface": surface, "user_id": "erwan"}
        listed = http.get("/v1/conversations", params=params, headers=headers).json()["conversations"]
        assert {c["id"] for c in listed} == {"app:erwan:one", "web:erwan:two"}
    # each reads, continues, renames, moves and deletes the other's
    params = {"surface": "app", "user_id": "erwan"}
    assert http.get("/v1/conversations/web:erwan:two/messages", params=params, headers=app).status_code == 200
    assert chat(http, app, conversation="web:erwan:two").status_code == 200
    assert http.patch("/v1/conversations/web:erwan:two", json={**params, "title": "Shared"}, headers=app).status_code == 200
    web_params = {"surface": "web", "user_id": "erwan"}
    titles = {c["id"]: c["title"] for c in http.get("/v1/conversations", params=web_params, headers=web).json()["conversations"]}
    assert titles["web:erwan:two"] == "Shared"
    assert http.delete("/v1/conversations/app:erwan:one", params=web_params, headers=web).status_code == 200
    # but only the user's own, and only the two surfaces
    assert chat(http, app, conversation="web:alice:one").status_code == 403
    assert chat(http, app, conversation="cli:erwan:one").status_code == 403
    chat(http, bearer(http, "alice", "web"), surface="web", user_id="alice", conversation="web:alice:one")
    assert http.get("/v1/conversations/web:alice:one/messages", params=params, headers=app).status_code == 403
    cli = bearer(http, surface="cli")
    cli_params = {"surface": "cli", "user_id": "erwan"}
    assert http.get("/v1/conversations", params=cli_params, headers=cli).json()["conversations"] == []
    assert chat(http, cli, surface="cli", conversation="web:erwan:two").status_code == 403


def test_one_user_is_one_person_on_every_surface_without_link_codes(http):
    http.post("/v1/memory/facts", json={"surface": "app", "user_id": "erwan", "text": "Likes tea"}, headers=bearer(http))
    cli = bearer(http, surface="cli")
    facts = http.get("/v1/memory/facts", params={"surface": "cli", "user_id": "erwan"}, headers=cli).json()["facts"]
    assert [f["text"] for f in facts] == ["Likes tea"]


def test_existing_memories_of_an_account_with_that_name_follow_the_user(http):
    app = http.app
    old = app.state.memory.resolve("cli", "carol", "Carol")
    app.state.memory.add_fact(old.id, "Plays chess")
    app.state.users.create("carol", PASSWORD)
    headers = bearer(http, "carol", "app")
    facts = http.get("/v1/memory/facts", params={"surface": "app", "user_id": "carol"}, headers=headers).json()["facts"]
    assert [f["text"] for f in facts] == ["Plays chess"]


def test_two_filled_people_are_not_merged_by_logging_in(http):
    memory = http.app.state.memory
    memory.add_fact(memory.resolve("app", "dave", "Dave").id, "Old life")
    users = http.app.state.users
    users.create("dave2", PASSWORD)
    memory.add_fact(users.person_of(users.get("dave2")).id, "New life")
    memory.database.execute("UPDATE users SET name = 'dave' WHERE name = 'dave2'")  # the user takes the name later
    answer = login(http, "dave", "app")
    assert answer.status_code == 409 and "operator" in answer.json()["detail"]


def test_me_logout_and_sessions(http):
    headers = bearer(http)
    me = http.get("/v1/auth/me", headers=headers).json()
    assert me["name"] == "erwan" and me["surface"] == "app" and "app:erwan" in me["accounts"]
    second = bearer(http, surface="cli")
    devices = http.get("/v1/auth/sessions", headers=headers).json()["sessions"]
    assert len(devices) == 2 and sum(d["current"] for d in devices) == 1
    other = next(d for d in devices if not d["current"])
    assert http.delete(f"/v1/auth/sessions/{other['id']}", headers=headers).status_code == 200
    assert http.get("/v1/auth/me", headers=second).status_code == 401
    assert http.post("/v1/auth/logout", headers=headers).status_code == 200
    assert http.get("/v1/auth/me", headers=headers).status_code == 401


def test_client_tokens_cannot_use_the_account_routes(http):
    assert http.get("/v1/auth/me", headers=CLI_TOKEN).status_code == 403


def test_changing_the_password_signs_out_the_other_devices(http):
    mine, other = bearer(http), bearer(http, surface="cli")
    body = {"current_password": "wrong password", "new_password": "a brand new password"}
    assert http.post("/v1/auth/password", json=body, headers=mine).status_code == 403
    assert http.post("/v1/auth/password", json={**body, "current_password": PASSWORD, "new_password": "short"}, headers=mine).status_code == 422
    done = http.post("/v1/auth/password", json={**body, "current_password": PASSWORD}, headers=mine)
    assert done.json() == {"signed_out_elsewhere": 1}
    assert http.get("/v1/auth/me", headers=mine).status_code == 200
    assert http.get("/v1/auth/me", headers=other).status_code == 401
    assert login(http).status_code == 401 and login(http, password="a brand new password").status_code == 200


def test_the_web_site_gets_a_cookie_that_only_works_with_its_header(http):
    answer = login(http, surface="web", **WEB)
    assert "token" not in answer.json()
    cookie = answer.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "clara_session=clu_" in cookie
    body = {"surface": "web", "user_id": "erwan", "message": "hi"}
    assert http.post("/v1/chat", json=body, headers=WEB).status_code == 200  # the client keeps the cookie
    assert http.post("/v1/chat", json=body).status_code == 401  # a page of another site cannot add the header
    assert http.get("/v1/auth/me", headers=WEB).json()["surface"] == "web"
    http.post("/v1/auth/logout", headers=WEB)
    assert http.get("/v1/auth/me", headers=WEB).status_code == 401


def test_wrong_passwords_are_limited_per_address(settings):
    limited = replace(settings, auth_max_failures=3)
    with TestClient(create_app(limited, fake_providers(limited, FakeBackend())), client=("203.0.113.5", 1)) as http:
        http.app.state.users.create("erwan", PASSWORD)
        assert [login(http, password="wrong password").status_code for _ in range(3)] == [401, 401, 401]
        assert login(http).status_code == 429  # even the right one


@pytest.fixture
def open_http(settings):
    opened = replace(settings, web_signup=True)
    with TestClient(create_app(opened, fake_providers(opened, FakeBackend(*[say("ok") for _ in range(5)])))) as client:
        client.app.state.users.create("erwan", PASSWORD)
        yield client


def register(http, name="newcomer", password=PASSWORD, **headers):
    return http.post("/v1/auth/register", json={"username": name, "password": password}, headers=headers)


def test_signing_up_is_closed_unless_the_server_allows_it(http):
    assert http.get("/v1/auth/signup").json() == {"open": False}
    assert register(http).status_code == 403
    assert http.app.state.users.get("newcomer") is None


def test_the_sign_in_page_can_ask_whether_signing_up_is_open(open_http):
    assert open_http.get("/v1/auth/signup").json() == {"open": True}  # no login needed


def test_signing_up_makes_a_plain_user_who_is_logged_in_on_the_web(open_http):
    answer = register(open_http, **WEB)
    assert answer.status_code == 201
    body = answer.json()
    assert "token" not in body and body["surface"] == "web"
    assert body["user"]["name"] == "newcomer" and not body["user"]["is_admin"]
    assert "HttpOnly" in answer.headers["set-cookie"]
    assert open_http.get("/v1/auth/me", headers=WEB).json()["name"] == "newcomer"
    assert login(open_http, "newcomer", "web", password=PASSWORD).status_code == 200  # and the password works


def test_signing_up_applies_the_user_rules_and_refuses_taken_names(open_http):
    assert register(open_http, "erwan").status_code == 409
    assert register(open_http, "ERWAN").status_code == 409
    assert register(open_http, "bad name!").status_code == 422
    assert register(open_http, password="short").status_code == 422
    assert open_http.app.state.users.get("bad name!") is None


def test_signing_up_never_takes_over_the_memories_of_an_account_with_that_name(open_http):
    memory = open_http.app.state.memory
    stranger = memory.resolve("discord", "1234", "Someone")
    memory.add_fact(stranger.id, "likes tea")
    assert register(open_http, "1234").status_code == 409  # a Discord id: it is not theirs to claim
    assert register(open_http, "cli-person").status_code == 201
    mine = memory.find_person("web", "cli-person")
    assert mine is not None and mine.id != stranger.id
    assert memory.facts(stranger.id)[0].text == "likes tea"  # nothing moved


def test_a_refused_sign_up_leaves_no_person_behind(open_http):
    database = open_http.app.state.memory.database
    before = database.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    assert register(open_http, "erwan").status_code == 409
    assert register(open_http, password="short").status_code == 422
    assert database.execute("SELECT COUNT(*) FROM people").fetchone()[0] == before


def test_sign_ups_are_limited_per_address(settings):
    opened = replace(settings, web_signup=True)
    with TestClient(create_app(opened, fake_providers(opened, FakeBackend())), client=("203.0.113.5", 1)) as http:
        made = [register(http, f"user{n}").status_code for n in range(5)]
        assert made == [201] * 5
        assert register(http, "one-more").status_code == 429


def test_sign_up_passwords_never_reach_the_traffic_log(open_http):
    secret = "a very private sign-up password"
    assert register(open_http, "quiet", secret).status_code == 201
    open_http.app.state.traffic.flush()
    text = "".join(path.read_text(encoding="utf-8") for path in open_http.app.state.settings.logs_dir.glob("traffic-*.jsonl"))
    assert "quiet" in text and secret not in text


def test_a_disabled_user_is_signed_out_at_once(http):
    headers = bearer(http)
    assert http.patch("/v1/admin/users/erwan", json={"disabled": True}, headers=ADMIN_TOKEN).status_code == 200
    assert http.get("/v1/auth/me", headers=headers).status_code == 401
    assert login(http).status_code == 401


# --- administration -------------------------------------------------------------------------------------


def test_only_administrators_reach_the_admin_routes(http):
    assert http.get("/v1/admin/users", headers=bearer(http, "erwan")).status_code == 403
    assert http.get("/v1/admin/users", headers=CLI_TOKEN).status_code == 401  # a chat token is not an admin token
    assert http.get("/v1/admin/users").status_code == 401
    names = [u["name"] for u in http.get("/v1/admin/users", headers=bearer(http, "root")).json()["users"]]
    assert names == ["alice", "erwan", "root"]
    assert http.get("/v1/admin/users", headers=ADMIN_TOKEN).status_code == 200


def test_an_administrator_creates_edits_and_removes_users(http):
    admin = bearer(http, "root")
    made = http.post("/v1/admin/users", json={"name": "Newbie"}, headers=admin)
    assert made.status_code == 201 and len(made.json()["password"]) >= 10
    assert login(http, "newbie", password=made.json()["password"]).status_code == 200
    assert http.post("/v1/admin/users", json={"name": "newbie"}, headers=admin).status_code == 422
    assert http.post("/v1/admin/users", json={"name": "weak", "password": "short"}, headers=admin).status_code == 422
    edited = http.patch("/v1/admin/users/newbie", json={"generate_password": True, "admin": True}, headers=admin).json()
    assert edited["user"]["is_admin"] and edited["user"]["sessions"] == 0  # the new password signed them out
    assert login(http, "newbie", password=edited["password"]).status_code == 200
    assert http.post("/v1/admin/users/newbie/sign-out", headers=admin).json() == {"signed_out": 1}
    assert http.delete("/v1/admin/users/newbie", headers=admin).status_code == 200
    assert http.delete("/v1/admin/users/newbie", headers=admin).status_code == 422
    assert http.patch("/v1/admin/users/ghost", json={"admin": True}, headers=admin).status_code == 404


def test_the_last_administrator_is_protected_through_the_api(http):
    assert http.patch("/v1/admin/users/root", json={"admin": False}, headers=ADMIN_TOKEN).status_code == 422
    assert http.delete("/v1/admin/users/root", headers=ADMIN_TOKEN).status_code == 422


def test_passwords_never_reach_the_traffic_log(http):
    admin = bearer(http, "root")
    made = http.post("/v1/admin/users", json={"name": "secretive"}, headers=admin).json()["password"]
    changed = http.post("/v1/auth/password", json={"current_password": PASSWORD, "new_password": "my private password"}, headers=bearer(http)).status_code
    created = http.post("/v1/admin/command", json={"line": "/user add viacommand"}, headers=ADMIN_TOKEN).json()["output"]
    command_password = created.split("Password: ")[1].split("\n")[0]
    http.app.state.traffic.flush()
    text = "".join(path.read_text(encoding="utf-8") for path in http.app.state.settings.logs_dir.glob("traffic-*.jsonl"))
    assert changed == 200 and "secretive" in text
    for secret in (made, PASSWORD, "my private password", command_password):
        assert secret not in text
    assert "user:root@app" in text


def test_status_models_people_and_facts_for_the_admin_page(http):
    admin = bearer(http, "root")
    status = http.get("/v1/admin/status", headers=admin).json()
    assert status["provider"]["id"] == "local" and status["model"] == "fake" and status["tailscale"]["mode"] == "off"
    assert "fake-big" in http.get("/v1/admin/models", headers=admin).json()["models"]
    people = {p["name"]: p for p in http.get("/v1/admin/people", headers=admin).json()["people"]}
    assert people["erwan"]["user"] == "erwan"
    pid = people["erwan"]["id"]
    assert http.post(f"/v1/admin/people/{pid}/facts", json={"text": "Likes jazz"}, headers=admin).status_code == 201
    facts = http.get(f"/v1/admin/people/{pid}/facts", headers=admin).json()["facts"]
    assert [f["text"] for f in facts] == ["Likes jazz"]
    assert http.get(f"/v1/admin/people/{pid}/footprint", headers=admin).json()["facts"] == 1
    assert http.delete(f"/v1/admin/people/{pid}/facts/{facts[0]['id']}", headers=admin).status_code == 200
    assert http.get("/v1/admin/people/999/facts", headers=admin).status_code == 404


def test_the_admin_console_runs_user_commands(http):
    async def run(line):
        return await registry.execute(line, http.app.state.commands)

    import asyncio

    out = asyncio.run(run("/user add zoe admin"))
    assert out.sensitive and "Password: " in out.output and "administrator" in out.output
    assert "zoe" in asyncio.run(run("/user list")).output
    assert "signed out" in asyncio.run(run("/user passwd zoe")).output
    assert "no longer" in asyncio.run(run("/user admin zoe off")).output
    assert "disabled" in asyncio.run(run("/user disable zoe")).output
    assert "can no longer log in" in asyncio.run(run("/user remove zoe")).output
    assert asyncio.run(run("/user passwd ghost")).output.startswith("! No user called ghost")
    assert asyncio.run(run("/user add x@y")).output.startswith("! A user name")


# --- documents and the site -----------------------------------------------------------------------------


def tiny_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)


def test_a_pdf_is_read_on_the_server(http):
    headers = bearer(http)
    answer = http.post("/v1/documents/extract", content=tiny_pdf("Hello Clara"), headers=headers)
    assert answer.status_code == 200 and "Hello Clara" in answer.json()["text"] and answer.json()["pages"] == 1
    assert http.post("/v1/documents/extract", content=b"not a pdf", headers=headers).status_code == 422
    assert http.post("/v1/documents/extract", content=tiny_pdf("x")).status_code == 401


def test_the_web_site_is_served_with_strict_headers_and_does_not_hide_the_api(http):
    page = http.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"
    assert http.get("/health").json()["status"] == "ok"
    assert http.get("/app.js").status_code == 200 and http.get("/nope.txt").status_code == 404
    assert http.get("/v1/auth/me").status_code == 401


def test_the_web_site_uses_the_icon_of_clara_as_its_favicon(http):
    from pathlib import Path

    import clara

    icon = http.get("/favicon.ico")
    assert icon.status_code == 200 and icon.content[:4] == bytes([0, 0, 1, 0])  # an .ico file
    assert 'href="/favicon.ico"' in http.get("/").text
    assert icon.content == (Path(clara.__file__).parent / "web" / "favicon.ico").read_bytes()
