"""The routes of projects, a conversation in a project, and GitHub repositories."""

import base64
import io
import json
import zipfile
from dataclasses import replace

import httpx
import pytest
from conftest import FakeBackend, call, fake_providers, say
from fastapi.testclient import TestClient

from clara.github import GitHub, GitHubError, parse_repo
from clara.server import create_app

AUTH = {"Authorization": "Bearer secret-cli"}
ME = {"surface": "web", "user_id": "erwan"}
OTHER = {"surface": "web", "user_id": "zoe"}


def b64(data: bytes | str) -> str:
    return base64.b64encode(data.encode() if isinstance(data, str) else data).decode()


def make_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture
def fake():
    return FakeBackend()


@pytest.fixture
def app(settings, fake):
    return create_app(settings, fake_providers(settings, fake))


@pytest.fixture
def http(app):
    with TestClient(app) as client:
        yield client


def new_project(http, **fields) -> dict:
    response = http.post("/v1/projects", headers=AUTH, json={**ME, "name": "My app", **fields})
    assert response.status_code == 201, response.text
    return response.json()


def upload(http, project_id: int, files: dict[str, bytes | str], who=ME) -> httpx.Response:
    body = {**who, "files": [{"path": path, "data": b64(data)} for path, data in files.items()]}
    return http.post(f"/v1/projects/{project_id}/files", headers=AUTH, json=body)


def test_a_project_is_made_filled_and_described(http):
    project = new_project(http, description="A web app", instructions="Use TypeScript")
    assert (project["name"], project["files"], project["sources"], project["file_list"]) == ("My app", 0, [], [])
    response = upload(http, project["id"], {
        "src/main.py": "print('hi')",
        "site.zip": make_zip({"index.html": "<p>hi</p>", "node_modules/x.js": "x"}),
        "photo.png": "not really",
    })
    assert response.status_code == 200
    result = response.json()
    assert sorted(result["added"]) == ["site/index.html", "src/main.py"]
    assert {s["path"] for s in result["skipped"]} == {"site/node_modules/x.js", "photo.png"}
    project = result["project"]
    assert project["files"] == 2 and project["context"]["inline"] is True
    assert [f["path"] for f in project["file_list"]] == ["site/index.html", "src/main.py"]
    listed = http.get("/v1/projects", headers=AUTH, params=ME).json()["projects"]
    assert [p["id"] for p in listed] == [project["id"]]
    file = http.get(f"/v1/projects/{project['id']}/file", headers=AUTH, params={**ME, "path": "src/main.py"}).json()
    assert (file["kind"], file["content"]) == ("python", "print('hi')")
    renamed = http.patch(f"/v1/projects/{project['id']}", headers=AUTH, json={**ME, "name": "Renamed"}).json()
    assert renamed["name"] == "Renamed" and renamed["instructions"] == "Use TypeScript"
    removed = http.delete(f"/v1/projects/{project['id']}/files", headers=AUTH, params={**ME, "path": "site", "folder": True})
    assert removed.json()["removed"] == 1
    assert http.delete(f"/v1/projects/{project['id']}", headers=AUTH, params=ME).json() == {"ok": True, "conversations_moved": 0}
    assert http.get("/v1/projects", headers=AUTH, params=ME).json()["projects"] == []


def test_a_project_is_its_owners_only(http):
    project = new_project(http)
    http.post("/v1/projects", headers=AUTH, json={**OTHER, "name": "Zoe's"})
    assert http.get(f"/v1/projects/{project['id']}", headers=AUTH, params=OTHER).status_code == 404
    assert upload(http, project["id"], {"a.txt": "a"}, who=OTHER).status_code == 404
    assert http.delete(f"/v1/projects/{project['id']}", headers=AUTH, params=OTHER).status_code == 404
    chat = {**OTHER, "message": "hi", "conversation": "web:zoe:1", "project": project["id"]}
    assert http.post("/v1/chat", headers=AUTH, json=chat).status_code == 404


