"""Connected resources: permissions, the vault, folders of the server, the broker, and requests for permission that
do not block a turn."""

import json
import os
import subprocess
import time
from datetime import datetime, timedelta

import pytest
from conftest import FakeBackend, call, fake_providers, say
from fastapi.testclient import TestClient

from clara.integrations import permissions
from clara.integrations.connectors.base import ConnectorError, Target
from clara.integrations.connectors.serverfs import ServerFolders, relative
from clara.integrations.vault import Vault, VaultError
from clara.server import create_app

AUTH = {"Authorization": "Bearer secret-cli"}
ADMIN = {"Authorization": "Bearer secret-admin"}
ME = {"surface": "web", "user_id": "erwan"}
OTHER = {"surface": "web", "user_id": "zoe"}
CONVERSATION = "web:erwan:talk"


def events_of(response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]


# ----------------------------------------------------------------------
# Permissions
# ----------------------------------------------------------------------
def test_the_most_specific_layer_wins_and_the_ceiling_caps_it():
    assert permissions.effective() == {"read": "allow", "write": "ask", "destructive": "ask"}
    layered = permissions.effective({"write": "allow"}, {"write": "deny", "destructive": "deny"}, {"read": "ask"})
    assert layered == {"read": "ask", "write": "allow", "destructive": "deny"}
    capped = permissions.effective({"destructive": "allow", "write": "allow"}, ceiling={"destructive": "ask", "write": "deny"})
    assert capped["destructive"] == "ask" and capped["write"] == "deny"
    assert permissions.capped("deny", "allow") == "deny"  # a ceiling never opens anything


def test_settings_that_make_no_sense_are_refused():
    assert permissions.clean({"read": "allow"}) == {"read": "allow"}
    assert permissions.clean(None) == {}
    for bad in ({"read": "yes"}, {"delete": "allow"}, ["read"], "{not json"):
        with pytest.raises(permissions.SettingError):
            permissions.clean(bad)
    assert permissions.clean({"read": "yes", "write": "ask"}, strict=False) == {"write": "ask"}


# ----------------------------------------------------------------------
# The vault
# ----------------------------------------------------------------------
def test_a_secret_is_unreadable_without_its_key(tmp_path):
    vault = Vault(key_file=tmp_path / "secret.key")
    sealed = vault.seal("ghp_token")
    assert "ghp_token" not in sealed and vault.open(sealed) == "ghp_token"
    assert Vault(key_file=tmp_path / "secret.key").open(sealed) == "ghp_token"  # the key was kept
    with pytest.raises(VaultError):
        Vault("another long key", tmp_path / "other.key").open(sealed)
    assert vault.seal("") == "" and vault.open("") == ""


# ----------------------------------------------------------------------
# Folders of the server
# ----------------------------------------------------------------------
@pytest.fixture
def root(tmp_path):
    folder = tmp_path / "docs"
    (folder / "src").mkdir(parents=True)
    (folder / "notes.md").write_bytes(b"one\ntwo\nthree\n")  # bytes: no newline translation on Windows
    (folder / "src" / "main.py").write_bytes(b"print('hello')\n")
    return folder


@pytest.fixture
def fs(tmp_path):
    return ServerFolders(lambda: [str(tmp_path)])


def target(folder) -> Target:
    return Target(1, "Docs", {"path": str(folder)})


async def test_a_folder_is_listed_read_and_searched(fs, root):
    assert (await fs.run("list", target(root), {})).splitlines() == ["src/", "notes.md (14 bytes)"]
    read = await fs.run("read", target(root), {"path": "notes.md", "start_line": 2})
    assert "two" in read and "one" not in read
    found = await fs.run("search", target(root), {"query": "HELLO"})
    assert "src/main.py:1: print('hello')" in found
    assert "No match" in await fs.run("search", target(root), {"query": "absent"})


