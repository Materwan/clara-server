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

The music site (musicweb/, at /music/) uses the same person and player, and these:

    GET    /v1/me/music/discover             the rows of Music Assistant's discover page (provider, item_id, name)
    GET    /v1/me/music/discover/items       ?provider=&item_id=: the items of one row
    GET    /v1/me/music/playlists            the playlists of the library: {playlists}, the favorites first (`favorite`)
    GET    /v1/me/music/playlist             ?uri=: one playlist opened: {playlist, tracks}, the tracks in its order
    GET    /v1/me/music/lookup               ?q=&media_type=: the library results, grouped by kind
    GET    /v1/me/music/now                  what the player plays, its position, shuffle, repeat and volume
    GET    /v1/me/music/queue                the queue, in Music Assistant's order
    POST   /v1/me/music/queue/jump           {index}: play that item of the queue
    POST   /v1/me/music/queue/remove         {index}: take it out of the queue
    POST   /v1/me/music/queue/move           {queue_item_id, shift}: one place up (-1) or down (1)
    POST   /v1/me/music/queue/clear          empty the queue
    POST   /v1/me/music/control              {action, value?}: play, pause, next, previous, shuffle, repeat, seek, volume
    GET    /v1/me/music/image                ?id=: a picture of the library (its proxy id), through Music Assistant
    POST   /v1/me/music/sendspin/pair        {pairing_token}: pair this browser as a player of Music Assistant
    WS     /v1/me/music/relay/{client_id}/sendspin   the browser player's Sendspin connection, relayed to Music
                                             Assistant with the person's token (the token never reaches the browser)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from contextlib import contextmanager
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response, WebSocket
from pydantic import BaseModel, Field

from .auth import COOKIE, LoggedIn
from .music import MusicAssistant, MusicError
from .musicaccounts import MAX_PLAYER, MAX_TOKEN
from .users import TOKEN_PREFIX

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


class PairBody(BaseModel):
    pairing_token: str = Field(min_length=3, max_length=300)  # the browser player's SP: token (see pairing.md)


class IndexBody(BaseModel):
    index: int = Field(ge=0, le=10_000)


class MoveBody(BaseModel):
    queue_item_id: str = Field(min_length=1, max_length=500)
    shift: Literal[-1, 1]


class ControlBody(BaseModel):
    action: Literal["play", "pause", "next", "previous", "shuffle", "repeat", "seek", "volume"]
    value: bool | int | str | None = None


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


@router.get("/v1/me/music/discover")
async def music_discover(caller: LoggedIn, request: Request) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        return {"rows": await client.discover_rows()}


@router.get("/v1/me/music/discover/items")
async def music_discover_items(
    caller: LoggedIn, request: Request,
    provider: str = Query(min_length=1, max_length=200), item_id: str = Query(min_length=1, max_length=200),
) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        return {"items": await client.discover_items(provider, item_id)}


@router.get("/v1/me/music/playlists")
async def music_playlists(caller: LoggedIn, request: Request) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        return {"playlists": await client.playlists()}


@router.get("/v1/me/music/playlist")
async def music_playlist(
    caller: LoggedIn, request: Request, uri: str = Query(min_length=1, max_length=500),
) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        return await client.playlist(uri)


@router.get("/v1/me/music/lookup")
async def music_lookup(
    caller: LoggedIn, request: Request, q: str = Query("", max_length=200), media_type: str = Query("", max_length=20),
) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        return await client.lookup(q, media_type)


@router.get("/v1/me/music/now")
async def music_now(caller: LoggedIn, request: Request) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        return await client.now()


@router.get("/v1/me/music/queue")
async def music_queue_items(caller: LoggedIn, request: Request) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        return await client.queue_items()


@router.post("/v1/me/music/queue/jump")
async def music_jump(body: IndexBody, caller: LoggedIn, request: Request) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        await client.jump(body.index)
    return {"ok": True}


