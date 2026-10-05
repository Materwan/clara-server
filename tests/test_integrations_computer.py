"""Folders on the person's computer: the server hands jobs to the desktop app and takes its answers."""

import json
import threading
import time

import pytest
from conftest import FakeBackend, call, fake_providers, say
from fastapi.testclient import TestClient

from clara.server import create_app

AUTH = {"Authorization": "Bearer secret-cli"}
APP = {"surface": "app", "user_id": "erwan"}
WEB = {"surface": "web", "user_id": "erwan"}
CONVERSATION = "app:erwan:disk"
FOLDER = {**APP, "kind": "computer_path", "device": "pc-1", "alias": "docs", "label": "Docs on my PC"}


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def app(settings, backend):
    app = create_app(settings, fake_providers(settings, backend))
    app.state.integrations.connectors["computer"].read_wait = 3.0
    app.state.integrations.connectors["computer"].write_wait = 3.0
    memory = app.state.memory  # a signed-in user is one person on the web site and in the app
    memory.link_account("app", "erwan", memory.resolve("web", "erwan", None))
    return app


@pytest.fixture
def http(app):
    with TestClient(app) as client:
        yield client


@pytest.fixture
def folder(http):
    added = http.post("/v1/integrations/resources", headers=AUTH, json=FOLDER)
    assert added.status_code == 201, added.text
    attached = http.put(
        "/v1/integrations/attachments", headers=AUTH, json={**APP, "resource": added.json()["id"], "conversation": CONVERSATION}
    )
    assert attached.status_code == 200
    return added.json()


def chat(http):
    response = http.post("/v1/chat/stream", headers=AUTH, json={**APP, "message": "go", "conversation": CONVERSATION})
    return [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]


def results(events) -> list[str]:
    return [e["result"] for e in events if e["type"] == "tool"]


def online(app):
    """The app is running: a stream of notifications is open for the person on the surface `app`."""
    person = app.state.memory.resolve("app", "erwan", None)
    app.state.notifier._present[(person.id, "app")] = 1


class FakeApp(threading.Thread):
    """The desktop app: fetches the jobs and answers them with `answer(job)` -> (ok, text)."""

    def __init__(self, http, answer):
        super().__init__(daemon=True)
        self.http, self.answer, self.seen, self.stopping = http, answer, [], threading.Event()

    def run(self):
        while not self.stopping.is_set():
            jobs = self.http.get("/v1/integrations/jobs", headers=AUTH, params={**APP, "device": "pc-1"}).json()["jobs"]
            for job in jobs:
                self.seen.append(job)
                ok, text = self.answer(job)
                self.http.post(f"/v1/integrations/jobs/{job['id']}/result", headers=AUTH, json={**APP, "ok": ok, "text": text})
            time.sleep(0.05)


def test_only_the_app_adds_a_folder_of_a_computer(http):
    assert http.post("/v1/integrations/resources", headers=AUTH, json={**FOLDER, **WEB}).status_code == 403
    assert http.post("/v1/integrations/resources", headers=AUTH, json={**FOLDER, "alias": ""}).status_code == 422
    first = http.post("/v1/integrations/resources", headers=AUTH, json=FOLDER).json()
    again = http.post("/v1/integrations/resources", headers=AUTH, json=FOLDER).json()
    assert first["id"] == again["id"] and first["locator"] == {"device": "pc-1", "alias": "docs"}
    overview = http.get("/v1/integrations", headers=AUTH, params=WEB).json()  # the web site lists it
    assert [r["label"] for r in overview["resources"]] == ["Docs on my PC"]


def test_a_read_goes_to_the_app_and_comes_back(http, app, backend, folder):
    online(app)
    robot = FakeApp(http, lambda job: (True, f"{job['op']} {job['alias']} {job['args'].get('path', '')} done"))
    robot.start()
    backend.rounds = [call("res_read", resource=folder["id"], path="notes.md"), say("ok")]
    try:
        assert results(chat(http)) == ["read docs notes.md done"]
    finally:
        robot.stopping.set()
    assert robot.seen[0]["alias"] == "docs" and robot.seen[0]["args"]["path"] == "notes.md"


def test_the_app_failing_is_told_to_clara_as_an_error(http, app, backend, folder):
    online(app)
    robot = FakeApp(http, lambda job: (False, "That folder is not on this computer any more."))
    robot.start()
    backend.rounds = [call("res_list", resource=folder["id"]), say("ok")]
    try:
        assert results(chat(http)) == ["Error: That folder is not on this computer any more."]
    finally:
        robot.stopping.set()


