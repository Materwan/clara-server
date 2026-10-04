import asyncio
import contextlib

import pytest
from conftest import FakeBackend, fake_providers, say
from fastapi.testclient import TestClient
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from clara.commands import CommandResult
from clara.console import build_completer, make_session, run_console
from clara.server import create_app

ADMIN = {"Authorization": "Bearer secret-admin"}
CHAT = {"Authorization": "Bearer secret-cli"}
COMMANDS = [
    {"name": "provider", "usage": "", "summary": "", "choices": ["local", "cloud"]},
    {"name": "status", "usage": "", "summary": "", "choices": []},
]


def completions(completer, text: str) -> list[str]:
    return [c.text for c in completer.get_completions(Document(text), None)]


def test_completion_suggests_commands_then_provider_names():
    completer = build_completer(COMMANDS)
    assert completions(completer, "/pro") == ["/provider"]
    assert completions(completer, "/provider ") == ["local", "cloud"]
    assert completions(completer, "/status ") == []


async def test_console_runs_lines_until_quit(capsys):
    seen = []

    async def execute(line: str) -> CommandResult:
        seen.append(line)
        return CommandResult(f"ran {line}", quit=line == "/quit")

    with create_pipe_input() as pipe:
        pipe.send_text("/status\n\n/provider local\n/quit\n/never\n")
        session = make_session(COMMANDS, None, input=pipe, output=DummyOutput())
        await run_console(execute, COMMANDS, banner="hello", session=session)

    assert seen == ["/status", "/provider local", "/quit"]  # blank line skipped, stops at /quit
    out = capsys.readouterr().out
    assert "hello" in out and "ran /provider local" in out


async def test_console_stops_on_ctrl_d():
    async def execute(line: str) -> CommandResult:
        raise AssertionError("nothing should run")

    with create_pipe_input() as pipe:
        pipe.send_text("\x04")  # Ctrl+D on an empty line
        session = make_session(COMMANDS, None, input=pipe, output=DummyOutput())
        await run_console(execute, COMMANDS, banner="", session=session)


# --- the remote console's server side -----------------------------------
@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, fake_providers(settings, FakeBackend(say("hi"))))) as c:
        yield c


def test_admin_commands_list_for_completion(client):
    response = client.get("/v1/admin/commands", headers=ADMIN)
    assert response.status_code == 200
    by_name = {entry["name"]: entry for entry in response.json()}
    assert by_name["provider"]["choices"] == ["local", "cloud", "gemini", "deepseek", "mistral"]


def test_admin_runs_a_command_and_switches_provider_for_everyone(client):
    response = client.post("/v1/admin/command", json={"line": "/provider cloud"}, headers=ADMIN)
    assert response.status_code == 200
    assert "Now using cloud" in response.json()["output"]
    assert response.json()["quit"] is False
    assert client.get("/health").json()["provider"] == "cloud"


def test_admin_quit_only_tells_the_console_to_close(client):
    response = client.post("/v1/admin/command", json={"line": "/quit"}, headers=ADMIN)
    assert response.json()["quit"] is True
    assert client.get("/health").status_code == 200  # the server keeps running


def test_chat_tokens_cannot_run_admin_commands(client):
    for headers in (CHAT, {}, {"Authorization": "Bearer nope"}):
        assert client.post("/v1/admin/command", json={"line": "/status"}, headers=headers).status_code == 401
        assert client.get("/v1/admin/commands", headers=headers).status_code == 401


def test_admin_tokens_cannot_chat(client):
    body = {"surface": "cli", "user_id": "x", "message": "hi"}
    assert client.post("/v1/chat", json=body, headers=ADMIN).status_code == 401


def test_remote_admin_is_off_without_admin_tokens(settings):
    from dataclasses import replace

    with TestClient(create_app(replace(settings, admin_tokens={}), fake_providers(settings))) as c:
        response = c.post("/v1/admin/command", json={"line": "/status"}, headers=ADMIN)
        assert response.status_code == 403
        assert "CLARA_ADMIN_TOKENS" in response.json()["detail"]


def test_chat_uses_the_provider_chosen_from_the_console(settings):
    local, cloud = FakeBackend(say("from local"), model="L"), FakeBackend(say("from cloud"), model="C")
    hosts = {"local": local, "cloud": cloud}
    from clara.providers import ProviderManager

    providers = ProviderManager.from_settings(settings, factory=lambda config, model: hosts[config.id])
    with TestClient(create_app(settings, providers)) as c:
        body = {"surface": "cli", "user_id": "x", "message": "hi"}
        assert c.post("/v1/chat", json=body, headers=CHAT).json()["reply"] == "from local"
        c.post("/v1/admin/command", json={"line": "/provider cloud"}, headers=ADMIN)
        assert c.post("/v1/chat", json=body, headers=CHAT).json()["reply"] == "from cloud"


async def test_embedded_console_runs_beside_the_server_and_stops_it(settings, monkeypatch):
    """serve(): the server answers while the console runs; when the console ends, the server stops."""
    import socket
    from dataclasses import replace

    import httpx

    import clara.console
    from clara.server import serve

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings = replace(settings, port=port)
    health = {}

    async def fake_console(execute, commands, **options):
        async with httpx.AsyncClient() as http:
            health.update((await http.get(f"http://127.0.0.1:{port}/health")).json())
        result = await execute("/provider cloud")  # the same executor the real prompt uses
        health["output"] = result.output

    monkeypatch.setattr(clara.console, "run_console", fake_console)
    # patch_stdout needs a real terminal, which a test run does not have
    monkeypatch.setattr("prompt_toolkit.patch_stdout.patch_stdout", contextlib.nullcontext)
    app = create_app(settings, fake_providers(settings))
    await asyncio.wait_for(serve(app, settings, with_console=True), timeout=20)

    assert health["provider"] == "local"
    assert "Now using cloud" in health["output"]
    assert app.state.providers.active == "cloud"
