"""Tailscale: the mapping is set at startup and removed at exit, whatever goes wrong stays a warning, and a public
URL cannot be brute-forced."""

import asyncio
import json
import threading
import time
from dataclasses import replace

import httpx
import pytest
import uvicorn
from conftest import FakeBackend, fake_providers, say
from fastapi.testclient import TestClient

from clara.commands import registry
from clara.ratelimit import FailureLimiter, is_local
from clara.server import create_app, uvicorn_config
from clara.settings import Settings, SettingsError
from clara.tailscale import CommandOutput, Tailscale

TARGET = "http://127.0.0.1:8765"
RUNNING = json.dumps({"BackendState": "Running", "Self": {"DNSName": "box.tail1234.ts.net."}})


class FakeTailscale:
    """Stands for the `tailscale` command: records what it is asked, answers from a table."""

    def __init__(self, **answers: CommandOutput):
        self.calls: list[list[str]] = []
        self.answers = answers

    async def __call__(self, command: list[str], timeout: float) -> CommandOutput:
        self.calls.append(command[1:])
        key = command[1]
        answer = self.answers.get(key)
        if isinstance(answer, Exception):
            raise answer
        if key == "status":
            return answer or CommandOutput(0, RUNNING)
        return answer or CommandOutput(0, "")


def make(mode="serve", port=443, **answers) -> tuple[Tailscale, FakeTailscale]:
    fake = FakeTailscale(**answers)
    return Tailscale(mode, port, TARGET, runner=fake), fake


# --- settings ---------------------------------------------------------------------------------------------


def env(**changes: str) -> dict[str, str]:
    base = {"CLARA_TOKENS": "terminal:" + "a" * 40, "CLARA_ADMIN_TOKENS": "ops:" + "b" * 40}
    return {**base, **changes}


def test_tailscale_is_off_by_default():
    settings = Settings.from_env(env())
    assert (settings.tailscale, settings.tailscale_port) == ("off", 443)


def test_tailscale_settings_are_read():
    settings = Settings.from_env(env(CLARA_TAILSCALE="Funnel", CLARA_TAILSCALE_PORT="8443"))
    assert (settings.tailscale, settings.tailscale_port) == ("funnel", 8443)


@pytest.mark.parametrize("changes", [{"CLARA_TAILSCALE": "public"}, {"CLARA_TAILSCALE_PORT": "8080"}])
def test_bad_tailscale_settings_are_refused(changes):
    with pytest.raises(SettingsError):
        Settings.from_env(env(**changes))


def test_funnel_refuses_short_tokens_but_serve_does_not():
    short = {"CLARA_TOKENS": "terminal:short-token"}
    with pytest.raises(SettingsError, match="terminal"):
        Settings.from_env(env(CLARA_TAILSCALE="funnel", **short))
    with pytest.raises(SettingsError, match="ops"):
        Settings.from_env(env(CLARA_TAILSCALE="funnel", CLARA_ADMIN_TOKENS="ops:short"))
    assert Settings.from_env(env(CLARA_TAILSCALE="serve", **short)).tailscale == "serve"
    assert Settings.from_env(env(CLARA_TAILSCALE="funnel")).tailscale == "funnel"


# --- the commands ----------------------------------------------------------------------------------------


async def test_serve_publishes_the_port_to_the_tailnet():
    tailscale, fake = make("serve")
    await tailscale.start()
    assert tailscale.url == "https://box.tail1234.ts.net" and tailscale.problem is None
    assert fake.calls == [
        ["status", "--json"],
        ["funnel", "--https=443", "off"],  # a public mapping left over from funnel mode must not stay
        ["serve", "--bg", "--https=443", TARGET],
    ]
    assert "your tailnet only" in tailscale.describe()


async def test_funnel_publishes_to_the_internet_on_the_chosen_port():
    tailscale, fake = make("funnel", port=8443)
    await tailscale.start()
    assert tailscale.url == "https://box.tail1234.ts.net:8443"
    assert fake.calls == [["status", "--json"], ["funnel", "--bg", "--https=8443", TARGET]]
    assert "public internet" in tailscale.describe()


async def test_stop_removes_the_mapping_it_made():
    tailscale, fake = make("funnel")
    await tailscale.start()
    fake.calls.clear()
    await tailscale.stop()
    assert fake.calls == [["funnel", "--https=443", "off"]]
    assert tailscale.url is None
    await tailscale.stop()  # once only
    assert len(fake.calls) == 1


async def test_nothing_is_run_when_off_and_nothing_is_removed_when_it_never_started():
    tailscale, fake = make("off")
    await tailscale.start()
    await tailscale.stop()
    assert fake.calls == [] and tailscale.describe() == "off"
    down, fake = make("serve", status=CommandOutput(0, json.dumps({"BackendState": "NeedsLogin", "Self": {"DNSName": "x."}})))
    await down.start()
    await down.stop()
    assert [call[0] for call in fake.calls] == ["status"]


