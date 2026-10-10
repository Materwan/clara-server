"""The music site's data: Music Assistant's discover page, the library search, the player's position, its queue and its
controls, and the pictures through its image proxy. Music Assistant is replaced by a fake that answers each command."""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
import websockets
from fastapi import FastAPI as _FastAPI
from fastapi.testclient import TestClient as _TestClient
from starlette.websockets import WebSocketDisconnect
from test_music import PLAYER, music
from test_musicapi import PLAYERS, SECRET, app_for

from clara.integrations.vault import Vault
from clara.music import MusicAssistant, MusicError
from clara.musicaccounts import MusicAccounts as _MusicAccounts
from clara.musicapi import install as _install_music

PC = {"player_id": PLAYER, "name": "Erwan's PC", "available": True, "playback_state": "playing", "volume_level": 40}
TRACK = {
    "uri": "library://track/7",
    "media_type": "track",
    "item_id": "7",
    "provider": "library",
    "name": "Blue",
    "artists": [{"name": "Joni"}],
    "album": {
        "name": "Blue",
        "image": {"type": "thumb", "path": "/cover.jpg", "provider": "library", "proxy_id": "cover-7"},
    },
    "duration": 215,
}
CURRENT = {
    "queue_item_id": "q1",
    "media_item": TRACK,
    "image": {"type": "thumb", "path": "/cover.jpg", "provider": "library", "proxy_id": "cover-7"},
    "duration": 215,
}


@pytest.mark.asyncio
async def test_discover_rows_come_from_the_recommendations_with_their_provider(memory):
    seen = []
    rows = [
        {"item_id": "recent", "provider": "library", "name": "Recently played", "translation_key": "recently_played"},
        {"item_id": "top", "provider": "spotify", "name": "", "translation_key": "top_albums"},
        {"item_id": None, "provider": "x", "name": "no id"},
    ]
    client = music({"music/recommendations": rows}, seen)

    found = await client.discover_rows()

    assert found == [
        {"provider": "library", "item_id": "recent", "name": "Recently played"},
        {"provider": "spotify", "item_id": "top", "name": "Top albums"},
    ]
    assert seen == [("music/recommendations", {}, "Bearer token")]


@pytest.mark.asyncio
async def test_the_library_playlists_come_favorites_first_with_their_mark(memory):
    seen = []
    playlists = [
        {"uri": "library://playlist/1", "media_type": "playlist", "item_id": "1", "provider": "library", "name": "Road",
         "owner": "Erwan", "favorite": False},
        {"uri": "library://playlist/2", "media_type": "playlist", "item_id": "2", "provider": "library",
         "name": "Favorite tracks", "owner": "Erwan", "favorite": True},
        {"uri": "library://playlist/3", "media_type": "playlist", "item_id": "3", "provider": "library",
         "name": "Ambient", "owner": "", "favorite": False},
    ]
    client = music({"music/playlists/library_items": playlists}, seen)

    found = await client.playlists()

    assert [(card["title"], card["favorite"]) for card in found] == [
        ("Favorite tracks", True), ("Road", False), ("Ambient", False)
    ]
    assert found[1]["subtitle"] == "Erwan" and found[2]["subtitle"] == "Playlist"
    assert seen[0][0] == "music/playlists/library_items" and seen[0][1]["order_by"] == "sort_name"


@pytest.mark.asyncio
async def test_a_playlist_opened_gives_its_card_and_its_tracks_in_order_with_their_length(memory):
    seen = []
    no_length = {"uri": "library://track/8", "media_type": "track", "item_id": "8", "provider": "library", "name": "Ra",
                 "artists": [{"name": "Joni"}], "duration": 0}
    answers = {
        "music/playlists/get": {"uri": "library://playlist/2", "media_type": "playlist", "item_id": "2",
                                "provider": "library", "name": "Favorite tracks", "owner": "Erwan", "favorite": True},
        "music/playlists/playlist_tracks": [TRACK, no_length],
    }
    client = music(answers, seen)

    found = await client.playlist("library://playlist/2")

    assert found["playlist"] | {"image": None} == {
        "uri": "library://playlist/2", "type": "playlist", "title": "Favorite tracks", "subtitle": "Erwan",
        "image": None, "favorite": True,
    }
    assert [(track["title"], track["subtitle"], track["duration"]) for track in found["tracks"]] == [
        ("Blue", "Joni", 215), ("Ra", "Joni", None)
    ]
    assert sorted(command for command, _, _ in seen) == ["music/playlists/get", "music/playlists/playlist_tracks"]
    assert all(args == {"item_id": "2", "provider_instance_id_or_domain": "library"} for _, args, _ in seen)


