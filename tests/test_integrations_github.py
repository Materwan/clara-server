"""A connected GitHub account: connecting it, adding a repository, and Clara working on it live (against a fake
GitHub: nothing here reaches the real one)."""

import base64
import json

import httpx
import pytest
from conftest import FakeBackend, call, fake_providers, say
from fastapi.testclient import TestClient

from clara.llm import LlmChunk, ToolCall
from clara.server import create_app

AUTH = {"Authorization": "Bearer secret-cli"}
ME = {"surface": "web", "user_id": "erwan"}
CONVERSATION = "web:erwan:code"
GOOD = "github_pat_good_token_123"


class FakeGitHub:
    """Enough of GitHub's REST API for one repository, erwan/app, with a `main` and a `dev` branch."""

    def __init__(self):
        self.files = {("main", "README.md"): "# App\nhello\n", ("main", "src/app.py"): "print('hi')\n"}
        self.branches = {"main": "sha-main", "dev": "sha-dev"}
        self.requests: list[tuple[str, str]] = []
        self.revoked = False
        self.opened: list[dict] = []

    def tree(self, branch):
        return {path for (b, path) in self.files if b == branch}

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        if request.headers.get("authorization") != f"Bearer {GOOD}" or self.revoked:
            return httpx.Response(401, json={"message": "Bad credentials"})
        if path == "/user":
            return httpx.Response(200, json={"login": "erwan"})
        if path == "/user/repos":
            return httpx.Response(200, json=[
                {"full_name": "erwan/app", "private": True, "default_branch": "main", "description": "My app"},
                {"full_name": "erwan/other", "private": False, "default_branch": "main", "description": None},
            ])
        if path == "/repos/erwan/app":
            return httpx.Response(200, json={"full_name": "erwan/app", "default_branch": "main"})
        if path.startswith("/repos/erwan/app/branches/"):
            name = path.rsplit("/", 1)[1]
            return httpx.Response(200 if name in self.branches else 404, json={"message": "Not Found"})
        if path == "/repos/erwan/app/branches":
            return httpx.Response(200, json=[{"name": n} for n in self.branches])
        if path.startswith("/repos/erwan/app/git/ref/heads/"):
            name = path.removeprefix("/repos/erwan/app/git/ref/heads/")
            if name not in self.branches:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": self.branches[name]}})
        if path == "/repos/erwan/app/git/refs" and method == "POST":
            body = json.loads(request.content)
            self.branches[body["ref"].removeprefix("refs/heads/")] = body["sha"]
            for (branch, file), text in list(self.files.items()):
                if self.branches.get(branch) == body["sha"] and branch != body["ref"].removeprefix("refs/heads/"):
                    self.files[(body["ref"].removeprefix("refs/heads/"), file)] = text
            return httpx.Response(201, json={})
        if path == "/repos/erwan/app/pulls" and method == "POST":
            body = json.loads(request.content)
            self.opened.append(body)
            return httpx.Response(201, json={"number": 7, "html_url": "https://github.com/erwan/app/pull/7"})
        if path == "/search/code":
            return httpx.Response(200, json={"items": [
                {"path": "src/app.py", "text_matches": [{"fragment": "def main():\n    print('hi')"}]}
            ]})
        if path.startswith("/repos/erwan/app/contents"):
            return self.contents(request, path.removeprefix("/repos/erwan/app/contents").strip("/"))
        return httpx.Response(404, json={"message": "Not Found"})

    def contents(self, request: httpx.Request, file: str) -> httpx.Response:
        branch = request.url.params.get("ref") or "main"
        if request.method == "GET":
            if not file or any(p.startswith(file + "/") for p in self.tree(branch)):
                names = {p[len(file) + 1:] if file else p for p in self.tree(branch) if not file or p.startswith(file + "/")}
                entries = [
                    {"name": n.split("/")[0], "type": "dir" if "/" in n else "file", "size": 5} for n in sorted(names)
                ]
                return httpx.Response(200, json=entries)
            if (branch, file) not in self.files:
                return httpx.Response(404, json={"message": "Not Found"})
            text = self.files[(branch, file)]
            if "raw" in request.headers.get("accept", ""):
                return httpx.Response(200, content=text.encode(), headers={"content-type": "text/plain"})
            return httpx.Response(200, json={"name": file, "sha": f"sha-{file}", "size": len(text)})
        body = json.loads(request.content)
        branch = body["branch"]
        if request.method == "PUT":
            if (branch, file) in self.files and body.get("sha") != f"sha-{file}":
                return httpx.Response(409, json={"message": "does not match"})
            self.files[(branch, file)] = base64.b64decode(body["content"]).decode()
            return httpx.Response(200, json={"commit": {"sha": "abcdef1234"}})
        if request.method == "DELETE":
            self.files.pop((branch, file), None)
            return httpx.Response(200, json={"commit": {"sha": "fedcba9876"}})
        return httpx.Response(405, json={})