async def test_a_folder_is_written_changed_moved_and_cleared(fs, root):
    t = target(root)
    assert await fs.level("write", t, {"path": "new.txt", "mode": "create"}) == "write"
    assert await fs.level("write", t, {"path": "notes.md", "mode": "overwrite"}) == "destructive"
    assert await fs.level("write", t, {"path": "notes.md", "mode": "append"}) == "write"
    assert await fs.level("delete", t, {"path": "notes.md"}) == "destructive"
    await fs.run("write", t, {"path": "a/b/new.txt", "content": "hi", "mode": "create"})
    assert (root / "a" / "b" / "new.txt").read_text() == "hi"
    with pytest.raises(ConnectorError, match="already exists"):
        await fs.run("write", t, {"path": "notes.md", "content": "x", "mode": "create"})
    await fs.run("write", t, {"path": "notes.md", "content": "!", "mode": "append"})
    assert (root / "notes.md").read_text().endswith("three\n!")
    await fs.run("move", t, {"path": "notes.md", "dest": "old.md"})
    await fs.run("delete", t, {"path": "old.md"})
    assert not (root / "notes.md").exists() and not (root / "old.md").exists()
    with pytest.raises(ConnectorError, match="folder with something"):
        await fs.run("delete", t, {"path": "src"})


@pytest.mark.parametrize("path", ["../secret.txt", "src/../../secret.txt", "/etc/passwd", "C:\\Windows\\win.ini", "..\\x"])
async def test_nothing_outside_the_folder_can_be_reached(fs, root, path):
    (root.parent / "secret.txt").write_text("nope")
    with pytest.raises(ConnectorError):
        await fs.run("read", target(root), {"path": path})
    with pytest.raises(ConnectorError):
        await fs.run("write", target(root), {"path": path, "content": "x", "mode": "overwrite"})


async def test_a_link_that_leaves_the_folder_is_refused(fs, root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope")
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        # Windows without the privilege for symbolic links: a junction leads out of the folder the same way
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "link"), str(outside)], capture_output=True)
        if os.name != "nt" or made.returncode != 0:
            pytest.skip("this system does not let a test make links")
    with pytest.raises(ConnectorError, match="outside"):
        await fs.run("read", target(root), {"path": "link/secret.txt"})


async def test_a_folder_the_administrator_no_longer_allows_stops_working(root, tmp_path):
    allowed = [str(tmp_path)]
    fs = ServerFolders(lambda: allowed)
    assert await fs.run("list", target(root), {})
    allowed.clear()
    with pytest.raises(ConnectorError, match="allows"):
        await fs.run("list", target(root), {})


def test_relative_paths_are_made_safe():
    assert relative("") == "" and relative("/") == "" and relative("./a//b/") == "a/b"
    for bad in ("../a", "a/../b", "/abs", "D:/x"):
        with pytest.raises(ConnectorError):
            relative(bad)


# ----------------------------------------------------------------------
# Through the server
# ----------------------------------------------------------------------
@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def app(settings, backend):
    return create_app(settings, fake_providers(settings, backend))


@pytest.fixture
def http(app):
    with TestClient(app) as client:
        app.state.integrations.approvals.followup_delay = 0.0
        yield client


@pytest.fixture
def folder(http, root, tmp_path):
    """The administrator allows the server's folders; a resource for `root` is added and attached to the talk."""
    response = http.put(
        "/v1/admin/integrations", headers=ADMIN, json={"enabled": {"server": True}, "roots": [str(tmp_path)]}
    )
    assert response.status_code == 200, response.text
    resource = http.post("/v1/integrations/resources", headers=AUTH, json={**ME, "kind": "server_path", "path": str(root)})
    assert resource.status_code == 201, resource.text
    attached = http.put(
        "/v1/integrations/attachments", headers=AUTH, json={**ME, "resource": resource.json()["id"], "conversation": CONVERSATION}
    )
    assert attached.status_code == 200, attached.text
    return resource.json()


def chat(http, message="go"):
    response = http.post("/v1/chat/stream", headers=AUTH, json={**ME, "message": message, "conversation": CONVERSATION})
    return events_of(response)


def wait_until(condition, seconds=5.0) -> None:
    """The follow-up turn runs in the server's own thread: wait for what it does."""
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert condition()


def tool_names(backend, round_number=0) -> set[str]:
    return {schema["function"]["name"] for schema in backend.calls[round_number][1] or []}


def test_the_tools_appear_only_where_something_is_connected(http, backend, folder):
    backend.rounds = [say("hi"), say("hi")]
    chat(http)
    assert {"res_read", "res_write", "resources"} <= tool_names(backend)
    http.post("/v1/chat/stream", headers=AUTH, json={**ME, "message": "go", "conversation": "web:erwan:other"})
    assert not {"res_read", "res_write"} & tool_names(backend, 1)


