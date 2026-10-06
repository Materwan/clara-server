"""`--headless` (survive the end of the terminal) and `--test` (check the installation before starting)."""

import logging
import os
import signal
import socket
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import FakeBackend, fake_providers

from clara import headless, selftest
from clara.selftest import FAIL, PASS, SKIP, WARN, Result, run_checks, run_self_test
from clara.server import create_app, main, parse_args
from clara.tailscale import CommandOutput, Tailscale

# --- the arguments ----------------------------------------------------------------------------------------

# what `main` needs of the app it builds when `serve` is replaced: the restart service says nothing was asked
FAKE_APP = SimpleNamespace(state=SimpleNamespace(restart=SimpleNamespace(record_boot=lambda: None, requested=False)))


def test_headless_and_test_exclude_each_other(capsys):
    for flags in (["--headless", "--test"], ["--test", "--no-console"]):
        with pytest.raises(SystemExit) as stop:
            parse_args(flags)
        assert stop.value.code == 2
        assert "not allowed with" in capsys.readouterr().err


def test_no_console_is_headless():
    assert parse_args(["--no-console"]).headless
    assert parse_args(["--headless"]).headless
    args = parse_args([])
    assert not args.headless and not args.test
    assert parse_args(["--test"]).test


@pytest.fixture
def restore_logging():
    """main() reconfigures the root logger: put it back, and release the log file."""
    root = logging.getLogger()
    before, level = root.handlers[:], root.level
    yield
    for handler in root.handlers:
        if handler not in before:
            handler.close()
    root.handlers[:] = before
    root.setLevel(level)
    logging.captureWarnings(False)


# --- headless ---------------------------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on this platform")
def test_headless_ignores_the_hangup_and_leaves_the_terminal(monkeypatch):
    handlers, redirected = {}, []
    monkeypatch.setattr(signal, "signal", lambda number, handler: handlers.update({number: handler}))
    monkeypatch.setattr(os, "dup2", lambda source, target: redirected.append(target))
    headless.detach_from_terminal()
    assert handlers == {signal.SIGHUP: signal.SIG_IGN}
    assert redirected == [0, 1, 2]


def test_headless_log_is_a_rotating_file_in_the_logs_directory(settings):
    path = headless.log_path(settings)
    assert path == settings.logs_dir / "clara-server.log"
    handler = headless.file_handler(path)
    try:
        record = logging.LogRecord("clara", logging.INFO, __file__, 1, "hello", None, None)
        handler.handle(record)
    finally:
        handler.close()
    assert "hello" in path.read_text(encoding="utf-8")


def test_main_headless_logs_to_the_file_and_detaches(settings, monkeypatch, capsys, restore_logging):
    calls = []
    monkeypatch.setattr("clara.server.Settings.from_env", classmethod(lambda cls, env=None: settings))
    monkeypatch.setattr(headless, "detach_from_terminal", lambda: calls.append("detached"))

    async def fake_serve(app, given, with_console, is_headless=False):
        calls.append(("serve", with_console, is_headless))

    monkeypatch.setattr("clara.server.serve", fake_serve)
    monkeypatch.setattr("clara.server.create_app", lambda given: FAKE_APP)
    main(["--headless"])
    assert calls == ["detached", ("serve", False, True)]
    assert str(headless.log_path(settings)) in capsys.readouterr().out
    assert headless.log_path(settings).exists()


def test_main_headless_records_a_crash_in_the_log(settings, monkeypatch, restore_logging):
    monkeypatch.setattr("clara.server.Settings.from_env", classmethod(lambda cls, env=None: settings))
    monkeypatch.setattr(headless, "detach_from_terminal", lambda: None)

    async def broken_serve(app, given, with_console, is_headless=False):
        raise RuntimeError("boom")

    monkeypatch.setattr("clara.server.serve", broken_serve)
    monkeypatch.setattr("clara.server.create_app", lambda given: FAKE_APP)
    with pytest.raises(SystemExit) as stop:
        main(["--headless"])
    assert stop.value.code == 1
    assert "boom" in headless.log_path(settings).read_text(encoding="utf-8")


# --- test -------------------------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def healthy(settings, tmp_path):
    """Settings under which every check passes (except Tailscale, which is off)."""
    prompt = tmp_path / "prompt.md"
    prompt.write_text("You are Clara.", encoding="utf-8")
    return replace(settings, port=free_port(), system_prompt_file=prompt)


def by_name(results: list[Result]) -> dict[str, Result]:
    return {result.name: result for result in results}


async def test_every_check_passes_on_a_sound_installation(healthy):
    seen: list[Result] = []
    results = await run_checks(healthy, seen.append, fake_providers(healthy), None)
    assert results == seen  # each result is reported as soon as it is known
    states = {name: result.status for name, result in by_name(results).items()}
    assert states == {
        "configuration": WARN,  # the two clients of the fixture may speak for any surface
        "data directory": PASS,
        "database": PASS,
        "system prompt": PASS,
        "web site": PASS,
        "port": PASS,
        "model provider": PASS,
        "tailscale": SKIP,
    }