@pytest.fixture
def github():
    return FakeGitHub()


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def app(settings, backend, github):
    app = create_app(settings, fake_providers(settings, backend))
    app.state.integrations.connectors["github"]._http.transport = httpx.MockTransport(github.handler)
    return app


@pytest.fixture
def http(app):
    with TestClient(app) as client:
        app.state.integrations.approvals.followup_delay = 0.0
        yield client


@pytest.fixture
def repo(http):
    connected = http.post("/v1/integrations/github", headers=AUTH, json={**ME, "token": GOOD})
    assert connected.status_code == 201, connected.text
    added = http.post(
        "/v1/integrations/resources", headers=AUTH,
        json={**ME, "kind": "github_repo", "repo": "https://github.com/erwan/app"},
    )
    assert added.status_code == 201, added.text
    attached = http.put(
        "/v1/integrations/attachments", headers=AUTH, json={**ME, "resource": added.json()["id"], "conversation": CONVERSATION}
    )
    assert attached.status_code == 200
    return added.json()


def tool(tool_name: str, arguments: dict) -> list[LlmChunk]:
    """A model round that calls a tool whose arguments hold a `name` (conftest.call() uses that word itself)."""
    return [LlmChunk(tool_calls=[ToolCall(tool_name, arguments)])]


def chat(http, message="go"):
    response = http.post("/v1/chat/stream", headers=AUTH, json={**ME, "message": message, "conversation": CONVERSATION})
    return [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]


def tool_results(events) -> list[str]:
    return [e["result"] for e in events if e["type"] == "tool"]


def test_an_account_is_connected_with_a_token_that_is_kept_secret(http, app):
    bad = http.post("/v1/integrations/github", headers=AUTH, json={**ME, "token": "github_pat_wrong_token_1"})
    assert bad.status_code == 422 and "refused" in bad.json()["detail"]
    good = http.post("/v1/integrations/github", headers=AUTH, json={**ME, "token": GOOD, "levels": {"write": "allow"}})
    assert good.status_code == 201
    account = good.json()
    assert (account["kind"], account["label"], account["status"], account["levels"]) == ("github", "erwan", "ok", {"write": "allow"})
    assert GOOD not in good.text and GOOD not in http.get("/v1/integrations", headers=AUTH, params=ME).text
    stored = app.state.integrations.store.account(account["id"])
    assert GOOD not in stored.secret and app.state.integrations.vault.open(stored.secret) == GOOD
    again = http.post("/v1/integrations/github", headers=AUTH, json={**ME, "token": GOOD})
    assert again.json()["id"] == account["id"]  # the same login: updated, not doubled


def test_repositories_are_listed_for_picking_and_added_from_any_spelling(http, repo):
    listed = http.get("/v1/integrations/browse/github", headers=AUTH, params={**ME, "q": "app"}).json()["repos"]
    assert [r["full_name"] for r in listed] == ["erwan/app"]
    assert repo["label"] == "erwan/app" and repo["locator"] == {"repo": "erwan/app", "ref": ""}
    again = http.post("/v1/integrations/resources", headers=AUTH, json={**ME, "kind": "github_repo", "repo": "erwan/app"})
    assert again.json()["id"] == repo["id"]  # one resource per repository and branch
    dev = http.post("/v1/integrations/resources", headers=AUTH, json={**ME, "kind": "github_repo", "repo": "erwan/app@dev"})
    assert dev.json()["label"] == "erwan/app@dev"
    missing = http.post("/v1/integrations/resources", headers=AUTH, json={**ME, "kind": "github_repo", "repo": "erwan/app@nope"})
    assert missing.status_code == 422
    unknown = http.post("/v1/integrations/resources", headers=AUTH, json={**ME, "kind": "github_repo", "repo": "erwan/ghost"})
    assert unknown.status_code == 422
    assert http.post("/v1/integrations/resources", headers=AUTH, json={**ME, "kind": "github_repo", "repo": "not a repo"}).status_code == 422


def test_clara_lists_reads_and_searches_a_repository(http, backend, repo):
    rid = repo["id"]
    backend.rounds = [
        call("res_list", resource=rid),
        call("res_read", resource=rid, path="src/app.py"),
        call("res_search", resource=rid, query="main"),
        say("Done."),
    ]
    listing, read, found = tool_results(chat(http))
    assert "README.md" in listing and "src/" in listing
    assert "print('hi')" in read and read.startswith("src/app.py, lines 1-1 of 1")
    assert "src/app.py: def main():" in found