def test_a_conversation_of_a_project_sees_its_files_and_instructions(http, fake):
    project = new_project(http, instructions="Always answer in haiku")
    upload(http, project["id"], {"notes.md": "The secret word is pineapple."})
    fake.rounds.append(say("ok"))
    chat = {**ME, "message": "What is the secret word?", "conversation": "web:erwan:1", "project": project["id"]}
    assert http.post("/v1/chat", headers=AUTH, json=chat).status_code == 200
    messages, tools = fake.calls[-1]
    system = messages[0]["content"]
    assert "## Project: My app" in system and "Always answer in haiku" in system and "pineapple" in system
    assert "read_project_file" not in {t["function"]["name"] for t in tools}  # the files are in the prompt
    listed = http.get("/v1/conversations", headers=AUTH, params={**ME, "project": project["id"]}).json()["conversations"]
    assert [c["id"] for c in listed] == ["web:erwan:1"] and listed[0]["project"] == project["id"]
    assert http.get("/v1/conversations", headers=AUTH, params={**ME, "project": "none"}).json()["conversations"] == []
    # the next message stays in the project, whatever it says
    fake.rounds.append(say("ok"))
    http.post("/v1/chat", headers=AUTH, json={**ME, "message": "again", "conversation": "web:erwan:1"})
    assert "pineapple" in fake.calls[-1][0][0]["content"]


def test_someone_else_in_the_conversation_does_not_get_the_project(http, fake):
    project = new_project(http)
    upload(http, project["id"], {"notes.md": "The secret word is pineapple."})
    fake.rounds += [say("ok"), say("ok")]
    http.post("/v1/chat", headers=AUTH, json={**ME, "message": "hi", "conversation": "web:shared", "project": project["id"]})
    http.post("/v1/chat", headers=AUTH, json={**OTHER, "message": "what is the word?", "conversation": "web:shared"})
    assert "pineapple" not in fake.calls[-1][0][0]["content"]


def test_a_big_project_is_read_with_tools(settings, fake):
    app = create_app(replace(settings, project_inline_percent=0), fake_providers(settings, fake))
    with TestClient(app) as http:
        project = new_project(http)
        upload(http, project["id"], {"src/a.py": "def secret():\n    return 42\n"})
        fake.rounds += [call("search_project", query="secret"), say("Found it.")]
        chat = {**ME, "message": "Where is secret?", "conversation": "web:erwan:2", "project": project["id"]}
        reply = http.post("/v1/chat", headers=AUTH, json=chat).json()
        assert reply["reply"] == "Found it." and reply["tools"] == ["search_project"]
        first, second = fake.calls[-2], fake.calls[-1]
        assert "return 42" not in first[0][0]["content"] and "- src/a.py" in first[0][0]["content"]
        assert {"read_project_file", "search_project", "list_project_files"} <= {t["function"]["name"] for t in first[1]}
        assert "src/a.py:1: def secret():" in second[0][-1]["content"]


def test_project_tools_are_not_offered_outside_projects(http, fake):
    fake.rounds.append(say("hi"))
    http.post("/v1/chat", headers=AUTH, json={**ME, "message": "hi", "conversation": "web:erwan:3"})
    assert not {"read_project_file", "search_project"} & {t["function"]["name"] for t in fake.calls[-1][1]}


def test_a_conversation_moves_into_and_out_of_a_project(http, fake):
    project = new_project(http)
    fake.rounds.append(say("ok"))
    http.post("/v1/chat", headers=AUTH, json={**ME, "message": "hi", "conversation": "web:erwan:4"})
    path = "/v1/conversations/web:erwan:4"
    assert http.patch(path, headers=AUTH, json={**ME, "project": project["id"]}).json()["project"] == project["id"]
    assert http.patch(path, headers=AUTH, json={**ME, "title": "T"}).json()["project"] == project["id"]  # not given: kept
    assert http.patch(path, headers=AUTH, json={**ME, "project": None}).json()["project"] is None
    assert http.patch(path, headers=AUTH, json={**ME, "project": 999}).status_code == 404
    http.patch(path, headers=AUTH, json={**ME, "project": project["id"]})
    moved = http.delete(f"/v1/projects/{project['id']}", headers=AUTH, params=ME).json()
    assert moved["conversations_moved"] == 1
    assert http.get("/v1/conversations", headers=AUTH, params={**ME, "project": "none"}).json()["conversations"][0]["id"] == "web:erwan:4"


