"""Music Assistant, through the `music_*` tools and the Music page: search its library, and play, queue or stop on a
person's player.

Music Assistant (MA) answers one command per request, at its HTTP API (its commands are listed at /api-docs/commands):

    POST {MUSIC_ASSISTANT_URL}/api  {"command": "music/search", "args": {...}}  -> the command's result, as JSON

with a token as a bearer token (MA refuses every command without one). Each person has their own player and token
(musicaccounts.py): `for_player` makes the client that controls their player, and only that one. Its queue has the
same id as the player.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from typing import Any

import httpx
import websockets

from .httpclient import SharedClient

log = logging.getLogger("clara")

TIMEOUT = 15.0
SEARCH_LIMIT = 5  # items of each kind MA searches for
MAX_RESULTS = 10  # lines a search answers with
MEDIA_TYPES = ("track", "album", "artist", "playlist", "radio", "audiobook", "podcast", "podcast_episode")
# The lists of MA's search results, with the media type of what they hold
RESULT_LISTS = (
    ("tracks", "track"),
    ("albums", "album"),
    ("artists", "artist"),
    ("playlists", "playlist"),
    ("radio", "radio"),
    ("audiobooks", "audiobook"),
    ("podcasts", "podcast"),
)
# MA's playback states (PlaybackState), as the model reads them
STATES = {"playing": "playing", "paused": "paused", "idle": "stopped"}
# The positions of `music_queue`, as MA's queue options: after the current track, or at the end of the queue
POSITIONS = {"next": "next", "end": "add"}
# How many of the last tracks played `music_next` looks at for a track to choose when none is queued
SEED_TRACKS = 3
# The music site (musicweb/): rows of Music Assistant's discover page, items of a row, results of a search, queue items
DISCOVER_ROWS = 24
ROW_ITEMS = 20
PLAYLISTS_LIMIT = 500  # the library playlists the site lists: its favorites first, then the others by name
PLAYLIST_TRACKS = 500  # the tracks of one playlist the site lists, in the playlist's order
LOOKUP_LIMIT = 25
QUEUE_LIMIT = 200
# The transport commands of the site, and the queue's repeat modes
TRANSPORT = {"play": "player_queues/play", "pause": "player_queues/pause", "next": "player_queues/next", "previous": "player_queues/previous"}
REPEAT_MODES = ("off", "one", "all")
IMAGE_ID = re.compile(r"[A-Za-z0-9_.=+-]{1,512}")  # a proxy id, as the image proxy names it


class MusicError(Exception):
    """Music Assistant could not be reached, refused the token, or could not do what was asked."""


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _artists(item: dict) -> str:
    names = [_text(artist.get("name")) for artist in item.get("artists") or [] if isinstance(artist, dict)]
    return ", ".join(name for name in names if name) or "-"


def _reason(response: httpx.Response) -> str:
    """What MA says went wrong: its `details` when it sends them, else its text."""
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("details"):
        return _text(data["details"])[:300]
    return _text(response.text)[:300] or "no details"


def _items(found: Any) -> list[dict]:
    """The items of an answer: a list, or a page of them under `items`."""
    if isinstance(found, dict):
        found = found.get("items")
    return [item for item in found or [] if isinstance(item, dict)] if isinstance(found, list) else []


def _media_of(item: dict) -> dict:
    """The media of a queue item (its `media_item`), or the item itself when it carries the media."""
    media = item.get("media_item")
    return media if isinstance(media, dict) and media.get("uri") else item


def _results(found: Any, item_type: str | None, limit: int = MAX_RESULTS) -> list[dict]:
    """The items that have a uri, as {uri, type, title, artist, album}. `item_type` None: each item's own media_type."""
    results = []
    for item in _items(found):
        if not item.get("uri"):
            continue
        album = item.get("album")
        album_name = _text(album.get("name")) if isinstance(album, dict) else ""
        kind = item_type or str(item.get("media_type") or "track")
        results.append(
            {
                "uri": str(item["uri"]),
                "type": kind,
                "title": _text(item.get("name")) or "-",
                "artist": _artists(item) if kind in ("track", "album") else "-",
                "album": album_name or "-",
            }
        )
    return results[:limit]