def test_the_prompt_lists_what_is_connected_and_the_rules(http, backend, folder):
    backend.rounds = [say("hi")]
    chat(http)
    system = backend.calls[0][0][0]["content"]
    assert f"[{folder['id']}] Server folders: docs (conversation)" in system
    assert "read: allow, write: ask, destructive: ask" in system
    assert "waiting for permission" in system


def test_reading_is_allowed_at_once(http, backend, folder):
    backend.rounds = [call("res_read", resource=folder["id"], path="notes.md"), say("It says one two three.")]
    events = chat(http)
    tool = next(e for e in events if e["type"] == "tool")
    assert "two" in tool["result"] and events[-1]["reply"].startswith("It says")
    assert not any(e["type"] == "approval" for e in events)


def test_a_write_waits_for_permission_without_blocking_the_turn(http, backend, folder, root):
    backend.rounds = [
        call("res_write", resource=folder["id"], path="todo.md", content="- milk", reason="you asked"),
        say("I asked; meanwhile, nothing else to do."),
    ]
    events = chat(http)
    asked = next(e for e in events if e["type"] == "approval")["approval"]
    assert asked["status"] == "pending" and "todo.md" in asked["summary"] and asked["level"] == "write"
    assert "Waiting for permission" in next(e for e in events if e["type"] == "tool")["result"]
    assert events[-1]["type"] == "done"  # the turn ended by itself
    assert not (root / "todo.md").exists()
    pending = http.get("/v1/approvals", headers=AUTH, params=ME).json()["approvals"]
    assert [a["id"] for a in pending] == [asked["id"]]


def test_approving_runs_the_action_and_tells_clara_in_a_follow_up_turn(http, backend, folder, root):
    backend.rounds = [call("res_write", resource=folder["id"], path="todo.md", content="- milk"), say("Asked.")]
    asked = next(e for e in chat(http) if e["type"] == "approval")["approval"]
    backend.rounds = [say("Done: todo.md is there.")]
    before = len(backend.calls)
    response = http.post(f"/v1/approvals/{asked['id']}/decide", headers=AUTH, json={**ME, "approve": True})
    assert response.status_code == 200 and response.json()["status"] == "done"
    assert (root / "todo.md").read_text() == "- milk"
    # the follow-up turn runs in the background: its message is stored in the conversation
    wait_until(lambda: len(backend.calls) > before)
    sent = backend.calls[-1][0][-1]["content"]
    assert "[Integration update]" in sent and f"#{asked['id']} approved and done" in sent
    def last_answer() -> str:
        found = http.get(f"/v1/conversations/{CONVERSATION}/messages", headers=AUTH, params=ME).json()["messages"]
        return found[-1]["content"]

    wait_until(lambda: last_answer() == "Done: todo.md is there.")
    assert http.get("/v1/approvals", headers=AUTH, params=ME).json()["approvals"] == []


def test_denying_does_nothing_and_clara_is_told_not_to_find_a_way_round(http, backend, folder, root):
    backend.rounds = [call("res_delete", resource=folder["id"], path="notes.md"), say("Asked.")]
    asked = next(e for e in chat(http) if e["type"] == "approval")["approval"]
    assert asked["level"] == "destructive"
    backend.rounds = [say("Understood, I leave it.")]
    before = len(backend.calls)
    response = http.post(f"/v1/approvals/{asked['id']}/decide", headers=AUTH, json={**ME, "approve": False})
    assert response.json()["status"] == "denied"
    assert (root / "notes.md").exists()
    wait_until(lambda: len(backend.calls) > before)
    assert "denied" in backend.calls[-1][0][-1]["content"] and "another way round" in backend.calls[-1][0][-1]["content"]


def test_overwriting_is_destructive_but_a_new_file_is_only_a_write(http, backend, folder):
    http.patch(
        f"/v1/integrations/resources/{folder['id']}", headers=AUTH, json={**ME, "levels": {"write": "allow"}}
    )
    backend.rounds = [
        call("res_write", resource=folder["id"], path="fresh.txt", content="a"),
        call("res_write", resource=folder["id"], path="notes.md", content="b", mode="overwrite"),
        say("ok"),
    ]
    events = chat(http)
    results = [e["result"] for e in events if e["type"] == "tool"]
    assert results[0].startswith("Created")  # allowed: a write
    assert "Waiting for permission" in results[1]  # replacing a file asks
    assert next(e for e in events if e["type"] == "approval")["approval"]["level"] == "destructive"