@pytest.mark.asyncio
async def test_only_a_playlist_uri_is_opened_as_a_playlist(memory):
    client = music({})

    with pytest.raises(ValueError):
        await client.playlist("library://track/7")


@pytest.mark.asyncio
async def test_a_discover_row_gives_site_cards_and_skips_what_cannot_play(memory):
    items = [
        TRACK,
        {"name": "Nothing", "media_type": "album"},
        {"item_id": "9", "provider": "library", "media_type": "playlist", "name": "Mix", "owner": "Erwan"},
    ]
    seen = []
    client = music({"music/recommendations/items": items}, seen)

    cards = await client.discover_items("library", "recent")

    assert cards == [
        {
            "uri": "library://track/7",
            "type": "track",
            "title": "Blue",
            "subtitle": "Joni",
            "image": {"id": "cover-7"},
        },
        {"uri": "library://playlist/9", "type": "playlist", "title": "Mix", "subtitle": "Erwan", "image": None},
    ]
    assert seen == [("music/recommendations/items", {"provider": "library", "item_id": "recent"}, "Bearer token")]


@pytest.mark.asyncio
async def test_a_library_item_without_a_picture_borrows_it_from_the_provider_item_of_the_same_name(memory):
    album = {"uri": "library://album/46", "media_type": "album", "name": "Golden"}
    items = [
        {"uri": "library://track/1", "media_type": "track", "name": "A", "album": album, "metadata": {}},
        {"uri": "library://track/2", "media_type": "track", "name": "B", "album": album, "metadata": {}},
        {"uri": "library://artist/3", "media_type": "artist", "name": "Cee", "metadata": None},
    ]
    hit = {"name": "golden", "media_type": "album", "image": {"proxy_id": "deezer-1"}}
    bare = {"name": "Golden", "media_type": "album", "metadata": {"images": []}}
    artist = {"name": "Cee", "media_type": "artist", "image": {"proxy_id": "deezer-2"}}
    seen = []
    client = music({
        "music/recommendations/items": items,
        "music/search": {"albums": [bare, hit], "artists": [artist]},
    }, seen)

    cards = await client.discover_items("library", "recent")

    assert [card["image"] for card in cards] == [{"id": "deezer-1"}, {"id": "deezer-1"}, {"id": "deezer-2"}]
    searched = sorted(args["search_query"] for command, args, _ in seen if command == "music/search")
    assert searched == ["Cee", "Golden"]  # one search per album, however many tracks


@pytest.mark.asyncio
async def test_library_search_is_grouped_by_kind(memory):
    answer = {
        "tracks": [TRACK],
        "albums": [],
        "artists": [{"uri": "library://artist/3", "name": "Joni", "media_type": "artist"}],
    }
    seen = []

    found = await music({"music/search": answer}, seen).lookup("  joni ")

    assert found["query"] == "joni"
    assert [group["type"] for group in found["groups"]] == ["track", "artist"]
    assert found["groups"][1]["items"][0]["subtitle"] == "Artist"
    assert seen == [("music/search", {"search_query": "joni", "limit": 25}, "Bearer token")]
    with pytest.raises(ValueError):
        await music({}).lookup("")


