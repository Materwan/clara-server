"""The HTTP API. Every client (terminal, Discord, web app...) talks to this and nothing else.

    GET    /health                      no auth
    POST   /v1/chat                     JSON answer
    POST   /v1/chat/stream              Server-Sent Events: token / tool / done / error
    GET    /v1/memory/facts             facts of an account's person
    POST   /v1/memory/facts
    DELETE /v1/memory/facts/{id}
    POST   /v1/accounts/link-code       a code proving control of an account
    POST   /v1/accounts/link            "this account is the same person as that one" (needs the code)
    POST   /v1/turns/{id}/tool-results  a client's answer to a `tool_requests` event
    POST   /v1/reminders                set a reminder: a text and a moment, announced to the person who set it
    GET    /v1/reminders                the reminders a person set that have not fired
    DELETE /v1/reminders/{id}           cancel one
    POST   /v1/notifications            send a notification to a person, now
    GET    /v1/notifications/stream     Server-Sent Events of an account: `reminder`, `notification`, `server`
                                        (also served as /v1/reminders/stream)
    GET    /v1/conversations            the conversations an account's person started on its surface
    GET    /v1/conversations/{id}/messages  its questions and answers, to show it again
    PATCH  /v1/conversations/{id}       rename, pin
    POST   /v1/conversations/{id}/title Clara writes its title (if nobody has)
    GET    /v1/conversations/{id}       size of the context, summary
    POST   /v1/conversations/{id}/compact   summarise the older messages
    DELETE /v1/conversations/{id}       forget the thread (facts are kept)
    GET    /v1/admin/commands           console commands, for completion   (admin token)
    POST   /v1/admin/command            run a console command              (admin token)

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
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator, Callable, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .agent import (
    Agent,
    ChatRequest,
    ClientToolTimeout,
    ModelTimeout,
    NothingToCompact,
    NothingToTitle,
    PromptTooLarge,
    ServerStopping,
)
from .announce import compose
from .auth import Admin, Client, peer_of, require_account, require_conversation, require_space
from .clientapi import install as install_clients
from .commands import CommandContext, CommandResult, registry
from .discord_bot.local import LocalBackend
from .discord_bot.service import DiscordService
from .lifecycle import Lifecycle
from .linking import LinkCodes
from .memory import MAX_TITLE_LENGTH, ConversationInfo, Memory, MergeRefused, Person
from .notifications import MAX_TARGETS, MAX_TEXT, MAX_TITLE, SURFACE_RE, NotificationError, Notifier
from .prompt import SystemPrompt
from .providers import ProviderManager
from .ratelimit import FailureLimiter
from .reminders import ReminderError, ReminderService, describe
from .settings import Settings, SettingsError
from .tailscale import Tailscale
from .tools import default_toolbox
from .traffic import TrafficLog, TrafficMiddleware
from .users import Users
from .webapi import install, install_web

log = logging.getLogger("clara")

Surface = Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,32}$")]
ExternalId = Annotated[str, Field(min_length=1, max_length=128)]


class _Body(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def _ids_may_be_numbers(cls, value: Any) -> Any:
        # Discord ids are big integers; accept them without making clients stringify
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


MAX_ROSTER = 500


class RosterEntry(_Body):
    user_id: ExternalId
    name: str = Field(default="", max_length=80)


class ChatBody(_Body):
    surface: Surface
    user_id: ExternalId
    user_name: str | None = Field(default=None, max_length=80)
    message: str = Field(min_length=1, max_length=200_000)
    conversation: str | None = Field(default=None, min_length=1, max_length=200)
    # What a client can add to a turn (see agent.py):
    tools: list[dict[str, Any]] = Field(default_factory=list, max_length=200)  # tools it runs itself
    instructions: str = Field(default="", max_length=100_000)  # added to the system prompt
    prefix: str = Field(default="", max_length=50_000)  # put before the message, never summarised
    ephemeral: bool = False  # one-shot job: no persona, no memory, nothing stored
    timezone: str | None = Field(default=None, max_length=64)  # IANA name, e.g. "Europe/Paris"
    quiet: bool = False  # never notify the person about this turn (the client shows the answer anyway)
    # A group space (a Discord server, see README "Discord"): its id, its members, who the message is about
    space: str | None = Field(default=None, min_length=1, max_length=200)
    roster: list[RosterEntry] = Field(default_factory=list, max_length=MAX_ROSTER)
    focus: list[ExternalId] = Field(default_factory=list, max_length=20)
    mode: Literal["answer", "observe", "maybe"] = "answer"

    def to_request(self, roster: tuple[Person, ...] = (), focus: tuple[Person, ...] = (), mode: str = "") -> ChatRequest:
        return ChatRequest(
            self.surface, self.user_id, self.user_name, self.message, self.conversation,
            tuple(self.tools), self.instructions, self.prefix, self.ephemeral, self.timezone,
            quiet=self.quiet or self.mode != "answer", space=self.space, roster=roster, focus=focus,
            mode=mode or self.mode,
        )


class ToolResult(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    content: str = Field(max_length=2_000_000)


class ToolResultsBody(BaseModel):
    results: list[ToolResult] = Field(max_length=100)


class CompactBody(BaseModel):
    focus: str = Field(default="", max_length=2000)


class FactBody(_Body):
    surface: Surface
    user_id: ExternalId
    user_name: str | None = Field(default=None, max_length=80)
    text: str = Field(min_length=1)


class LinkCodeBody(_Body):
    surface: Surface
    user_id: ExternalId


class LinkBody(_Body):
    surface: Surface  # the account to attach...
    user_id: ExternalId
    code: str = Field(min_length=1, max_length=100)  # ...proved with the code it was given...
    to_surface: Surface  # ...to the person who owns this one
    to_user_id: ExternalId


class ReminderBody(_Body):
    surface: Surface
    user_id: ExternalId
    user_name: str | None = Field(default=None, max_length=80)
    text: str = Field(min_length=1, max_length=2000)
    at: str = Field(min_length=1, max_length=64)  # ISO 8601, e.g. 2026-10-05T09:00 or ...T09:00+02:00
    repeat: str = Field(default="", max_length=16)  # "", "daily", "weekly" or "monthly"
    timezone: str | None = Field(default=None, max_length=64)  # IANA name; read a time without offset in it
    conversation: str | None = Field(default=None, min_length=1, max_length=200)  # where Clara announces it
    targets: list[Surface] = Field(default_factory=list, max_length=MAX_TARGETS)  # surfaces shown on; []: all


class NotificationBody(_Body):
    surface: Surface  # the account of the person to notify
    user_id: ExternalId
    user_name: str | None = Field(default=None, max_length=80)
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    title: str = Field(default="", max_length=MAX_TITLE)
    targets: list[Surface] = Field(default_factory=list, max_length=MAX_TARGETS)  # surfaces shown on; []: all
    conversation: str | None = Field(default=None, min_length=1, max_length=200)  # what it is about, if any


class AccountBody(_Body):
    surface: Surface
    user_id: ExternalId


class ConversationBody(AccountBody):
    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)  # "": no title
    pinned: bool | None = None


def describe_conversation(info: ConversationInfo) -> dict:
    return {
        "id": info.conversation,
        "title": info.title,
        "titled_by": info.titled_by,
        "pinned": info.pinned,
        "created_at": info.created_at,
        "updated_at": info.updated_at,
        "preview": info.preview,
    }


class CommandBody(BaseModel):
    line: str = Field(min_length=1, max_length=2000)


def sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


KEEPALIVE_SECONDS = 15.0


async def with_keepalive(events: AsyncIterator[dict], interval: float = KEEPALIVE_SECONDS) -> AsyncIterator[dict | None]:
    """The events, plus a None every `interval` seconds of silence (a client may spend minutes
    running a tool, and idle connections get dropped by proxies)."""
    iterator = events.__aiter__()
    pending = asyncio.ensure_future(iterator.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield None
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            yield event
            pending = asyncio.ensure_future(iterator.__anext__())
    finally:
        pending.cancel()
        with contextlib.suppress(BaseException):
            await pending
        with contextlib.suppress(Exception):
            await iterator.aclose()  # type: ignore[attr-defined]


def create_app(
    settings: Settings, providers: ProviderManager | None = None, tailscale: Tailscale | None = None
) -> FastAPI:
    memory = Memory(settings.db_path)
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
    agent = Agent(
        memory,
        providers,
        default_toolbox(),
        SystemPrompt(settings.system_prompt_file),
        history_turns=settings.history_turns,
        max_concurrent_llm=settings.max_concurrent_llm,
        max_tool_rounds=settings.max_tool_rounds,
        context_window=lambda: providers.context_window,
        compact_percent=settings.compact_percent,
        keep_recent_turns=settings.keep_recent_turns,
        facts_token_budget=settings.facts_token_budget,
        purge_summarised=settings.purge_summarised,
        tool_timeout=settings.tool_timeout,
        first_token_timeout=settings.llm_first_token_timeout,
        idle_timeout=settings.llm_idle_timeout,
        reminders=reminders,
        notifier=notifier,
        long_turn_seconds=settings.notify_long_turn,
    )

    if settings.reminder_ai_timeout:
        ai_timeout = float(settings.reminder_ai_timeout)
        reminders.composer = lambda reminder: compose(agent, reminder, ai_timeout)
    lifecycle = Lifecycle(agent, reminders)
    users = Users(memory, settings.session_days)
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
        scheduler = asyncio.create_task(reminders.run())
        publishing = asyncio.create_task(tailscale.start())  # slow if tailscale hangs: not before the server is up
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
            scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler
            memory.close()
            if traffic is not None:
                traffic.close()

    app = FastAPI(title="Clara", lifespan=lifespan)
    app.add_middleware(
        TrafficMiddleware, traffic=lambda: traffic, identify=lambda headers: peer_of(settings, app.state.users, headers)
    )
    app.state.settings = settings
    app.state.auth_limiter = FailureLimiter(settings.auth_max_failures, settings.auth_block_seconds)
    app.state.users = users
    app.state.tailscale = tailscale
    app.state.memory = memory
    app.state.agent = agent
    app.state.reminders = reminders
    app.state.notifier = notifier
    app.state.traffic = traffic
    app.state.lifecycle = lifecycle
    app.state.providers = providers
    app.state.discord = discord_bot
    app.state.commands = CommandContext(
        settings, memory, agent, providers, time.monotonic(), f"{settings.host}:{settings.port}", lifecycle,
        notifier, tailscale, users, discord_bot,
    )

    def known_person(surface: Surface, user_id: ExternalId) -> Person:
        person = memory.find_person(surface, user_id)
        if person is None:
            raise HTTPException(404, f"Nobody known as {surface}:{user_id}")
        return person

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "provider": providers.active, "model": providers.model}

    def refuse_when_stopping() -> None:
        if lifecycle.stopping:
            raise HTTPException(503, "Clara is stopping and takes no new question.")

    def members(surface: str, ids: list[tuple[str, str]]) -> tuple[Person, ...]:
        """The people behind the accounts `ids` of a surface (id, name shown), once each, leaving out the
        accounts Clara does not know (and, on a login surface, those not signed in): nothing is created."""
        signed_in = users.signed_in_accounts(surface) if surface in settings.login_surfaces else None
        found: dict[int, Person] = {}
        for user_id, name in ids:
            if signed_in is not None and user_id not in signed_in:
                continue
            person = memory.find_person(surface, user_id)
            if person is not None and person.id not in found:
                found[person.id] = Person(person.id, " ".join(name.split()) or person.name)
        return tuple(found.values())

    def checked(http: Request, client: str, body: ChatBody) -> ChatRequest:
        refuse_when_stopping()
        require_account(http, client, body.surface, body.user_id)
        if body.conversation:
            require_conversation(http, client, body.conversation)
        roster: tuple[Person, ...] = ()
        focus: tuple[Person, ...] = ()
        mode = body.mode
        if body.space is not None:
            require_space(http, client, body.space, body.surface)
            roster = members(body.surface, [(entry.user_id, entry.name) for entry in body.roster])
            named = {entry.user_id: entry.name for entry in body.roster}
            focus = members(body.surface, [(user_id, named.get(user_id, "")) for user_id in body.focus])
        elif body.roster or body.focus:
            raise HTTPException(422, "roster and focus need a space")
        if mode == "maybe" and not memory.chime_allowed(body.space):
            mode = "observe"  # an administrator did not let Clara answer what is not for her there
        request = body.to_request(roster, focus, mode)
        try:
            agent.validate(request)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return request

    @app.post("/v1/chat")
    async def chat(body: ChatBody, client: Client, http: Request) -> dict:
        if body.tools:
            raise HTTPException(422, "Client tools need a stream: use /v1/chat/stream")
        request = checked(http, client, body)
        final: dict | None = None
        try:
            async for event in agent.turn(request, client):
                final = event
        except PromptTooLarge as error:
            raise HTTPException(413, str(error)) from None
        except ServerStopping as error:
            raise HTTPException(503, str(error)) from None
        except ModelTimeout as error:
            raise HTTPException(504, str(error)) from None
        except Exception:
            log.exception("chat failed (client=%s)", client)
            raise HTTPException(502, "The language model failed") from None
        return final or {}

    @app.post("/v1/chat/stream")
    async def chat_stream(body: ChatBody, client: Client, http: Request) -> StreamingResponse:
        request = checked(http, client, body)

        async def events() -> AsyncIterator[str]:
            try:
                async with contextlib.aclosing(with_keepalive(agent.turn(request, client))) as stream:
                    async for event in stream:
                        yield ": keepalive\n\n" if event is None else sse(event)
            except (ClientToolTimeout, ModelTimeout, PromptTooLarge, ServerStopping) as error:
                yield sse({"type": "error", "message": str(error)})
            except Exception:
                log.exception("chat stream failed (client=%s)", client)
                yield sse({"type": "error", "message": "The language model failed"})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/reminders", status_code=201)
    async def add_reminder(body: ReminderBody, client: Client, http: Request) -> dict:
        require_account(http, client, body.surface, body.user_id)
        if body.conversation:
            require_conversation(http, client, body.conversation)
        person = memory.resolve(body.surface, body.user_id, body.user_name)
        origin = (body.surface, body.user_id, body.conversation or f"{body.surface}:{body.user_id}")
        try:
            reminder = reminders.create(
                person, body.text, body.at, body.repeat, body.timezone, origin, body.targets
            )
        except ReminderError as error:
            raise HTTPException(422, str(error)) from None
        return describe(reminder)

    @app.post("/v1/notifications", status_code=201)
    async def send_notification(body: NotificationBody, client: Client, http: Request) -> dict:
        """A notification for the person behind an account, on some of their surfaces (any, not only the
        client's own: it is the same person)."""
        require_account(http, client, body.surface, body.user_id)
        if body.conversation:
            require_conversation(http, client, body.conversation)
        person = memory.resolve(body.surface, body.user_id, body.user_name)
        try:
            event = notifier.notify(
                person.id, body.text, body.title, body.targets, client, body.conversation or ""
            )
        except NotificationError as error:
            status = 429 if "Too many" in str(error) else 422
            raise HTTPException(status, str(error)) from None
        return {"id": event.id, "sent_at": event.fired_at, "targets": list(event.targets)}

    def recipients(surface: str) -> Callable[[int], list[str]]:
        """The accounts of a person on a surface that can be sent something (signed in, on a login surface)."""

        def accounts(person_id: int) -> list[str]:
            mine = [external for s, external in memory.accounts_of(person_id) if s == surface]
            if surface in settings.login_surfaces:
                mine = [external for external in mine if users.account_user(surface, external) is not None]
            return mine

        return accounts

    async def event_stream(
        client: Client, http: Request, surface: str | None, user_id: str | None, every_account: bool = False
    ):
        """What the server announces to an account: its person's reminders and notifications (for its
        surface), and the state of the server. Without an account: only the state of the server and what
        is for everybody. `every_account` (a client of a whole surface): what is for any of its accounts."""
        if every_account:
            if not surface or user_id or not SURFACE_RE.match(surface):
                raise HTTPException(422, "all=true needs a surface and no user_id")
            if getattr(client, "user", None) is not None:
                raise HTTPException(403, "Only a client may listen for a whole surface")
            allowed = settings.client_surfaces.get(client)
            if allowed is not None and surface not in allowed:
                raise HTTPException(403, f"This client may not use the surface {surface!r}")
        elif bool(surface) != bool(user_id):
            raise HTTPException(422, "Give both surface and user_id, or neither")
        elif surface is not None:
            if not SURFACE_RE.match(surface) or not 1 <= len(user_id or "") <= 128:
                raise HTTPException(422, "Bad surface or user_id")
            require_account(http, client, surface, user_id)

        async def events() -> AsyncIterator[str]:
            try:
                if every_account:
                    stream = notifier.surface_events(client, surface, recipients(surface))
                else:
                    stream = notifier.events(client, surface, user_id)
                async with contextlib.aclosing(with_keepalive(stream)) as keepalive:
                    async for event in keepalive:
                        yield ": keepalive\n\n" if event is None else sse(event)
            except Exception:
                log.exception("event stream failed (client=%s)", client)
                yield sse({"type": "error", "message": "The event stream failed"})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/notifications/stream")
    async def notification_stream(
        client: Client, http: Request, surface: str | None = None, user_id: str | None = None,
        all: bool = False,  # noqa: A002 - the name of the query parameter
    ) -> StreamingResponse:
        return await event_stream(client, http, surface, user_id, all)

    @app.get("/v1/reminders/stream")
    async def reminder_stream(
        client: Client, http: Request, surface: str | None = None, user_id: str | None = None
    ) -> StreamingResponse:
        """The same stream as /v1/notifications/stream (its earlier name)."""
        return await event_stream(client, http, surface, user_id)

    @app.get("/v1/reminders")
    async def list_reminders(client: Client, http: Request, surface: Surface, user_id: ExternalId) -> dict:
        require_account(http, client, surface, user_id)
        person = memory.find_person(surface, user_id)
        return {"reminders": [describe(r) for r in reminders.upcoming(person)] if person else []}

    @app.delete("/v1/reminders/{reminder_id}")
    async def cancel_reminder(
        reminder_id: int, client: Client, http: Request, surface: Surface, user_id: ExternalId
    ) -> dict:
        require_account(http, client, surface, user_id)
        person = known_person(surface, user_id)
        if not reminders.cancel(person, reminder_id):
            raise HTTPException(404, "No such reminder of yours")
        return {"deleted": reminder_id}

    @app.get("/v1/memory/facts")
    async def list_facts(
        client: Client, http: Request, surface: Surface, user_id: ExternalId
    ) -> dict:
        require_account(http, client, surface, user_id)
        person = known_person(surface, user_id)
        return {
            "person": {"id": person.id, "name": person.name},
            "facts": [{"id": fact.id, "text": fact.text} for fact in memory.facts(person.id, 1000)],
        }

    @app.post("/v1/memory/facts", status_code=201)
    async def add_fact(body: FactBody, client: Client, http: Request) -> dict:
        require_account(http, client, body.surface, body.user_id)
        person = memory.resolve(body.surface, body.user_id, body.user_name)
        try:
            fact = memory.add_fact(person.id, body.text)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return {"created": fact is not None, "id": fact.id if fact else None}

    @app.delete("/v1/memory/facts/{fact_id}")
    async def delete_fact(
        fact_id: int, client: Client, http: Request, surface: Surface, user_id: ExternalId
    ) -> dict:
        require_account(http, client, surface, user_id)
        person = known_person(surface, user_id)
        if not memory.delete_fact(person.id, fact_id):
            raise HTTPException(404, "No such fact for this person")
        return {"deleted": fact_id}

    @app.post("/v1/accounts/link-code")
    async def issue_link_code(body: LinkCodeBody, client: Client, http: Request) -> dict:
        """Step 1, from the client of the account to attach: a code valid for ten minutes."""
        require_account(http, client, body.surface, body.user_id)
        code = link_codes.issue(body.surface, body.user_id)
        return {"code": code, "expires_in": int(link_codes.lifetime)}

    @app.post("/v1/accounts/link")
    async def link_accounts(body: LinkBody, client: Client, http: Request) -> dict:
        """Step 2, from the client of the person to attach to: the code proves that whoever
        asks controls the account `surface:user_id`. Only the target's surface is checked here,
        since the two accounts usually belong to different clients."""
        require_account(http, client, body.to_surface, body.to_user_id)
        target = known_person(body.to_surface, body.to_user_id)
        if not link_codes.redeem(body.surface, body.user_id, body.code):
            raise HTTPException(403, "Wrong or expired link code")
        try:
            person = memory.link_account(body.surface, body.user_id, target)
        except MergeRefused as error:
            raise HTTPException(409, str(error)) from None
        accounts = [f"{surface}:{external}" for surface, external in memory.accounts_of(person.id)]
        return {"person": {"id": person.id, "name": person.name}, "accounts": accounts}

    @app.post("/v1/turns/{turn_id}/tool-results")
    async def tool_results(turn_id: str, body: ToolResultsBody, client: Client) -> dict:
        results = {item.id: item.content for item in body.results}
        try:
            agent.submit_results(turn_id, client, results)
        except KeyError:
            raise HTTPException(404, "No turn of yours is waiting for tool results") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return {"accepted": len(results)}

    def own_conversation(
        client: str, http: Request, surface: str, user_id: str, conversation: str
    ) -> ConversationInfo:
        """The listed conversation, if the account's person started it; else 404."""
        require_account(http, client, surface, user_id)
        require_conversation(http, client, conversation)
        person = memory.find_person(surface, user_id)
        info = memory.conversation_info(conversation)
        if person is None or info is None or info.person_id != person.id:
            raise HTTPException(404, "No such conversation of yours")
        return info

    @app.get("/v1/conversations")
    async def list_conversations(
        client: Client,
        http: Request,
        surface: Surface,
        user_id: ExternalId,
        q: Annotated[str, Query(max_length=200)] = "",
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> dict:
        """The conversations the account's person started on this surface, pinned first, then the last
        written in; `q` keeps those whose title, messages or summary contain it."""
        require_account(http, client, surface, user_id)
        person = memory.find_person(surface, user_id)
        found = memory.conversations_of(person.id, surface, q, limit) if person else []
        return {"conversations": [describe_conversation(info) for info in found]}

    # Before GET /v1/conversations/{conversation:path}, which would take ".../messages" for an id
    @app.get("/v1/conversations/{conversation:path}/messages")
    async def conversation_messages(
        conversation: str,
        client: Client,
        http: Request,
        surface: Surface,
        user_id: ExternalId,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> dict:
        info = own_conversation(client, http, surface, user_id, conversation)
        shown, more = memory.transcript(conversation, limit)
        state = memory.state(conversation)
        first_kept = shown[0].id if shown else 0
        return {
            **describe_conversation(info),
            # the summary, when it stands for messages not given (deleted by CLARA_PURGE_SUMMARISED, or too old)
            "summary": state.summary if state.summary and (not shown or state.upto_id < first_kept) else "",
            "earlier": more,  # older messages exist that are not given
            "messages": [
                {"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at} for m in shown
            ],
        }

    @app.patch("/v1/conversations/{conversation:path}")
    async def update_conversation(
        conversation: str, body: ConversationBody, client: Client, http: Request
    ) -> dict:
        own_conversation(client, http, body.surface, body.user_id, conversation)
        memory.update_conversation(conversation, body.title, body.pinned)
        return describe_conversation(memory.conversation_info(conversation))

    @app.post("/v1/conversations/{conversation:path}/title")
    async def title_conversation(conversation: str, body: AccountBody, client: Client, http: Request) -> dict:
        own_conversation(client, http, body.surface, body.user_id, conversation)
        try:
            title = await agent.title(conversation)
        except NothingToTitle as error:
            raise HTTPException(409, str(error)) from None
        except ServerStopping as error:
            raise HTTPException(503, str(error)) from None
        except ModelTimeout as error:
            raise HTTPException(504, str(error)) from None
        except Exception:
            log.exception("titling failed (client=%s)", client)
            raise HTTPException(502, "The language model failed") from None
        return {"id": conversation, "title": title}

    @app.get("/v1/conversations/{conversation:path}")
    async def conversation_info(conversation: str, client: Client, http: Request) -> dict:
        require_conversation(http, client, conversation)
        return agent.context(conversation)

    @app.post("/v1/conversations/{conversation:path}/compact")
    async def compact_conversation(
        conversation: str, body: CompactBody, client: Client, http: Request
    ) -> dict:
        require_conversation(http, client, conversation)
        refuse_when_stopping()
        try:
            before, after = await agent.compact(conversation, body.focus)
        except NothingToCompact as error:
            raise HTTPException(409, str(error)) from None
        except ServerStopping as error:
            raise HTTPException(503, str(error)) from None
        except ModelTimeout as error:
            raise HTTPException(504, str(error)) from None
        except Exception:
            log.exception("compaction failed (client=%s)", client)
            raise HTTPException(502, "The language model failed") from None
        return {
            "before_percent": round(before, 1),
            "after_percent": round(after, 1),
            "summary": memory.state(conversation).summary,
        }

    @app.delete("/v1/conversations/{conversation:path}")
    async def clear_conversation(
        conversation: str,
        client: Client,
        http: Request,
        surface: Surface | None = None,
        user_id: ExternalId | None = None,
    ) -> dict:
        """Forget a conversation. With an account: only one its person started (404 otherwise)."""
        require_conversation(http, client, conversation)
        if surface is not None or user_id is not None:
            if surface is None or user_id is None:
                raise HTTPException(422, "Give both surface and user_id, or neither")
            own_conversation(client, http, surface, user_id, conversation)
        return {"deleted_messages": memory.clear_conversation(conversation)}

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

    # what the built-in Discord bot calls directly (discord_bot/local.py)
    app.state.check_chat = checked
    app.state.surface_recipients = recipients

    install(app)
    install_clients(app)
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
    # see the real client (nothing else is trusted: anyone else could write whatever address they like)
    options = {"host": settings.host, "port": settings.port, "proxy_headers": True,
               "forwarded_allow_ips": "127.0.0.1,::1", **options}
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
    try:
        asyncio.run(serve(create_app(settings), settings, with_console, args.headless))
    except KeyboardInterrupt:
        pass
    except Exception:
        if not args.headless:
            raise
        log.exception("clara-server crashed")  # nobody is watching a terminal: the log is the only trace
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