def test_a_denied_level_is_refused_and_a_ceiling_beats_what_the_person_allows(http, backend, folder, root):
    http.patch(
        f"/v1/integrations/resources/{folder['id']}", headers=AUTH, json={**ME, "levels": {"destructive": "allow"}}
    )
    http.put("/v1/admin/integrations", headers=ADMIN, json={"ceiling": {"server": {"destructive": "deny"}}})
    backend.rounds = [call("res_delete", resource=folder["id"], path="notes.md"), say("no")]
    events = chat(http)
    assert "Not allowed" in next(e for e in events if e["type"] == "tool")["result"]
    assert (root / "notes.md").exists()


def test_remembering_an_answer_stops_the_questions(http, backend, folder, root):
    backend.rounds = [call("res_write", resource=folder["id"], path="a.txt", content="1"), say("Asked.")]
    asked = next(e for e in chat(http) if e["type"] == "approval")["approval"]
    backend.rounds = [say("Done.")]
    http.post(
        f"/v1/approvals/{asked['id']}/decide", headers=AUTH, json={**ME, "approve": True, "remember": "conversation"}
    )
    backend.rounds = [call("res_write", resource=folder["id"], path="b.txt", content="2"), say("Both.")]
    events = chat(http)
    assert not any(e["type"] == "approval" for e in events)
    assert (root / "b.txt").read_text() == "2"


def test_only_the_person_asked_can_answer_and_only_once(http, backend, folder):
    backend.rounds = [call("res_write", resource=folder["id"], path="a.txt", content="1"), say("Asked.")]
    asked = next(e for e in chat(http) if e["type"] == "approval")["approval"]
    stranger = http.post(f"/v1/approvals/{asked['id']}/decide", headers=AUTH, json={**OTHER, "approve": True})
    assert stranger.status_code == 404
    backend.rounds = [say("ok")]
    assert http.post(f"/v1/approvals/{asked['id']}/decide", headers=AUTH, json={**ME, "approve": False}).status_code == 200
    again = http.post(f"/v1/approvals/{asked['id']}/decide", headers=AUTH, json={**ME, "approve": True})
    assert again.status_code == 409


def test_the_model_has_no_tool_to_answer_its_own_request(http, backend, folder):
    backend.rounds = [say("hi")]
    chat(http)
    assert not {name for name in tool_names(backend) if "approv" in name or "decide" in name}


def test_asking_twice_for_the_same_thing_makes_one_request(http, backend, folder):
    same = {"resource": folder["id"], "path": "a.txt", "content": "1"}
    backend.rounds = [call("res_write", **same), call("res_write", **same), say("Asked.")]
    events = chat(http)
    assert len([e for e in events if e["type"] == "approval"]) == 1
    assert len(http.get("/v1/approvals", headers=AUTH, params=ME).json()["approvals"]) == 1


def test_a_resource_of_another_person_cannot_be_used_or_attached(http, backend, folder):
    assert http.patch(f"/v1/integrations/resources/{folder['id']}", headers=AUTH, json={**OTHER, "label": "mine"}).status_code == 404
    attach = http.put(
        "/v1/integrations/attachments", headers=AUTH,
        json={**OTHER, "resource": folder["id"], "conversation": "web:zoe:talk"},
    )
    assert attach.status_code == 404
    # zoe in the same conversation sees and reaches nothing of erwan's
    backend.rounds = [say("hi")]
    http.post("/v1/chat/stream", headers=AUTH, json={**OTHER, "message": "go", "conversation": CONVERSATION})
    assert "Connected resources" not in backend.calls[0][0][0]["content"]


def test_a_folder_outside_the_allowed_roots_cannot_be_added(http, folder, tmp_path):
    outside = tmp_path.parent
    refused = http.post("/v1/integrations/resources", headers=AUTH, json={**ME, "kind": "server_path", "path": str(outside)})
    assert refused.status_code == 422
    http.put("/v1/admin/integrations", headers=ADMIN, json={"enabled": {"server": False}})
    off = http.post("/v1/integrations/resources", headers=AUTH, json={**ME, "kind": "server_path", "path": str(tmp_path)})
    assert off.status_code == 403


