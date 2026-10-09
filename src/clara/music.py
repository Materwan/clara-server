"""Music Assistant, through the `music_*` tools and the Music page: search its library, and play, queue or stop on a
person's player.

Music Assistant (MA) answers one command per request, at its HTTP API (its commands are listed at /api-docs/commands):

    POST {MUSIC_ASSISTANT_URL}/api  {"command": "music/search", "args": {...}}  -> the command's result, as JSON

with a token as a bearer token (MA refuses every command without one). Each person has their own player and token
(musicaccounts.py): `for_player` makes the client that controls their player, and only that one. Its queue has the
same id as the player.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

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
