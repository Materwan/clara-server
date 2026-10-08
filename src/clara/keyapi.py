"""The routes of the API keys people bring (userkeys.py): the Usage page lists the providers, saves or removes a key,
and shows what the person used on their own keys, apart from the server's (the credits).

    GET    /v1/me/api-keys              ?days=: the providers that take a key (with the hint of the saved one) and the
                                        tokens used on the person's own keys
    PUT    /v1/me/api-keys/{provider}   {api_key}: check it with the provider, then keep it (replaces the earlier one)
    DELETE /v1/me/api-keys/{provider}   forget it: the models of that provider are paid in credits again
    GET    /v1/me/api-keys/history      ?kind=&model=&days=&before=&limit=: every call made on the person's own keys
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .auth import LoggedIn
from .userkeys import MAX_KEY, UserKeyError

log = logging.getLogger("clara")

router = APIRouter()


class KeyBody(BaseModel):
    api_key: str = Field(min_length=1, max_length=MAX_KEY)  # called api_key: the traffic log redacts that name


def _state(request: Request, person_id: int, days: int) -> dict:
    state = request.app.state
    keys, usage_log = state.user_keys, state.usage_log
    saved = keys.saved(person_id)
    used = usage_log.by_provider(person_id, days, own_key=True)
    [mine] = usage_log.per_user(days, person_id, own_key=True) or [None]
    return {
        "days": days,
        "providers": [
            {
                **provider,
                "saved": provider["id"] in saved,
                "hint": saved[provider["id"]].hint if provider["id"] in saved else "",
                "created_at": saved[provider["id"]].created_at if provider["id"] in saved else None,
                "usage": used.get(provider["id"]),
            }
            for provider in keys.providers()
        ],
        "usage": mine,
    }


@router.get("/v1/me/api-keys")
async def my_keys(caller: LoggedIn, request: Request, days: int = Query(0, ge=0, le=3650)) -> dict:
    return _state(request, caller.user.person_id, days)


@router.put("/v1/me/api-keys/{provider}")
async def save_key(provider: str, body: KeyBody, caller: LoggedIn, request: Request) -> dict:
    try:
        await request.app.state.user_keys.save(caller.user.person_id, provider, body.api_key)
    except UserKeyError as error:
        raise HTTPException(422, str(error)) from None
    log.info("%s saved an API key for %s", caller.user.name, provider)  # never the key
    return _state(request, caller.user.person_id, 0)


@router.delete("/v1/me/api-keys/{provider}")
async def forget_key(provider: str, caller: LoggedIn, request: Request) -> dict:
    if not request.app.state.user_keys.remove(caller.user.person_id, provider):
        raise HTTPException(404, f"You saved no key for {provider}")
    log.info("%s removed their API key for %s", caller.user.name, provider)
    return _state(request, caller.user.person_id, 0)


@router.get("/v1/me/api-keys/history")
async def keys_history(
    caller: LoggedIn, request: Request, kind: Literal["", "message", "scheduled", "compaction", "title"] = "",
    model: str = Query("", max_length=200), days: int = Query(0, ge=0, le=3650), before: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    return request.app.state.usage_log.history(
        caller.user.person_id, kind=kind, model=model, days=days, before=before, limit=limit, own_key=True
    )


def install(app: FastAPI) -> None:
    app.include_router(router)