async def test_tailscale_not_logged_in_is_a_problem_not_an_exception():
    tailscale, _ = make(status=CommandOutput(0, json.dumps({"BackendState": "NeedsLogin", "Self": {"DNSName": "x."}})))
    await tailscale.start()
    assert tailscale.url is None and "NeedsLogin" in tailscale.problem and "tailscale up" in tailscale.problem
    assert "NOT AVAILABLE" in tailscale.describe()


async def test_a_missing_binary_is_a_problem():
    tailscale, _ = make(status=FileNotFoundError())
    await tailscale.start()
    assert "CLARA_TAILSCALE_BIN" in tailscale.problem


async def test_unreadable_status_is_a_problem():
    tailscale, _ = make(status=CommandOutput(0, "not json"))
    await tailscale.start()
    assert tailscale.url is None and "status" in tailscale.problem


async def test_a_failing_command_reports_its_output_and_the_operator_hint():
    tailscale, _ = make("funnel", funnel=CommandOutput(1, "Access denied: serve config denied"))
    await tailscale.start()
    assert tailscale.url is None
    assert "Access denied" in tailscale.problem and "--operator" in tailscale.problem


async def test_a_command_waiting_for_approval_is_cut_and_explained():
    tailscale, _ = make("funnel", funnel=CommandOutput(None, "Funnel is not enabled. Visit https://login.tailscale.com/f/funnel"))
    await tailscale.start()
    assert "did not finish" in tailscale.problem and "login.tailscale.com/f/funnel" in tailscale.problem


class Starting(FakeTailscale):
    """Reports each of the given states for `status`, in turn, then Running."""

    def __init__(self, *states: str):
        super().__init__()
        self.states = list(states)

    async def __call__(self, command: list[str], timeout: float) -> CommandOutput:
        if command[1] != "status" or not self.states:
            return await super().__call__(command, timeout)
        self.calls.append(command[1:])
        state = self.states.pop(0)
        return CommandOutput(0, json.dumps({"BackendState": state, "Self": {"DNSName": "box.tail1234.ts.net."}}))


async def test_start_until_up_tries_again_until_tailscale_runs():
    fake = Starting("NeedsLogin", "Starting")
    tailscale = Tailscale("serve", 443, TARGET, runner=fake)
    await tailscale.start_until_up(first_wait=0, longest_wait=0)
    assert tailscale.url == "https://box.tail1234.ts.net" and tailscale.problem is None
    assert fake.calls.count(["status", "--json"]) == 3
    assert ["serve", "--bg", "--https=443", TARGET] in fake.calls


async def test_start_until_up_waits_longer_after_each_failure(monkeypatch):
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", record)
    tailscale = Tailscale("serve", 443, TARGET, runner=Starting("NeedsLogin", "NeedsLogin", "NeedsLogin", "NeedsLogin"))
    await tailscale.start_until_up(first_wait=5, longest_wait=12)
    assert waits == [5, 10, 12, 12]
    assert tailscale.url is not None


