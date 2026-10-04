"""Who is calling, and what they may touch.

Two kinds of credentials reach the API:

* a **client token** (`CLARA_TOKENS`): a program (the Discord bot, a script) that is trusted to say who is
  speaking, within the surfaces `CLARA_CLIENT_SURFACES` gives it;
* a **user token** (`clu_...`, from `POST /v1/auth/login`): a person who proved their password. It is bound to
  that user and one surface, so the server ignores whatever else the request claims and refuses it (403).

A browser keeps its user token in an HttpOnly cookie. A cookie is only honoured with the header
`X-Clara-Web: 1`, which a page of another site cannot add without our consent (CORS is not enabled).
Operators use `CLARA_ADMIN_TOKENS`, or a user flagged administrator.
"""

from __future__ import annotations

import logging
import math
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from .ratelimit import FailureLimiter
from .settings import Settings
from .users import TOKEN_PREFIX, Session, User, Users

log = logging.getLogger("clara")

COOKIE = "clara_session"
WEB_HEADER = "x-clara-web"


class Caller(str):
    """The name of whoever is calling (a client's name, or `user@surface`). For a user who logged in it also
    holds who, and on which surface."""

    user: User | None = None
    session: Session | None = None
    surface: str | None = None
    admin_token: bool = False


def credentials(headers) -> str:
    """The token of a request: `Authorization: Bearer ...`, or the session cookie of the web site."""
    scheme, _, given = headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and given.strip():
        return given.strip()
    if headers.get(WEB_HEADER) == "1":
        for part in headers.get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
    return ""


def _named(given: str, tokens: dict[str, str]) -> str | None:
    caller = None
    for token, name in tokens.items():  # no early exit: constant-ish time
        if secrets.compare_digest(token.encode(), given.encode()):
            caller = name
    return caller


def _user_caller(user: User, session: Session) -> Caller:
    caller = Caller(f"{user.name}@{session.surface}")
    caller.user, caller.session, caller.surface = user, session, session.surface
    return caller


def _resolve(request: Request, tokens: dict[str, str]) -> Caller | None:
    """The caller behind the request, or None; an address sending too many wrong ones is refused (429)."""
    limiter: FailureLimiter = request.app.state.auth_limiter
    address = request.client.host if request.client else None
    wait = limiter.blocked_for(address)
    if wait:
        raise HTTPException(
            429, "Too many wrong tokens from this address: try again later.",
            headers={"Retry-After": str(math.ceil(wait))},
        )
    given = credentials(request.headers)
    caller: Caller | None = None
    if given.startswith(TOKEN_PREFIX):
        users: Users = request.app.state.users
        found = users.lookup(given)
        caller = _user_caller(*found) if found else None
    elif given:
        name = _named(given, tokens)
        caller = Caller(name) if name else None
    if caller is not None:
        limiter.succeeded(address)
    elif limiter.failed(address):
        log.warning("%s sent %d wrong tokens: refused for %d s", address, limiter.max_failures, limiter.block_seconds)
    return caller


def authenticate(request: Request) -> Caller:
    """The calling chat client or user, or 401 (429 after too many wrong tokens)."""
    caller = _resolve(request, request.app.state.settings.tokens)
    if caller is None:
        raise HTTPException(401, "Missing or invalid token", headers={"WWW-Authenticate": "Bearer"})
    return caller


def authenticate_admin(request: Request) -> Caller:
    """The calling operator (an admin token, or a user flagged administrator); 403 when remote admin is
    off or the user is not one, 401 for a bad token."""
    settings: Settings = request.app.state.settings
    if not settings.admin_tokens and not credentials(request.headers).startswith(TOKEN_PREFIX):
        raise HTTPException(403, "Remote admin is disabled: set CLARA_ADMIN_TOKENS")
    caller = _resolve(request, settings.admin_tokens)
    if caller is None:
        raise HTTPException(401, "Missing or invalid admin token", headers={"WWW-Authenticate": "Bearer"})
    if caller.user is None:
        caller.admin_token = True
    elif not caller.user.is_admin:
        raise HTTPException(403, "Administrators only")
    return caller


def authenticate_user(request: Request) -> Caller:
    """A user who logged in (not a client token)."""
    caller = authenticate(request)
    if caller.user is None:
        raise HTTPException(403, "Only a user who logged in can do this")
    return caller


NOT_SIGNED_IN = "not signed in"  # in the 403 of an account that must sign in first (clients look for it)


def require_account(request: Request, client: str, surface: str, user_id: str, signed_in: bool = True) -> None:
    """403 unless the caller may speak for the account `surface:user_id`: a client within its surfaces
    (CLARA_CLIENT_SURFACES), a user as themselves on the surface they logged in on. On the surfaces of
    CLARA_LOGIN_SURFACES the account must also be signed in as a user (unless `signed_in` is False: the
    routes that sign it in)."""
    user: User | None = getattr(client, "user", None)
    if user is not None:
        if surface != client.surface or user_id != user.name:  # type: ignore[attr-defined]
            raise HTTPException(403, "This login may only act as its own account")
        return
    settings: Settings = request.app.state.settings
    allowed = settings.client_surfaces.get(client)
    if allowed is not None and surface not in allowed:
        raise HTTPException(403, f"This client may not use the surface {surface!r}")
    if signed_in and surface in settings.login_surfaces:
        users: Users = request.app.state.users
        if users.account_user(surface, user_id) is None:
            raise HTTPException(403, f"The account {surface}:{user_id} is {NOT_SIGNED_IN}: sign in or register first")


def require_conversation(request: Request, client: str, conversation: str) -> None:
    """403 unless the conversation belongs to a surface the client may use, or is one of the user's own."""
    user: User | None = getattr(client, "user", None)
    if user is not None:
        own = f"{client.surface}:{user.name}"  # type: ignore[attr-defined]
        if conversation != own and not conversation.startswith(own + ":"):
            raise HTTPException(403, "This login may not use that conversation")
        return
    allowed = request.app.state.settings.client_surfaces.get(client)
    if allowed is not None and not any(conversation.startswith(f"{surface}:") for surface in allowed):
        raise HTTPException(403, "This client may not use that conversation")


def require_space(request: Request, client: str, space: str, surface: str) -> None:
    """403 unless the client may name that space: it is of the request's surface (`discord:guild:42` for
    `discord`), one the client may use. A user who logged in has no spaces."""
    if getattr(client, "user", None) is not None:
        raise HTTPException(403, "Only a client may speak in a space")
    if not space.startswith(f"{surface}:"):
        raise HTTPException(403, f"A space of the surface {surface!r} starts with {surface}:")
    allowed = request.app.state.settings.client_surfaces.get(client)
    if allowed is not None and surface not in allowed:
        raise HTTPException(403, f"This client may not use the surface {surface!r}")


Client = Annotated[Caller, Depends(authenticate)]
Admin = Annotated[Caller, Depends(authenticate_admin)]
LoggedIn = Annotated[Caller, Depends(authenticate_user)]


def peer_of(settings: Settings, users: Users | None, headers) -> str:
    """Who is calling, for the traffic log: never the token itself."""
    given = credentials(headers)
    if not given:
        return "anonymous"
    if given.startswith(TOKEN_PREFIX):
        found = users.lookup(given, touch=False) if users is not None else None
        return f"user:{found[0].name}@{found[1].surface}" if found else "unknown-token"
    for tokens, kind in ((settings.tokens, "client"), (settings.admin_tokens, "admin")):
        for token, name in tokens.items():
            if secrets.compare_digest(token.encode(), given.encode()):
                return f"{kind}:{name}"
    return "unknown-token"
