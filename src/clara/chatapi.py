"""The routes of a turn of conversation (see agent.py), and the checks every chat request goes through (the built-in
Discord bot runs them too, discord_bot/local.py).

    POST   /v1/chat                     JSON answer
    POST   /v1/chat/stream              Server-Sent Events: token / tool / done / error
    POST   /v1/turns/{id}/tool-results  a client's answer to a `tool_requests` event
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .agent import Agent, ChatRequest, ClientToolTimeout, ModelTimeout, PromptTooLarge, ServerStopping
from .apicommon import (
    Body,
    ExternalId,
    Surface,
    event_stream,
    log,
    model_failure,
    own_project_id,
    refuse_when_stopping,
    sse,
    sse_lines,
)
from .auth import Client, require_account, require_conversation, require_space
from .limits import UsageLimitReached
from .memory import Memory, Person
from .settings import Settings

router = APIRouter()

MAX_ROSTER = 500


class RosterEntry(Body):
    user_id: ExternalId
    name: str = Field(default="", max_length=80)


class ChatBody(Body):
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
    # The project a new conversation is part of (one that exists stays in its own, see PATCH /v1/conversations)
    project: int | None = Field(default=None, ge=1)

    def to_request(self, roster: tuple[Person, ...] = (), focus: tuple[Person, ...] = (), mode: str = "") -> ChatRequest:
        return ChatRequest(
            surface=self.surface, user_id=self.user_id, user_name=self.user_name, message=self.message,
            conversation=self.conversation, tools=tuple(self.tools), instructions=self.instructions,
            prefix=self.prefix, ephemeral=self.ephemeral, timezone=self.timezone,
            quiet=self.quiet or self.mode != "answer", space=self.space, roster=roster, focus=focus,
            mode=mode or self.mode,
        )


class ToolResult(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    content: str = Field(max_length=2_000_000)


class ToolResultsBody(BaseModel):
    results: list[ToolResult] = Field(max_length=100)


# ----------------------------------------------------------------------
# The checks of a chat request
# ----------------------------------------------------------------------
def members(request: Request, surface: str, ids: list[tuple[str, str]]) -> tuple[Person, ...]:
    """The people behind the accounts `ids` of a surface (id, name shown), once each, leaving out the
    accounts Clara does not know (and, on a login surface, those not signed in): nothing is created."""
    settings: Settings = request.app.state.settings
    memory: Memory = request.app.state.memory
    signed_in = request.app.state.users.signed_in_accounts(surface) if surface in settings.login_surfaces else None
    found: dict[int, Person] = {}
    for user_id, name in ids:
        if signed_in is not None and user_id not in signed_in:
            continue
        person = memory.find_person(surface, user_id)
        if person is not None and person.id not in found:
            found[person.id] = Person(person.id, " ".join(name.split()) or person.name)
    return tuple(found.values())


def conversation_project(request: Request, body: ChatBody) -> int | None:
    """The project of the conversation: the one it is in, or (a new one) the one the message names."""
    memory: Memory = request.app.state.memory
    info = memory.conversation_info(body.conversation or f"{body.surface}:{body.user_id}")
    if info is not None:
        # only for its owner: a conversation shared with others never brings someone's files to another
        person = memory.find_person(body.surface, body.user_id)
        return info.project_id if person is not None and info.person_id == person.id else None
    return own_project_id(request, body.surface, body.user_id, body.project) if body.project is not None else None


def checked(request: Request, client: str, body: ChatBody) -> ChatRequest:
    """The turn a chat request asks for, once everything in it was checked (else an HTTPException)."""
    refuse_when_stopping(request)
    require_account(request, client, body.surface, body.user_id)
    if body.conversation:
        require_conversation(request, client, body.conversation)
    roster: tuple[Person, ...] = ()
    focus: tuple[Person, ...] = ()
    mode = body.mode
    if body.space is not None:
        require_space(request, client, body.space, body.surface)
        roster = members(request, body.surface, [(entry.user_id, entry.name) for entry in body.roster])
        named = {entry.user_id: entry.name for entry in body.roster}
        focus = members(request, body.surface, [(user_id, named.get(user_id, "")) for user_id in body.focus])
    elif body.roster or body.focus:
        raise HTTPException(422, "roster and focus need a space")
    if mode == "maybe" and not request.app.state.memory.chime_allowed(body.space):
        mode = "observe"  # an administrator did not let Clara answer what is not for her there
    turn = body.to_request(roster, focus, mode)
    project = None if body.ephemeral else conversation_project(request, body)
    if project is not None:
        turn = dataclasses.replace(turn, project=project)
    try:
        request.app.state.agent.validate(turn)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return turn


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@router.post("/v1/chat")
async def chat(body: ChatBody, client: Client, request: Request) -> dict:
    if body.tools:
        raise HTTPException(422, "Client tools need a stream: use /v1/chat/stream")
    turn = checked(request, client, body)
    agent: Agent = request.app.state.agent
    final: dict | None = None
    try:
        async for event in agent.turn(turn, client):
            final = event
    except Exception as error:
        raise model_failure(error, "chat", client) from None
    return final or {}


@router.post("/v1/chat/stream")
async def chat_stream(body: ChatBody, client: Client, request: Request) -> StreamingResponse:
    turn = checked(request, client, body)
    agent: Agent = request.app.state.agent

    async def lines() -> AsyncIterator[str]:
        try:
            async for line in sse_lines(agent.turn(turn, client)):
                yield line
        except UsageLimitReached as error:
            yield sse({"type": "error", "reason": "usage_limit", "message": str(error)})
        except (ClientToolTimeout, ModelTimeout, PromptTooLarge, ServerStopping) as error:
            yield sse({"type": "error", "message": str(error)})
        except Exception:
            log.exception("chat stream failed (client=%s)", client)
            yield sse({"type": "error", "message": "The language model failed"})

    return event_stream(lines())


@router.post("/v1/turns/{turn_id}/tool-results")
async def tool_results(turn_id: str, body: ToolResultsBody, client: Client, request: Request) -> dict:
    results = {item.id: item.content for item in body.results}
    try:
        request.app.state.agent.submit_results(turn_id, client, results)
    except KeyError:
        raise HTTPException(404, "No turn of yours is waiting for tool results") from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return {"accepted": len(results)}


def install(app: FastAPI) -> None:
    app.include_router(router)