def test_a_project_attachment_is_inherited_and_the_conversation_may_decide_otherwise(http, backend, folder):
    project = http.post("/v1/projects", headers=AUTH, json={**ME, "name": "Notes"}).json()
    own = http.get("/v1/integrations/attachments", headers=AUTH, params={**ME, "conversation": CONVERSATION}).json()
    http.delete(f"/v1/integrations/attachments/{own['attachments'][0]['attachment']}", headers=AUTH, params=ME)
    http.put("/v1/integrations/attachments", headers=AUTH, json={**ME, "resource": folder["id"], "project": project["id"]})
    backend.rounds = [say("hi")]
    http.post(
        "/v1/chat/stream", headers=AUTH,
        json={**ME, "message": "go", "conversation": "web:erwan:inproject", "project": project["id"]},
    )
    assert "(project)" in backend.calls[0][0][0]["content"]
    listed = http.get("/v1/integrations/attachments", headers=AUTH, params={**ME, "conversation": "web:erwan:inproject"}).json()
    assert [a["scope"] for a in listed["inherited"]] == ["project"]
    http.put(
        "/v1/integrations/attachments", headers=AUTH,
        json={**ME, "resource": folder["id"], "conversation": "web:erwan:inproject", "levels": {"read": "deny"}},
    )
    backend.rounds = [say("hi")]
    http.post(
        "/v1/chat/stream", headers=AUTH,
        json={**ME, "message": "again", "conversation": "web:erwan:inproject", "project": project["id"]},
    )
    assert "(conversation)" in backend.calls[1][0][0]["content"] and "read: deny" in backend.calls[1][0][0]["content"]


def test_secrets_never_come_back_from_the_api(http, app, folder):
    store = app.state.integrations.store
    person = app.state.memory.find_person("web", "erwan")
    account = store.add_account(person.id, "github", "erwan", app.state.integrations.vault.seal("ghp_secret"))
    overview = http.get("/v1/integrations", headers=AUTH, params=ME)
    assert "ghp_secret" not in overview.text and app.state.integrations.vault.seal("x") not in overview.text
    listed = overview.json()["accounts"]
    assert listed == [{**listed[0], "id": account.id, "kind": "github", "status": "ok"}]
    assert "secret" not in listed[0]


# ----------------------------------------------------------------------
# Time: pushing, expiring
# ----------------------------------------------------------------------
async def test_an_unanswered_request_is_pushed_to_the_other_surfaces_then_expires(http, app, backend, folder):
    backend.rounds = [call("res_write", resource=folder["id"], path="a.txt", content="1"), say("Asked.")]
    asked = next(e for e in chat(http) if e["type"] == "approval")["approval"]
    approvals = app.state.integrations.approvals
    memory = app.state.memory
    start = datetime.fromisoformat(asked["created_at"])

    approvals.clock = lambda: start + timedelta(seconds=30)
    await approvals.sweep()
    assert memory.reminder_events_after(0) == []  # too early: it is still in its conversation

    approvals.clock = lambda: start + timedelta(seconds=61)
    await approvals.sweep()
    pushed = [e for e in memory.reminder_events_after(0) if e.kind == "approval"]
    assert len(pushed) == 1
    assert pushed[0].targets == ("app", "discord")  # not "web": that is where it was asked
    assert pushed[0].payload["approval"] == asked["id"] and "a.txt" in pushed[0].text
    await approvals.sweep()
    assert len([e for e in memory.reminder_events_after(0) if e.kind == "approval"]) == 1  # once

    approvals.clock = lambda: start + timedelta(hours=24, seconds=5)
    await approvals.sweep()
    assert app.state.integrations.store.approval(asked["id"]).status == "expired"
    assert [e.kind for e in memory.reminder_events_after(0)][-1] == "approval_resolved"
    backend.rounds = [say("ok")]
    chat(http, "any news?")
    assert "nobody answered in time" in backend.calls[-1][0][0]["content"]
    assert http.post(f"/v1/approvals/{asked['id']}/decide", headers=AUTH, json={**ME, "approve": True}).status_code == 409


async def test_a_person_can_ask_never_to_be_pushed(http, app, backend, folder):
    http.put("/v1/integrations/settings", headers=AUTH, json={**ME, "approval_notify_after": 0})
    backend.rounds = [call("res_write", resource=folder["id"], path="a.txt", content="1"), say("Asked.")]
    asked = next(e for e in chat(http) if e["type"] == "approval")["approval"]
    approvals = app.state.integrations.approvals
    approvals.clock = lambda: datetime.fromisoformat(asked["created_at"]) + timedelta(hours=2)
    await approvals.sweep()
    assert not [e for e in app.state.memory.reminder_events_after(0) if e.kind == "approval"]


