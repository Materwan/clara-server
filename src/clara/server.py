"""The HTTP API. Every client (terminal, Discord, web app...) talks to this and nothing else.

    GET    /health                      no auth
    POST   /v1/chat                     JSON answer
    POST   /v1/chat/stream              Server-Sent Events: token / tool / done / error
    GET    /v1/memory/facts             facts of an account's person
    POST   /v1/memory/facts
    DELETE /v1/memory/facts/{id}
    GET    /v1/settings                 the settings of an account's person (notify_after: seconds a task takes
    PATCH  /v1/settings                 before it notifies them when done; 0: never; null: the server's default)
    POST   /v1/accounts/link-code       a code proving control of an account
    POST   /v1/accounts/link            "this account is the same person as that one" (needs the code)
    POST   /v1/turns/{id}/tool-results  a client's answer to a `tool_requests` event
    POST   /v1/reminders                set a reminder: a text and a moment, announced to the person who set it
    GET    /v1/reminders                the reminders a person set that have not fired
    DELETE /v1/reminders/{id}           cancel one
    GET|POST|PATCH|DELETE /v1/tasks...  a person's to-do list: tasks with their reminders (taskapi.py)
    GET|POST|PATCH|DELETE /v1/schedules...  prompts Clara runs by herself at a set time, once or as a routine (scheduleapi.py)
    POST   /v1/notifications            send a notification to a person, now
    GET    /v1/notifications/stream     Server-Sent Events of an account: `reminder`, `notification`, `server`
                                        (also served as /v1/reminders/stream)
    GET    /v1/conversations            the conversations an account's person started on its surface (the web
                                        site and the app share theirs, auth.SHARED_SURFACES)
                                        (?project=<id>: those of a project; ?project=none: those in none)
    GET    /v1/conversations/{id}/messages  its questions and answers, to show it again (with the QCM asked, qcm.py)
    PATCH  /v1/conversations/{id}       rename, pin, move to a project
    GET|POST|PATCH|DELETE /v1/projects...  a person's projects: their files and repositories (projectapi.py)
    POST   /v1/conversations/{id}/title Clara writes its title (if nobody has)
    GET    /v1/conversations/{id}       size of the context, summary
    POST   /v1/conversations/{id}/compact   summarise the older messages
    DELETE /v1/conversations/{id}       forget the thread (facts are kept)
    GET    /v1/admin/commands           console commands, for completion   (admin token)
    POST   /v1/admin/command            run a console command              (admin token)

The routes are in their own modules (chatapi.py, notificationapi.py, accountapi.py, conversationapi.py, and the
others named above); this one builds the application and its services, and runs it.

Chat clients are trusted: the bearer token proves *which client* is calling, and
the client states which user is talking. Give each client its own token, and with
CLARA_CLIENT_SURFACES limit the surfaces (and so the people and conversations) it can reach.
Operator commands need a different kind of token (CLARA_ADMIN_TOKENS), so a
chat client can never switch the provider or edit memory.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from .accountapi import install as install_accounts
from .agent import Agent, ChatRequest
from .announce import compose
from .auth import Admin, peer_of
from .bodylimit import BodyLimitMiddleware
from .chatapi import install as install_chat
from .clientapi import install as install_clients
from .commands import CommandContext, CommandResult, registry
from .conversationapi import install as install_conversations
from .discord_bot.local import LocalBackend
from .discord_bot.service import DiscordService
from .github import GitHub
from .integrationapi import install as install_integrations
from .integrations.service import build as build_integrations
from .integrations.store import PENDING
from .keyapi import install as install_keys
from .lifecycle import Lifecycle
from .limits import UsageLimits
from .linking import LinkCodes
from .markdownapi import install as install_markdown
from .markdownfiles import MarkdownFiles
from .memory import Memory
from .modelapi import install as install_models
from .models import ModelCatalog
from .music import MusicAssistant
from .musicaccounts import MusicAccounts
from .musicapi import install as install_music
from .notificationapi import install as install_notifications
from .notifications import Notifier
from .private import harden_tree, private_dir
from .projectapi import install as install_projects
from .projects import Projects
from .prompt import SystemPrompt
from .providers import ProviderManager
from .ratelimit import FailureLimiter
from .reminders import ReminderService
from .restart import RestartService, relaunch
from .schedule import ScheduleService
from .scheduleapi import install as install_schedules
from .settings import Settings, SettingsError
from .tailscale import Tailscale
from .taskai import follow as follow_task
from .taskai import plan as plan_task
from .taskapi import install as install_tasks
from .tasks import TaskService
from .tools import default_toolbox
from .traffic import TrafficLog, TrafficMiddleware
from .usagelog import UsageLog
from .userkeys import UserKeys
from .users import Users
from .web import WebClient
from .webapi import SIGNUPS, SIGNUPS_BLOCK, install, install_web

log = logging.getLogger("clara")


class CommandBody(BaseModel):
    line: str = Field(min_length=1, max_length=2000)


def create_app(
    settings: Settings, providers: ProviderManager | None = None, tailscale: Tailscale | None = None
) -> FastAPI:
    private_dir(settings.data_dir)
    memory = Memory(settings.db_path)
    harden_tree(settings.data_dir)  # the database, the logs, the key: for the account that runs the server only
    link_codes = LinkCodes()
    for name in settings.unrestricted_clients:
        log.warning(
            "client %r may speak for any surface: set CLARA_CLIENT_SURFACES to limit it", name
        )
    providers = providers or ProviderManager.from_settings(settings)
    traffic = (
        TrafficLog(settings.logs_dir, settings.traffic_log_days, settings.traffic_log_max_body)
        if settings.traffic_log
        else None
    )
    providers.traffic = traffic
    notifier = Notifier(memory)
    reminders = ReminderService(memory, notifier=notifier)
    tasks = TaskService(memory, notifier, max_reminders=settings.task_max_reminders)
    schedules = ScheduleService(memory, notifier)
    web = WebClient(settings.ollama_api_key) if settings.web_tools and settings.ollama_api_key else None
    # the music_* tools and the Music page need Music Assistant's address; each person's player and token are theirs
    music = MusicAssistant(settings.music_assistant_url) if settings.music_assistant_url else None
    projects = Projects(
        memory, settings.project_max_bytes, settings.project_max_files, settings.project_inline_percent
    )
    markdown = MarkdownFiles(memory)
    limits = UsageLimits(memory, settings.default_daily_tokens)
    usage_log = UsageLog(memory)
    models = ModelCatalog(memory, providers, settings.weight_reference_b)
    integrations = build_integrations(memory, settings, notifier)
    user_keys = UserKeys(memory, integrations.vault, providers)
    models.user_keys = user_keys
    music_accounts = MusicAccounts(memory, integrations.vault)
    agent = Agent(
        memory,
        providers,
        default_toolbox(web, music, music_accounts),
        SystemPrompt(settings.system_prompt_file),
        history_turns=settings.history_turns,
        max_concurrent_llm=settings.max_concurrent_llm,
        max_turns_per_user=settings.max_turns_per_user,
        web_fetch_any_url=settings.web_fetch_any_url,
        max_tool_rounds=settings.max_tool_rounds,
        context_window=lambda: providers.context_window,
        compact_percent=settings.compact_percent,
        keep_recent_turns=settings.keep_recent_turns,
        facts_token_budget=settings.facts_token_budget,
        purge_summarised=settings.purge_summarised,
        tool_timeout=settings.tool_timeout,
        first_token_timeout=settings.llm_first_token_timeout,
        idle_timeout=settings.llm_idle_timeout,
        retries=settings.llm_retries,
        retry_delay=settings.llm_retry_delay,
        reminders=reminders,
        notifier=notifier,
        long_turn_seconds=settings.notify_long_turn,
        projects=projects,
        markdown=markdown,
        limits=limits,
        usage_log=usage_log,
        models=models,
        tasks=tasks,
        integrations=integrations.broker,
    )

    async def integration_followup(approval, project_id, message):
        """A turn that tells Clara how the requests she held ended (the person answered elsewhere)."""
        request = ChatRequest(
            surface=approval.surface, user_id=approval.user_id, user_name=None, message=message,
            conversation=approval.conversation, project=project_id,
        )
        async for _ in agent.turn(request, "integrations"):
            pass

    integrations.approvals.followup = integration_followup
    schedules.agent = agent
    schedules.waiting = lambda conversation: len(integrations.store.approvals_in(conversation, (PENDING,)))

    if settings.reminder_ai_timeout:
        ai_timeout = float(settings.reminder_ai_timeout)
        reminders.composer = lambda reminder: compose(agent, reminder, ai_timeout)
        tasks.plan_timeout = min(ai_timeout, tasks.plan_timeout)
        tasks.planner = lambda task, now: plan_task(agent, task, now, tasks.plan_timeout, tasks.family(task))
        tasks.follower = lambda task, now: follow_task(
            agent, task, now, ai_timeout, tasks.max_reminders, tasks.family(task)
        )
    lifecycle = Lifecycle(agent, reminders, tasks=tasks, schedules=schedules)
    restart = RestartService(settings.data_dir, lifecycle)
    users = Users(memory, settings.session_days, session_max_days=settings.session_max_days)
    users.prune()
    tailscale = tailscale or Tailscale.from_settings(settings)
    if tailscale.enabled:
        if settings.host not in ("127.0.0.1", "localhost", "::1"):
            log.warning(
                "CLARA_HOST=%s: the port is reachable without Tailscale too; keep 127.0.0.1 with CLARA_TAILSCALE",
                settings.host,
            )
        if tailscale.public and settings.admin_tokens:
            log.warning("CLARA_TAILSCALE=funnel: the remote admin console (CLARA_ADMIN_TOKENS) is public too")
    discord_bot = DiscordService(
        settings.discord_token, lambda: LocalBackend(app), settings.discord_invite_url, settings.discord_auto_start
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        lifecycle.loop = asyncio.get_running_loop()
        schedulers = [
            asyncio.create_task(loop.run())
            for loop in (reminders, tasks, schedules, integrations.approvals)
        ]
        # tailscaled may come up (or log in) after the server: keep trying. Slow if tailscale hangs: not before the server is up
        publishing = asyncio.create_task(tailscale.start_until_up())
        bot_start = None
        if settings.discord_auto_start:
            if discord_bot.state in ("unavailable", "no-token"):
                log.warning("AUTO_START_DISCORD_BOT is on, but: %s", discord_bot.describe())
            else:
                bot_start = asyncio.create_task(discord_bot.start())  # connecting takes seconds: in the background
        try:
            yield
        finally:
            if bot_start is not None and not bot_start.done():
                bot_start.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await bot_start
            await discord_bot.stop()
            publishing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await publishing
            await tailscale.stop()
            for scheduler in schedulers:
                scheduler.cancel()
            await asyncio.gather(*schedulers, return_exceptions=True)
            await integrations.approvals.close()
            # the HTTP clients kept open for the providers, the web tools, the music tools and the connectors
            await providers.aclose()
            if web is not None:
                await web.aclose()
            if music is not None:
                await music.aclose()
            for connector in integrations.connectors.values():
                await connector.aclose()
            memory.close()
            if traffic is not None:
                traffic.close()

    app = FastAPI(title="Clara", lifespan=lifespan)
    app.add_middleware(BodyLimitMiddleware)  # inside the traffic log: a refused request is logged too
    app.add_middleware(
        TrafficMiddleware, traffic=lambda: traffic, identify=lambda headers: peer_of(settings, app.state.users, headers)
    )
    app.state.settings = settings
    app.state.auth_limiter = FailureLimiter(settings.auth_max_failures, settings.auth_block_seconds)
    app.state.signup_limiter = FailureLimiter(SIGNUPS, SIGNUPS_BLOCK)
    app.state.users = users
    app.state.tailscale = tailscale
    app.state.memory = memory
    app.state.agent = agent
    app.state.reminders = reminders
    app.state.tasks = tasks
    app.state.notifier = notifier
    app.state.traffic = traffic
    app.state.lifecycle = lifecycle
    app.state.restart = restart
    app.state.providers = providers
    app.state.discord = discord_bot
    app.state.projects = projects
    app.state.markdown = markdown
    app.state.limits = limits
    app.state.usage_log = usage_log
    app.state.models = models
    app.state.user_keys = user_keys
    app.state.music = music
    app.state.music_accounts = music_accounts
    app.state.github = GitHub(settings.github_token)
    app.state.integrations = integrations
    app.state.schedules = schedules
    app.state.link_codes = link_codes
    app.state.commands = CommandContext(
        settings=settings, memory=memory, agent=agent, providers=providers, started_at=time.monotonic(),
        listen=f"{settings.host}:{settings.port}", lifecycle=lifecycle, notifier=notifier, tailscale=tailscale,
        users=users, discord=discord_bot, limits=limits, models=models, restart=restart, traffic=traffic,
    )

    @app.get("/health")
    async def health() -> dict:
        # `restarted`: the id of the restart that started this server, which whoever asked it waits for
        return {"status": "ok", "provider": providers.active, "model": providers.model, "restarted": restart.restarted}

    @app.get("/v1/admin/commands")
    async def admin_commands(admin: Admin) -> list[dict]:
        return registry.describe(app.state.commands)

    @app.post("/v1/admin/command")
    async def admin_command(body: CommandBody, admin: Admin, http: Request) -> dict:
        log.info("admin %s ran /%s", admin, body.line.lstrip("/").split(None, 1)[0])
        result = await registry.execute(body.line, app.state.commands)
        if result.sensitive:
            http.scope["clara_sensitive"] = True  # it holds a password: the traffic log does not keep it
        return {"output": result.output, "quit": result.quit}

    install_chat(app)
    install_notifications(app)
    install_accounts(app)
    install_conversations(app)
    install(app)
    install_clients(app)
    install_projects(app)
    install_markdown(app)
    install_models(app)
    install_keys(app)
    install_music(app)
    install_tasks(app)
    install_integrations(app)
    install_schedules(app)
    install_web(app)
    return app


def configure_logging(handler: logging.Handler | None = None) -> None:
    """Log to stderr, or to `handler` (the file of headless mode)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[handler or logging.StreamHandler()],
        force=True,
    )


