"""Google Drive: connecting an account with OAuth, adding a folder, and Clara working in it (against a fake Google: nothing
here reaches the real one)."""

import json
import re
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from conftest import FakeBackend, fake_providers, say
from fastapi.testclient import TestClient

from clara.integrations.vault import Vault
from clara.server import create_app

AUTH = {"Authorization": "Bearer secret-cli"}
ME = {"surface": "web", "user_id": "erwan"}
CONVERSATION = "web:erwan:drive"
FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
PUBLIC = "https://clara.example"


class FakeGoogle:
    """OAuth and a small Drive: My Drive > Work (the attached folder) > notes.txt, report (a Doc), Sub > deep.md, and
    a file outside Work."""

    def __init__(self):
        self.files: dict[str, dict] = {}
        self.next = 100
        self.refresh_ok = True
        self.codes = {"good-code"}
        self.add("root", "My Drive", FOLDER, None)
        self.add("work", "Work", FOLDER, "root")
        self.add("notes", "notes.txt", "text/plain", "work", "line one\nline two\n")
        self.add("report", "report", DOC, "work", "Quarterly report\nrevenue grew")
        self.add("sub", "Sub", FOLDER, "work")
        self.add("deep", "deep.md", "text/markdown", "sub", "# deep\nneedle here\n")
        self.add("secret", "private.txt", "text/plain", "root", "not in Work")
        self.requests: list[tuple[str, str]] = []

    def add(self, file_id, name, mime, parent, content="") -> dict:
        record = {"id": file_id, "name": name, "mimeType": mime, "parents": [parent] if parent else [], "trashed": False,
                  "content": content}
        self.files[file_id] = record
        return record

    def public(self, record) -> dict:
        size = {"size": str(len(record["content"]))} if record["mimeType"] != FOLDER and not record["mimeType"].startswith("application/vnd.google-apps") else {}
        return {k: v for k, v in record.items() if k != "content"} | size

    def query(self, q: str) -> list[dict]:
        parents = re.findall(r"'((?:[^'\\]|\\.)*)' in parents", q)
        found = [f for f in self.files.values() if not f["trashed"] and (not parents or any(p in f["parents"] for p in parents))]
        if match := re.search(r"name = '((?:[^'\\]|\\.)*)'", q):
            found = [f for f in found if f["name"] == match.group(1).replace("\\'", "'")]
        if match := re.search(r"mimeType = '([^']*)'", q):
            found = [f for f in found if f["mimeType"] == match.group(1)]
        if match := re.search(r"fullText contains '((?:[^'\\]|\\.)*)'", q):
            found = [f for f in found if match.group(1).lower() in f["content"].lower() or match.group(1).lower() in f["name"].lower()]
        if match := re.search(r"name contains '((?:[^'\\]|\\.)*)'", q):
            found = [f for f in found if match.group(1).lower() in f["name"].lower()]
        return sorted(found, key=lambda f: (f["mimeType"] != FOLDER, f["name"]))

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, url = request.method, request.url
        self.requests.append((method, url.path))
        if url.host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                if form["code"][0] not in self.codes:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "access-1", "refresh_token": "refresh-xyz", "expires_in": 3600})
            if not self.refresh_ok or form["refresh_token"] != ["refresh-xyz"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "access-2", "expires_in": 3600})
        if not request.headers.get("authorization", "").startswith("Bearer access-"):
            return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})
        path = url.path.removeprefix("/drive/v3").removeprefix("/upload/drive/v3")
        params = dict(url.params)
        if path == "/about":
            return httpx.Response(200, json={"user": {"emailAddress": "erwan@example.com"}})
        if path == "/files" and method == "GET":
            return httpx.Response(200, json={"files": [self.public(f) for f in self.query(params["q"])]})
        if path == "/files" and method == "POST":
            if "upload" in url.path:
                return self.upload(request, None)
            body = json.loads(request.content)
            made = self.add(f"new{self.next}", body["name"], body["mimeType"], body["parents"][0])
            self.next += 1
            return httpx.Response(200, json=self.public(made))
        match = re.match(r"^/files/([^/]+)(/export)?$", path)
        if match:
            record = self.files.get(match.group(1))
            if record is None:
                return httpx.Response(404, json={"error": {"message": "File not found"}})
            if match.group(2):
                return httpx.Response(200, content=record["content"].encode(), headers={"content-type": "text/plain"})
            if method == "GET":
                if params.get("alt") == "media":
                    return httpx.Response(200, content=record["content"].encode())
                return httpx.Response(200, json=self.public(record))
            if method == "PATCH":
                if "upload" in url.path:
                    return self.upload(request, record)
                body = json.loads(request.content)
                record.update({k: v for k, v in body.items() if k in ("name", "trashed")})
                if "addParents" in params:
                    record["parents"] = [params["addParents"]]
                return httpx.Response(200, json=self.public(record))
        return httpx.Response(404, json={"error": {"message": f"no route {method} {path}"}})

    def upload(self, request: httpx.Request, record) -> httpx.Response:
        parts = request.content.decode().split("--clara-boundary-8f2c")
        meta = json.loads(parts[1].split("\r\n\r\n", 1)[1].strip())
        content = parts[2].split("\r\n\r\n", 1)[1].rsplit("\r\n", 1)[0]
        if record is None:
            record = self.add(f"new{self.next}", meta["name"], "text/plain", meta["parents"][0])
            self.next += 1
        record["content"] = content
        return httpx.Response(200, json=self.public(record))