def test_what_was_approved_but_never_finished_is_not_run_again(app, folder, backend):
    store = app.state.integrations.store
    person = app.state.memory.find_person("web", "erwan")
    approval, _ = store.add_approval(person.id, CONVERSATION, folder["id"], "write", "write", {"path": "x"}, "write x")
    store.decide(approval.id, "approved", "web")
    app.state.integrations.approvals.recover()
    after = store.approval(approval.id)
    assert after.status == "failed" and "Check" in after.result


def test_the_log_keeps_what_was_asked_and_done(http, backend, folder):
    backend.rounds = [call("res_write", resource=folder["id"], path="a.txt", content="1"), say("Asked.")]
    asked = next(e for e in chat(http) if e["type"] == "approval")["approval"]
    backend.rounds = [say("Done.")]
    http.post(f"/v1/approvals/{asked['id']}/decide", headers=AUTH, json={**ME, "approve": True})
    entries = http.get("/v1/admin/integrations/log", headers=ADMIN).json()["entries"]
    outcomes = [(e["op"], e["outcome"]) for e in entries]
    assert ("write", "asked") in outcomes and ("write", "done") in outcomes and ("write", "approved") in outcomes
    assert all(e["resource"] == "docs" for e in entries)
    assert http.get("/v1/admin/integrations/log", headers=AUTH).status_code in (401, 403)


def test_the_administrator_settings_are_checked(http, tmp_path):
    assert http.put("/v1/admin/integrations", headers=ADMIN, json={"enabled": {"nothing": True}}).status_code == 422
    assert http.put("/v1/admin/integrations", headers=ADMIN, json={"roots": [str(tmp_path / "missing")]}).status_code == 422
    assert http.put("/v1/admin/integrations", headers=ADMIN, json={"ceiling": {"server": {"read": "maybe"}}}).status_code == 422
    ok = http.put("/v1/admin/integrations", headers=ADMIN, json={"roots": [str(tmp_path)], "enabled": {"server": True}})
    assert ok.json()["roots"] == [str(tmp_path.resolve())] and ok.json()["enabled"]["server"] is True
    types = {t["id"]: t["available"] for t in ok.json()["types"]}
    assert types["server"] is True



def test_clearing_a_conversation_takes_its_own_connections_with_it_but_not_the_resource(http, app, folder):
    store = app.state.integrations.store
    assert [a.resource.id for a in store.attachments_of(conversation=CONVERSATION)] == [folder["id"]]
    app.state.memory.clear_conversation(CONVERSATION)
    assert store.attachments_of(conversation=CONVERSATION) == []
    assert store.resource(folder["id"]) is not None  # still there to be attached again


def test_old_finished_requests_are_forgotten_but_waiting_ones_stay(app, folder):
    store = app.state.integrations.store
    person = app.state.memory.find_person("web", "erwan")
    old, _ = store.add_approval(person.id, CONVERSATION, folder["id"], "write", "write", {"path": "a", "content": "x"}, "write a")
    waiting, _ = store.add_approval(person.id, CONVERSATION, folder["id"], "write", "write", {"path": "b", "content": "y"}, "write b")
    unread, _ = store.add_approval(person.id, CONVERSATION, folder["id"], "write", "write", {"path": "c", "content": "z"}, "write c")
    store.decide(old.id, "denied", "web")
    store.mark_told([old.id])
    store.decide(unread.id, "denied", "web")  # Clara was not told yet: it is kept until she is
    assert store.prune_approvals("2999-01-01T00:00:00+00:00") == 1
    assert store.approval(old.id) is None and store.approval(waiting.id) is not None and store.approval(unread.id) is not None


def test_erasing_a_person_erases_what_they_connected_and_what_was_logged(app, folder):
    memory, store = app.state.memory, app.state.integrations.store
    person = memory.find_person("web", "erwan")
    account = store.add_account(person.id, "github", "erwan", app.state.integrations.vault.seal("ghp_x"))
    approval, _ = store.add_approval(person.id, CONVERSATION, folder["id"], "write", "write", {"path": "a"}, "write a")
    store.log(person.id, CONVERSATION, "docs", "write", "write", "write a", "asked", approval.id)
    store.add_job(person.id, "pc-1", "list", {})
    memory.delete_person(person.id)
    assert store.account(account.id) is None and store.resource(folder["id"]) is None and store.approval(approval.id) is None
    assert store.log_entries() == [] and store.jobs_for(person.id, statuses=("queued", "sent")) == []
