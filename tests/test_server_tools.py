"""HTTP side of client tools, compaction and keepalives (one test goes through a real server)."""

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from conftest import FakeBackend, call, fake_providers, say
from fastapi.testclient import TestClient

from clara.apicommon import with_keepalive
from clara.server import create_app

CHAT = {"Authorization": "Bearer secret-cli"}
OTHER = {"Authorization": "Bearer secret-discord"}
READ_FILE = {
    "type": "function",
    "function": {"name": "read_file", "description": "Read a file.", "parameters": {"type": "object", "properties": {}}},
}
BODY = {"surface": "console", "user_id": "erwan", "message": "hi"}


def events_of(response: httpx.Response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]


@pytest.fixture
def make_client(settings):
    clients = []

    def build(*rounds):
        client = TestClient(create_app(settings, fake_providers(settings, FakeBackend(*rounds))))
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def live(settings):
    """A real server on a free port: (start(*rounds) -> base url)."""
    running = []

    def start(*rounds, **overrides):
        from dataclasses import replace

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        app = create_app(replace(settings, port=port, **overrides), fake_providers(settings, FakeBackend(*rounds)))
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        assert server.started
        running.append((server, thread))
        return f"http://127.0.0.1:{port}", app

    yield start
    for server, thread in running:
        server.should_exit = True
        thread.join(timeout=10)


def test_client_tools_need_a_stream_and_a_valid_name(make_client):
    client = make_client()
    assert client.post("/v1/chat", json={**BODY, "tools": [READ_FILE]}, headers=CHAT).status_code == 422
    reserved = {"type": "function", "function": {"name": "remember"}}
    response = client.post("/v1/chat/stream", json={**BODY, "tools": [reserved]}, headers=CHAT)
    assert response.status_code == 422 and "remember" in response.json()["detail"]


def test_tool_results_for_an_unknown_turn(make_client):
    client = make_client()
    response = client.post("/v1/turns/nope/tool-results", json={"results": [{"id": "a", "content": "x"}]}, headers=CHAT)
    assert response.status_code == 404
    assert client.post("/v1/turns/nope/tool-results", json={"results": []}).status_code == 401


def test_conversation_info_and_compaction_endpoints(make_client):
    client = make_client(say("answer", prompt_tokens=5000), say("A summary."))
    empty = client.get("/v1/conversations/console:erwan", headers=CHAT).json()
    assert empty["tokens"] == 0 and empty["summary"] == "" and empty["window"] > 0
    assert client.post("/v1/conversations/console:erwan/compact", json={}, headers=CHAT).status_code == 409

    client.post("/v1/chat", json=BODY, headers=CHAT)
    info = client.get("/v1/conversations/console:erwan", headers=CHAT).json()
    assert info["tokens"] == 5003 and info["messages"] == 2

    done = client.post("/v1/conversations/console:erwan/compact", json={"focus": "x"}, headers=CHAT)
    assert done.status_code == 200
    assert done.json()["summary"] == "A summary." and done.json()["after_percent"] <= done.json()["before_percent"]
    assert client.get("/v1/conversations/console:erwan", headers=CHAT).json()["messages"] == 0


def test_compaction_failure_is_a_502_not_a_crash(make_client):
    client = make_client(say("answer"))  # no round left for the summary: the fake blows up
    client.post("/v1/chat", json=BODY, headers=CHAT)
    assert client.post("/v1/conversations/console:erwan/compact", json={}, headers=CHAT).status_code == 502


async def test_keepalive_fills_the_silence():
    async def slow():
        yield {"type": "a"}
        await asyncio.sleep(0.35)
        yield {"type": "b"}

    seen = [item async for item in with_keepalive(slow(), interval=0.1)]
    assert seen[0] == {"type": "a"} and seen[-1] == {"type": "b"}
    assert seen.count(None) >= 2


def test_full_round_trip_over_http(live):
    url, app = live(call("read_file", path="x.txt"), say("It says hi."))
    body = {**BODY, "tools": [READ_FILE]}

    with httpx.Client(base_url=url, headers=CHAT, timeout=10) as http:
        with http.stream("POST", "/v1/chat/stream", json=body) as response:
            assert response.status_code == 200
            seen = []
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                seen.append(event)
                if event["type"] == "tool_requests":
                    # Another client cannot answer for us...
                    stranger = httpx.post(
                        f"{url}/v1/turns/{event['turn']}/tool-results", headers=OTHER, timeout=10,
                        json={"results": [{"id": event["calls"][0]["id"], "content": "evil"}]},
                    )
                    assert stranger.status_code == 404
                    # ...we can.
                    answer = http.post(
                        f"/v1/turns/{event['turn']}/tool-results",
                        json={"results": [{"id": event["calls"][0]["id"], "content": "hi from the file"}]},
                    )
                    assert answer.status_code == 200
    assert [e["type"] for e in seen][-1] == "done"
    assert seen[-1]["reply"] == "It says hi."
    tool_message = [m for m in app.state.memory.messages_after("console:erwan") if m.role == "tool"][0]
    assert tool_message.content == "hi from the file"


def test_a_client_that_disconnects_frees_the_conversation(live):
    url, app = live(call("read_file", path="x"), say("second turn works"))

    with httpx.Client(base_url=url, headers=CHAT, timeout=10) as http:
        with http.stream("POST", "/v1/chat/stream", json={**BODY, "tools": [READ_FILE]}) as response:
            for line in response.iter_lines():
                if line.startswith("data: ") and json.loads(line[6:])["type"] == "tool_requests":
                    break  # leave while the server waits for our tool results
        # the connection is closed; the server must notice and let go of the conversation
        deadline = time.time() + 5
        while app.state.agent._pending and time.time() < deadline:
            time.sleep(0.05)
        assert app.state.agent._pending == {}
        reply = http.post("/v1/chat", json=BODY, timeout=5)
    assert reply.status_code == 200 and reply.json()["reply"] == "second turn works"