@pytest.mark.asyncio
async def test_the_position_moves_on_while_the_player_plays_and_stops_at_the_end(memory):
    queue = {
        "state": "playing",
        "current_item": CURRENT,
        "current_index": 2,
        "elapsed_time": 100.0,
        "elapsed_time_last_updated": time.time() - 5,
        "shuffle_enabled": True,
        "repeat_mode": "all",
    }
    client = music({"players/get": PC, "player_queues/get": queue})

    now = await client.now()

    assert now["state"] == "playing" and now["track"]["title"] == "Blue"
    assert 104 <= now["position"] <= 106 and now["duration"] == 215
    assert now["shuffle"] is True and now["repeat"] == "all" and now["volume"] == 40 and now["index"] == 2

    queue["state"] = "paused"
    assert (await client.now())["position"] == 100.0
    queue["elapsed_time"] = 999.0
    assert (await client.now())["position"] == 215.0


@pytest.mark.asyncio
async def test_the_queue_is_listed_in_music_assistants_order_with_the_current_item_marked(memory):
    items = [
        CURRENT,
        {
            "queue_item_id": "q2",
            "name": "Gone",
            "media_item": {"uri": "", "name": "Gone"},
            "duration": 0,
            "available": False,
        },
    ]
    client = music({"players/get": PC, "player_queues/get": {"current_index": 0}, "player_queues/items": items})

    queue = await client.queue_items()

    assert queue["index"] == 0
    assert [row["queue_item_id"] for row in queue["items"]] == ["q1", "q2"]
    assert queue["items"][0]["current"] is True and queue["items"][0]["duration"] == 215
    assert (
        queue["items"][1]["uri"] == ""
        and queue["items"][1]["title"] == "Gone"
        and queue["items"][1]["available"] is False
    )


@pytest.mark.asyncio
async def test_controls_send_the_matching_command_and_refuse_wrong_values(memory):
    seen = []
    client = music(
        {
            "players/get": PC,
            "player_queues/next": None,
            "player_queues/shuffle": None,
            "player_queues/repeat": None,
            "player_queues/seek": None,
            "players/cmd/volume_set": None,
        },
        seen,
    )

    await client.control("next")
    await client.control("shuffle", True)
    await client.control("repeat", "one")
    await client.control("seek", 42)
    await client.control("volume", 70)

    sent = [(command, args) for command, args, _ in seen if command != "players/get"]
    assert sent == [
        ("player_queues/next", {"queue_id": PLAYER}),
        ("player_queues/shuffle", {"queue_id": PLAYER, "shuffle_enabled": True}),
        ("player_queues/repeat", {"queue_id": PLAYER, "repeat_mode": "one"}),
        ("player_queues/seek", {"queue_id": PLAYER, "position": 42}),
        ("players/cmd/volume_set", {"player_id": PLAYER, "volume_level": 70}),
    ]
    for action, value in [("shuffle", "yes"), ("repeat", "loop"), ("seek", -1), ("volume", 101), ("fly", None)]:
        with pytest.raises(ValueError):
            await client.control(action, value)


@pytest.mark.asyncio
async def test_queue_edits_are_checked_before_music_assistant_is_asked(memory):
    seen = []
    client = music(
        {
            "players/get": PC,
            "player_queues/play_index": None,
            "player_queues/move_item": None,
            "player_queues/delete_item": None,
            "player_queues/clear": None,
        },
        seen,
    )

    await client.jump(3)
    await client.move("q1", -1)
    await client.remove(0)
    await client.clear()
    with pytest.raises(ValueError):
        await client.move("q1", 2)
    with pytest.raises(ValueError):
        await client.jump(-1)

    assert [command for command, _, _ in seen if command != "players/get"] == [
        "player_queues/play_index",
        "player_queues/move_item",
        "player_queues/delete_item",
        "player_queues/clear",
    ]
    moved = [args for command, args, _ in seen if command == "player_queues/move_item"]
    assert moved == [{"queue_id": PLAYER, "queue_item_id": "q1", "pos_shift": -1}]


@pytest.mark.asyncio
async def test_a_picture_is_fetched_through_the_image_proxy_with_the_token(memory):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params), request.headers.get("authorization")))
        if request.url.path == "/imageproxy/cover-7":
            return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
        return httpx.Response(404, text="no")

    client = MusicAssistant("http://ma:8095/", PLAYER, "token", transport=httpx.MockTransport(handler))

    data, kind = await client.image("cover-7")

    assert (data, kind) == (b"\x89PNG", "image/png")
    assert seen == [("/imageproxy/cover-7", {}, "Bearer token")]