def test_a_read_is_refused_at_once_when_the_app_is_not_running(http, backend, folder):
    backend.rounds = [call("res_read", resource=folder["id"], path="a"), say("I carry on without it.")]
    started = time.monotonic()
    events = chat(http)
    assert "not running" in results(events)[0] and time.monotonic() - started < 2
    assert events[-1]["type"] == "done"


def test_a_change_waits_in_a_queue_until_the_app_is_back(http, backend, folder):
    http.patch(f"/v1/integrations/resources/{folder['id']}", headers=AUTH, json={**APP, "levels": {"write": "allow"}})
    backend.rounds = [call("res_write", resource=folder["id"], path="todo.md", content="- milk"), say("ok")]
    assert results(chat(http))[0].startswith("Queued")
    jobs = http.get("/v1/integrations/jobs", headers=AUTH, params={**APP, "device": "pc-1"}).json()["jobs"]
    assert [(j["op"], j["alias"], j["args"]["path"]) for j in jobs] == [("write", "docs", "todo.md")]
    assert http.get("/v1/integrations/jobs", headers=AUTH, params={**APP, "device": "pc-1"}).json()["jobs"] == []  # once


def test_replacing_asks_even_on_a_computer_and_runs_there_after_approval(http, app, backend, folder):
    backend.rounds = [call("res_write", resource=folder["id"], path="notes.md", content="new", mode="overwrite"), say("Asked.")]
    events = chat(http)
    asked = next(e for e in events if e["type"] == "approval")["approval"]
    assert asked["level"] == "destructive"
    online(app)
    robot = FakeApp(http, lambda job: (True, "Replaced notes.md (3 characters)."))
    robot.start()
    backend.rounds = [say("Done.")]
    try:
        done = http.post(f"/v1/approvals/{asked['id']}/decide", headers=AUTH, json={**APP, "approve": True}).json()
    finally:
        robot.stopping.set()
    assert done["status"] == "done" and "Replaced notes.md" in done["result"]


def test_a_job_can_only_be_answered_by_its_own_person_and_once(http, app, backend, folder):
    http.patch(f"/v1/integrations/resources/{folder['id']}", headers=AUTH, json={**APP, "levels": {"write": "allow"}})
    backend.rounds = [call("res_write", resource=folder["id"], path="a.md", content="x"), say("ok")]
    chat(http)
    job = http.get("/v1/integrations/jobs", headers=AUTH, params={**APP, "device": "pc-1"}).json()["jobs"][0]
    stranger = http.post(f"/v1/integrations/jobs/{job['id']}/result", headers=AUTH, json={"surface": "app", "user_id": "zoe", "ok": True, "text": "x"})
    assert stranger.status_code == 404
    assert http.post(f"/v1/integrations/jobs/{job['id']}/result", headers=AUTH, json={**APP, "ok": True, "text": "Created a.md"}).status_code == 200
    assert http.post(f"/v1/integrations/jobs/{job['id']}/result", headers=AUTH, json={**APP, "ok": True, "text": "again"}).status_code == 409
    assert http.get("/v1/integrations/jobs", headers=AUTH, params={**WEB, "device": "pc-1"}).status_code == 403
    assert http.post(f"/v1/integrations/jobs/{job['id']}/result", headers=AUTH, json={**WEB, "ok": True, "text": "x"}).status_code == 403


async def test_a_stream_that_is_open_says_the_app_is_running(app):
    memory, notifier = app.state.memory, app.state.notifier
    person = memory.resolve("app", "erwan", None)
    assert not notifier.connected(person.id, "app")
    stream = notifier.events("client", "app", "erwan")
    await stream.__anext__()  # the state of the server: the stream is open
    assert notifier.connected(person.id, "app") and not notifier.connected(person.id, "discord")
    await stream.aclose()
    assert not notifier.connected(person.id, "app")


def test_old_jobs_are_given_up(app):
    store = app.state.integrations.store
    person = app.state.memory.resolve("app", "erwan", None)
    sent = store.add_job(person.id, "pc-1", "write", {})
    store.take_jobs(person.id, "pc-1")
    queued = store.add_job(person.id, "pc-1", "write", {})
    assert store.expire_jobs("2999-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00") == 1
    assert store.job(sent.id).status == "failed" and store.job(queued.id).status == "queued"
    assert store.expire_jobs("2000-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00") == 1
    assert store.job(queued.id).status == "failed"
