"""The routes of the conversations: the list of an account's person, a transcript, a title, a summary.

    GET    /v1/conversations            the conversations an account's person started on its surface (the web
                                        site and the app share theirs, auth.SHARED_SURFACES)
                                        (?project=<id>: those of a project; ?project=none: those in none)
    GET    /v1/conversations/{id}/messages   its questions and answers (?calls=true: with the tools called)
    PATCH  /v1/conversations/{id}       {title?, pinned?, project?}
    POST   /v1/conversations/{id}/title Clara writes a title for it
    GET    /v1/conversations/{id}       context size and summary
    POST   /v1/conversations/{id}/compact  replace its older messages by a summary
    DELETE /v1/conversations/{id}       forget it
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .agent import Agent
from .apicommon import AccountBody, ExternalId, Surface, model_failure, own_project_id, refuse_when_stopping
from .auth import Client, require_account, require_conversation, shared_surfaces
from .memory import ANY_PROJECT, MAX_TITLE_LENGTH, ConversationInfo, Memory
from .qcm import with_answers

router = APIRouter()


class ConversationBody(AccountBody):
    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)  # "": no title
    pinned: bool | None = None
    project: int | None = Field(default=None, ge=1)  # given as null: out of its project


class CompactBody(BaseModel):
    focus: str = Field(default="", max_length=2000)


def describe_conversation(info: ConversationInfo) -> dict:
    return {
        "id": info.conversation,
        "title": info.title,
        "titled_by": info.titled_by,
        "pinned": info.pinned,
        "created_at": info.created_at,
        "updated_at": info.updated_at,
        "preview": info.preview,
        "project": info.project_id,
    }


def own_conversation(request: Request, client: str, surface: str, user_id: str, conversation: str) -> ConversationInfo:
    """The listed conversation, if the account's person started it; else 404."""
    require_account(request, client, surface, user_id)
    require_conversation(request, client, conversation)
    memory: Memory = request.app.state.memory
    person = memory.find_person(surface, user_id)
    info = memory.conversation_info(conversation)
    if person is None or info is None or info.person_id != person.id:
        raise HTTPException(404, "No such conversation of yours")
    return info


@router.get("/v1/conversations")
async def list_conversations(
    client: Client,
    request: Request,
    surface: Surface,
    user_id: ExternalId,
    q: Annotated[str, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    project: Annotated[str | None, Query(pattern=r"^(none|[0-9]{1,18})$")] = None,
) -> dict:
    """The conversations the account's person started on this surface, pinned first, then the last
    written in; `q` keeps those whose title, messages or summary contain it, `project` those of a project
    ("none": those in no project)."""
    require_account(request, client, surface, user_id)
    memory: Memory = request.app.state.memory
    person = memory.find_person(surface, user_id)
    which: int | str | None = ANY_PROJECT if project is None else None if project == "none" else int(project)
    surfaces = shared_surfaces(request, client, surface)
    if person is None:
        found = []
    elif q.strip():  # a search reads every message of the person: in a thread, the event loop goes on
        found = await asyncio.to_thread(memory.conversations_of, person.id, surfaces, q, limit, which)
    else:
        found = memory.conversations_of(person.id, surfaces, q, limit, which)
    return {"conversations": [describe_conversation(info) for info in found]}


# Before GET /v1/conversations/{conversation:path}, which would take ".../messages" for an id
@router.get("/v1/conversations/{conversation:path}/messages")
async def conversation_messages(
    conversation: str,
    client: Client,
    request: Request,
    surface: Surface,
    user_id: ExternalId,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    calls: bool = False,
) -> dict:
    """The questions and answers of a conversation; with `calls`, the answers also carry the tools they
    called (and an answer that only called tools is given, with no text)."""
    info = own_conversation(request, client, surface, user_id, conversation)
    memory: Memory = request.app.state.memory
    shown, more = memory.transcript(conversation, limit, forms=True, calls=calls)
    state = memory.state(conversation)
    first_kept = shown[0].id if shown else 0
    return {
        **describe_conversation(info),
        # the summary, when it stands for messages not given (deleted by CLARA_PURGE_SUMMARISED, or too old)
        "summary": state.summary if state.summary and (not shown or state.upto_id < first_kept) else "",
        "earlier": more,  # older messages exist that are not given
        "messages": [
            {"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at}
            # a QCM asked by this answer, with the answers the user gave it later (null: not answered)
            | ({"qcm": with_answers(list(m.forms), [n.content for n in shown[at + 1 :] if n.role == "user"])} if m.forms else {})
            | ({"calls": list(m.calls)} if m.calls else {})
            for at, m in enumerate(shown)
        ],
    }


@router.patch("/v1/conversations/{conversation:path}")
async def update_conversation(conversation: str, body: ConversationBody, client: Client, request: Request) -> dict:
    own_conversation(request, client, body.surface, body.user_id, conversation)
    memory: Memory = request.app.state.memory
    if "project" in body.model_fields_set:  # null: out of its project
        project = None if body.project is None else own_project_id(request, body.surface, body.user_id, body.project)
        memory.set_conversation_project(conversation, project)
    memory.update_conversation(conversation, body.title, body.pinned)
    return describe_conversation(memory.conversation_info(conversation))


@router.post("/v1/conversations/{conversation:path}/title")
async def title_conversation(conversation: str, body: AccountBody, client: Client, request: Request) -> dict:
    own_conversation(request, client, body.surface, body.user_id, conversation)
    agent: Agent = request.app.state.agent
    try:
        title = await agent.title(conversation)
    except Exception as error:
        raise model_failure(error, "titling", client) from None
    return {"id": conversation, "title": title}


@router.get("/v1/conversations/{conversation:path}")
async def conversation_info(conversation: str, client: Client, request: Request) -> dict:
    require_conversation(request, client, conversation)
    return request.app.state.agent.context(conversation)


@router.post("/v1/conversations/{conversation:path}/compact")
async def compact_conversation(conversation: str, body: CompactBody, client: Client, request: Request) -> dict:
    require_conversation(request, client, conversation)
    refuse_when_stopping(request)
    agent: Agent = request.app.state.agent
    try:
        before, after = await agent.compact(conversation, body.focus)
    except Exception as error:
        raise model_failure(error, "compaction", client) from None
    return {
        "before_percent": round(before, 1),
        "after_percent": round(after, 1),
        "summary": request.app.state.memory.state(conversation).summary,
    }


@router.delete("/v1/conversations/{conversation:path}")
async def clear_conversation(
    conversation: str,
    client: Client,
    request: Request,
    surface: Surface | None = None,
    user_id: ExternalId | None = None,
) -> dict:
    """Forget a conversation. With an account: only one its person started (404 otherwise)."""
    require_conversation(request, client, conversation)
    if surface is not None or user_id is not None:
        if surface is None or user_id is None:
            raise HTTPException(422, "Give both surface and user_id, or neither")
        own_conversation(request, client, surface, user_id, conversation)
    return {"deleted_messages": request.app.state.memory.clear_conversation(conversation)}


def install(app: FastAPI) -> None:
    app.include_router(router)