@router.post("/v1/me/music/queue/remove")
async def music_remove(body: IndexBody, caller: LoggedIn, request: Request) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        await client.remove(body.index)
    return {"ok": True}


@router.post("/v1/me/music/queue/move")
async def music_move(body: MoveBody, caller: LoggedIn, request: Request) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        await client.move(body.queue_item_id, body.shift)
    return {"ok": True}


@router.post("/v1/me/music/queue/clear")
async def music_clear(caller: LoggedIn, request: Request) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        await client.clear()
    return {"ok": True}


@router.post("/v1/me/music/control")
async def music_control(body: ControlBody, caller: LoggedIn, request: Request) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        await client.control(body.action, body.value)
    return {"ok": True}


@router.get("/v1/me/music/image")
async def music_image(
    caller: LoggedIn, request: Request, image_id: str = Query(alias="id", min_length=1, max_length=512),
) -> Response:
    client = _mine(request, caller.user.person_id)
    with _answering():
        data, kind = await client.image(image_id)
    return Response(data, media_type=kind, headers={"Cache-Control": "private, max-age=3600"})


@router.post("/v1/me/music/sendspin/pair")
async def music_sendspin_pair(body: PairBody, caller: LoggedIn, request: Request) -> dict:
    client = _mine(request, caller.user.person_id)
    with _answering():
        return {"message": await client.pair_web_player(body.pairing_token)}


# ---- the browser player: a Sendspin connection, relayed -------------------------------------------------------------
# The browser plays what Music Assistant streams to it. Its Sendspin connection is end to end encrypted, so this
# relay only moves bytes: it opens the connection to Music Assistant with the person's token (which the browser never
# has) and forwards both ways. A browser cannot send the X-Clara-Web header on a WebSocket, so its session cookie is
# read as it is: the cookie is SameSite=Strict, and the Origin header must be this site.

CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{43}")  # the browser player's id: a base64url public key


def _browser_person(websocket: WebSocket) -> int | None:
    """The person whose session cookie this page's connection carries, or None."""
    origin = websocket.headers.get("origin", "")
    if not origin or urlsplit(origin).netloc != websocket.headers.get("host"):
        return None
    token = ""
    for part in websocket.headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            token = value
    if not token.startswith(TOKEN_PREFIX):
        return None
    found = websocket.app.state.users.lookup(token)
    if not found:
        return None
    user, session = found
    return user.person_id if session.surface == "web" else None


@router.websocket("/v1/me/music/relay/{client_id}/sendspin")
async def music_relay(websocket: WebSocket, client_id: str) -> None:
    person_id = _browser_person(websocket)
    music = websocket.app.state.music
    accounts = websocket.app.state.music_accounts
    if person_id is None or music is None or accounts.saved(person_id) is None or not CLIENT_ID.fullmatch(client_id):
        await websocket.close(code=4403)  # refused before the handshake
        return
    try:
        client = accounts.client(music, person_id)
    except MusicError:
        await websocket.close(code=4403)
        return
    await websocket.accept()
    try:
        upstream = await client.open_sendspin(client_id)
    except MusicError as error:
        await websocket.close(code=4502, reason=str(error)[:100])
        return
    await _pipe(websocket, upstream)


async def _pipe(browser: WebSocket, upstream) -> None:
    """Move the messages of one connection to the other until either side ends it."""

    async def from_music() -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                await browser.send_bytes(message)
            else:
                await browser.send_text(message)

    async def from_browser() -> None:
        while True:
            message = await browser.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                await upstream.send(message["bytes"])
            elif message.get("text") is not None:
                await upstream.send(message["text"])

    tasks = {asyncio.ensure_future(from_music()), asyncio.ensure_future(from_browser())}
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await upstream.close()
        with contextlib.suppress(Exception):  # the browser may be gone already
            await browser.close(code=upstream.close_code or 1000)


def install(app: FastAPI) -> None:
    app.include_router(router)
