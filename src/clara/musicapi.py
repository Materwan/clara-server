"""The routes of the Music page (music.py, musicaccounts.py): each person chooses the player of their PC and gives the
token of their own Music Assistant user, then searches, plays, queues and stops on that player. Only the person
signed in is ever controlled, and the token is never given back.

    GET    /v1/me/music                 the player the person chose (none: not set up yet); `server`: whether the server
                                        has a Music Assistant at all
    PUT    /v1/me/music                 {player, token?}: check that the token works and that the player is there, then keep
                                        both (the token encrypted). Without a token: the one saved, only the player changes
    DELETE /v1/me/music                 forget them
    POST   /v1/me/music/players         {token?}: the players of Music Assistant, with the token given (else the saved one)
    GET    /v1/me/music/status          the player: its name, whether it plays, paused or is stopped, and what it plays
    GET    /v1/me/music/search          ?q=&media_type=: the results (uri, type, title, artist, album)
    POST   /v1/me/music/play            {uri}: play it now, replacing the queue
    POST   /v1/me/music/queue           {uri, position}: position next (after the current track) or end
    POST   /v1/me/music/stop            stop the player
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .auth import LoggedIn
from .music import MusicAssistant, MusicError
from .musicaccounts import MAX_PLAYER, MAX_TOKEN

log = logging.getLogger("clara")

router = APIRouter()


class MusicBody(BaseModel):
    player: str = Field(min_length=1, max_length=MAX_PLAYER)
    token: str | None = Field(default=None, max_length=MAX_TOKEN)  # named token: the traffic log redacts that name


class TokenBody(BaseModel):
    token: str | None = Field(default=None, max_length=MAX_TOKEN)


class UriBody(BaseModel):
    uri: str = Field(min_length=1, max_length=500)


class QueueBody(UriBody):
    position: Literal["next", "end"]


@contextmanager
def _answering():
    """Music Assistant's errors as the page reads them: 502 when it could not do it, 422 when the request is wrong."""
    try:
        yield
    except MusicError as error:
        raise HTTPException(502, str(error)) from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


def _server(request: Request) -> MusicAssistant:
    music = request.app.state.music
    if music is None:
        raise HTTPException(404, "Music Assistant is not set up on this server (MUSIC_ASSISTANT_URL).")
    return music


def _mine(request: Request, person_id: int) -> MusicAssistant:
    """The Music Assistant of the person: their player, with their token. 409 when they chose none yet."""
    music = _server(request)
    accounts = request.app.state.music_accounts
    if accounts.saved(person_id) is None:
        raise HTTPException(409, "Music is not set up for you yet: choose your player and token first.")
    try:
        return accounts.client(music, person_id)
    except MusicError as error:
        raise HTTPException(409, str(error)) from None


def _state(request: Request, person_id: int) -> dict:
    saved = request.app.state.music_accounts.saved(person_id)
    return {
        "server": request.app.state.music is not None,
        "player": saved.player if saved else None,
        "saved_at": saved.saved_at if saved else None,
    }


@router.get("/v1/me/music")
async def my_music(caller: LoggedIn, request: Request) -> dict:
    return _state(request, caller.user.person_id)


@router.put("/v1/me/music")
async def save_music(body: MusicBody, caller: LoggedIn, request: Request) -> dict:
    music = _server(request)
    accounts = request.app.state.music_accounts
    person_id = caller.user.person_id
    token = None if body.token is None else body.token.strip()
    if token == "":
        raise HTTPException(422, "The token is empty.")
    with _answering():
        client = music.for_player("", token) if token else _mine(request, person_id)
        players = await client.players()
    if body.player not in {player["id"] for player in players}:
        raise HTTPException(422, "Music Assistant knows no player of that id: load the players again and choose one.")
    if token:
        accounts.save(person_id, body.player, token)
    else:
        accounts.choose(person_id, body.player)
    log.info("%s chose their Music Assistant player", caller.user.name)  # never the token
    return _state(request, person_id)


@router.delete("/v1/me/music")
async def forget_music(caller: LoggedIn, request: Request) -> dict:
    if not request.app.state.music_accounts.remove(caller.user.person_id):
        raise HTTPException(404, "You chose no Music Assistant player")
    log.info("%s forgot their Music Assistant player", caller.user.name)
    return _state(request, caller.user.person_id)


@router.post("/v1/me/music/players")
async def music_players(body: TokenBody, caller: LoggedIn, request: Request) -> dict:
    given = (body.token or "").strip()
    client = _server(request).for_player("", given) if given else _mine(request, caller.user.person_id)
    with _answering():
        return {"players": await client.players()}


@router.get("/v1/me/music/status")
async def music_status(caller: LoggedIn, request: Request) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        return await client.status()


@router.get("/v1/me/music/search")
async def music_search(
    caller: LoggedIn, request: Request, q: str = Query("", max_length=200),
    media_type: str = Query("", max_length=20),
) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        return {"results": await client.find(q, media_type)}


@router.post("/v1/me/music/play")
async def music_play(body: UriBody, caller: LoggedIn, request: Request) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        return {"message": await client.play(body.uri)}


@router.post("/v1/me/music/queue")
async def music_queue(body: QueueBody, caller: LoggedIn, request: Request) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        return {"message": await client.queue(body.uri, body.position)}


@router.post("/v1/me/music/stop")
async def music_stop(caller: LoggedIn, request: Request) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        return {"message": await client.stop()}


def install(app: FastAPI) -> None:
    app.include_router(router)