@pytest.mark.asyncio
async def test_a_missing_picture_is_an_error_not_a_picture(memory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="no such picture")

    client = MusicAssistant("http://ma:8095/", PLAYER, "token", transport=httpx.MockTransport(handler))

    with pytest.raises(MusicError):
        await client.image("gone-1")


def test_the_site_routes_answer_for_a_person_with_a_player(memory):
    answers = {
        "players/all": PLAYERS,
        "players/get": PC,
        "player_queues/get": {"state": "idle", "current_item": None, "current_index": None},
        "player_queues/items": [],
        "music/recommendations": [],
        "music/playlists/library_items": [],
    }
    client, _ = app_for(memory, music(answers), "erwan")
    client.put("/v1/me/music", json={"player": PLAYER, "token": SECRET})

    assert client.get("/v1/me/music/now").json()["state"] == "stopped"
    assert client.get("/v1/me/music/queue").json() == {"index": None, "items": []}
    assert client.get("/v1/me/music/discover").json() == {"rows": []}
    assert client.get("/v1/me/music/playlists").json() == {"playlists": []}
    assert client.post("/v1/me/music/control", json={"action": "fly"}).status_code == 422
    assert client.post("/v1/me/music/queue/move", json={"queue_item_id": "q", "shift": 3}).status_code == 422


def test_the_site_routes_refuse_a_person_who_chose_no_player(memory):
    client, _ = app_for(memory, music({}), "erwan")

    assert client.get("/v1/me/music/now").status_code == 409
    assert client.get("/v1/me/music/discover").status_code == 409
    assert client.get("/v1/me/music/playlists").status_code == 409
    assert client.get("/v1/me/music/image", params={"id": "cover-7"}).status_code == 409


def test_the_playlist_route_answers_with_the_playlist_and_refuses_what_is_not_one(memory):
    answers = {
        "players/all": PLAYERS,
        "music/playlists/get": {"uri": "library://playlist/2", "media_type": "playlist", "item_id": "2",
                                "provider": "library", "name": "Favorite tracks", "owner": "Erwan"},
        "music/playlists/playlist_tracks": [TRACK],
    }
    client, _ = app_for(memory, music(answers), "erwan")
    client.put("/v1/me/music", json={"player": PLAYER, "token": SECRET})

    body = client.get("/v1/me/music/playlist", params={"uri": "library://playlist/2"}).json()

    assert body["playlist"]["title"] == "Favorite tracks" and [track["title"] for track in body["tracks"]] == ["Blue"]
    assert client.get("/v1/me/music/playlist", params={"uri": "library://track/7"}).status_code == 422


def test_the_site_routes_need_a_server_with_music_assistant(memory):
    client, _ = app_for(memory, None, "erwan")

    assert client.get("/v1/me/music/lookup", params={"q": "x"}).status_code == 404


def test_json_bodies_of_the_site_are_read_strictly(memory):
    client, _ = app_for(memory, music({}), "erwan")

    assert client.post("/v1/me/music/queue/jump", json={"index": "two"}).status_code == 422
    assert client.post("/v1/me/music/queue/jump", json={"index": -1}).status_code == 422


def test_the_music_site_is_served_at_music_and_the_web_site_still_at_the_root():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from clara.webapi import install_web

    app = FastAPI()
    install_web(app)
    client = TestClient(app)

    page = client.get("/music/")
    assert page.status_code == 200 and "Music – Clara" in page.text and "/music/app.js" in page.text
    assert client.get("/music/site.css").status_code == 200
    assert client.get("/music/player.js").headers["content-security-policy"].startswith("default-src 'self'")
    assert "Clara" in client.get("/").text
    assert client.get("/music.js").status_code == 404  # the old page is gone


@pytest.mark.asyncio
async def test_a_picture_id_is_checked_before_it_is_put_in_the_address(memory):
    seen = []
    client = MusicAssistant(
        "http://ma:8095/",
        PLAYER,
        "token",
        transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200)),
    )

    for bad in ["../players/all", "a b", "x?y=1", ""]:
        with pytest.raises(ValueError):
            await client.image(bad)
    assert seen == []