class ClaraServer(uvicorn.Server):
    """uvicorn, except that Ctrl+C / SIGTERM stop the server the careful way (see lifecycle.py): the first
    one lets what is running finish, the second one does not wait."""

    def __init__(self, config: uvicorn.Config, lifecycle: Lifecycle):
        super().__init__(config)
        self.lifecycle = lifecycle
        lifecycle.on_exit = self.finish

    def finish(self, forced: bool) -> None:
        self.should_exit = True
        if forced:  # do not wait for the turns that are running: cut them and their connections
            self.force_exit = True
            for task in list(self.server_state.tasks):
                task.cancel()
            for connection in list(self.server_state.connections):
                transport = getattr(connection, "transport", None)
                if transport is not None:
                    transport.close()

    def handle_exit(self, sig: int, frame) -> None:
        if self.lifecycle.loop is None:  # not started yet: nothing to wait for
            super().handle_exit(sig, frame)
            return
        self._captured_signals.append(sig)  # raised again once we are done, as uvicorn does
        self.lifecycle.request_stop_threadsafe(force=self.lifecycle.stopping)


def uvicorn_config(app: FastAPI, settings: Settings, **options: Any) -> uvicorn.Config:
    # tailscaled proxies from this machine: trust its X-Forwarded-For, so logs and the limit of wrong tokens
    # see the real client (nothing else is trusted: anyone else could write whatever address they like).
    # No colours: under the console's patch_stdout the escape codes are shown as "?[32m", and the log file has none
    options = {"host": settings.host, "port": settings.port, "proxy_headers": True,
               "forwarded_allow_ips": "127.0.0.1,::1", "use_colors": False, **options}
    return uvicorn.Config(app, **options)