def _lines(results: list[dict]) -> str:
    return "\n".join(
        f"{item['uri']} | {item['type']} | {item['title']} | {item['artist']} | {item['album']}" for item in results
    )


def _split_uri(uri: str) -> tuple[str, str, str]:
    """The provider, media type and item id of a uri such as library://track/1."""
    provider, separator, rest = uri.partition("://")
    kind, slash, item_id = rest.rpartition("/")
    if not separator or not slash or not provider or not kind or not item_id:
        raise ValueError("That is not a music uri: take it from a music_search result.")
    return provider, kind, item_id


def _row_name(row: dict) -> str:
    return _text(row.get("name") or row.get("translation_key") or row.get("item_id")) or "-"


def _limit(value: Any) -> int:
    """A number of items, kept between 1 and MAX_RESULTS."""
    return min(max(int(value), 1), MAX_RESULTS)


def _row_title(row: dict) -> str:
    """The name of a discover row; a row Music Assistant names only by a key reads as that key, spaced."""
    name = _text(row.get("name")) or _text(row.get("translation_key")).replace("_", " ")
    return name[:1].upper() + name[1:] if name else "-"


# The addresses of the pictures that Music Assistant gives without a proxy id (the items of other providers): they are
# fetched by the server, and only these addresses ever are, so the server is not a fetcher for anything else.
REMOTE_PICTURES: OrderedDict[str, str] = OrderedDict()
REMOTE_LIMIT = 5000
PICTURE_BYTES = 8 * 1024 * 1024


def _picture(image: Any) -> dict | None:
    """A picture as Music Assistant gives it, reduced to {id}: the proxy id its image proxy serves, or, for a picture
    with an address on the web and no proxy id, an id the server keeps that address under (None when it has neither)."""
    if not isinstance(image, dict):
        return None
    if image.get("proxy_id"):
        return {"id": str(image["proxy_id"])}
    path = str(image.get("path") or "")
    if path.startswith(("https://", "http://")):
        picture = "r" + hashlib.sha256(path.encode()).hexdigest()[:40]
        REMOTE_PICTURES[picture] = path
        REMOTE_PICTURES.move_to_end(picture)
        while len(REMOTE_PICTURES) > REMOTE_LIMIT:
            REMOTE_PICTURES.popitem(last=False)
        return {"id": picture}
    return None


def _image(item: dict) -> dict | None:
    """The picture of an item: its own, else its album's, else one of its metadata's (the thumbnail first)."""
    album = item.get("album")
    metadata = item.get("metadata")
    images = metadata.get("images") if isinstance(metadata, dict) else None
    pictures = [image for image in images or [] if isinstance(image, dict)]
    pictures.sort(key=lambda image: image.get("type") != "thumb")
    for image in (item.get("image"), album.get("image") if isinstance(album, dict) else None, *pictures):
        if picture := _picture(image):
            return picture
    return None


