"""The music_* tools: Music Assistant's commands, the network replaced."""

import json

import httpx
import pytest
from conftest import FakeBackend, call, say

from clara.agent import Agent, ChatRequest
from clara.integrations.vault import Vault
from clara.music import MusicAssistant, MusicError
from clara.musicaccounts import MusicAccounts
from clara.prompt import SystemPrompt
from clara.settings import Settings
from clara.tools import ToolContext, default_toolbox

PLAYER = "ma_pc_player"
PC = {"player_id": PLAYER, "name": "Erwan's PC", "available": True, "playback_state": "idle", "current_media": None}


def server(answers: dict, seen: list):
    """A fake Music Assistant. `answers` maps each command to its result, or to (status, text) for an error;
    `seen` gets (command, args, authorization header) for each request."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api"
        body = json.loads(request.content)
        seen.append((body["command"], body["args"], request.headers.get("authorization")))
        answer = answers[body["command"]]
        if isinstance(answer, tuple):
            status, text = answer
            return httpx.Response(status, text=text)
        return httpx.Response(200, content=json.dumps(answer))  # None is the JSON null, as MA answers it

    return httpx.MockTransport(handler)


def music(answers: dict, seen: list | None = None) -> MusicAssistant:
    transport = server(answers, [] if seen is None else seen)
    return MusicAssistant("http://ma:8095/", PLAYER, "token", transport=transport)


def accounts_with(memory, *names: str) -> MusicAccounts:
    """Each named person (on the cli surface) has chosen the test player, with the test token."""
    accounts = MusicAccounts(memory, Vault("test-secret"))
    for name in names:
        accounts.save(memory.resolve("cli", name, name).id, PLAYER, "token")
    return accounts


def context_of(memory, name: str) -> ToolContext:
    return ToolContext(person=memory.resolve("cli", name, name), memory=memory)


async def test_search_answers_one_line_per_item_with_its_uri_title_artist_and_album():
    seen = []
    found = {
        "tracks": [
            {"uri": "library://track/1", "name": "Blue", "artists": [{"name": "Joni"}], "album": {"name": "Blue LP"}}
        ],
        "albums": [{"uri": "library://album/2", "name": "Blue LP", "artists": [{"name": "Joni"}]}],
        "artists": [],
        "playlists": [{"uri": "spotify://playlist/3", "name": "Sunday"}],
    }

    text = await music({"music/search": found}, seen).search("blue")

    assert text.splitlines() == [
        "library://track/1 | track | Blue | Joni | Blue LP",
        "library://album/2 | album | Blue LP | Joni | -",
        "spotify://playlist/3 | playlist | Sunday | - | -",
    ]
    assert seen == [("music/search", {"search_query": "blue", "limit": 5}, "Bearer token")]


async def test_a_media_type_narrows_the_search_and_an_unknown_one_is_refused():
    seen = []
    client = music({"music/search": {"albums": []}}, seen)

    assert await client.search("blue", "Album") == "No result for 'blue'."
    assert seen[0][1] == {"search_query": "blue", "limit": 5, "media_types": ["album"]}
    with pytest.raises(ValueError, match="media_type must be one of"):
        await client.search("blue", "song")


async def test_play_replaces_the_queue_of_the_player_once_it_is_checked_to_be_there():
    seen = []
    client = music({"players/get": PC, "player_queues/play_media": None, "player_queues/autoplay": None}, seen)

    text = await client.play("library://track/1")

    assert text == "Playing library://track/1 on your player, replacing its queue."
    assert [command for command, _, _ in seen] == [
        "players/get",
        "player_queues/play_media",
        "player_queues/autoplay",
    ]
    assert seen[2][1] == {"queue_id": PLAYER, "autoplay_enabled": True}
    assert seen[0][1] == {"player_id": PLAYER}
    assert seen[1][1] == {"queue_id": PLAYER, "media": "library://track/1", "option": "replace"}


@pytest.mark.parametrize("position, option", [("next", "next"), ("end", "add")])
async def test_queue_puts_the_item_after_the_current_track_or_at_the_end(position, option):
    seen = []
    client = music({"players/get": PC, "player_queues/play_media": None}, seen)

    await client.queue("library://track/1", position)

    assert seen[1][1] == {"queue_id": PLAYER, "media": "library://track/1", "option": option}


async def test_queue_refuses_any_other_position_before_asking_music_assistant():
    seen = []
    with pytest.raises(ValueError, match='"next"'):
        await music({}, seen).queue("library://track/1", "middle")
    assert seen == []


async def test_stop_stops_the_queue_of_the_player():
    seen = []
    client = music({"players/get": PC, "player_queues/stop": None}, seen)

    assert await client.stop() == "Stopped your player."
    assert seen[1][:2] == ("player_queues/stop", {"queue_id": PLAYER})


@pytest.mark.parametrize(
    "state, expected",
    [
        ("playing", "Erwan's PC is playing: Blue by Joni (album Blue LP)"),
        ("paused", "Erwan's PC is paused: Blue by Joni (album Blue LP)"),
        ("idle", "Erwan's PC is stopped."),
    ],
)
async def test_now_playing_says_the_track_and_whether_it_plays_pauses_or_stops(state, expected):
    media = {"title": "Blue", "artist": "Joni", "album": "Blue LP", "uri": "library://track/1"}
    player = {**PC, "playback_state": state, "current_media": media}

    assert await music({"players/get": player}).now_playing() == expected


async def test_an_offline_player_or_an_unknown_one_is_reported_as_such():
    client = music({"players/get": {**PC, "available": False}})
    with pytest.raises(MusicError, match="is offline"):
        await client.now_playing()

    with pytest.raises(MusicError, match="has no player"):
        await music({"players/get": None}).now_playing()


async def test_an_unreachable_music_assistant_is_said_so_to_the_model(memory):
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = MusicAssistant("http://ma:8095", transport=httpx.MockTransport(unreachable))
    toolbox = default_toolbox(music=client, accounts=accounts_with(memory, "erwan"))

    assert await toolbox.arun("music_now_playing", context_of(memory, "erwan"), {}) == (
        "Error: Music Assistant could not be reached (ConnectError)."
    )


async def test_a_refused_token_is_said_so_to_the_model(memory):
    toolbox = default_toolbox(
        music=music({"players/get": (401, "Authentication required")}), accounts=accounts_with(memory, "erwan")
    )

    assert await toolbox.arun("music_stop", context_of(memory, "erwan"), {}) == (
        "Error: Music Assistant refused your token: save it again on the Music page."
    )


async def test_an_offline_player_reaches_the_model_as_text_not_a_traceback(memory):
    toolbox = default_toolbox(
        music=music({"players/get": {**PC, "available": False}}), accounts=accounts_with(memory, "erwan")
    )

    assert await toolbox.arun("music_play", context_of(memory, "erwan"), {"uri": "library://track/1"}) == (
        "Error: Your player (Erwan's PC) is offline."
    )


async def test_a_person_who_chose_no_player_is_told_so_and_nothing_is_sent(memory):
    seen = []
    toolbox = default_toolbox(music=music({"players/get": PC}, seen), accounts=accounts_with(memory))

    assert await toolbox.arun("music_stop", context_of(memory, "erwan"), {}) == (
        "Error: Music is not set up for you yet: choose your player and token on the Music page."
    )
    assert seen == []


async def test_each_person_controls_only_their_own_player_with_their_own_token(memory):
    seen = []
    accounts = MusicAccounts(memory, Vault("test-secret"))
    accounts.save(memory.resolve("cli", "erwan", "Erwan").id, "erwan_pc", "erwan-token")
    accounts.save(memory.resolve("cli", "anna", "Anna").id, "anna_pc", "anna-token")
    toolbox = default_toolbox(music=music({"players/get": PC, "player_queues/stop": None}, seen), accounts=accounts)

    await toolbox.arun("music_stop", context_of(memory, "erwan"), {})
    await toolbox.arun("music_stop", context_of(memory, "anna"), {})

    stops = [(args, auth) for command, args, auth in seen if command == "player_queues/stop"]
    assert stops == [({"queue_id": "erwan_pc"}, "Bearer erwan-token"), ({"queue_id": "anna_pc"}, "Bearer anna-token")]


async def test_the_model_can_call_a_music_tool(memory, tmp_path):
    backend = FakeBackend(call("music_now_playing"), say("Nothing is playing."))
    toolbox = default_toolbox(music=music({"players/get": PC}), accounts=accounts_with(memory, "erwan"))
    agent = Agent(memory, backend, toolbox, SystemPrompt(tmp_path / "none.md"))

    events = [
        e async for e in agent.turn(ChatRequest(surface="cli", user_id="erwan", user_name="E", message="playing?"))
    ]

    results = [e["result"] for e in events if e["type"] == "tool"]
    assert results == ["Erwan's PC is stopped."]


def test_the_music_tools_are_offered_only_with_a_music_assistant_and_the_accounts(memory):
    names = {"music_search", "music_play", "music_queue", "music_stop", "music_now_playing"}

    assert names <= default_toolbox(music=music({}), accounts=accounts_with(memory)).names
    assert not names & default_toolbox(music=music({})).names
    assert not names & default_toolbox(accounts=accounts_with(memory)).names
    assert not names & default_toolbox().names


def test_only_the_reads_may_run_in_parallel(memory):
    toolbox = default_toolbox(music=music({}), accounts=accounts_with(memory))

    assert toolbox.parallel("music_search") and toolbox.parallel("music_now_playing")
    assert not any(toolbox.parallel(name) for name in ("music_play", "music_queue", "music_stop"))


def test_the_music_server_is_read_from_the_environment_and_no_player_or_token_is_global():
    settings = Settings.from_env(
        {
            "CLARA_TOKENS": "terminal:" + "t" * 40,
            "MUSIC_ASSISTANT_URL": "http://100.104.72.57:8095/",
            "MUSIC_ASSISTANT_PLAYER": "pc",
            "MUSIC_ASSISTANT_TOKEN": "ma-secret",
        }
    )

    assert settings.music_assistant_url == "http://100.104.72.57:8095"
    assert not hasattr(settings, "music_assistant_player") and not hasattr(settings, "music_assistant_token")
    assert "ma-secret" not in repr(settings)
