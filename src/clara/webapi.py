"""The routes that serve people rather than programs: logging in with a password, the account page, the
administration of users and people, reading a PDF, and the web site itself (`web/`, served at `/`).

    POST   /v1/auth/login               {username, password, surface?, device?} -> a token (or a session cookie)
    GET    /v1/auth/signup              {open}: whether the web site lets people make their own user
    POST   /v1/auth/register            {username, password}: make a user and log them in on the web (CLARA_WEB_SIGNUP)
    POST   /v1/auth/logout              sign out of this device
    GET    /v1/auth/me                  who am I, which accounts are mine
    POST   /v1/auth/password            {current_password, new_password}: change it, other devices are signed out
    GET    /v1/auth/export              everything the server keeps about me, as one JSON file
    POST   /v1/auth/delete-account      {password}: erase my user, my memories and my lines of the traffic log
    GET    /v1/auth/sessions            my devices
    DELETE /v1/auth/sessions/{id}       sign one out
    POST   /v1/documents/extract        the text of a PDF (body: the bytes)
    POST   /v1/documents/docx           the text of a Word document (body: the bytes)

    GET    /v1/admin/users              (administrators; also with an admin token)
    POST   /v1/admin/users              {name, password?, admin?, discord_id?} -> the password, shown once
    PATCH  /v1/admin/users/{name}       {admin?, disabled?, password?, generate_password?}
    DELETE /v1/admin/users/{name}
    POST   /v1/admin/users/{name}/sign-out
    GET    /v1/admin/limits             {default}: the tokens a day of a user with no limit of their own (null: none)
    PUT    /v1/admin/limits/default     {tokens}: change it (0: no limit); a user's own is set with PATCH /users/{name}
    GET    /v1/me/usage                 ?days=: your own tokens in and out (Discord apart), your models
    GET    /v1/me/usage/history         ?group=&surface=&kind=&model=&days=&before=&limit=: your own calls, newest first
    GET    /v1/admin/usage              ?days=: tokens in and out of each person (Discord apart), their models
    GET    /v1/admin/usage/history      ?person=&group=&surface=&kind=&model=&days=&before=&limit=: every call, newest first
    GET    /v1/admin/status             provider, model, activity, address
    GET    /v1/admin/restart            {needed, reasons, in_progress, last}: is a restart worth it, and why
    POST   /v1/admin/restart            {now?}: pull, update, stop and start again (202 {id}; /health says `restarted: id`)
    GET    /v1/admin/models             what the active provider offers
    GET    /v1/admin/people             everybody Clara knows
    GET|POST /v1/admin/people/{id}/facts, DELETE /v1/admin/people/{id}/facts/{fact}, GET /v1/admin/people/{id}/footprint
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ingest
from .auth import COOKIE, WEB_HEADER, Admin, Client, LoggedIn
from .clientapi import DISCORD, DISCORD_ID, announce_discord_sign_in
from .erasure import erase_person
from .ingest import IngestError
from .limits import show_limit
from .memory import MergeRefused
from .restart import RestartError
from .users import User, UserError, generate_password

log = logging.getLogger("clara")

WEB_DIR = Path(__file__).parent / "web"
MAX_PDF_BYTES = 30_000_000
MAX_DOCX_BYTES = 30_000_000
MAX_DOCUMENT_CHARS = 400_000  # the text of one document sent back: a longer one is cut
SIGNUPS = 5  # an address that makes this many users within a minute...
SIGNUPS_BLOCK = 3600  # ...may make no other for an hour

router = APIRouter()


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)
    surface: str = Field(default="web", pattern=r"^[A-Za-z0-9_-]{1,32}$")
    device: str = Field(default="", max_length=80)


class RegisterBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)


class PasswordBody(BaseModel):
    # named so that the traffic log recognises them as secrets (traffic.SECRET_KEYS)
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)


class NewUserBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str | None = Field(default=None, max_length=512)  # none: Clara makes one
    admin: bool = False
    discord_id: str | None = Field(default=None, pattern=DISCORD_ID)  # signed in as the new user at once


class UserPatch(BaseModel):
    admin: bool | None = None
    disabled: bool | None = None
    token_limit: int | None = Field(default=None, ge=0, le=10**13)  # tokens a day; 0: no limit
    follow_default_limit: bool = False  # back to the server's default (instead of token_limit)
    password: str | None = Field(default=None, max_length=512)
    generate_password: bool = False


class DefaultLimit(BaseModel):
    tokens: int = Field(ge=0, le=10**13)  # 0: no limit


class FactText(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def describe_user(app: FastAPI, user: User) -> dict:
    users = app.state.users
    sessions = users.sessions_of(user.name)
    person = app.state.memory.person_by_id(user.person_id)
    accounts = users.accounts_signed_in_as(user.name)
    discord_ids = [account.removeprefix(f"{DISCORD}:") for account in accounts if account.startswith(f"{DISCORD}:")]
    limits = app.state.limits
    quota = limits.quota_for_user(user.person_id, user.is_admin and not user.disabled, user.token_limit)
    return {
        "name": user.name,
        "is_admin": user.is_admin,
        "disabled": user.disabled,
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
        "person": {"id": person.id, "name": person.name} if person else None,
        "sessions": len(sessions),
        "surfaces": sorted({s.surface for s in sessions}),
        "signed_in_accounts": accounts,  # signed in by a client (Discord)
        # tokens used today and the limit (null: none; administrators have none); `own_limit` is what an
        # administrator set for this user (null: the default, 0: no limit)
        "usage": {**quota.describe(), "own_limit": user.token_limit, "default_limit": limits.default() or None},
        "discord_accounts": [
            {"user_id": user_id, "discord_name": app.state.discord.discord_name(user_id)} for user_id in discord_ids
        ],
    }


def secret(request: Request) -> None:
    """The answer holds a password: the traffic log does not write it."""
    request.scope["clara_sensitive"] = True


# ----------------------------------------------------------------------
# Logging in
# ----------------------------------------------------------------------
@router.post("/v1/auth/login")
async def login(body: LoginBody, request: Request) -> JSONResponse:
    state = request.app.state
    limiter, users, settings = state.auth_limiter, state.users, state.settings
    address = request.client.host if request.client else None
    wait = limiter.blocked_for(address)
    if wait:
        raise HTTPException(
            429, "Too many wrong passwords from this address: try again later.",
            headers={"Retry-After": str(int(wait) + 1)},
        )
    surface = body.surface.lower()
    if surface not in settings.user_surfaces:
        raise HTTPException(403, f"Logging in on {surface!r} is not allowed (CLARA_USER_SURFACES).")
    user = await asyncio.to_thread(users.authenticate, body.username, body.password)  # scrypt takes a moment
    if user is None:
        if limiter.failed(address):
            log.warning("%s sent %d wrong passwords: refused for %d s", address, limiter.max_failures, limiter.block_seconds)
        raise HTTPException(401, "Wrong user name or password")
    limiter.succeeded(address)
    response = start_session(request, user, surface, body.device)
    log.info("%s logged in on %s from %s", user.name, surface, address)
    return response


def start_session(request: Request, user: User, surface: str, device: str = "", status: int = 200) -> JSONResponse:
    """Log `user` in on `surface`: a token in the answer, or for the web site a cookie out of reach of scripts."""
    state = request.app.state
    users, settings = state.users, state.settings
    address = request.client.host if request.client else None
    try:  # this user is one person on every surface: the account of this surface joins theirs
        state.memory.link_account(surface, user.name, users.person_of(user))
    except MergeRefused as error:
        raise HTTPException(409, f"{error} (account {surface}:{user.name})") from None
    device = device or request.headers.get("user-agent", "")[:80]
    token, session = users.open_session(user, surface, device, address or "")
    payload: dict[str, Any] = {
        "user": describe_user(request.app, user), "surface": surface, "expires_in_days": settings.session_days,
    }
    web = request.headers.get(WEB_HEADER) == "1"
    if not web:
        payload["token"] = token
    response = JSONResponse(payload, status_code=status)
    if web:  # a browser: the token stays in a cookie, out of reach of scripts
        response.set_cookie(
            COOKIE, token, max_age=(settings.session_days or 3650) * 86400, httponly=True, samesite="strict",
            path="/", secure=request.url.scheme == "https",
        )
    return response


def privacy_notice(state) -> dict:
    """What a person should know before they give this server their words, for the sign-in page: who else reads
    them, how long the server logs them, how to take them back."""
    settings = state.settings
    hosts = []
    for config in state.providers.configs.values():
        host = urlsplit(config.host or "").hostname or config.label
        if config.usable and host and host not in hosts:
            hosts.append(host)
    return {
        "model_hosts": hosts,  # what you write is sent to the language model of one of these
        "web": bool(state.agent.toolbox.names & {"web_search", "web_fetch"}),  # searches and addresses go to ollama.com
        "log_days": settings.traffic_log_days if settings.traffic_log else 0,  # the server logs requests and prompts
        "kept_until_deleted": not settings.purge_summarised,
    }


@router.get("/v1/auth/signup")
async def signup_open(request: Request) -> dict:
    """Asked by the sign-in page, before anybody is logged in: should it offer to make an account?"""
    state = request.app.state
    return {"open": state.settings.web_signup, "privacy": privacy_notice(state)}


@router.post("/v1/auth/register", status_code=201)
async def register(body: RegisterBody, request: Request) -> JSONResponse:
    """Make a user (never an administrator) and log them in on the web site, when the server allows it."""
    state = request.app.state
    settings = state.settings
    if not settings.web_signup:
        raise HTTPException(403, "Making an account here is not allowed: ask the person who runs this server.")
    if "web" not in settings.user_surfaces:
        raise HTTPException(403, "Logging in on 'web' is not allowed (CLARA_USER_SURFACES).")
    address = request.client.host if request.client else None
    wait = state.signup_limiter.blocked_for(address)
    if wait:
        raise HTTPException(
            429, "Too many accounts made from this address: try again later.", headers={"Retry-After": str(int(wait) + 1)},
        )
    try:
        user = await asyncio.to_thread(state.users.register, body.username, body.password)  # scrypt takes a moment
    except UserError as error:
        raise HTTPException(409 if "already" in str(error) else 422, str(error)) from None
    state.signup_limiter.failed(address)  # counts the users made, not failures
    if not settings.signup_integrations:
        state.integrations.store.opt_out(user.person_id)
    response = start_session(request, user, "web", status=201)
    log.info("%s made the user %s on the web site", address, user.name)
    return response


@router.post("/v1/auth/logout")
async def logout(caller: LoggedIn, request: Request) -> JSONResponse:
    request.app.state.users.revoke_session(caller.user.name, caller.session.id)
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


@router.get("/v1/auth/me")
async def me(caller: LoggedIn, request: Request) -> dict:
    memory = request.app.state.memory
    person = memory.person_by_id(caller.user.person_id)
    return {
        **describe_user(request.app, caller.user),
        "surface": caller.surface,
        "session": caller.session.id,
        "accounts": [f"{s}:{e}" for s, e in memory.accounts_of(caller.user.person_id)],
        "person": {"id": person.id, "name": person.name} if person else None,
    }


@router.post("/v1/auth/password")
async def change_password(body: PasswordBody, caller: LoggedIn, request: Request) -> dict:
    state = request.app.state
    address = request.client.host if request.client else None
    if state.auth_limiter.blocked_for(address):
        raise HTTPException(429, "Too many wrong passwords from this address: try again later.")
    if await asyncio.to_thread(state.users.authenticate, caller.user.name, body.current_password) is None:
        state.auth_limiter.failed(address)
        raise HTTPException(403, "The current password is wrong")
    try:
        signed_out = state.users.set_password(caller.user.name, body.new_password, keep_session=caller.session.id)
    except UserError as error:
        raise HTTPException(422, str(error)) from None
    return {"signed_out_elsewhere": signed_out}


class DeleteAccountBody(BaseModel):
    password: str = Field(min_length=1, max_length=512)  # named so that the traffic log hides it


@router.get("/v1/auth/export")
async def export_my_data(caller: LoggedIn, request: Request) -> JSONResponse:
    """Everything the server keeps about the person behind this login, as one file (their right of access and to take
    their data with them). Passwords, tokens and the secrets of connected accounts are not in it."""
    state = request.app.state
    memory, user = state.memory, caller.user
    person = memory.person_by_id(user.person_id)
    surfaces = tuple(dict.fromkeys(surface for surface, _ in memory.accounts_of(user.person_id)))

    def build() -> dict[str, Any]:
        conversations = []
        for info in memory.conversations_of(user.person_id, surfaces, "", 100_000):
            shown, _ = memory.transcript(info.conversation, 1_000_000)
            conversations.append({
                "id": info.conversation, "title": info.title, "created_at": info.created_at,
                "updated_at": info.updated_at, "summary": memory.state(info.conversation).summary,
                "messages": [{"role": m.role, "content": m.content, "at": m.created_at} for m in shown],
            })
        files = [
            {"name": f.name, "created_at": f.created_at, "updated_at": f.updated_at,
             "content": (state.markdown.get(user.person_id, f.id) or (None, ""))[1]}
            for f in state.markdown.of(user.person_id)
        ]
        projects = [
            {"name": p.name, "description": p.description, "instructions": p.instructions, "created_at": p.created_at,
             "files": [{"path": f.path, "size": f.size} for f in state.projects.files(p.id)]}
            for p in state.projects.of(user.person_id)
        ]
        return {
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "user": {"name": user.name, "created_at": user.created_at, "last_login_at": user.last_login_at,
                     "is_admin": user.is_admin},
            "person": {"name": person.name if person else None, "relation": memory.relation(user.person_id)},
            "accounts": [f"{s}:{e}" for s, e in memory.accounts_of(user.person_id)],
            "facts": [f.text for f in memory.facts(user.person_id, 100_000)],
            "conversations": conversations,
            "tasks": [asdict(t) for t in state.tasks.tasks(person, None)] if person else [],
            "reminders": [asdict(r) for r in state.reminders.upcoming(person)] if person else [],
            "markdown_files": files,
            "projects": projects,  # the text of their files can be read with the project routes
            "devices": [{"surface": s.surface, "device": s.device, "address": s.address, "created_at": s.created_at,
                         "last_used_at": s.last_used_at} for s in state.users.sessions_of(user.name)],
            "connected_accounts": [{"kind": a.kind, "label": a.label} for a in state.integrations.store.accounts_of(user.person_id)],
        }

    data = await asyncio.to_thread(build)
    return JSONResponse(
        jsonable_encoder(data), headers={"Content-Disposition": f'attachment; filename="clara-{user.name}.json"', "Cache-Control": "no-store"}
    )


@router.post("/v1/auth/delete-account")
async def delete_my_account(body: DeleteAccountBody, caller: LoggedIn, request: Request) -> JSONResponse:
    """Erase this user, the person behind them and everything they said: facts, conversations, files, tasks, projects,
    connected accounts, and their lines in the traffic log. The password is asked again. There is no undo."""
    state = request.app.state
    address = request.client.host if request.client else None
    if state.auth_limiter.blocked_for(address):
        raise HTTPException(429, "Too many wrong passwords from this address: try again later.")
    if await asyncio.to_thread(state.users.authenticate, caller.user.name, body.password) is None:
        state.auth_limiter.failed(address)
        raise HTTPException(403, "The password is wrong")
    if caller.user.is_admin and state.users.admin_count() <= 1:
        raise HTTPException(422, "This is the last administrator: make someone else one first.")
    name, person_id = caller.user.name, caller.user.person_id
    found, lines = await asyncio.to_thread(erase_person, state.memory, state.traffic, person_id)
    log.info("%s erased their own account: %d messages, %d lines of the traffic log", name, found.messages, lines)
    response = JSONResponse({"erased": {"facts": found.facts, "messages": found.messages, "conversations": found.conversations}})
    response.delete_cookie(COOKIE, path="/")
    return response


@router.get("/v1/auth/sessions")
async def my_sessions(caller: LoggedIn, request: Request) -> dict:
    return {
        "sessions": [
            {
                "id": s.id, "surface": s.surface, "device": s.device, "address": s.address,
                "created_at": s.created_at, "last_used_at": s.last_used_at, "current": s.id == caller.session.id,
            }
            for s in request.app.state.users.sessions_of(caller.user.name)
        ]
    }


@router.delete("/v1/auth/sessions/{session_id}")
async def sign_out_device(session_id: int, caller: LoggedIn, request: Request) -> dict:
    if not request.app.state.users.revoke_session(caller.user.name, session_id):
        raise HTTPException(404, "No such device")
    return {"ok": True}


# ----------------------------------------------------------------------
# Documents
# ----------------------------------------------------------------------
def pdf_text(data: bytes) -> tuple[str, int]:
    try:
        return ingest.pdf_text(data)
    except IngestError as error:
        raise HTTPException(501 if "pypdf" in str(error) else 422, str(error)) from None


def docx_text(data: bytes) -> str:
    try:
        return ingest.docx_text(data)
    except IngestError as error:
        raise HTTPException(422, str(error)) from None


async def uploaded_bytes(request: Request, limit: int, what: str) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():  # counted as it comes: a body with no Content-Length is not read whole first
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"{what} can be {limit // 1_000_000} MB at most.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/v1/documents/extract")
async def extract_document(request: Request, client: Client) -> dict:
    data = await uploaded_bytes(request, MAX_PDF_BYTES, "A PDF")
    if not data.startswith(b"%PDF"):
        raise HTTPException(422, "This is not a PDF file.")
    text, pages = await asyncio.to_thread(pdf_text, data)
    return {"text": text[:MAX_DOCUMENT_CHARS], "pages": pages, "truncated": len(text) > MAX_DOCUMENT_CHARS}


@router.post("/v1/documents/docx")
async def extract_docx(request: Request, client: Client) -> dict:
    data = await uploaded_bytes(request, MAX_DOCX_BYTES, "A Word document")
    if not data.startswith(b"PK\x03\x04"):  # a .docx is a zip archive; the check on its parts comes in docx_text
        raise HTTPException(422, "This is not a Word (.docx) file.")
    text = await asyncio.to_thread(docx_text, data)
    return {"text": text[:MAX_DOCUMENT_CHARS], "truncated": len(text) > MAX_DOCUMENT_CHARS}


# ----------------------------------------------------------------------
# Administration
# ----------------------------------------------------------------------
@router.get("/v1/admin/users")
async def admin_users(admin: Admin, request: Request) -> dict:
    return {"users": [describe_user(request.app, u) for u in request.app.state.users.list()]}


@router.post("/v1/admin/users", status_code=201)
async def admin_add_user(body: NewUserBody, admin: Admin, request: Request) -> dict:
    password = body.password or generate_password()
    users = request.app.state.users
    try:
        if body.discord_id:
            user = await asyncio.to_thread(users.create_with_account, body.name, password, body.admin, DISCORD, body.discord_id)
        else:
            user = await asyncio.to_thread(users.create, body.name, password, body.admin)
    except UserError as error:
        raise HTTPException(422, str(error)) from None
    secret(request)
    log.info("%s created the user %s", admin, user.name)
    if body.discord_id:
        announce_discord_sign_in(request, user, body.discord_id, admin)
    return {"user": describe_user(request.app, user), "password": password}


@router.patch("/v1/admin/users/{name}")
async def admin_edit_user(name: str, body: UserPatch, admin: Admin, request: Request) -> dict:
    users = request.app.state.users
    result: dict[str, Any] = {}
    try:
        if users.get(name) is None:
            raise HTTPException(404, f"No user called {name}")
        if body.admin is not None:
            users.set_admin(name, body.admin)
        if body.disabled is not None:
            users.set_disabled(name, body.disabled)
        if body.follow_default_limit and body.token_limit is not None:
            raise HTTPException(422, "Give token_limit or follow_default_limit, not both")
        if body.follow_default_limit:
            users.set_token_limit(name, None)
        elif body.token_limit is not None:
            users.set_token_limit(name, body.token_limit)
        password = generate_password() if body.generate_password else body.password
        if password:
            await asyncio.to_thread(users.set_password, name, password)
            result["password"] = password
            secret(request)
    except UserError as error:
        raise HTTPException(422, str(error)) from None
    log.info("%s edited the user %s", admin, name)
    result["user"] = describe_user(request.app, users.get(name))
    return result


@router.delete("/v1/admin/users/{name}")
async def admin_remove_user(name: str, admin: Admin, request: Request) -> dict:
    try:
        request.app.state.users.delete(name)
    except UserError as error:
        raise HTTPException(422, str(error)) from None
    log.info("%s removed the user %s", admin, name)
    return {"ok": True}


@router.post("/v1/admin/users/{name}/sign-out")
async def admin_sign_out(name: str, admin: Admin, request: Request) -> dict:
    if request.app.state.users.get(name) is None:
        raise HTTPException(404, f"No user called {name}")
    return {"signed_out": request.app.state.users.revoke_all(name)}


@router.get("/v1/admin/limits")
async def admin_limits(admin: Admin, request: Request) -> dict:
    return {"default": request.app.state.limits.default() or None}


@router.put("/v1/admin/limits/default")
async def admin_set_default_limit(body: DefaultLimit, admin: Admin, request: Request) -> dict:
    limits = request.app.state.limits
    limits.set_default(body.tokens)
    log.info("%s set the default limit to %s", admin, show_limit(limits.default()))
    return {"default": limits.default() or None}


@router.get("/v1/me/usage")
async def my_usage(caller: LoggedIn, request: Request, days: int = Query(0, ge=0, le=3650)) -> dict:
    """The caller's own statistics: the same figures the administration sees for them, and nobody else's."""
    usage_log, person_id = request.app.state.usage_log, caller.user.person_id
    [mine] = usage_log.per_user(days, person_id) or [None]
    return {"days": days, "usage": mine, "quota": describe_user(request.app, caller.user)["usage"]}


@router.get("/v1/me/usage/history")
async def my_usage_history(
    caller: LoggedIn, request: Request, group: Literal["", "discord", "other"] = "",
    surface: str = Query("", max_length=32), kind: Literal["", "message", "scheduled", "compaction", "title"] = "",
    model: str = Query("", max_length=200), days: int = Query(0, ge=0, le=3650), before: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    return request.app.state.usage_log.history(caller.user.person_id, group, surface, kind, model, days, before, limit)


@router.get("/v1/admin/usage")
async def admin_usage(admin: Admin, request: Request, days: int = Query(0, ge=0, le=3650)) -> dict:
    usage_log = request.app.state.usage_log
    return {"days": days, "users": usage_log.per_user(days), "totals": usage_log.totals(days)}


@router.get("/v1/admin/usage/history")
async def admin_usage_history(
    admin: Admin, request: Request, person: int | None = None,
    group: Literal["", "discord", "other"] = "", surface: str = Query("", max_length=32),
    kind: Literal["", "message", "scheduled", "compaction", "title"] = "", model: str = Query("", max_length=200),
    days: int = Query(0, ge=0, le=3650), before: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
) -> dict:
    return request.app.state.usage_log.history(person, group, surface, kind, model, days, before, limit)


@router.get("/v1/admin/status")
async def admin_status(admin: Admin, request: Request) -> dict:
    state = request.app.state
    context, stats, providers = state.commands, state.agent.stats, state.providers
    people, facts = state.memory.counts()
    tailscale = state.tailscale
    return {
        "provider": {"id": providers.config.id, "label": providers.config.label, "host": providers.config.host},
        "model": providers.model,
        "providers": [
            {"id": c.id, "label": c.label, "usable": c.usable, "model": providers.model_of(c.id)}
            for c in providers.configs.values()
        ],
        "listen": context.listen,
        "uptime_seconds": int(time.monotonic() - context.started_at),
        "stopping": state.lifecycle.stopping,
        "turns": {"running": stats.active, "since_start": stats.turns},
        "tokens": {"prompt": stats.prompt_tokens, "completion": stats.completion_tokens},
        "people": people,
        "facts": facts,
        "tailscale": {"mode": tailscale.mode, "url": tailscale.url, "problem": tailscale.problem},
    }


class RestartBody(BaseModel):
    now: bool = False  # do not wait for the answers that are running


@router.get("/v1/admin/restart")
async def admin_restart_status(admin: Admin, request: Request, refresh: bool = False) -> dict:
    """Does the server need a restart (and why), is one under way, what did the last one say?"""
    return await request.app.state.restart.status(refresh)


@router.post("/v1/admin/restart", status_code=202)
async def admin_restart(body: RestartBody, admin: Admin, request: Request) -> dict:
    """Pull, update, stop and start again. 202 once it is under way: `id` is what `/health` says as `restarted`
    when the new server is up. 409 / 500, the server going on, when it cannot (with the output of the step)."""
    log.info("%s asked for a restart", admin)
    try:
        done = await request.app.state.restart.restart(str(admin), now=body.now)
    except RestartError as error:
        raise HTTPException(error.status, str(error)) from None
    return done


@router.get("/v1/admin/models")
async def admin_models(admin: Admin, request: Request) -> dict:
    providers = request.app.state.providers
    try:
        return {"provider": providers.config.id, "model": providers.model, "models": await providers.list_models()}
    except Exception as error:
        return {"provider": providers.config.id, "model": providers.model, "models": [],
                "error": f"{type(error).__name__}: {str(error)[:200]}"}


@router.get("/v1/admin/people")
async def admin_people(admin: Admin, request: Request) -> dict:
    state = request.app.state
    by_person: dict[int, str] = {u.person_id: u.name for u in state.users.list()}
    return {
        "people": [
            {"id": s.person.id, "name": s.person.name, "facts": s.facts, "accounts": s.accounts,
             "user": by_person.get(s.person.id), "relation": s.relation}
            for s in state.memory.summaries()
        ]
    }


def person_or_404(request: Request, person_id: int):
    person = request.app.state.memory.person_by_id(person_id)
    if person is None:
        raise HTTPException(404, "No such person")
    return person


@router.get("/v1/admin/people/{person_id}/facts")
async def admin_person_facts(person_id: int, admin: Admin, request: Request) -> dict:
    person = person_or_404(request, person_id)
    facts = request.app.state.memory.facts(person.id, 1000)
    return {"person": {"id": person.id, "name": person.name}, "facts": [{"id": f.id, "text": f.text} for f in facts]}


@router.post("/v1/admin/people/{person_id}/facts", status_code=201)
async def admin_add_fact(person_id: int, body: FactText, admin: Admin, request: Request) -> dict:
    person = person_or_404(request, person_id)
    try:
        fact = request.app.state.memory.add_fact(person.id, body.text)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return {"stored": fact is not None, "fact": {"id": fact.id, "text": fact.text} if fact else None}


@router.delete("/v1/admin/people/{person_id}/facts/{fact_id}")
async def admin_delete_fact(person_id: int, fact_id: int, admin: Admin, request: Request) -> dict:
    person = person_or_404(request, person_id)
    if not request.app.state.memory.delete_fact(person.id, fact_id):
        raise HTTPException(404, "No such fact")
    return {"ok": True}


@router.get("/v1/admin/people/{person_id}/footprint")
async def admin_footprint(person_id: int, admin: Admin, request: Request) -> dict:
    person = person_or_404(request, person_id)
    found = request.app.state.memory.footprint(person.id)
    return {
        "person": {"id": person.id, "name": person.name},
        "accounts": found.accounts, "facts": found.facts, "messages": found.messages,
        "conversations": found.conversations,
    }


# ----------------------------------------------------------------------
# The web site
# ----------------------------------------------------------------------
class WebFiles(StaticFiles):
    """The web site's files, with the headers that keep it self-contained: no script or style from elsewhere,
    no framing, and a browser asks again for them (they change with the server)."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-cache"
        return response


def install(app: FastAPI) -> None:
    app.include_router(router)


def install_web(app: FastAPI) -> None:
    """Last, so that no file shadows a route of the API."""
    if WEB_DIR.is_dir():
        app.mount("/", WebFiles(directory=WEB_DIR, html=True), name="web")