def _whole(value: Any, low: int, high: int, name: str) -> int:
    """A whole number between `low` and `high`, or a ValueError naming what it is for."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number.") from None
    if not low <= number <= high:
        raise ValueError(f"{name} must be between {low} and {high}.")
    return number


def _card(item: dict, image: Any = None) -> dict | None:
    """An item as the site lists it: {uri, type, title, subtitle, image}. None for what has no uri or id to play."""
    kind = str(item.get("media_type") or "")
    uri = _text(item.get("uri"))
    if not uri and kind and item.get("item_id") is not None and item.get("provider"):
        uri = f"{item['provider']}://{kind}/{item['item_id']}"  # the form Music Assistant itself gives a uri
    if not uri:
        return None
    if kind in ("track", "album"):
        subtitle = _artists(item)
    elif kind == "playlist":
        subtitle = _text(item.get("owner")) or "Playlist"
    else:
        subtitle = kind.replace("_", " ").capitalize() or "-"
    return {
        "uri": uri,
        "type": kind or "track",
        "title": _text(item.get("name")) or "-",
        "subtitle": "" if subtitle == "-" else subtitle,
        "image": _picture(image) or _image(item),
    }


class MusicAssistant:
    def __init__(
        self, url: str, player: str = "", token: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ):
        self._url = url.rstrip("/")
        self._player = player
        self._token = token
        self._http = SharedClient(timeout=TIMEOUT, transport=transport)  # transport: tests replace the network

    def for_player(self, player: str, token: str | None) -> MusicAssistant:
        """The same server, for one person's player and token. It shares this client's connections, so closing
        the client closes them for all of them."""
        account = MusicAssistant(self._url, player, token)
        account._http = self._http
        return account

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _command(self, command: str, **args: Any) -> Any:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            response = await self._http.get().post(
                f"{self._url}/api", json={"command": command, "args": args}, headers=headers
            )
        except httpx.HTTPError as error:
            raise MusicError(f"Music Assistant could not be reached ({type(error).__name__}).") from None
        if response.status_code in (401, 403):
            raise MusicError("Music Assistant refused your token: save it again on the Music page.")
        if response.status_code >= 400:
            raise MusicError(f"Music Assistant answered HTTP {response.status_code}: {_reason(response)}")
        try:
            return response.json()
        except ValueError:
            raise MusicError("Music Assistant sent something that is not JSON.") from None

    async def _player_state(self) -> dict:
        """The player, as MA sees it now; an error when it is not there or offline."""
        player = await self._command("players/get", player_id=self._player)
        if not isinstance(player, dict) or not player:
            raise MusicError(f"Music Assistant has no player {self._player!r}: choose another on the Music page.")
        if not player.get("available"):
            raise MusicError(f"Your player ({_text(player.get('name')) or self._player}) is offline.")
        return player

    async def players(self) -> list[dict]:
        """The players Music Assistant knows, as {id, name, available}."""
        found = await self._command("players/all")
        players = []
        for player in found if isinstance(found, list) else []:
            if isinstance(player, dict) and player.get("player_id"):
                name = _text(player.get("display_name") or player.get("name")) or str(player["player_id"])
                players.append({"id": str(player["player_id"]), "name": name, "available": bool(player.get("available"))})
        return players

    async def find(self, query: str, media_type: str = "") -> list[dict]:
        """The results of a search: {uri, type, title, artist, album}, as many as MA found (at most MAX_RESULTS)."""
        query = _text(query)
        if not query:
            raise ValueError("The query is empty.")
        kind = _text(media_type).lower()
        if kind and kind not in MEDIA_TYPES:
            raise ValueError(f"media_type must be one of: {', '.join(MEDIA_TYPES)}.")
        args: dict[str, Any] = {"search_query": query, "limit": SEARCH_LIMIT}
        if kind:
            args["media_types"] = [kind]
        data = await self._command("music/search", **args)
        if not isinstance(data, dict):
            data = {}
        results = []
        for key, item_type in RESULT_LISTS:
            results.extend(_results(data.get(key), item_type))
        return results[:MAX_RESULTS]

    async def search(self, query: str, media_type: str = "") -> str:
        """The results, one per line: uri | type | title | artist | album."""
        results = await self.find(query, media_type)
        if not results:
            return f"No result for {_text(query)!r}."
        return _lines(results)

    async def similar(self, uri: str, limit: int = SEARCH_LIMIT) -> str:
        """Tracks that sound like the track of `uri` (Music Assistant looks on the other providers too)."""
        provider, kind, item_id = _split_uri(_uri(uri))
        if kind != "track":
            raise ValueError("Similar music is found from a track: give the uri of a track.")
        found = await self._command(
            "music/tracks/similar_tracks",
            item_id=item_id,
            provider_instance_id_or_domain=provider,
            limit=_limit(limit),
            allow_lookup=True,
        )
        results = _results(found, "track")
        if not results:
            return f"No similar track to {uri}."
        return _lines(results)

    async def genre(self, name: str, limit: int = SEARCH_LIMIT) -> str:
        """Tracks of a genre of the library, in a random order. Only genres tagged in the library are known."""
        wanted = _text(name)
        if not wanted:
            raise ValueError("The genre is empty.")
        found = await self._command("music/genres/library_items", search=wanted, limit=SEARCH_LIMIT)
        genres = [item for item in _items(found) if item.get("item_id") is not None]
        if not genres:
            return f"No genre matching {wanted!r} in the library."
        best = next((item for item in genres if _text(item.get("name")).lower() == wanted.lower()), genres[0])
        found = await self._command(
            "music/genres/tracks", item_id=best["item_id"], limit=_limit(limit), order_by="random"
        )
        title = _text(best.get("name")) or wanted
        results = _results(found, "track")
        if not results:
            return f"The genre {title} has no tracks in the library."
        return f"Tracks of the genre {title}:\n{_lines(results)}"

    async def recommend(self, row: str = "", limit: int = SEARCH_LIMIT) -> str:
        """The items of one of Music Assistant's recommendation rows (the first one when `row` is not given)."""
        rows = [item for item in _items(await self._command("music/recommendations")) if item.get("item_id")]
        if not rows:
            return "Music Assistant has no recommendations."
        wanted = _text(row).lower()
        if wanted:
            chosen = next((item for item in rows if wanted in _row_name(item).lower()), None)
            if chosen is None:
                names = ", ".join(_row_name(item) for item in rows[:MAX_RESULTS])
                return f"No recommendation row matching {_text(row)!r}. The rows are: {names}."
        else:
            chosen = rows[0]
        found = await self._command(
            "music/recommendations/items", provider=chosen.get("provider") or "", item_id=chosen["item_id"]
        )
        results = _results(found, None, _limit(limit))
        if not results:
            return f"The row {_row_name(chosen)} has no items."
        return f"Recommended in {_row_name(chosen)}:\n{_lines(results)}"

    async def play(self, uri: str) -> str:
        uri = _uri(uri)
        await self._player_state()
        await self._command("player_queues/play_media", queue_id=self._player, media=uri, option="replace")
        await self._autoplay()
        return f"Playing {uri} on your player, replacing its queue."

    async def queue(self, uri: str, position: str) -> str:
        uri = _uri(uri)
        where = _text(position).lower()
        if where not in POSITIONS:
            raise ValueError('position must be "next" (after the current track) or "end" (end of the queue).')
        await self._player_state()
        await self._command("player_queues/play_media", queue_id=self._player, media=uri, option=POSITIONS[where])
        place = "after the current track" if where == "next" else "at the end of the queue"
        return f"Queued {uri} {place} on your player."

    async def stop(self) -> str:
        await self._player_state()
        await self._command("player_queues/stop", queue_id=self._player)
        return "Stopped your player."

    async def _autoplay(self) -> None:
        """Music Assistant keeps the queue going with its own picks when it runs out. A refusal is only logged: the
        music itself does not depend on it."""
        try:
            await self._command("player_queues/autoplay", queue_id=self._player, autoplay_enabled=True)
        except MusicError as error:
            log.warning("Music Assistant would not turn autoplay on: %s", error)

    async def next(self) -> str:
        """Skip to the next track. When none is queued after the current one, a track similar to the last few played
        is queued after it and played: the same choice Clara makes, instead of Music Assistant's."""
        await self._player_state()
        await self._autoplay()
        queue = await self._command("player_queues/get", queue_id=self._player)
        queue = queue if isinstance(queue, dict) else {}
        queued = _items(await self._command("player_queues/items", queue_id=self._player))
        current = queue.get("current_index")
        if not isinstance(current, int) or not queued:
            return "Nothing is queued on your player."
        if current + 1 < len(queued):
            await self._command("player_queues/next", queue_id=self._player)
            return "Skipped to the next track."
        uris = {str(_media_of(item).get("uri")) for item in queued}
        choice, seed = await self._similar_to_recent(queued[max(0, current - SEED_TRACKS + 1) : current + 1], uris)
        if choice is None:
            return "No next track is queued, and nothing similar to the last tracks was found."
        await self._command("player_queues/play_media", queue_id=self._player, media=choice["uri"], option="next")
        await self._command("player_queues/next", queue_id=self._player)
        by = f" by {choice['artist']}" if choice["artist"] != "-" else ""
        return f"No next track was queued, so I chose {choice['title']}{by}, similar to {seed}."

    async def _similar_to_recent(self, recent: list[dict], queued: set[str]) -> tuple[dict | None, str]:
        """The first track similar to the most recent of `recent` (newest first) that is not queued yet, and its seed."""
        for item in reversed(recent):
            media = _media_of(item)
            try:
                provider, kind, item_id = _split_uri(str(media.get("uri") or ""))
            except ValueError:
                continue
            if kind != "track":
                continue
            found = await self._command(
                "music/tracks/similar_tracks",
                item_id=item_id,
                provider_instance_id_or_domain=provider,
                limit=SEARCH_LIMIT,
                allow_lookup=True,
            )
            for choice in _results(found, "track"):
                if choice["uri"] not in queued:
                    return choice, _text(media.get("name")) or choice["uri"]
        return None, ""

    # ---- the music site (musicweb/): the discover page, the search, the queue and the controls -------------------

    async def discover_rows(self) -> list[dict]:
        """The rows of Music Assistant's discover page, as its web page fetches them: {provider, item_id, name}. The
        items of a row come later (discover_items), so that each row loads on its own."""
        rows = []
        for row in _items(await self._command("music/recommendations")):
            if row.get("item_id") is None or not row.get("provider"):
                continue
            rows.append({"provider": str(row["provider"]), "item_id": str(row["item_id"]), "name": _row_title(row)})
        return rows[:DISCOVER_ROWS]

    async def discover_items(self, provider: str, item_id: str) -> list[dict]:
        """The items of one discover row, as site cards (see _card)."""
        found = await self._command("music/recommendations/items", provider=provider, item_id=item_id)
        items = _items(found)[:ROW_ITEMS]
        cards = [_card(item) for item in items]
        await self._add_pictures(items, cards)
        return [card for card in cards if card][:ROW_ITEMS]

    async def playlists(self) -> list[dict]:
        """The playlists of the library, as site cards with `favorite` (the mark Music Assistant gives them): the
        favorites first, each group in name order."""
        found = await self._command("music/playlists/library_items", order_by="sort_name", limit=PLAYLISTS_LIMIT)
        cards = []
        for item in _items(found):
            card = _card(item)
            if card:
                card["favorite"] = bool(item.get("favorite"))
                cards.append(card)
        return sorted(cards, key=lambda card: not card["favorite"])

    async def playlist(self, uri: str) -> dict:
        """A playlist of the library, opened: its card, and its tracks as site cards with their length, in the order of
        the playlist (at most PLAYLIST_TRACKS of them)."""
        provider, kind, item_id = _split_uri(uri)
        if kind != "playlist":
            raise ValueError("That is not a playlist: take its uri from the Playlists page.")
        found, tracks = await asyncio.gather(
            self._command("music/playlists/get", item_id=item_id, provider_instance_id_or_domain=provider),
            self._command("music/playlists/playlist_tracks", item_id=item_id, provider_instance_id_or_domain=provider),
        )
        card = _card({**found, "media_type": "playlist"}) if isinstance(found, dict) else None
        if card is None:
            raise ValueError("Music Assistant has no such playlist.")
        card["favorite"] = bool(found.get("favorite"))
        items = _items(tracks)[:PLAYLIST_TRACKS]
        cards = [_card(item) for item in items]
        await self._add_pictures(items, cards)
        rows = []
        for item, track in zip(items, cards):
            if track:
                duration = item.get("duration")
                track["duration"] = int(duration) if isinstance(duration, (int, float)) and duration > 0 else None
                rows.append(track)
        return {"playlist": card, "tracks": rows}

    async def _add_pictures(self, items: list[dict], cards: list[dict | None]) -> None:
        """Music Assistant's own library rows come without pictures (its copy of a Deezer album or artist has none, and
        asking for the provider's item gives that copy back). Search the providers for the same name instead, and take
        the picture of the match (a track: the one of its album)."""
        wanted: dict[tuple[str, str], list[tuple[dict, str]]] = {}
        for item, card in zip(items, cards):
            if not card or card["image"] or card["type"] not in ("track", "album", "artist"):
                continue
            album = item.get("album")
            if card["type"] == "track":
                kind, name = "album", _text(album.get("name")) if isinstance(album, dict) else ""
            else:
                kind, name = card["type"], card["title"]
            by = _artists(item) if kind == "album" else ""
            if name and name != "-":
                wanted.setdefault((kind, name), []).append((card, "" if by == "-" else by))

        async def find(kind: str, name: str, by: str) -> dict | None:
            try:
                data = await self._command("music/search", search_query=name, media_types=[kind], limit=SEARCH_LIMIT)
            except MusicError:
                return None
            found = [hit for hit in _items(data.get(kind + "s") if isinstance(data, dict) else None)
                     if _text(hit.get("name")).casefold() == name.casefold() and _image(hit)]
            found.sort(key=lambda hit: by.casefold() not in _artists(hit).casefold())  # the same artist first
            return _image(found[0]) if found else None

        async def fetch(key: tuple[str, str], cards_by: list[tuple[dict, str]]) -> None:
            for card, by in cards_by:
                if key not in found_for:
                    found_for[key] = await find(key[0], key[1], by)
                card["image"] = found_for[key]

        found_for: dict[tuple[str, str], dict | None] = {}
        await asyncio.gather(*(fetch(key, cards_by) for key, cards_by in wanted.items()))

    async def lookup(self, query: str, media_type: str = "") -> dict:
        """The library search of the site: the results grouped by kind, as {query, groups: [{type, items}]}."""
        query = _text(query)
        if not query:
            raise ValueError("The query is empty.")
        kind = _text(media_type).lower()
        if kind and kind not in MEDIA_TYPES:
            raise ValueError(f"media_type must be one of: {', '.join(MEDIA_TYPES)}.")
        args: dict[str, Any] = {"search_query": query, "limit": LOOKUP_LIMIT}
        if kind:
            args["media_types"] = [kind]
        data = await self._command("music/search", **args)
        data = data if isinstance(data, dict) else {}
        groups = []
        for key, item_type in RESULT_LISTS:
            cards = [card for card in (_card(item) for item in _items(data.get(key))) if card]
            if cards:
                groups.append({"type": item_type, "items": cards[:LOOKUP_LIMIT]})
        return {"query": query, "groups": groups}

    async def image(self, image_id: str) -> tuple[bytes, str]:
        """A picture of Music Assistant's library, from its image proxy (`/imageproxy/{id}`), with the person's token;
        or a picture of another provider, from the address Music Assistant gave it (fetched without the token)."""
        image_id = _text(image_id)
        if not IMAGE_ID.fullmatch(image_id):
            raise ValueError("That is not a picture id: take it from a music result.")
        if image_id.startswith("r"):
            address = REMOTE_PICTURES.get(image_id)
            if address is None:
                raise ValueError("That picture is not known any more: search or reload to get it again.")
            return await self._fetch_remote(address)
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            response = await self._http.get().get(f"{self._url}/imageproxy/{image_id}", headers=headers)
        except httpx.HTTPError as error:
            raise MusicError(f"Music Assistant could not be reached ({type(error).__name__}).") from None
        kind = response.headers.get("content-type", "")
        if response.status_code >= 400 or not kind.startswith("image/"):
            raise MusicError(f"Music Assistant has no picture there (HTTP {response.status_code}).")
        return response.content, kind

    async def _fetch_remote(self, address: str) -> tuple[bytes, str]:
        try:
            response = await self._http.get().get(address, follow_redirects=True)
        except httpx.HTTPError as error:
            raise MusicError(f"The picture could not be fetched ({type(error).__name__}).") from None
        kind = response.headers.get("content-type", "")
        if response.status_code >= 400 or not kind.startswith("image/"):
            raise MusicError(f"The picture is not there (HTTP {response.status_code}).")
        if len(response.content) > PICTURE_BYTES:
            raise MusicError("The picture is too big.")
        return response.content, kind

    def sendspin_url(self) -> str:
        """Music Assistant's Sendspin server: the same address, its websocket, at /sendspin."""
        scheme = "wss" if self._url.startswith("https") else "ws"
        return re.sub(r"^https?", scheme, self._url, count=1) + "/sendspin"

    async def open_sendspin(self, client_id: str) -> Any:
        """A connection to Music Assistant's Sendspin server for the browser player `client_id`. Music Assistant wants
        the first message to be the person's token (and the player's id), and answers it before the Sendspin
        protocol starts: that answer is read here, so the caller gets the protocol itself, end to end encrypted."""
        if not self._token:
            raise MusicError("Music Assistant needs your token for the browser player: save it on the music site.")
        try:
            upstream = await websockets.connect(self.sendspin_url(), open_timeout=TIMEOUT, max_size=None, ping_interval=20)
        except (OSError, TimeoutError, websockets.WebSocketException) as error:
            raise MusicError(f"Music Assistant's Sendspin server could not be reached ({type(error).__name__}).") from None
        try:
            await upstream.send(json.dumps({"type": "auth", "token": self._token, "client_id": client_id}))
            answer = await asyncio.wait_for(upstream.recv(), TIMEOUT)
        except websockets.ConnectionClosed as error:
            await upstream.close()
            raise MusicError(f"Music Assistant refused the browser player ({error.rcvd.code if error.rcvd else 'closed'}).") from None
        except TimeoutError:
            await upstream.close()
            raise MusicError("Music Assistant did not answer the browser player in time.") from None
        if isinstance(answer, str) and answer.lstrip().startswith("{") and '"error"' in answer:
            await upstream.close()
            raise MusicError("Music Assistant refused the browser player.")
        return upstream

    async def pair_web_player(self, pairing_token: str) -> str:
        """Pair the browser player that minted `pairing_token` (its identity and pairing secret, as Music Assistant's
        own web page does): from then on Music Assistant accepts that browser as one of the person's players."""
        token = _text(pairing_token)
        if not token.upper().startswith("SP:"):
            raise ValueError("That is not a browser pairing token.")
        await self._command("sendspin/pair_web_player", pairing_token=token)
        return "The browser player is paired with Music Assistant."

    async def now(self) -> dict:
        """What the player plays and how far: {name, state, track (a card or None), position, duration, shuffle,
        repeat, volume, index}. The position is moved on from Music Assistant's last report while it plays."""
        player = await self._player_state()
        queue = await self._command("player_queues/get", queue_id=self._player)
        queue = queue if isinstance(queue, dict) else {}
        current = queue.get("current_item") if isinstance(queue.get("current_item"), dict) else {}
        track = _card(_media_of(current), current.get("image")) if current else None
        state = STATES.get(str(queue.get("state") or player.get("playback_state")), "unknown")
        duration = int(current.get("duration") or _media_of(current).get("duration") or 0) if current else 0
        position = float(queue.get("elapsed_time") or 0)
        reported = float(queue.get("elapsed_time_last_updated") or 0)
        if state == "playing" and reported:
            position += max(0.0, time.time() - reported)
        if duration:
            position = min(position, duration)
        repeat = str(queue.get("repeat_mode") or "off")
        return {
            "name": _text(player.get("name")) or "Your player",
            "state": state,
            "track": track,
            "position": round(position, 1),
            "duration": duration,
            "shuffle": bool(queue.get("shuffle_enabled")),
            "repeat": repeat if repeat in REPEAT_MODES else "off",
            "volume": int(player.get("volume_level") or 0),
            "index": queue.get("current_index") if isinstance(queue.get("current_index"), int) else None,
        }

    async def queue_items(self) -> dict:
        """The queue of the player, in Music Assistant's order: {items: [card + queue_item_id, index, duration, current]}."""
        await self._player_state()
        queue = await self._command("player_queues/get", queue_id=self._player)
        queue = queue if isinstance(queue, dict) else {}
        current = queue.get("current_index")
        items = []
        for index, item in enumerate(_items(await self._command("player_queues/items", queue_id=self._player, limit=QUEUE_LIMIT))):
            card = _card(_media_of(item), item.get("image"))
            if card is None:  # MA lists what it cannot play too: the site shows it by its name
                card = {"uri": "", "type": "track", "title": _text(item.get("name")) or "-", "subtitle": "", "image": None}
            items.append(
                {
                    **card,
                    "queue_item_id": str(item.get("queue_item_id") or ""),
                    "index": index,
                    "duration": int(item.get("duration") or 0),
                    "current": index == current,
                    "available": bool(item.get("available", True)),
                }
            )
        return {"index": current if isinstance(current, int) else None, "items": items}

    async def control(self, action: str, value: Any = None) -> None:
        """A transport or setting of the player: play, pause, next, previous, shuffle (a bool), repeat (off, one or
        all), seek (a position in seconds) or volume (0 to 100)."""
        await self._player_state()
        if action in TRANSPORT:
            await self._command(TRANSPORT[action], queue_id=self._player)
        elif action == "shuffle":
            if not isinstance(value, bool):
                raise ValueError("shuffle must be true or false.")
            await self._command("player_queues/shuffle", queue_id=self._player, shuffle_enabled=value)
        elif action == "repeat":
            if value not in REPEAT_MODES:
                raise ValueError(f"repeat must be one of: {', '.join(REPEAT_MODES)}.")
            await self._command("player_queues/repeat", queue_id=self._player, repeat_mode=value)
        elif action == "seek":
            await self._command("player_queues/seek", queue_id=self._player, position=_whole(value, 0, 86400, "seek"))
        elif action == "volume":
            await self._command("players/cmd/volume_set", player_id=self._player, volume_level=_whole(value, 0, 100, "volume"))
        else:
            raise ValueError(f"Unknown action {action!r}.")

    async def jump(self, index: int) -> None:
        await self._player_state()
        await self._command("player_queues/play_index", queue_id=self._player, index=_whole(index, 0, QUEUE_LIMIT, "index"))

    async def remove(self, index: int) -> None:
        await self._player_state()
        await self._command("player_queues/delete_item", queue_id=self._player, item_id_or_index=_whole(index, 0, QUEUE_LIMIT, "index"))

    async def move(self, queue_item_id: str, shift: int) -> None:
        """One place up (-1) or down (1) in the queue."""
        item = _text(queue_item_id)
        if not item:
            raise ValueError("The queue item is empty.")
        if shift not in (-1, 1):
            raise ValueError("shift must be -1 (up) or 1 (down).")
        await self._player_state()
        await self._command("player_queues/move_item", queue_id=self._player, queue_item_id=item, pos_shift=shift)

    async def clear(self) -> None:
        await self._player_state()
        await self._command("player_queues/clear", queue_id=self._player)

    async def status(self) -> dict:
        """The player, and what it plays: {name, state (playing, paused, stopped or unknown), title, artist, album}."""
        player = await self._player_state()
        media = player.get("current_media")
        media = media if isinstance(media, dict) else {}
        return {
            "name": _text(player.get("name")) or "Your player",
            "state": STATES.get(str(player.get("playback_state")), "unknown"),
            "title": _text(media.get("title")),
            "artist": _text(media.get("artist")),
            "album": _text(media.get("album")),
        }

    async def now_playing(self) -> str:
        current = await self.status()
        name, state = current["name"], current["state"]
        if state == "stopped":
            return f"{name} is stopped."
        if state == "unknown":
            return f"{name}'s state is unknown."
        if not current["title"]:
            return f"{name} is {state}."
        text = f"{name} is {state}: {current['title']}"
        text += f" by {current['artist']}" if current["artist"] else ""
        text += f" (album {current['album']})" if current["album"] else ""
        return text


def _uri(value: Any) -> str:
    uri = _text(value)
    if not uri:
        raise ValueError("The uri is empty: take it from a music_search result.")
    return uri