# --- GitHub ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), [
    ("octo/hello", ("octo/hello", "")),
    ("octo/hello@dev", ("octo/hello", "dev")),
    ("https://github.com/octo/hello.git", ("octo/hello", "")),
    ("https://github.com/octo/hello/tree/feature/x", ("octo/hello", "feature/x")),
    ("github.com/octo/my.repo", ("octo/my.repo", "")),
    ("git@github.com:octo/hello.git", ("octo/hello", "")),
])
def test_repositories_are_read_from_what_people_type(text, expected):
    assert parse_repo(text) == expected


def test_what_is_not_a_repository_is_refused():
    with pytest.raises(GitHubError):
        parse_repo("https://gitlab.com/octo/hello")


def fake_github(archive: bytes, seen: list):
    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/repos/octo/hello":
            return httpx.Response(200, json={"full_name": "octo/hello", "default_branch": "main"})
        if path == "/repos/octo/hello/commits/main":
            return httpx.Response(200, text="abc123")
        if path == "/repos/octo/hello/zipball/abc123":
            return httpx.Response(200, content=archive)
        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.MockTransport(answer)


def test_a_repository_is_downloaded_synced_and_removed(app, http):
    seen: list[httpx.Request] = []
    archive = make_zip({"octo-hello-abc123/README.md": "# Hello", "octo-hello-abc123/src/x.py": "x = 1"})
    app.state.github = GitHub("gh-token", fake_github(archive, seen))
    project = new_project(http)
    response = http.post(f"/v1/projects/{project['id']}/github", headers=AUTH, json={**ME, "repo": "https://github.com/octo/hello"})
    assert response.status_code == 200, response.text
    project = response.json()["project"]
    assert [f["path"] for f in project["file_list"]] == ["hello/README.md", "hello/src/x.py"]
    source = project["sources"][0]
    assert (source["repo"], source["commit"], source["files"], source["folder"]) == ("octo/hello", "abc123", 2, "hello")
    assert seen[0].headers["authorization"] == "Bearer gh-token"
    again = http.post(f"/v1/projects/{project['id']}/github", headers=AUTH, json={**ME, "repo": "octo/hello"})
    assert again.status_code == 409
    archive_v2 = make_zip({"octo-hello-abc123/README.md": "# Hello again"})
    app.state.github = GitHub(None, fake_github(archive_v2, seen))
    synced = http.post(f"/v1/projects/{project['id']}/sources/{source['id']}/sync", headers=AUTH, json=ME).json()
    assert [f["path"] for f in synced["project"]["file_list"]] == ["hello/README.md"]
    removed = http.delete(f"/v1/projects/{project['id']}/sources/{source['id']}", headers=AUTH, params=ME).json()
    assert removed["removed"] == 1 and removed["project"]["files"] == 0


def test_a_repository_that_cannot_be_found_is_not_kept(app, http):
    app.state.github = GitHub(None, fake_github(b"", []))
    project = new_project(http)
    response = http.post(f"/v1/projects/{project['id']}/github", headers=AUTH, json={**ME, "repo": "octo/missing"})
    assert response.status_code == 502
    assert "not found" in response.json()["detail"] and "GITHUB_TOKEN" in response.json()["detail"]
    assert http.get(f"/v1/projects/{project['id']}", headers=AUTH, params=ME).json()["sources"] == []


def test_a_bad_base64_file_is_skipped(http):
    project = new_project(http)
    body = {**ME, "files": [{"path": "a.txt", "data": "%%%"}]}
    result = http.post(f"/v1/projects/{project['id']}/files", headers=AUTH, json=body).json()
    assert result["skipped"] == [{"path": "a.txt", "reason": "not sent as base64"}]


def test_traffic_and_json_shape_of_a_listing(http):
    project = new_project(http)
    listed = http.get("/v1/projects", headers=AUTH, params=ME).json()["projects"][0]
    assert set(listed) >= {"id", "name", "files", "size", "conversations", "context", "limits"}
    assert json.dumps(listed)  # plain JSON
    assert listed["context"]["window"] == 32_768 and project["limits"]["files"] == 5_000
