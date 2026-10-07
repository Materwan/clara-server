"""The routes of what Clara holds about the person behind an account: facts, settings, and the other accounts of the
same person.

    GET    /v1/memory/facts             facts of an account's person
    POST   /v1/memory/facts
    DELETE /v1/memory/facts/{id}
    GET    /v1/settings                 the settings of an account's person (notify_after: seconds a task takes
    PATCH  /v1/settings                 before it notifies them when done; 0: never; null: the server's default)
    POST   /v1/accounts/link-code       a code proving control of an account
    POST   /v1/accounts/link            "this account is the same person as that one" (needs the code)
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import Field

from .apicommon import AccountBody, Body, ExternalId, Surface, known_person
from .auth import Client, require_account
from .linking import LinkCodes
from .memory import MAX_NOTIFY_AFTER, Memory, MergeRefused, Person

router = APIRouter()


class FactBody(AccountBody):
    user_name: str | None = Field(default=None, max_length=80)
    text: str = Field(min_length=1)


class SettingsBody(AccountBody):
    user_name: str | None = Field(default=None, max_length=80)
    # seconds a task takes before it notifies its person when done; 0: never; null: the server's default
    notify_after: int | None = Field(ge=0, le=MAX_NOTIFY_AFTER)


class LinkBody(Body):
    surface: Surface  # the account to attach...
    user_id: ExternalId
    code: str = Field(min_length=1, max_length=100)  # ...proved with the code it was given...
    to_surface: Surface  # ...to the person who owns this one
    to_user_id: ExternalId


# ----------------------------------------------------------------------
# Facts
# ----------------------------------------------------------------------
@router.get("/v1/memory/facts")
async def list_facts(client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    require_account(request, client, surface, user_id)
    person = known_person(request, surface, user_id)
    memory: Memory = request.app.state.memory
    return {
        "person": {"id": person.id, "name": person.name},
        "facts": [{"id": fact.id, "text": fact.text} for fact in memory.facts(person.id, 1000)],
    }


@router.post("/v1/memory/facts", status_code=201)
async def add_fact(body: FactBody, client: Client, request: Request) -> dict:
    require_account(request, client, body.surface, body.user_id)
    memory: Memory = request.app.state.memory
    person = memory.resolve(body.surface, body.user_id, body.user_name)
    try:
        fact = memory.add_fact(person.id, body.text)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return {"created": fact is not None, "id": fact.id if fact else None}


@router.delete("/v1/memory/facts/{fact_id}")
async def delete_fact(fact_id: int, client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    require_account(request, client, surface, user_id)
    person = known_person(request, surface, user_id)
    if not request.app.state.memory.delete_fact(person.id, fact_id):
        raise HTTPException(404, "No such fact for this person")
    return {"deleted": fact_id}


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------
def describe_settings(request: Request, person: Person | None) -> dict:
    default = request.app.state.settings.notify_long_turn
    own = request.app.state.memory.notify_after(person.id) if person else None
    return {
        "notify_after": own,  # null: not set, the server's default applies
        "notify_after_default": default,
        "notify_after_effective": default if own is None else own,
    }


@router.get("/v1/settings")
async def get_settings(client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    """The settings of an account's person (the defaults for someone the server does not know yet)."""
    require_account(request, client, surface, user_id)
    return describe_settings(request, request.app.state.memory.find_person(surface, user_id))


@router.patch("/v1/settings")
async def update_settings(body: SettingsBody, client: Client, request: Request) -> dict:
    require_account(request, client, body.surface, body.user_id)
    memory: Memory = request.app.state.memory
    person = memory.resolve(body.surface, body.user_id, body.user_name)
    memory.set_notify_after(person.id, body.notify_after)
    return describe_settings(request, person)


# ----------------------------------------------------------------------
# Linking accounts
# ----------------------------------------------------------------------
@router.post("/v1/accounts/link-code")
async def issue_link_code(body: AccountBody, client: Client, request: Request) -> dict:
    """Step 1, from the client of the account to attach: a code valid for ten minutes."""
    require_account(request, client, body.surface, body.user_id)
    link_codes: LinkCodes = request.app.state.link_codes
    code = link_codes.issue(body.surface, body.user_id)
    return {"code": code, "expires_in": int(link_codes.lifetime)}


@router.post("/v1/accounts/link")
async def link_accounts(body: LinkBody, client: Client, request: Request) -> dict:
    """Step 2, from the client of the person to attach to: the code proves that whoever
    asks controls the account `surface:user_id`. Only the target's surface is checked here,
    since the two accounts usually belong to different clients."""
    require_account(request, client, body.to_surface, body.to_user_id)
    target = known_person(request, body.to_surface, body.to_user_id)
    if not request.app.state.link_codes.redeem(body.surface, body.user_id, body.code):
        raise HTTPException(403, "Wrong or expired link code")
    memory: Memory = request.app.state.memory
    try:
        person = memory.link_account(body.surface, body.user_id, target)
    except MergeRefused as error:
        raise HTTPException(409, str(error)) from None
    accounts = [f"{surface}:{external}" for surface, external in memory.accounts_of(person.id)]
    return {"person": {"id": person.id, "name": person.name}, "accounts": accounts}


def install(app: FastAPI) -> None:
    app.include_router(router)