@pytest.fixture
def google():
    return FakeGoogle()


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def app(settings, backend, google):
    configured = replace(settings, google_client_id="client-id", google_client_secret="client-secret", public_url=PUBLIC)
    app = create_app(configured, fake_providers(configured, backend))
    app.state.integrations.connectors["gdrive"]._http.transport = httpx.MockTransport(google.handler)
    return app


@pytest.fixture
def http(app):
    with TestClient(app) as client:
        app.state.integrations.approvals.followup_delay = 0.0
        yield client


def start(http) -> str:
    started = http.post("/v1/integrations/google/start", headers=AUTH, json=ME)
    assert started.status_code == 200, started.text
    return parse_qs(urlparse(started.json()["url"]).query)["state"][0]


def confirm(http, state, code="good-code"):
    """The person pressed "Connect" on the page Google sent them back to."""
    return http.post(
        "/v1/integrations/google/confirm", content=urlencode({"code": code, "state": state}),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def connect(http, code="good-code"):
    """The whole round trip: the link, Google sending the person back, the confirmation."""
    state = start(http)
    shown = http.get("/v1/integrations/google/callback", params={"code": code, "state": state})
    assert shown.status_code == 200, shown.text
    return confirm(http, state, code), state


@pytest.fixture
def folder(http):
    page, _ = connect(http)
    assert page.status_code == 200, page.text
    added = http.post("/v1/integrations/resources", headers=AUTH, json={**ME, "kind": "drive_folder", "file_id": "work"})
    assert added.status_code == 201, added.text
    attached = http.put(
        "/v1/integrations/attachments", headers=AUTH, json={**ME, "resource": added.json()["id"], "conversation": CONVERSATION}
    )
    assert attached.status_code == 200
    return added.json()


def tool(tool_name: str, **arguments):
    from clara.llm import LlmChunk, ToolCall

    return [LlmChunk(tool_calls=[ToolCall(tool_name, arguments)])]


def chat(http, message="go"):
    response = http.post("/v1/chat/stream", headers=AUTH, json={**ME, "message": message, "conversation": CONVERSATION})
    return [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]


def results(events) -> list[str]:
    return [e["result"] for e in events if e["type"] == "tool"]


def allow_all(http, folder):
    http.patch(f"/v1/integrations/resources/{folder['id']}", headers=AUTH, json={**ME, "levels": {"write": "allow", "destructive": "allow"}})


# ----------------------------------------------------------------------
# Connecting
# ----------------------------------------------------------------------
def test_the_sign_in_link_asks_for_drive_and_offline_access(http):
    started = http.post("/v1/integrations/google/start", headers=AUTH, json=ME).json()
    query = parse_qs(urlparse(started["url"]).query)
    assert urlparse(started["url"]).netloc == "accounts.google.com"
    assert query["client_id"] == ["client-id"] and query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
    assert "https://www.googleapis.com/auth/drive" in query["scope"][0]
    assert started["redirect_uri"] == f"{PUBLIC}/v1/integrations/google/callback" == query["redirect_uri"][0]


def test_coming_back_from_google_connects_nothing_until_the_person_confirms_whose_drive_it_is(http, app):
    state = start(http)
    shown = http.get("/v1/integrations/google/callback", params={"code": "good-code", "state": state})
    assert shown.status_code == 200 and "Clara user" in shown.text and "<strong>erwan</strong>" in shown.text
    assert "action='/v1/integrations/google/confirm'" in shown.text and "good-code" in shown.text
    assert shown.headers["referrer-policy"] == "no-referrer" and shown.headers["cache-control"] == "no-store"
    assert http.get("/v1/integrations", headers=AUTH, params=ME).json()["accounts"] == []  # nothing is connected yet
    assert "erwan@example.com" in confirm(http, state).text
    assert len(http.get("/v1/integrations", headers=AUTH, params=ME).json()["accounts"]) == 1


def test_a_link_someone_else_made_names_their_user_so_that_the_person_can_refuse(http, app):
    """The attack: a user starts the sign-in, sends the link to someone, who agrees in Google. The page says whose
    Clara user would get the Drive: it is not the person's own."""
    memory = app.state.memory
    mine = memory.resolve("web", "mallory", None)
    link = app.state.integrations.vault.seal(
        json.dumps({"person": mine.id, "until": time.time() + 600, "nonce": "n1", "levels": {}})
    )
    shown = http.get("/v1/integrations/google/callback", params={"code": "good-code", "state": link})
    assert "<strong>mallory</strong>" in shown.text  # the victim sees a name that is not theirs, and closes the window
    assert http.get("/v1/integrations", headers=AUTH, params={"surface": "web", "user_id": "mallory"}).json()["accounts"] == []


def test_google_sends_the_person_back_and_the_account_is_kept_encrypted(http, app):
    page, _ = connect(http)
    assert page.status_code == 200 and "erwan@example.com" in page.text
    accounts = http.get("/v1/integrations", headers=AUTH, params=ME).json()["accounts"]
    assert [(a["kind"], a["label"], a["status"]) for a in accounts] == [("gdrive", "erwan@example.com", "ok")]
    assert "refresh-xyz" not in http.get("/v1/integrations", headers=AUTH, params=ME).text
    stored = app.state.integrations.store.account(accounts[0]["id"])
    assert "refresh-xyz" not in stored.secret and app.state.integrations.vault.open(stored.secret) == "refresh-xyz"
    page, _ = connect(http)  # connecting again updates it
    assert len(http.get("/v1/integrations", headers=AUTH, params=ME).json()["accounts"]) == 1


def test_a_forged_replayed_or_expired_link_connects_nothing(http, app):
    page, state = connect(http)
    assert page.status_code == 200
    replay = confirm(http, state)
    assert replay.status_code == 400 and "expired" in replay.text
    assert http.get("/v1/integrations/google/callback", params={"code": "good-code", "state": state}).status_code == 400
    for bad in ("forged", ""):
        assert http.get("/v1/integrations/google/callback", params={"code": "good-code", "state": bad}).status_code == 400
        assert confirm(http, bad).status_code == 400
    stranger = Vault("someone else's long key", app.state.settings.secret_key_file.with_name("other.key"))
    forged = stranger.seal(json.dumps({"person": 1, "until": time.time() + 600, "nonce": "n"}))
    assert http.get("/v1/integrations/google/callback", params={"code": "good-code", "state": forged}).status_code == 400
    assert confirm(http, forged).status_code == 400
    old = app.state.integrations.vault.seal(json.dumps({"person": 1, "until": time.time() - 5, "nonce": "old"}))
    assert http.get("/v1/integrations/google/callback", params={"code": "good-code", "state": old}).status_code == 400
    assert confirm(http, old).status_code == 400
    refused = http.get("/v1/integrations/google/callback", params={"error": "access_denied"})
    assert refused.status_code == 400 and "access_denied" in refused.text
    bad_code, _ = connect(http, code="wrong")
    assert bad_code.status_code == 502


def test_without_a_google_client_on_the_server_drive_is_not_offered(settings):
    app = create_app(settings, fake_providers(settings, FakeBackend()))
    with TestClient(app) as http:
        assert http.post("/v1/integrations/google/start", headers=AUTH, json=ME).status_code == 422
        types = {t["id"]: t["available"] for t in http.get("/v1/integrations", headers=AUTH, params=ME).json()["types"]}
        assert types["gdrive"] is False and types["github"] is True


def test_folders_are_browsed_and_picked_but_a_file_is_not_a_folder(http, folder):
    listing = http.get("/v1/integrations/browse/drive", headers=AUTH, params={**ME, "folder": "root"}).json()
    assert listing["folder"]["name"] == "My Drive" and [i["name"] for i in listing["items"]] == ["Work", "private.txt"]
    found = http.get("/v1/integrations/browse/drive", headers=AUTH, params={**ME, "q": "deep"}).json()
    assert [i["name"] for i in found["items"]] == ["deep.md"]
    assert folder["label"] == "Work" and folder["locator"] == {"id": "work", "name": "Work"}
    wrong = http.post("/v1/integrations/resources", headers=AUTH, json={**ME, "kind": "drive_file", "file_id": "work"})
    assert wrong.status_code == 422
    one = http.post("/v1/integrations/resources", headers=AUTH, json={**ME, "kind": "drive_file", "file_id": "notes"})
    assert one.status_code == 201 and one.json()["label"] == "notes.txt"
    assert http.post("/v1/integrations/resources", headers=AUTH, json={**ME, "kind": "drive_file", "file_id": "ghost"}).status_code == 422


# ----------------------------------------------------------------------
# Working in a folder
# ----------------------------------------------------------------------
def test_clara_lists_reads_exports_and_searches(http, backend, folder):
    rid = folder["id"]
    backend.rounds = [
        tool("res_list", resource=rid),
        tool("res_read", resource=rid, path="notes.txt", start_line=2),
        tool("res_read", resource=rid, path="report"),
        tool("res_read", resource=rid, path="Sub/deep.md"),
        tool("res_search", resource=rid, query="needle"),
        say("Done."),
    ]
    listing, notes, report, deep, found = results(chat(http))
    assert "Sub/ [id:sub]" in listing and "notes.txt (" in listing and "report (Google document) [id:report]" in listing
    assert "line two" in notes and "line one" not in notes
    assert "revenue grew" in report and "needle here" in deep
    assert "deep.md" in found and "[id:deep]" in found and "private.txt" not in found


def test_nothing_outside_the_attached_folder_can_be_reached(http, backend, folder, google):
    rid = folder["id"]
    backend.rounds = [
        tool("res_read", resource=rid, path="id:secret"),
        tool("res_read", resource=rid, path="../private.txt"),
        tool("res_read", resource=rid, path="Sub/../notes.txt"),
        say("no"),
    ]
    outside, up, up_again = results(chat(http))
    assert "not inside Work" in outside and "may not go up" in up and "may not go up" in up_again


def test_a_new_file_is_a_write_and_replacing_one_is_destructive(http, backend, folder, google):
    rid = folder["id"]
    http.patch(f"/v1/integrations/resources/{rid}", headers=AUTH, json={**ME, "levels": {"write": "allow"}})
    backend.rounds = [
        tool("res_write", resource=rid, path="Sub/new/plan.md", content="# plan"),
        tool("res_write", resource=rid, path="notes.txt", content="replaced", mode="overwrite"),
        tool("res_write", resource=rid, path="notes.txt", content="more\n", mode="append"),
        say("ok"),
    ]
    events = chat(http)
    created, replaced, appended = results(events)
    assert created.startswith("Created plan.md") and any(f["name"] == "plan.md" for f in google.files.values())
    plan = next(f for f in google.files.values() if f["name"] == "plan.md")
    assert plan["content"] == "# plan" and google.files[plan["parents"][0]]["name"] == "new"  # the folders were made
    assert "Waiting for permission" in replaced and google.files["notes"]["content"].startswith("line one")
    assert appended.startswith("Added to notes.txt") and google.files["notes"]["content"].endswith("line two\nmore\n")
    assert next(e for e in events if e["type"] == "approval")["approval"]["level"] == "destructive"


def test_deleting_moves_to_the_trash_after_permission_and_a_native_doc_is_not_overwritten(http, backend, folder, google):
    rid = folder["id"]
    backend.rounds = [tool("res_delete", resource=rid, path="notes.txt"), say("Asked.")]
    asked = next(e for e in chat(http) if e["type"] == "approval")["approval"]
    assert "trash" in asked["summary"] and google.files["notes"]["trashed"] is False
    backend.rounds = [say("Done.")]
    done = http.post(f"/v1/approvals/{asked['id']}/decide", headers=AUTH, json={**ME, "approve": True}).json()
    assert done["status"] == "done" and "trash" in done["result"] and google.files["notes"]["trashed"] is True
    allow_all(http, folder)
    backend.rounds = [tool("res_write", resource=rid, path="report", content="x", mode="overwrite"), say("no")]
    assert "Google document" in results(chat(http))[0] and google.files["report"]["content"].startswith("Quarterly")


def test_moving_and_renaming_stay_inside_the_folder(http, backend, folder, google):
    allow_all(http, folder)
    rid = folder["id"]
    backend.rounds = [
        tool("res_move", resource=rid, path="notes.txt", dest="Sub/renamed.txt"),
        tool("res_move", resource=rid, path="Sub/deep.md", dest="Sub/renamed.txt"),
        say("ok"),
    ]
    moved, clash = results(chat(http))
    assert moved == "Moved notes.txt to Sub/renamed.txt." and google.files["notes"]["name"] == "renamed.txt"
    assert google.files["notes"]["parents"] == ["sub"]
    assert "already exists" in clash and google.files["deep"]["name"] == "deep.md"


def test_a_connection_google_no_longer_accepts_asks_for_a_new_one(http, backend, folder, google, app):
    google.refresh_ok = False
    app.state.integrations.connectors["gdrive"]._access.clear()  # the access token ended: a new one is asked for
    backend.rounds = [tool("res_list", resource=folder["id"]), say("ok")]
    assert "connect it again" in results(chat(http))[0]
    assert http.get("/v1/integrations", headers=AUTH, params=ME).json()["accounts"][0]["status"] == "needs_reconnect"
    google.refresh_ok = True
    page, _ = connect(http)
    assert page.status_code == 200
    assert http.get("/v1/integrations", headers=AUTH, params=ME).json()["accounts"][0]["status"] == "ok"
    backend.rounds = [tool("res_list", resource=folder["id"]), say("ok")]
    assert "notes.txt" in results(chat(http))[0]


def test_a_search_without_folders_and_names_with_quotes_are_safe(http, backend, folder, google):
    allow_all(http, folder)
    rid = folder["id"]
    backend.rounds = [tool("res_write", resource=rid, path="it's here.txt", content="quote"), tool("res_read", resource=rid, path="it's here.txt"), say("ok")]
    created, read = results(chat(http))
    assert created.startswith("Created it's here.txt") and "quote" in read
