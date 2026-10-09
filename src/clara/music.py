"""Music Assistant, through the `music_*` tools: search its library, and play, queue or stop on the PC's player.

Music Assistant (MA) answers one command per request, at its HTTP API (its commands are listed at /api-docs/commands):

    POST {MUSIC_ASSISTANT_URL}/api  {"command": "music/search", "args": {...}}  -> the command's result, as JSON

with its token as a bearer token (MA refuses every command without one). Only the player named by
MUSIC_ASSISTANT_PLAYER is ever controlled; its queue has the same id as the player.
"""

from __future__ import annotations

from typing import Any

import httpx

from .httpclient import SharedClient

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


class MusicAssistant:
    def __init__(self, url: str, player: str, token: str | None, transport: httpx.AsyncBaseTransport | None = None):
        self._url = url.rstrip("/")
        self._player = player
        self._token = token
        self._http = SharedClient(timeout=TIMEOUT, transport=transport)  # transport: tests replace the network

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
            raise MusicError("Music Assistant refused its token (MUSIC_ASSISTANT_TOKEN).")
        if response.status_code >= 400:
            raise MusicError(f"Music Assistant answered HTTP {response.status_code}: {_reason(response)}")
        try:
            return response.json()
        except ValueError:
            raise MusicError("Music Assistant sent something that is not JSON.") from None

    async def _player_state(self) -> dict:
        """The PC's player, as MA sees it now; an error when it is not there or offline."""
        player = await self._command("players/get", player_id=self._player)
        if not isinstance(player, dict) or not player:
            raise MusicError(f"Music Assistant has no player {self._player!r} (MUSIC_ASSISTANT_PLAYER).")
        if not player.get("available"):
            raise MusicError(f"The PC player ({_text(player.get('name')) or self._player}) is offline.")
        return player

    async def search(self, query: str, media_type: str = "") -> str:
        """The results, one per line: uri | type | title | artist | album."""
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
        lines = []
        for key, item_type in RESULT_LISTS:
            for item in data.get(key) or []:
                if not isinstance(item, dict) or not item.get("uri"):
                    continue
                album = item.get("album")
                album_name = _text(album.get("name")) if isinstance(album, dict) else ""
                artists = _artists(item) if item_type in ("track", "album") else "-"
                lines.append(
                    f"{item['uri']} | {item_type} | {_text(item.get('name')) or '-'} | {artists} | {album_name or '-'}"
                )
        if not lines:
            return f"No result for {query!r}."
        return "\n".join(lines[:MAX_RESULTS])

    async def play(self, uri: str) -> str:
        uri = _uri(uri)
        await self._player_state()
        await self._command("player_queues/play_media", queue_id=self._player, media=uri, option="replace")
        return f"Playing {uri} on the PC player, replacing its queue."

    async def queue(self, uri: str, position: str) -> str:
        uri = _uri(uri)
        where = _text(position).lower()
        if where not in POSITIONS:
            raise ValueError('position must be "next" (after the current track) or "end" (end of the queue).')
        await self._player_state()
        await self._command("player_queues/play_media", queue_id=self._player, media=uri, option=POSITIONS[where])
        place = "after the current track" if where == "next" else "at the end of the queue"
        return f"Queued {uri} {place} on the PC player."

    async def stop(self) -> str:
        await self._player_state()
        await self._command("player_queues/stop", queue_id=self._player)
        return "Stopped the PC player."

    async def now_playing(self) -> str:
        player = await self._player_state()
        name = _text(player.get("name")) or "The PC player"
        state = STATES.get(str(player.get("playback_state")), "unknown")
        if state == "stopped":
            return f"{name} is stopped."
        if state == "unknown":
            return f"{name}'s state is unknown."
        media = player.get("current_media")
        media = media if isinstance(media, dict) else {}
        title = _text(media.get("title"))
        if not title:
            return f"{name} is {state}."
        artist = _text(media.get("artist"))
        album = _text(media.get("album"))
        text = f"{name} is {state}: {title}"
        text += f" by {artist}" if artist else ""
        text += f" (album {album})" if album else ""
        return text


def _uri(value: Any) -> str:
    uri = _text(value)
    if not uri:
        raise ValueError("The uri is empty: take it from a music_search result.")
    return uri
