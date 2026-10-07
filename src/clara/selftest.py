"""`clara-server --test`: check that the server can run, and say what is wrong, before it starts.

Every check ends in PASS, WARN (the server runs, but not as you may expect), FAIL (it will not work properly)
or SKIP (not applicable with this configuration). Nothing is changed: the database is opened read-only, the
port is only tried, Tailscale is only asked for its status.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import socket
import sqlite3
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .providers import ProviderManager
from .settings import Settings
from .tailscale import Tailscale

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
COLORS = {PASS: "32", WARN: "33", FAIL: "31", SKIP: "90"}
WEB_DIR = Path(__file__).parent / "web"


@dataclass(frozen=True)
class Result:
    name: str
    status: str
    detail: str = ""


Report = Callable[[Result], None]


def check_configuration(settings: Settings) -> Result:
    # the settings are validated when they are read: reaching this point means they are consistent
    detail = (
        f"{len(settings.tokens)} chat token(s), {len(settings.admin_tokens)} admin token(s), "
        f"provider {settings.default_provider}"
    )
    if settings.unrestricted_clients:
        return Result(
            "configuration",
            WARN,
            f"{detail}; {', '.join(settings.unrestricted_clients)} may speak for any surface (CLARA_CLIENT_SURFACES)",
        )
    return Result("configuration", PASS, detail)


def check_data_dir(settings: Settings) -> Result:
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=settings.data_dir):
            pass
    except OSError as error:
        return Result("data directory", FAIL, f"{settings.data_dir} is not writable: {error}")
    return Result("data directory", PASS, f"{settings.data_dir} is writable")


def check_database(settings: Settings) -> Result:
    path = settings.db_path
    if not path.exists():
        return Result("database", PASS, f"{path} does not exist yet: it is created at the first start")
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            verdict = connection.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            connection.close()
    except sqlite3.Error as error:
        return Result("database", FAIL, f"{path} cannot be read: {error}")
    if verdict != "ok":
        return Result("database", FAIL, f"{path} is damaged: {verdict}")
    return Result("database", PASS, f"{path} is sound ({path.stat().st_size // 1024} KiB)")


def check_system_prompt(settings: Settings) -> Result:
    path = settings.system_prompt_file
    if not path.exists():
        return Result("system prompt", WARN, f"{path} does not exist: Clara has the default personality")
    try:
        size = len(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        return Result("system prompt", FAIL, f"{path} cannot be read: {error}")
    return Result("system prompt", PASS, f"{path} ({size} characters)")


def check_web_site(settings: Settings) -> Result:
    index = WEB_DIR / "index.html"
    if not index.is_file():
        return Result("web site", WARN, f"{index} is missing: the server has no web site (reinstall the package)")
    return Result("web site", PASS, f"{WEB_DIR}")


def check_port(settings: Settings) -> Result:
    where = f"{settings.host}:{settings.port}"
    try:
        family, kind, protocol, _, address = socket.getaddrinfo(
            settings.host, settings.port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )[0]
        with socket.socket(family, kind, protocol) as probe:
            if os.name != "nt":  # what the server does: a port in TIME_WAIT is usable
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(address)
    except OSError as error:
        return Result("port", FAIL, f"{where} cannot be used: {error} (is Clara already running?)")
    return Result("port", PASS, f"{where} is free")


async def check_provider(providers: ProviderManager) -> Result:
    config, model = providers.config, providers.model
    where = f"{config.label} at {config.host}"
    problem = await providers.check()
    if problem:
        return Result("model provider", FAIL, f"{where}: {problem}")
    try:
        models = await providers.list_models()
    except Exception:  # it just answered: the list is a bonus
        return Result("model provider", PASS, f"{where} answers (model {model})")
    if model not in models and f"{model}:latest" not in models:
        hint = f" (ollama pull {model})" if config.id == "local" else ""
        return Result("model provider", WARN, f"{where} answers, but it does not list the model {model!r}{hint}")
    return Result("model provider", PASS, f"{where} answers, model {model} is there")


async def check_tailscale(tailscale: Tailscale) -> Result:
    if not tailscale.enabled:
        return Result("tailscale", SKIP, "CLARA_TAILSCALE is off")
    problem = await tailscale.check()
    if problem:  # the server runs on localhost anyway
        return Result("tailscale", WARN, f"{tailscale.mode} would not be available: {problem}")
    return Result("tailscale", PASS, f"{tailscale.mode} can be set (port {tailscale.https_port})")


async def run_checks(
    settings: Settings,
    report: Report | None = None,
    providers: ProviderManager | None = None,
    tailscale: Tailscale | None = None,
) -> list[Result]:
    """Run every check, in order, telling `report` of each result as soon as it is known."""
    providers = providers or ProviderManager.from_settings(settings)
    tailscale = tailscale or Tailscale.from_settings(settings)
    checks: list[tuple[str, Callable[[], Result | Awaitable[Result]]]] = [
        ("configuration", lambda: check_configuration(settings)),
        ("data directory", lambda: check_data_dir(settings)),
        ("database", lambda: check_database(settings)),
        ("system prompt", lambda: check_system_prompt(settings)),
        ("web site", lambda: check_web_site(settings)),
        ("port", lambda: check_port(settings)),
        ("model provider", lambda: check_provider(providers)),
        ("tailscale", lambda: check_tailscale(tailscale)),
    ]
    results: list[Result] = []
    for name, check in checks:
        try:
            result = check()
            if inspect.isawaitable(result):
                result = await result
        except Exception as error:  # a check must never stop the others
            result = Result(name, FAIL, f"the check itself failed: {type(error).__name__}: {error}")
        results.append(result)
        if report is not None:
            report(result)
    return results


def format_result(result: Result, color: bool = False) -> str:
    tag = f"[{result.status}]"
    if color:
        tag = f"\x1b[{COLORS[result.status]}m{tag}\x1b[0m"
    return f"  {tag} {result.name:<15} {result.detail}"


def summary(results: list[Result]) -> str:
    counts = {status: sum(1 for result in results if result.status == status) for status in (PASS, WARN, FAIL, SKIP)}
    return ", ".join(f"{count} {status.lower()}" for status, count in counts.items() if count)


def run_self_test(
    settings: Settings,
    *,
    providers: ProviderManager | None = None,
    tailscale: Tailscale | None = None,
    ask: Callable[[str], str] = input,
    interactive: bool | None = None,
) -> bool:
    """Run the checks and print each result. True when the server may start: nothing failed, or somebody
    answered yes to starting anyway (asked only when `interactive`, by default when there is a terminal)."""
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    print("Checking the installation...")
    results = asyncio.run(
        run_checks(settings, lambda result: print(format_result(result, color), flush=True), providers, tailscale)
    )
    print(f"Result: {summary(results)}.")
    if not any(result.status == FAIL for result in results):
        return True
    if not interactive:
        print("Not starting: a check failed.")
        return False
    try:
        answer = ask("Some checks failed. Start anyway? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() in ("y", "yes"):
        return True
    print("Not starting.")
    return False