async def serve(app: FastAPI, settings: Settings, with_console: bool, headless: bool = False) -> None:
    """Run the HTTP server, plus the interactive console on the same event loop. `headless`: the log
    handlers are the ones of `configure_logging` (uvicorn would write to stderr, which is gone)."""
    lifecycle: Lifecycle = app.state.lifecycle
    if not with_console:
        options = {"log_config": None} if headless else {}
        await ClaraServer(uvicorn_config(app, settings, **options), lifecycle).serve()
        return

    from prompt_toolkit.patch_stdout import patch_stdout

    from .console import run_console

    # Everything is created inside patch_stdout so server logs print above the prompt
    with patch_stdout():
        configure_logging()  # again: the handler must capture the patched stderr
        server = ClaraServer(uvicorn_config(app, settings, access_log=False), lifecycle)
        server_task = asyncio.create_task(server.serve())
        while not server.started and not server_task.done():
            await asyncio.sleep(0.05)
        if server_task.done():  # could not start (port in use...): surface the reason
            await server_task
            return

        context: CommandContext = app.state.commands

        async def execute(line: str) -> CommandResult:
            return await registry.execute(line, context)

        console_task = asyncio.create_task(
            run_console(
                execute,
                registry.describe(context),
                banner="Clara console. /help for commands, /stop (or /quit, Ctrl+D) stops the server.",
                history_path=settings.data_dir / "console_history.txt",
            )
        )
        await asyncio.wait({server_task, console_task}, return_when=asyncio.FIRST_COMPLETED)
        if not server_task.done():  # the console was closed: stop the server without cutting anybody off
            log.info(lifecycle.request_stop())
            await server_task
        console_task.cancel()
        await asyncio.gather(server_task, console_task, return_exceptions=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="clara-server", description="Run the Clara server.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--headless",
        "--no-console",
        dest="headless",
        action="store_true",
        help="no interactive prompt, and survive the end of the terminal (an SSH session that closes): "
        "SIGHUP is ignored and the log goes to <data dir>/logs/clara-server.log",
    )
    mode.add_argument(
        "--test",
        action="store_true",
        help="check the installation (configuration, data, port, model provider, Tailscale...) and show the "
        "state of every check, then start normally (asking first if one failed)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        settings = Settings.from_env()
    except SettingsError as error:
        raise SystemExit(str(error)) from None
    if args.test:
        from .selftest import run_self_test

        if not run_self_test(settings):
            raise SystemExit(1)
    if args.headless:
        from .headless import detach_from_terminal, file_handler, log_path

        path = log_path(settings)
        configure_logging(file_handler(path))
        logging.captureWarnings(True)
        print(f"Headless: no console, SIGHUP ignored, the log is {path}", flush=True)
        detach_from_terminal()
    else:
        configure_logging()
    with_console = not args.headless and sys.stdin.isatty() and sys.stdout.isatty()
    app = create_app(settings)
    app.state.restart.record_boot()
    try:
        asyncio.run(serve(app, settings, with_console, args.headless))
    except KeyboardInterrupt:
        pass
    except Exception:
        if not args.headless:
            raise
        log.exception("clara-server crashed")  # nobody is watching a terminal: the log is the only trace
        raise SystemExit(1) from None
    if app.state.restart.requested:  # asked from the web site or with /restart: start again
        log.info("starting again")
        relaunch(sys.argv[1:] if argv is None else argv, app.state.restart.env_keys, with_console)


if __name__ == "__main__":
    main()
