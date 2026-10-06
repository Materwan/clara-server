import json

import pytest
from conftest import FakeBackend, fake_providers, say
from fastapi.testclient import TestClient

from clara.server import create_app
from clara.settings import Settings, SettingsError

AUTH = {"Authorization": "Bearer secret-cli"}
ME = {"surface": "cli", "user_id": "erwan"}


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


def test_health_needs_no_token(make_client):
    assert make_client().get("/health").json() == {
        "status": "ok",
        "provider": "local",
        "model": "fake",
        "restarted": None,
    }


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "secret-cli"}]
)
def test_api_refuses_missing_or_wrong_tokens(make_client, headers):
    client = make_client()
    assert client.get("/v1/memory/facts", params=ME, headers=headers).status_code == 401
    body = {**ME, "message": "hi"}
    assert client.post("/v1/chat", json=body, headers=headers).status_code == 401


def test_chat(make_client):
    client = make_client(say("Hello ", "there"))
    response = client.post("/v1/chat", json={**ME, "user_name": "Erwan", "message": "hi"}, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["reply"] == "Hello there"
    assert response.json()["person"]["name"] == "Erwan"


def test_chat_stream_sends_sse_events(make_client):
    client = make_client(say("Hel", "lo"))
    with client.stream("POST", "/v1/chat/stream", json={**ME, "message": "hi"}, headers=AUTH) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
    assert [e["type"] for e in events] == ["turn", "token", "token", "usage", "done"]
    assert events[-1]["reply"] == "Hello"


def test_numeric_ids_are_accepted(make_client):
    client = make_client(say("ok"))
    body = {"surface": "discord", "user_id": 123456789012345678, "message": "hi"}
    response = client.post("/v1/chat", json=body, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["conversation"] == "discord:123456789012345678"


@pytest.mark.parametrize("surface", ["", "Has Space", "UPPER", "x" * 40])
def test_bad_surface_is_rejected(make_client, surface):
    response = make_client().post(
        "/v1/chat", json={"surface": surface, "user_id": "u", "message": "hi"}, headers=AUTH
    )
    assert response.status_code == 422


def test_model_failure_is_a_502_not_a_crash(make_client):
    client = make_client()  # no scripted round: the fake backend blows up
    response = client.post("/v1/chat", json={**ME, "message": "hi"}, headers=AUTH)
    assert response.status_code == 502


def test_model_failure_while_streaming_ends_with_an_error_event(make_client):
    client = make_client()
    with client.stream("POST", "/v1/chat/stream", json={**ME, "message": "hi"}, headers=AUTH) as r:
        events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
    assert events[-1]["type"] == "error"


def test_facts_roundtrip(make_client):
    client = make_client()
    created = client.post(
        "/v1/memory/facts", json={**ME, "user_name": "Erwan", "text": "Likes tea"}, headers=AUTH
    )
    assert created.status_code == 201 and created.json()["created"]
    listed = client.get("/v1/memory/facts", params=ME, headers=AUTH).json()
    assert [f["text"] for f in listed["facts"]] == ["Likes tea"]

    fact_id = listed["facts"][0]["id"]
    assert client.delete(f"/v1/memory/facts/{fact_id}", params=ME, headers=AUTH).status_code == 200
    assert client.delete(f"/v1/memory/facts/{fact_id}", params=ME, headers=AUTH).status_code == 404


def test_unknown_person_is_404(make_client):
    response = make_client().get("/v1/memory/facts", params=ME, headers=AUTH)
    assert response.status_code == 404


def test_link_accounts_shares_facts(make_client):
    client = make_client()
    client.post("/v1/memory/facts", json={**ME, "text": "Likes tea"}, headers=AUTH)
    code = client.post(
        "/v1/accounts/link-code", json={"surface": "discord", "user_id": 42}, headers=AUTH
    ).json()["code"]
    link = {
        "surface": "discord", "user_id": 42, "code": code, "to_surface": "cli", "to_user_id": "erwan",
    }
    response = client.post("/v1/accounts/link", json=link, headers=AUTH)
    assert response.json()["accounts"] == ["cli:erwan", "discord:42"]

    facts = client.get("/v1/memory/facts", params={"surface": "discord", "user_id": "42"}, headers=AUTH)
    assert [f["text"] for f in facts.json()["facts"]] == ["Likes tea"]


def test_clear_conversation(make_client):
    client = make_client(say("ok"))
    client.post("/v1/chat", json={**ME, "message": "hi"}, headers=AUTH)
    response = client.delete("/v1/conversations/cli:erwan", headers=AUTH)
    assert response.json() == {"deleted_messages": 2}


def test_settings_refuse_to_start_without_tokens():
    with pytest.raises(SettingsError):
        Settings.from_env({})


def test_settings_token_parsing():
    parsed = Settings.from_env({"CLARA_TOKENS": "a:one, b:two ,three"}).tokens
    assert parsed == {"one": "a", "two": "b", "three": "client"}


@pytest.mark.parametrize("variable", ["CLARA_TOKENS", "CLARA_ADMIN_TOKENS"])
def test_settings_refuse_the_placeholder_tokens_of_the_example(variable):
    env = {"CLARA_TOKENS": "terminal:secret-cli", variable: "terminal:Change-Me-too"}
    with pytest.raises(SettingsError, match="placeholder"):
        Settings.from_env(env)


def test_the_example_env_file_does_not_start_as_it_is():
    from pathlib import Path

    env = {}
    for line in (Path(__file__).parent.parent / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key] = value
    with pytest.raises(SettingsError, match="placeholder"):
        Settings.from_env(env)
    assert not env["CLARA_LOCAL_MODEL"].endswith("-cloud")  # a model you can pull, not one that needs a sign-in
