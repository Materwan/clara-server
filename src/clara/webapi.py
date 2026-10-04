"""The routes that serve people rather than programs: logging in with a password, the account page, the
administration of users and people, reading a PDF, and the web site itself (`web/`, served at `/`).

    POST   /v1/auth/login               {username, password, surface?, device?} -> a token (or a session cookie)
    POST   /v1/auth/logout              sign out of this device
    GET    /v1/auth/me                  who am I, which accounts are mine
    POST   /v1/auth/password            {current_password, new_password}: change it, other devices are signed out
    GET    /v1/auth/sessions            my devices
    DELETE /v1/auth/sessions/{id}       sign one out
    POST   /v1/documents/extract        the text of a PDF (body: the bytes)

    GET    /v1/admin/users              (administrators; also with an admin token)
    POST   /v1/admin/users              {name, password?, admin?} -> the password, shown once
    PATCH  /v1/admin/users/{name}       {admin?, disabled?, password?, generate_password?}
    DELETE /v1/admin/users/{name}
    POST   /v1/admin/users/{name}/sign-out
    GET    /v1/admin/status             provider, model, activity, address
    GET    /v1/admin/models             what the active provider offers
    GET    /v1/admin/people             everybody Clara knows
    GET|POST /v1/admin/people/{id}/facts, DELETE /v1/admin/people/{id}/facts/{fact}, GET /v1/admin/people/{id}/footprint
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ingest
from .auth import COOKIE, WEB_HEADER, Admin, Client, LoggedIn
from .ingest import IngestError
from .memory import MergeRefused
from .users import User, UserError, generate_password

log = logging.getLogger("clara")

WEB_DIR = Path(__file__).parent / "web"
MAX_PDF_BYTES = 30_000_000
MAX_PDF_CHARS = 400_000

router = APIRouter()


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)
    surface: str = Field(default="web", pattern=r"^[A-Za-z0-9_-]{1,32}$")
    device: str = Field(default="", max_length=80)


class PasswordBody(BaseModel):
    # named so that the traffic log recognises them as secrets (traffic.SECRET_KEYS)
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)


class NewUserBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str | None = Field(default=None, max_length=512)  # none: Clara makes one
    admin: bool = False


class UserPatch(BaseModel):
    admin: bool | None = None
    disabled: bool | None = None
    password: str | None = Field(default=None, max_length=512)
    generate_password: bool = False


class FactText(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def describe_user(app: FastAPI, user: User) -> dict:
    users = app.state.users
    sessions = users.sessions_of(user.name)
    person = app.state.memory.person_by_id(user.person_id)
    return {
        "name": user.name,
        "is_admin": user.is_admin,
        "disabled": user.disabled,
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
        "person": {"id": person.id, "name": person.name} if person else None,
        "sessions": len(sessions),
        "surfaces": sorted({s.surface for s in sessions}),
        "signed_in_accounts": users.accounts_signed_in_as(user.name),  # signed in by a client (Discord)
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
    try:  # this user is one person on every surface: the account of this surface joins theirs
        state.memory.link_account(surface, user.name, users.person_of(user))
    except MergeRefused as error:
        raise HTTPException(409, f"{error} (account {surface}:{user.name})") from None
    device = body.device or request.headers.get("user-agent", "")[:80]
    token, session = users.open_session(user, surface, device, address or "")
    log.info("%s logged in on %s from %s", user.name, surface, address)
    payload: dict[str, Any] = {
        "user": describe_user(request.app, user), "surface": surface, "expires_in_days": settings.session_days,
    }
    web = request.headers.get(WEB_HEADER) == "1"
    if not web:
        payload["token"] = token
    response = JSONResponse(payload)
    if web:  # a browser: the token stays in a cookie, out of reach of scripts
        response.set_cookie(
            COOKIE, token, max_age=(settings.session_days or 3650) * 86400, httponly=True, samesite="strict",
            path="/", secure=request.url.scheme == "https",
        )
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


@router.post("/v1/documents/extract")
async def extract_document(request: Request, client: Client) -> dict:
    declared = int(request.headers.get("content-length") or 0)
    if declared > MAX_PDF_BYTES:
        raise HTTPException(413, f"A PDF can be {MAX_PDF_BYTES // 1_000_000} MB at most.")
    data = await request.body()
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, f"A PDF can be {MAX_PDF_BYTES // 1_000_000} MB at most.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(422, "This is not a PDF file.")
    text, pages = await asyncio.to_thread(pdf_text, data)
    return {"text": text[:MAX_PDF_CHARS], "pages": pages, "truncated": len(text) > MAX_PDF_CHARS}


# ----------------------------------------------------------------------
# Administration
# ----------------------------------------------------------------------
@router.get("/v1/admin/users")
async def admin_users(admin: Admin, request: Request) -> dict:
    return {"users": [describe_user(request.app, u) for u in request.app.state.users.list()]}


@router.post("/v1/admin/users", status_code=201)
async def admin_add_user(body: NewUserBody, admin: Admin, request: Request) -> dict:
    password = body.password or generate_password()
    try:
        user = await asyncio.to_thread(request.app.state.users.create, body.name, password, body.admin)
    except UserError as error:
        raise HTTPException(422, str(error)) from None
    secret(request)
    log.info("%s created the user %s", admin, user.name)
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

