"""Restarting from the web site: update first, stop the careful way, say it came back."""

import json
import os
import subprocess

import pytest
from conftest import FakeBackend, fake_providers
from fastapi.testclient import TestClient

from clara import restart as restart_module
from clara.restart import RestartError, RestartService, Step, merged_env, relaunch, update_code, update_env
from clara.server import create_app

ADMIN = {"Authorization": "Bearer secret-admin"}
CHAT = {"Authorization": "Bearer secret-cli"}


class FakeLifecycle:
    def __init__(self):
        self.stopping = False
        self.stops = []

    def request_stop(self, force=False):
        self.stopping = True
        self.stops.append(force)
        return "Stopping: nothing is running."


@pytest.fixture
def service(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    return RestartService(tmp_path / "data", FakeLifecycle(), root=tmp_path, env_file=env)


def steps_ok(root, head):
    return [Step("git pull", True, "Already up to date."), Step(".env", True, ".env already up to date")]


# ---- .env -------------------------------------------------------------------------------------------------------

EXAMPLE = "# the tokens\nCLARA_TOKENS=change-me\n# CLARA_NEW=default\nPORT=8765\n"


def test_the_example_gets_the_values_of_the_old_env():
    merged = merged_env("CLARA_TOKENS=real\nPORT=9000\nOLD=kept\n", EXAMPLE)
    assert merged.splitlines()[:4] == ["# the tokens", "CLARA_TOKENS=real", "# CLARA_NEW=default", "PORT=9000"]
    assert merged.endswith("# Kept from the previous .env (not in .env.example any more)\nOLD=kept\n")


def test_a_new_option_appears_commented_and_the_last_value_wins():
    merged = merged_env("export PORT=1\nPORT=2\nCLARA_TOKENS=t\n", EXAMPLE)
    assert "PORT=2" in merged and "PORT=1" not in merged
    assert "# CLARA_NEW=default" in merged and "export" not in merged


def test_update_env_saves_the_old_file_only_when_it_changes(tmp_path):
    (tmp_path / ".env.example").write_text(EXAMPLE, encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("CLARA_TOKENS=real\n", encoding="utf-8")
    assert "previous file: .env.bak." in update_env(tmp_path).output
    assert "CLARA_TOKENS=real" in env.read_text(encoding="utf-8")
    assert len(list(tmp_path.glob(".env.bak.*"))) == 1
    assert update_env(tmp_path).output == ".env already up to date"
    assert len(list(tmp_path.glob(".env.bak.*"))) == 1


# ---- the steps ----------------------------------------------------------------------------------------------------

def fake_run(monkeypatch, results):
    """`_run` answers from `results` (command prefix -> (ok, output)); returns the commands that ran."""
    ran = []

    def run(command, cwd, timeout):
        ran.append(command)
        for prefix, result in results.items():
            if command[: len(prefix)] == list(prefix):
                return result
        return True, ""

    monkeypatch.setattr(restart_module, "_run", run)
    return ran


def test_a_failed_pull_stops_everything(monkeypatch, tmp_path):
    ran = fake_run(
        monkeypatch,
        {("git", "rev-parse"): (True, "a" * 40), ("git", "pull"): (False, "Not possible to fast-forward")},
    )
    steps = update_code(tmp_path, "b" * 40)
    assert [(s.name, s.ok) for s in steps] == [("git pull", False)]
    assert not any("pip" in command for command in ran)


def test_new_code_is_installed_and_a_failed_install_stops(monkeypatch, tmp_path):
    monkeypatch.setattr(restart_module.importlib.util, "find_spec", lambda name: object())
    ran = fake_run(monkeypatch, {("git", "rev-parse"): (True, "a" * 40)})
    assert [s.name for s in update_code(tmp_path, "b" * 40)] == ["git pull", "pip install", ".env"]
    assert any("pip" in command for command in ran)
    fake_run(
        monkeypatch,
        {("git", "rev-parse"): (True, "a" * 40), (restart_module.sys.executable,): (False, "no network")},
    )
    steps = update_code(tmp_path, "b" * 40)
    assert [(s.name, s.ok) for s in steps] == [("git pull", True), ("pip install", False)]


def test_nothing_is_installed_when_the_code_did_not_change(monkeypatch, tmp_path):
    monkeypatch.setattr(restart_module.importlib.util, "find_spec", lambda name: object())
    ran = fake_run(monkeypatch, {("git", "rev-parse"): (True, "a" * 40)})
    steps = update_code(tmp_path, "a" * 40)
    assert steps[1].output == "the code did not change: nothing to install"
    assert not any("pip" in command for command in ran)


# ---- the service --------------------------------------------------------------------------------------------------

async def test_restart_updates_writes_the_record_and_starts_stopping(service, monkeypatch):
    monkeypatch.setattr(restart_module, "update_code", steps_ok)
    done = await service.restart("erwan@web")
    assert service.requested and service.lifecycle.stops == [False]
    record = json.loads(service._file.read_text(encoding="utf-8"))
    assert record["id"] == done["id"] and record["by"] == "erwan@web" and "done_at" not in record
    with pytest.raises(RestartError, match="already"):
        await service.restart("erwan@web")


async def test_a_failed_step_leaves_the_server_running(service, monkeypatch):
    monkeypatch.setattr(restart_module, "update_code", lambda root, head: [Step("git pull", False, "conflict")])
    with pytest.raises(RestartError, match="(?s)git pull failed.*conflict") as caught:
        await service.restart("erwan@web")
    assert caught.value.status == 500
    assert not service.requested and service.lifecycle.stops == [] and not service._file.exists()
    monkeypatch.setattr(restart_module, "update_code", steps_ok)
    await service.restart("erwan@web")  # and it can be tried again


async def test_the_new_process_finishes_the_record_and_health_shows_it(service, monkeypatch):
    monkeypatch.setattr(restart_module, "update_code", steps_ok)
    done = await service.restart("erwan@web")
    assert service.restarted is None  # this process is the old one
    newcomer = RestartService(service._file.parent, FakeLifecycle(), root=service.root, env_file=service._env_file)
    assert newcomer.restarted == done["id"]
    assert json.loads(service._file.read_text(encoding="utf-8"))["done_at"]


async def test_a_changed_env_or_new_commits_make_a_restart_worth_it(service, monkeypatch):
    monkeypatch.setattr(restart_module, "git_head", lambda root: "a" * 40)
    monkeypatch.setattr(restart_module, "commits_behind", lambda root: 0)
    service.record_boot()
    assert (await service.status())["needed"] is False
    service._env_file.write_text("A=2\n", encoding="utf-8")
    monkeypatch.setattr(restart_module, "git_head", lambda root: "b" * 40)
    monkeypatch.setattr(restart_module, "commits_behind", lambda root: 2)
    status = await service.status(refresh=True)
    assert [r["id"] for r in status["reasons"]] == ["env", "code", "update"]
    assert "2 new commits" in status["reasons"][2]["text"]


# ---- relaunching ----------------------------------------------------------------------------------------------------

def test_relaunch_gives_the_new_server_the_environment_of_the_env_file(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("CLARA_PORT=9\nFROM_OUTSIDE=1\n", encoding="utf-8")
    monkeypatch.setenv("CLARA_PORT", "8")  # loaded from the old .env: dotenv would not replace it
    monkeypatch.setenv("GONE", "x")  # was in the old .env, is not any more
    monkeypatch.setenv("FROM_OUTSIDE", "2")  # set by the service manager: not the file's to change
    started = []
    monkeypatch.setattr(os, "execv", lambda exe, args: started.append(args))
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kwargs: started.append(args))
    relaunch(["--headless", "--test"], {"CLARA_PORT", "GONE"}, console=False, env_file=env)
    assert os.environ["CLARA_PORT"] == "9" and "GONE" not in os.environ and os.environ["FROM_OUTSIDE"] == "2"
    assert started[0][1:] == ["-m", "clara", "--headless"]  # the self-test is not run again


# ---- the API ----------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr(restart_module, "commits_behind", lambda root: None)
    with TestClient(create_app(settings, fake_providers(settings, FakeBackend()))) as http:
        yield http


def test_only_an_administrator_may_look_or_restart(client):
    assert client.get("/v1/admin/restart").status_code == 401
    assert client.get("/v1/admin/restart", headers=CHAT).status_code == 401
    assert client.post("/v1/admin/restart", json={}, headers=CHAT).status_code == 401
    assert not client.app.state.restart.requested


def test_the_status_says_whether_a_restart_is_needed(client):
    found = client.get("/v1/admin/restart", headers=ADMIN).json()
    assert found["in_progress"] is False and found["last"] is None


def test_asking_for_a_restart_updates_then_stops(client, monkeypatch):
    monkeypatch.setattr(restart_module, "update_code", steps_ok)
    response = client.post("/v1/admin/restart", json={}, headers=ADMIN)
    assert response.status_code == 202
    assert response.json()["id"] and response.json()["steps"][0]["name"] == "git pull"
    assert client.app.state.restart.requested and client.app.state.lifecycle.stopping
    assert client.get("/v1/admin/restart", headers=ADMIN).json()["in_progress"] is True
    assert client.post("/v1/admin/restart", json={}, headers=ADMIN).status_code == 409


def test_a_failed_update_answers_with_its_output_and_the_server_goes_on(client, monkeypatch):
    monkeypatch.setattr(restart_module, "update_code", lambda root, head: [Step("pip install", False, "no network")])
    response = client.post("/v1/admin/restart", json={}, headers=ADMIN)
    assert response.status_code == 500 and "no network" in response.json()["detail"]
    assert not client.app.state.lifecycle.stopping


def test_the_restart_command_does_the_same(client, monkeypatch):
    monkeypatch.setattr(restart_module, "update_code", steps_ok)
    output = client.post("/v1/admin/command", json={"line": "/restart"}, headers=ADMIN).json()["output"]
    assert "git pull: Already up to date." in output and "Stopping" in output
    assert client.app.state.restart.requested