async def test_start_until_up_keeps_its_problem_until_cancelled():
    tailscale = Tailscale("serve", 443, TARGET, runner=Starting(*["NeedsLogin"] * 1000))
    task = asyncio.create_task(tailscale.start_until_up(first_wait=0.01, longest_wait=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tailscale.url is None and "NeedsLogin" in tailscale.problem


async def test_start_until_up_does_nothing_when_off():
    tailscale, fake = make("off")
    await tailscale.start_until_up(first_wait=0, longest_wait=0)
    assert fake.calls == []


def test_the_target_follows_the_listening_address(settings):
    assert Tailscale.from_settings(replace(settings, host="0.0.0.0", port=9000)).target == "http://127.0.0.1:9000"
    assert Tailscale.from_settings(replace(settings, host="::1")).target == "http://[::1]:8765"


# --- in the application ----------------------------------------------------------------------------------


def app_with(settings, tailscale):
    return create_app(settings, fake_providers(settings, FakeBackend(say("ok"))), tailscale)


def test_the_server_publishes_at_startup_and_unpublishes_at_exit(settings):
    tailscale, fake = make("serve")
    with TestClient(app_with(settings, tailscale)):
        deadline = time.time() + 5
        while tailscale.url is None and time.time() < deadline:
            time.sleep(0.01)
        assert tailscale.url == "https://box.tail1234.ts.net"
    assert fake.calls[-1] == ["serve", "--https=443", "off"]


async def test_status_shows_the_url_only_when_tailscale_is_on(settings):
    async def status(tailscale):
        return (await registry.execute("/status", app_with(settings, tailscale).state.commands)).output

    on, _ = make("funnel")
    await on.start()
    assert "https://box.tail1234.ts.net (public internet)" in await status(on)
    assert "Tailscale" not in await status(make("off")[0])


# --- wrong tokens ----------------------------------------------------------------------------------------


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def test_an_address_is_blocked_after_too_many_wrong_tokens_then_released():
    clock = Clock()
    limiter = FailureLimiter(3, 300, clock)
    assert [limiter.failed("8.8.8.8") for _ in range(3)] == [False, False, True]
    assert 0 < limiter.blocked_for("8.8.8.8") <= 300
    assert limiter.blocked_for("1.1.1.1") == 0
    clock.now = 301
    assert limiter.blocked_for("8.8.8.8") == 0


def test_failures_spread_over_more_than_a_minute_do_not_add_up():
    clock = Clock()
    limiter = FailureLimiter(3, 300, clock)
    for _ in range(5):
        assert not limiter.failed("8.8.8.8")
        clock.now += 40
    assert limiter.blocked_for("8.8.8.8") == 0


def test_a_good_token_forgets_the_failures_and_zero_turns_the_limit_off():
    limiter = FailureLimiter(3, 300, Clock())
    limiter.failed("8.8.8.8")
    limiter.failed("8.8.8.8")
    limiter.succeeded("8.8.8.8")
    assert not limiter.failed("8.8.8.8")
    off = FailureLimiter(0, 300, Clock())
    assert not any(off.failed("8.8.8.8") for _ in range(50)) and off.blocked_for("8.8.8.8") == 0


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_this_machine_is_never_blocked(address):
    limiter = FailureLimiter(1, 300, Clock())
    assert is_local(address) and not limiter.failed(address) and limiter.blocked_for(address) == 0


def test_a_flood_of_addresses_does_not_grow_the_memory_without_bound():
    limiter = FailureLimiter(10, 300, Clock())
    for number in range(25_000):
        limiter.failed(f"10.{number // 65536}.{number // 256 % 256}.{number % 256}")
    assert len(limiter._failures) + len(limiter._blocked) <= 10_000


def test_wrong_tokens_end_in_429_with_retry_after_and_block_the_right_token_too(settings):
    limited = replace(settings, auth_max_failures=3, auth_block_seconds=120)
    with TestClient(app_with(limited, make()[0]), client=("203.0.113.9", 4000)) as http:
        bad = {"Authorization": "Bearer nope"}
        assert [http.get("/v1/memory/facts", params={"surface": "cli", "user_id": "e"}, headers=bad).status_code
                for _ in range(3)] == [401, 401, 401]
        blocked = http.get("/v1/memory/facts", params={"surface": "cli", "user_id": "e"},
                           headers={"Authorization": "Bearer secret-cli"})
        assert blocked.status_code == 429 and 0 < int(blocked.headers["Retry-After"]) <= 120
        assert http.post("/v1/admin/command", json={"line": "/status"},
                         headers={"Authorization": "Bearer secret-admin"}).status_code == 429
        assert http.get("/health").status_code == 200  # no token needed there


def test_the_admin_route_counts_wrong_tokens_too(settings):
    limited = replace(settings, auth_max_failures=2)
    with TestClient(app_with(limited, make()[0]), client=("203.0.113.9", 4000)) as http:
        chat_token = {"Authorization": "Bearer secret-cli"}  # right for chat, wrong for admin
        codes = [http.post("/v1/admin/command", json={"line": "/status"}, headers=chat_token).status_code for _ in range(3)]
    assert codes == [401, 401, 429]


def test_this_machine_can_get_the_token_wrong_as_often_as_it_likes(settings):
    limited = replace(settings, auth_max_failures=2)
    with TestClient(app_with(limited, make()[0]), client=("127.0.0.1", 4000)) as http:
        for _ in range(5):
            assert http.get("/v1/memory/facts", headers={"Authorization": "Bearer nope"}).status_code == 401


# --- the client's address behind tailscaled -------------------------------------------------------------


@pytest.fixture
def served(settings):
    """A real server configured like the real one, to be called from 127.0.0.1 like tailscaled does."""
    app = app_with(settings, make()[0])
    server = uvicorn.Server(uvicorn_config(app, settings, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    yield app, f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    thread.join(10)


def test_uvicorn_trusts_only_this_machine_for_the_forwarded_address(settings):
    config = uvicorn_config(create_app(settings, fake_providers(settings, FakeBackend())), settings)
    assert config.proxy_headers and config.forwarded_allow_ips == "127.0.0.1,::1"


def test_the_traffic_log_and_the_limit_see_the_client_behind_the_proxy(served):
    app, url = served
    bad = {"Authorization": "Bearer nope", "X-Forwarded-For": "198.51.100.7"}
    for _ in range(app.state.settings.auth_max_failures):
        assert httpx.get(f"{url}/v1/memory/facts", headers=bad).status_code == 401
    assert httpx.get(f"{url}/v1/memory/facts", headers=bad).status_code == 429
    # another visitor behind the same proxy is not affected, and neither is the proxy's own machine
    other = {"Authorization": "Bearer nope", "X-Forwarded-For": "198.51.100.8"}
    assert httpx.get(f"{url}/v1/memory/facts", headers=other).status_code == 401
    assert httpx.get(f"{url}/v1/memory/facts", headers={"Authorization": "Bearer nope"}).status_code == 401
    app.state.traffic.flush()
    paths = list(app.state.settings.logs_dir.glob("traffic-*.jsonl"))
    sources = {json.loads(line).get("from") for path in paths for line in path.read_text().splitlines()}
    assert "198.51.100.7" in sources and "198.51.100.8" in sources  # an address, not "127.0.0.1:port"