PAIRING = "SP:0AAAQEAYEAUDAOCAJBIFQYDIOB4IBCEQTCQKRMFYYDENBWHA5DYP6BYPC4PSOLZXH5DU6V97M5XXO74HR6LZ7J5PW674PT6X37T6757Y"
CLIENT = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"  # 43 characters: a client id


def test_the_browser_player_is_paired_with_the_persons_token(memory):
    seen = []
    client, _ = app_for(memory, music({"players/all": PLAYERS, "sendspin/pair_web_player": None}, seen), "erwan")
    client.put("/v1/me/music", json={"player": PLAYER, "token": SECRET})
    seen.clear()

    response = client.post("/v1/me/music/sendspin/pair", json={"pairing_token": PAIRING})

    assert response.status_code == 200 and "paired" in response.json()["message"]
    assert seen == [("sendspin/pair_web_player", {"pairing_token": PAIRING}, "Bearer " + SECRET)]
    assert client.post("/v1/me/music/sendspin/pair", json={"pairing_token": "not a token"}).status_code == 422


@pytest.mark.asyncio
async def test_the_token_goes_first_and_its_answer_is_not_passed_on():
    received = []

    async def music_assistant(connection):
        first = json.loads(await connection.recv())
        received.append(first)
        if first.get("token") != "good":
            await connection.close(4001, "Invalid or expired token")
            return
        await connection.send('{"type": "auth_ok"}')
        await connection.send(b"\x09sendspin")
        received.append(await connection.recv())

    async with websockets.serve(music_assistant, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        upstream = await MusicAssistant(f"http://127.0.0.1:{port}", PLAYER, "good").open_sendspin(CLIENT)
        assert await upstream.recv() == b"\x09sendspin"
        await upstream.send("hello")
        await upstream.close()
        await asyncio.sleep(0.05)

    assert received == [{"type": "auth", "token": "good", "client_id": CLIENT}, "hello"]


@pytest.mark.asyncio
async def test_a_refused_token_is_said_as_a_refusal():
    async def refuse(connection):
        await connection.recv()
        await connection.close(4001, "Invalid or expired token")

    async with websockets.serve(refuse, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(MusicError, match="refused the browser player"):
            await MusicAssistant(f"http://127.0.0.1:{port}", PLAYER, "bad").open_sendspin(CLIENT)


def test_the_sendspin_address_follows_the_music_assistant_address():
    assert MusicAssistant("http://100.1.2.3:8095/").sendspin_url() == "ws://100.1.2.3:8095/sendspin"
    assert MusicAssistant("https://music.example").sendspin_url() == "wss://music.example/sendspin"


class _FakeUpstream:
    """Music Assistant's end of the relay: it sends `first`, then echoes what it is sent, until it is closed."""

    close_code = None

    def __init__(self, first):
        self.queue = asyncio.Queue()
        for message in first:
            self.queue.put_nowait(message)

    async def send(self, message):
        await self.queue.put(message)

    async def close(self):
        await self.queue.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.queue.get()
        if message is None:
            raise StopAsyncIteration
        return message


class _FakeMusic:
    """Stands for the Music Assistant client of a person: `open_sendspin` hands out the fake upstream."""

    def __init__(self, first):
        self.first = first
        self.opened = []

    def for_player(self, player, token):
        return self

    async def open_sendspin(self, client_id):
        self.opened.append(client_id)
        return _FakeUpstream(self.first)


class _FakeUsers:
    def __init__(self, person_id):
        self.person_id = person_id

    def lookup(self, token):
        if token != "clu_erwan":
            return None
        return SimpleNamespace(person_id=self.person_id, name="erwan"), SimpleNamespace(surface="web")


def _relay(memory, music_client, saved=True) -> _TestClient:
    app = _FastAPI()
    _install_music(app)
    person = memory.resolve("web", "erwan", "erwan")
    accounts = _MusicAccounts(memory, Vault("test-secret"))
    if saved:
        accounts.save(person.id, PLAYER, SECRET)
    app.state.music = music_client
    app.state.music_accounts = accounts
    app.state.users = _FakeUsers(person.id)
    return _TestClient(app)


SESSION = {"origin": "http://testserver", "cookie": "clara_session=clu_erwan"}


def test_the_relay_moves_messages_both_ways_for_the_person_who_chose_a_player(memory):
    fake = _FakeMusic([b"\x01\x02", "server text"])
    client = _relay(memory, fake)

    with client.websocket_connect(f"/v1/me/music/relay/{CLIENT}/sendspin", headers=SESSION) as ws:
        assert ws.receive_bytes() == b"\x01\x02"
        assert ws.receive_text() == "server text"
        ws.send_text("hello")
        assert ws.receive_text() == "hello"  # echoed back by the fake Music Assistant
        ws.send_bytes(b"\xff")
        assert ws.receive_bytes() == b"\xff"
    assert fake.opened == [CLIENT]


@pytest.mark.parametrize(
    "headers, path_id, saved",
    [
        ({"origin": "http://testserver"}, CLIENT, True),  # no session cookie
        ({"origin": "http://evil.example", "cookie": "clara_session=clu_erwan"}, CLIENT, True),  # another site
        ({"cookie": "clara_session=clu_erwan"}, CLIENT, True),  # a browser always says where it comes from
        (SESSION, "short", True),  # not a client id
        (SESSION, CLIENT, False),  # the person chose no player
    ],
)
def test_the_relay_refuses_what_is_not_this_persons_browser(memory, headers, path_id, saved):
    fake = _FakeMusic([])
    client = _relay(memory, fake, saved=saved)

    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(f"/v1/me/music/relay/{path_id}/sendspin", headers=headers) as ws:
            ws.receive()
    assert fake.opened == []
    assert refused.value.code == 4403


def test_a_picture_without_a_proxy_id_is_kept_under_its_web_address():
    from clara.music import REMOTE_PICTURES, _picture

    picture = _picture(
        {"type": "thumb", "path": "https://i.example/cover.jpg", "provider": "spotify", "proxy_id": None}
    )

    assert picture["id"].startswith("r") and REMOTE_PICTURES[picture["id"]] == "https://i.example/cover.jpg"
    assert _picture({"path": "/local/cover.jpg", "provider": "library", "proxy_id": None}) is None
    assert _picture(None) is None


def test_a_playlist_without_a_picture_of_its_own_takes_the_thumbnail_of_its_metadata():
    from clara.music import REMOTE_PICTURES, _card

    card = _card(
        {
            "uri": "deezer--x://playlist/687945565",
            "media_type": "playlist",
            "name": "Hits Dance",
            "owner": "Deezer",
            "image": None,
            "metadata": {
                "images": [
                    {"type": "fanart", "path": "https://i.example/fan.jpg", "provider": "deezer"},
                    {"type": "thumb", "path": "https://i.example/thumb.jpg", "provider": "deezer"},
                ]
            },
        }
    )

    assert REMOTE_PICTURES[card["image"]["id"]] == "https://i.example/thumb.jpg"


def test_a_playlist_with_no_picture_anywhere_has_none():
    from clara.music import _card

    card = _card({"uri": "library://playlist/9", "media_type": "playlist", "name": "New", "metadata": {"images": []}})

    assert card["image"] is None


@pytest.mark.asyncio
async def test_a_remote_picture_is_fetched_from_its_address_without_the_token():
    from clara.music import _picture

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})

    picture = _picture({"path": "https://i.example/album.jpg", "provider": "tidal"})
    client = MusicAssistant("http://ma:8095/", PLAYER, "token", transport=httpx.MockTransport(handler))

    assert await client.image(picture["id"]) == (b"\x89PNG", "image/png")
    assert seen == [("https://i.example/album.jpg", None)]  # the token never goes to another provider


@pytest.mark.asyncio
async def test_only_pictures_music_assistant_gave_are_fetched(memory):
    client = MusicAssistant(
        "http://ma:8095/", PLAYER, "token", transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )

    with pytest.raises(ValueError):
        await client.image("r" + "0" * 40)  # never given: the server is no fetcher for it