async def test_an_existing_database_is_checked(healthy):
    create_app(healthy, fake_providers(healthy)).state.memory.close()  # creates and migrates it
    assert healthy.db_path.exists()
    assert by_name(await run_checks(healthy, providers=fake_providers(healthy)))["database"].status == PASS
    healthy.db_path.write_bytes(b"this is not a database" * 100)
    assert by_name(await run_checks(healthy, providers=fake_providers(healthy)))["database"].status == FAIL


async def test_a_busy_port_fails(healthy):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = replace(healthy, port=busy.getsockname()[1])
        result = by_name(await run_checks(taken, providers=fake_providers(taken)))["port"]
    assert result.status == FAIL
    assert str(taken.port) in result.detail


async def test_a_data_directory_that_is_a_file_fails(healthy, tmp_path):
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("x")
    results = by_name(await run_checks(replace(healthy, data_dir=not_a_directory), providers=fake_providers(healthy)))
    assert results["data directory"].status == FAIL


async def test_a_missing_system_prompt_only_warns(healthy, tmp_path):
    missing = replace(healthy, system_prompt_file=tmp_path / "nope.md")
    assert by_name(await run_checks(missing, providers=fake_providers(missing)))["system prompt"].status == WARN


async def test_unreachable_provider_fails_and_missing_model_warns(healthy):
    class Down(FakeBackend):
        async def verify(self):
            raise ConnectionError("connection refused")

    down = by_name(await run_checks(healthy, providers=fake_providers(healthy, Down())))["model provider"]
    assert down.status == FAIL and "connection refused" in down.detail

    other = replace(healthy, local_model="not-installed")
    missing = by_name(await run_checks(other, providers=fake_providers(other)))["model provider"]
    assert missing.status == WARN and "not-installed" in missing.detail


async def test_tailscale_is_checked_without_changing_anything(healthy):
    calls = []

    async def runner(command, timeout):
        calls.append(command[1:])
        return CommandOutput(1, "NeedsLogin") if "status" in command else CommandOutput(0, "")

    broken = Tailscale("serve", 443, "http://127.0.0.1:8765", runner=runner)
    result = by_name(await run_checks(healthy, providers=fake_providers(healthy), tailscale=broken))["tailscale"]
    assert result.status == WARN
    assert calls == [["status", "--json"]]  # no mapping was touched


async def test_a_check_that_crashes_does_not_stop_the_others(healthy, monkeypatch):
    def crash(settings):
        raise RuntimeError("bug")

    monkeypatch.setattr(selftest, "check_database", crash)
    results = by_name(await run_checks(healthy, providers=fake_providers(healthy)))
    assert results["database"].status == FAIL and "bug" in results["database"].detail
    assert results["port"].status == PASS


def run(settings, healthy_providers=None, **options):
    return run_self_test(settings, providers=healthy_providers or fake_providers(settings), **options)


def test_self_test_starts_straight_away_when_nothing_failed(healthy, capsys):
    assert run(healthy, ask=lambda question: pytest.fail("asked"), interactive=True)
    out = capsys.readouterr().out
    assert "[PASS] port" in out and "[SKIP] tailscale" in out
    assert "Result:" in out and "fail" not in out.lower()


def test_self_test_asks_before_starting_after_a_failure(healthy, capsys):
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    try:
        taken = replace(healthy, port=busy.getsockname()[1])
        answers = iter(["", "y"])
        questions = []

        def ask(question):
            questions.append(question)
            return next(answers)

        assert not run(taken, ask=ask, interactive=True)  # empty answer: no
        assert run(taken, ask=ask, interactive=True)  # "y": start anyway
    finally:
        busy.close()
    assert len(questions) == 2
    assert "[FAIL] port" in capsys.readouterr().out


def test_self_test_does_not_start_after_a_failure_without_a_terminal(healthy, capsys):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = replace(healthy, port=busy.getsockname()[1])
        assert not run(taken, ask=lambda question: pytest.fail("asked"), interactive=False)
    assert "Not starting" in capsys.readouterr().out


def test_main_with_test_does_not_start_when_refused(settings, monkeypatch):
    monkeypatch.setattr("clara.server.Settings.from_env", classmethod(lambda cls, env=None: settings))
    monkeypatch.setattr("clara.selftest.run_self_test", lambda given: False)
    monkeypatch.setattr("clara.server.serve", lambda *args: pytest.fail("started"))
    with pytest.raises(SystemExit) as stop:
        main(["--test"])
    assert stop.value.code == 1


def test_main_with_test_goes_on_normally_when_accepted(settings, monkeypatch):
    calls = []
    monkeypatch.setattr("clara.server.Settings.from_env", classmethod(lambda cls, env=None: settings))
    monkeypatch.setattr("clara.selftest.run_self_test", lambda given: calls.append("tested") or True)

    async def fake_serve(app, given, with_console, is_headless=False):
        calls.append(("serve", is_headless))

    monkeypatch.setattr("clara.server.serve", fake_serve)
    monkeypatch.setattr("clara.server.create_app", lambda given: FAKE_APP)
    main(["--test"])
    assert calls == ["tested", ("serve", False)]


def test_results_are_formatted_for_a_terminal():
    result = Result("port", FAIL, "taken")
    assert selftest.format_result(result) == "  [FAIL] port            taken"
    assert "\x1b[31m" in selftest.format_result(result, color=True)
    assert selftest.summary([result, Result("a", PASS), Result("b", PASS)]) == "2 pass, 1 fail"