def test_a_commit_to_a_branch_is_a_write_and_to_the_default_branch_is_destructive(http, backend, github, repo):
    rid = repo["id"]
    http.patch(f"/v1/integrations/resources/{rid}", headers=AUTH, json={**ME, "levels": {"write": "allow"}})
    backend.rounds = [
        call("res_write", resource=rid, path="notes/todo.md", content="- a", branch="dev"),
        call("res_write", resource=rid, path="notes/main.md", content="- b"),
        say("ok"),
    ]
    events = chat(http)
    first, second = tool_results(events)
    assert first.startswith("Created notes/todo.md on dev (commit abcdef1)")
    assert github.files[("dev", "notes/todo.md")] == "- a"
    assert "Waiting for permission" in second  # the default branch asks, even though writes are allowed
    assert ("main", "notes/main.md") not in github.files
    assert next(e for e in events if e["type"] == "approval")["approval"]["level"] == "destructive"


def test_approving_a_delete_makes_the_commit(http, backend, github, repo):
    backend.rounds = [call("res_delete", resource=repo["id"], path="README.md", branch="dev"), say("Asked.")]
    github.files[("dev", "README.md")] = "# dev"
    asked = next(e for e in chat(http) if e["type"] == "approval")["approval"]
    assert asked["level"] == "destructive" and "README.md" in asked["summary"]
    backend.rounds = [say("Deleted.")]
    done = http.post(f"/v1/approvals/{asked['id']}/decide", headers=AUTH, json={**ME, "approve": True})
    assert done.json()["status"] == "done" and ("dev", "README.md") not in github.files


def test_overwriting_and_appending_on_a_branch_update_the_file_with_its_current_version(http, backend, github, repo):
    http.patch(f"/v1/integrations/resources/{repo['id']}", headers=AUTH, json={**ME, "levels": {"write": "allow"}})
    github.files[("dev", "x.txt")] = "old"
    backend.rounds = [call("res_write", resource=repo["id"], path="x.txt", content="new", mode="overwrite", branch="dev"), say("ok")]
    result = tool_results(chat(http))[0]
    assert result.startswith("Updated x.txt on dev") and github.files[("dev", "x.txt")] == "new"
    backend.rounds = [call("res_write", resource=repo["id"], path="x.txt", content="+more", mode="append", branch="dev"), say("ok")]
    tool_results(chat(http))
    assert github.files[("dev", "x.txt")] == "new+more"


def test_a_branch_a_pull_request_and_the_rest_that_asks(http, backend, github, repo):
    rid = repo["id"]
    http.patch(f"/v1/integrations/resources/{rid}", headers=AUTH, json={**ME, "levels": {"write": "allow"}})
    backend.rounds = [
        tool("github_branch", {"resource": rid, "action": "create", "name": "clara/fix"}),
        call("github_pr", resource=rid, action="open", title="Fix", head="clara/fix", body="Please"),
        call("github_pr", resource=rid, action="merge", number=7),
        tool("github_branch", {"resource": rid, "action": "delete", "name": "main"}),
        say("ok"),
    ]
    events = chat(http)
    made, opened, merge, delete = tool_results(events)
    assert "Created the branch clara/fix from main" in made and "clara/fix" in github.branches
    assert "pull/7" in opened and github.opened[0]["base"] == "main" and github.opened[0]["head"] == "clara/fix"
    assert "Waiting for permission" in merge  # merging is destructive
    assert "Waiting for permission" in delete  # so is deleting a branch (the default one is refused when it runs)
    assert len([e for e in events if e["type"] == "approval"]) == 2


def test_a_revoked_token_asks_the_person_to_connect_again(http, backend, github, app, repo):
    github.revoked = True
    backend.rounds = [call("res_list", resource=repo["id"]), say("ok")]
    assert "connect it again" in tool_results(chat(http))[0]
    overview = http.get("/v1/integrations", headers=AUTH, params=ME).json()
    assert overview["accounts"][0]["status"] == "needs_reconnect"
    backend.rounds = [call("res_list", resource=repo["id"]), say("ok")]
    assert "needs to be connected again" in tool_results(chat(http))[0]
    github.revoked = False
    assert http.post("/v1/integrations/github", headers=AUTH, json={**ME, "token": GOOD}).status_code == 201
    backend.rounds = [call("res_list", resource=repo["id"]), say("ok")]
    assert "README.md" in tool_results(chat(http))[0]


def test_disconnecting_removes_the_account_and_its_repositories(http, repo):
    account = http.get("/v1/integrations", headers=AUTH, params=ME).json()["accounts"][0]
    assert http.delete(f"/v1/integrations/accounts/{account['id']}", headers=AUTH, params=ME).json()["resources_removed"] == 1
    overview = http.get("/v1/integrations", headers=AUTH, params=ME).json()
    assert overview["accounts"] == [] and overview["resources"] == []
    assert http.get("/v1/integrations/attachments", headers=AUTH, params={**ME, "conversation": CONVERSATION}).json()["attachments"] == []
