"""The routes of a client that speaks for many people (the Discord bot): signing its people in, and the group
spaces it is in. Plus the administration of those spaces and of Clara's relationship with each person.

On the surfaces of CLARA_LOGIN_SURFACES (`discord` by default) an account must be signed in as a user before it
may talk to Clara. The client has no token per person: it sends their user name and password once (from a
private form), and the account stays signed in until it signs out, or the user is signed out everywhere,
disabled or removed. A person can also make their user right there (`register`): the same user then signs in on
the web site, the desktop app or the terminal.

    POST   /v1/accounts/register        {surface, user_id, user_name?, username, password}: make a user, sign in
    POST   /v1/accounts/login           {surface, user_id, user_name?, username, password}: sign in as a user
    POST   /v1/accounts/logout          {surface, user_id}
    GET    /v1/accounts/me              ?surface=&user_id=  who the account is signed in as, and what Clara knows
    GET    /v1/accounts/signed-in       ?surface=  every account of the surface that is signed in
    PUT    /v1/spaces                   {surface, spaces: [{id, name}]}: the spaces the client is in now

    GET    /v1/admin/spaces             (administrators) the spaces, and whether Clara may chime in
    PATCH  /v1/admin/spaces             {default_chime}
    PATCH  /v1/admin/spaces/{id}        {chime: true | false | null}
    PATCH  /v1/admin/people/{id}        {relation: 0-100 | null}
    GET    /v1/admin/discord            the built-in Discord bot, its servers, the Discord accounts signed in
    GET    /v1/admin/discord/members    ?q=  people in the bot's servers (to pick one to sign in)
    POST   /v1/admin/discord/accounts   {user_id, user}: sign a Discord account in as a user, no password
    POST   /v1/admin/discord/{action}   start | stop | restart
    DELETE /v1/admin/discord/accounts/{id}  sign a Discord account out
"""

from __future__ import annotations

import asyncio
import logging
import math

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .auth import Admin, Client, require_account
from .memory import MergeRefused, Person, Space
from .prompt import relation_label
from .ratelimit import FailureLimiter
from .users import User, UserError

log = logging.getLogger("clara")

router = APIRouter()

REGISTRATIONS = 3  # an account that makes this many users within a minute...
REGISTRATIONS_BLOCK = 3600  # ...may make no other for an hour
MAX_SPACES = 500
ME_FACTS = 200  # facts shown by /v1/accounts/me


class _Account(BaseModel):
    surface: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    user_id: str = Field(min_length=1, max_length=128)

    @field_validator("user_id", mode="before")
    @classmethod
    def _ids_may_be_numbers(cls, value):
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


class SignInBody(_Account):
    user_name: str | None = Field(default=None, max_length=80)
    # named so that the traffic log hides them (traffic.SECRET_KEYS)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)


class SpaceEntry(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(default="", max_length=200)


class SpacesBody(BaseModel):
    surface: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    spaces: list[SpaceEntry] = Field(max_length=MAX_SPACES)


class SpacePatch(BaseModel):
    chime: bool | None  # null: back to the default


class SpacesPatch(BaseModel):
    default_chime: bool


class PersonPatch(BaseModel):
    relation: int | None = Field(ge=0, le=100)  # null: no relationship


DISCORD_ID = r"^[0-9]{1,20}$"


class DiscordLink(BaseModel):
    user_id: str = Field(pattern=DISCORD_ID)
    user: str = Field(min_length=1, max_length=64)

    @field_validator("user_id", mode="before")
    @classmethod
    def _ids_may_be_numbers(cls, value):
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


def describe_space(space: Space, default: bool) -> dict:
    return {
        "id": space.id, "surface": space.surface, "name": space.name, "chime": space.chime,
        "chime_effective": default if space.chime is None else space.chime, "present": space.present,
        "seen_at": space.seen_at,
    }


# ----------------------------------------------------------------------
# Signing accounts in
# ----------------------------------------------------------------------
def _login_surface(request: Request, client: str, body: _Account) -> None:
    if getattr(client, "user", None) is not None:
        raise HTTPException(403, "Only a client signs its accounts in")
    if body.surface not in request.app.state.settings.login_surfaces:
        raise HTTPException(403, f"Accounts of {body.surface!r} are not signed in (CLARA_LOGIN_SURFACES)")
    require_account(request, client, body.surface, body.user_id, signed_in=False)


def _owned_by_a_user(request: Request, person: Person) -> bool:
    return request.app.state.users.owns_person(person.id)


def _attach(request: Request, surface: str, external_id: str, user: User) -> None:
    """Make the account belong to the user's person. An account that was another user's (signed in as someone
    else before) is only moved, nobody is merged; an account nobody signed in with joins the user with its
    memories (refused, 409, when both have some: an operator merges them with /link)."""
    memory = request.app.state.memory
    target = request.app.state.users.person_of(user)
    current = memory.find_person(surface, external_id)
    if current is None or current.id == target.id:
        memory.link_account(surface, external_id, target)
    elif _owned_by_a_user(request, current):
        memory.move_account(surface, external_id, target)
    else:
        try:
            memory.link_account(surface, external_id, target)
        except MergeRefused as error:
            raise HTTPException(409, f"{error} (account {surface}:{external_id})") from None


def _signed_in(request: Request, user: User, body: _Account) -> dict:
    memory = request.app.state.memory
    person = memory.person_by_id(user.person_id)
    return {
        "user": user.name,
        "is_admin": user.is_admin,
        "person": {"id": person.id, "name": person.name} if person else None,
        "accounts": [f"{s}:{e}" for s, e in memory.accounts_of(user.person_id)],
        "account": f"{body.surface}:{body.user_id}",
    }


def _already(request: Request, body: _Account) -> None:
    user = request.app.state.users.account_user(body.surface, body.user_id)
    if user is not None:
        raise HTTPException(409, f"This account is already signed in as {user.name}: sign out first")


@router.post("/v1/accounts/register", status_code=201)
async def register(body: SignInBody, client: Client, request: Request) -> dict:
    """Make a user for the person behind the account, and sign the account in as them."""
    _login_surface(request, client, body)
    _already(request, body)
    state = request.app.state
    key = f"{client}/{body.surface}:{body.user_id}"
    wait = state.registrations.blocked_for(key)
    if wait:
        raise HTTPException(429, "Too many users made from this account: try again later.",
                            headers={"Retry-After": str(math.ceil(wait))})
    memory, users = state.memory, state.users
    current = memory.find_person(body.surface, body.user_id)
    # the account's own memories come along, unless they are someone else's (a user who signed out of it)
    person = current if current is not None and not _owned_by_a_user(request, current) else None
    fresh = None if person is not None else memory.create_person(body.user_name or body.username)
    try:
        user = await asyncio.to_thread(users.create, body.username, body.password, False, person or fresh)  # scrypt
    except UserError as error:
        if fresh is not None:
            memory.delete_person(fresh.id)
        raise HTTPException(409 if "already" in str(error) else 422, str(error)) from None
    state.registrations.failed(key)  # counts the users made, not failures
    if fresh is not None:
        memory.move_account(body.surface, body.user_id, fresh)
    users.sign_in_account(user, body.surface, body.user_id, client)
    log.info("%s registered user %s for %s:%s", client, user.name, body.surface, body.user_id)
    return _signed_in(request, user, body)


@router.post("/v1/accounts/login")
async def account_login(body: SignInBody, client: Client, request: Request) -> dict:
    """Sign the account in as an existing user (their password proves it)."""
    _login_surface(request, client, body)
    state = request.app.state
    key = f"{client}/{body.surface}:{body.user_id}"
    wait = state.account_limiter.blocked_for(key)
    if wait:
        raise HTTPException(429, "Too many wrong passwords for this account: try again later.",
                            headers={"Retry-After": str(math.ceil(wait))})
    user = await asyncio.to_thread(state.users.authenticate, body.username, body.password)
    if user is None:
        state.account_limiter.failed(key)
        raise HTTPException(401, "Wrong user name or password")
    state.account_limiter.succeeded(key)
    _attach(request, body.surface, body.user_id, user)
    state.users.sign_in_account(user, body.surface, body.user_id, client)
    log.info("%s signed %s:%s in as %s", client, body.surface, body.user_id, user.name)
    return _signed_in(request, user, body)


@router.post("/v1/accounts/logout")
async def account_logout(body: _Account, client: Client, request: Request) -> dict:
    _login_surface(request, client, body)
    return {"signed_out": request.app.state.users.sign_out_account(body.surface, body.user_id)}


@router.get("/v1/accounts/me")
async def account_me(surface: str, user_id: str, client: Client, request: Request) -> dict:
    """Who the account is signed in as (`signed_in: false` if nobody), and what Clara knows about them."""
    body = _Account(surface=surface, user_id=user_id)
    require_account(request, client, body.surface, body.user_id, signed_in=False)
    state = request.app.state
    user = state.users.account_user(body.surface, body.user_id) if body.surface in state.settings.login_surfaces else None
    person = state.memory.find_person(body.surface, body.user_id)
    if body.surface in state.settings.login_surfaces and user is None:
        return {"signed_in": False}
    relation = state.memory.relation(person.id) if person else None
    return {
        "signed_in": True,
        "user": user.name if user else None,
        "person": {"id": person.id, "name": person.name} if person else None,
        "accounts": [f"{s}:{e}" for s, e in state.memory.accounts_of(person.id)] if person else [],
        "relation": relation,
        "relation_label": relation_label(relation),
        "facts": [{"id": f.id, "text": f.text} for f in state.memory.facts(person.id, ME_FACTS)] if person else [],
    }


@router.get("/v1/accounts/signed-in")
async def signed_in_accounts(surface: str, client: Client, request: Request) -> dict:
    """Every account of the surface that is signed in: `{accounts: [{user_id, user, name}]}`."""
    body = _Account(surface=surface, user_id="-")
    state = request.app.state
    if getattr(client, "user", None) is not None:
        raise HTTPException(403, "Only a client may list the accounts of a surface")
    allowed = state.settings.client_surfaces.get(client)
    if allowed is not None and body.surface not in allowed:
        raise HTTPException(403, f"This client may not use the surface {body.surface!r}")
    accounts = []
    for external_id, user in state.users.signed_in_accounts(body.surface).items():
        person = state.memory.find_person(body.surface, external_id)
        accounts.append({"user_id": external_id, "user": user.name, "name": person.name if person else user.name})
    return {"accounts": accounts}


# ----------------------------------------------------------------------
# Spaces
# ----------------------------------------------------------------------
@router.put("/v1/spaces")
async def sync_spaces(body: SpacesBody, client: Client, request: Request) -> dict:
    """The client is in these spaces of its surface now (each id starts with `<surface>:`)."""
    state = request.app.state
    if getattr(client, "user", None) is not None:
        raise HTTPException(403, "Only a client is in spaces")
    allowed = state.settings.client_surfaces.get(client)
    if allowed is not None and body.surface not in allowed:
        raise HTTPException(403, f"This client may not use the surface {body.surface!r}")
    for space in body.spaces:
        if not space.id.startswith(f"{body.surface}:"):
            raise HTTPException(422, f"A space of {body.surface} starts with {body.surface}: ({space.id!r})")
    found = state.memory.sync_spaces(body.surface, [(space.id, space.name) for space in body.spaces])
    default = state.memory.chime_default()
    return {"default_chime": default, "spaces": [describe_space(space, default) for space in found]}


@router.get("/v1/admin/spaces")
async def admin_spaces(admin: Admin, request: Request) -> dict:
    memory = request.app.state.memory
    default = memory.chime_default()
    return {"default_chime": default, "spaces": [describe_space(space, default) for space in memory.spaces()]}


@router.patch("/v1/admin/spaces")
async def admin_spaces_default(body: SpacesPatch, admin: Admin, request: Request) -> dict:
    request.app.state.memory.set_chime_default(body.default_chime)
    log.info("admin %s: Clara may chime in by default: %s", admin, body.default_chime)
    return {"default_chime": body.default_chime}


@router.patch("/v1/admin/spaces/{space_id:path}")
async def admin_space(space_id: str, body: SpacePatch, admin: Admin, request: Request) -> dict:
    memory = request.app.state.memory
    if not memory.set_space_chime(space_id, body.chime):
        raise HTTPException(404, "No such space")
    log.info("admin %s: chime in %s: %s", admin, space_id, body.chime)
    return describe_space(memory.space(space_id), memory.chime_default())


# ----------------------------------------------------------------------
# The Discord bot built into the server (the web site's Discord page)
# ----------------------------------------------------------------------
DISCORD = "discord"


@router.get("/v1/admin/discord")
async def admin_discord(admin: Admin, request: Request) -> dict:
    """The bot's state, the Discord servers it is in (with chime in), and the Discord accounts signed in."""
    state = request.app.state
    bot = state.discord
    memory = state.memory
    default = memory.chime_default()
    accounts = [
        describe_discord_account(request, external_id, user)
        for external_id, user in sorted(state.users.signed_in_accounts(DISCORD).items(), key=lambda item: item[1].name)
    ]
    return {
        "bot": bot.status(),
        "default_chime": default,
        "spaces": [describe_space(space, default) for space in memory.spaces(DISCORD)],
        "accounts": accounts,
    }


def describe_discord_account(request: Request, user_id: str, user: User) -> dict:
    person = request.app.state.memory.find_person(DISCORD, user_id)
    return {
        "user_id": user_id, "user": user.name, "person": person.name if person else None,
        "discord_name": request.app.state.discord.discord_name(user_id),
    }


def announce_discord_sign_in(request: Request, user: User, user_id: str, admin: str) -> None:
    """An administrator signed a Discord account in as `user`: the running bot answers it at once."""
    request.app.state.discord.signed_in(user_id, user.name)
    log.info("admin %s signed discord:%s in as %s", admin, user_id, user.name)


@router.get("/v1/admin/discord/members")
async def admin_discord_members(admin: Admin, request: Request, q: str = "") -> dict:
    """People in the servers of the running bot (`running: false` when it is not), with the user each one is
    signed in as."""
    state = request.app.state
    signed_in = {external_id: user.name for external_id, user in state.users.signed_in_accounts(DISCORD).items()}
    members = [{**member, "user": signed_in.get(member["user_id"])} for member in state.discord.members(q[:100])]
    return {"running": state.discord.state == "running", "members": members}


@router.post("/v1/admin/discord/accounts")
async def admin_discord_sign_in(body: DiscordLink, admin: Admin, request: Request) -> dict:
    """Sign a Discord account in as a user (it replaces whoever it was signed in as). Its memories are merged
    into the user's, unless they are another user's: then the account only moves."""
    users = request.app.state.users
    user = users.get(body.user)
    if user is None:
        raise HTTPException(404, f"No user called {body.user}")
    if user.disabled:
        raise HTTPException(422, f"{user.name} is disabled: enable them first")
    users.attach_account(user, DISCORD, body.user_id)
    announce_discord_sign_in(request, user, body.user_id, admin)
    return describe_discord_account(request, body.user_id, user)


@router.post("/v1/admin/discord/{action}")
async def admin_discord_action(action: str, admin: Admin, request: Request) -> dict:
    """start, stop or restart the bot (until the server restarts: AUTO_START_DISCORD_BOT decides then)."""
    bot = request.app.state.discord
    if action not in ("start", "stop", "restart"):
        raise HTTPException(404, "start, stop or restart")
    log.info("admin %s: Discord bot %s", admin, action)
    output = await getattr(bot, action)()
    return {"output": output, "bot": bot.status()}


@router.delete("/v1/admin/discord/accounts/{user_id}")
async def admin_discord_sign_out(user_id: str, admin: Admin, request: Request) -> dict:
    state = request.app.state
    if not state.users.sign_out_account(DISCORD, user_id):
        raise HTTPException(404, "This Discord account is not signed in")
    state.discord.signed_out(user_id)
    log.info("admin %s signed discord:%s out", admin, user_id)
    return {"signed_out": True}


# ----------------------------------------------------------------------
# Relationship
# ----------------------------------------------------------------------
@router.patch("/v1/admin/people/{person_id}")
async def admin_person(person_id: int, body: PersonPatch, admin: Admin, request: Request) -> dict:
    memory = request.app.state.memory
    person = memory.person_by_id(person_id)
    if person is None:
        raise HTTPException(404, "No such person")
    relation = memory.set_relation(person.id, body.relation)
    return {"id": person.id, "name": person.name, "relation": relation, "relation_label": relation_label(relation)}


def install(app) -> None:
    settings = app.state.settings
    # wrong passwords per account: the bot calls from one address, which is not what to limit
    app.state.account_limiter = FailureLimiter(settings.auth_max_failures, settings.auth_block_seconds)
    app.state.registrations = FailureLimiter(REGISTRATIONS, REGISTRATIONS_BLOCK)
    app.include_router(router)
