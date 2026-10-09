"""`music_next`: skip to the next queued track, or choose one similar to the last tracks played when none is queued."""

from test_music import PC, PLAYER, accounts_with, context_of, music

from clara.tools import default_toolbox


def queued(uri: str, name: str) -> dict:
    return {"queue_item_id": uri, "media_item": {"uri": uri, "name": name, "artists": [{"name": "Joni"}], "album": {"name": "Blue LP"}}}


QUEUE = [queued("library://track/1", "Blue"), queued("library://track/2", "River"), queued("library://track/3", "Carey")]
HEJIRA = {"uri": "deezer://track/9", "name": "Hejira", "artists": [{"name": "Joni"}], "album": {"name": "Hejira"}}


def player_at(index: int, **answers) -> dict:
    return {
        "players/get": PC,
        "player_queues/autoplay": None,
        "player_queues/get": {"current_index": index},
        "player_queues/items": QUEUE,
        "player_queues/next": None,
        "player_queues/play_media": None,
        **answers,
    }


async def test_next_skips_to_the_queued_track_after_the_current_one_and_chooses_nothing():
    seen = []

    text = await music(player_at(0), seen).next()

    assert text == "Skipped to the next track."
    assert [command for command, _, _ in seen] == [
        "players/get",
        "player_queues/autoplay",
        "player_queues/get",
        "player_queues/items",
        "player_queues/next",
    ]


async def test_at_the_end_of_the_queue_it_chooses_a_track_similar_to_the_last_one_played():
    seen = []

    text = await music(player_at(2, **{"music/tracks/similar_tracks": [HEJIRA]}), seen).next()

    assert text == "No next track was queued, so I chose Hejira by Joni, similar to Carey."
    similar = [args for command, args, _ in seen if command == "music/tracks/similar_tracks"]
    assert similar == [{"item_id": "3", "provider_instance_id_or_domain": "library", "limit": 5, "allow_lookup": True}]
    played = [args for command, args, _ in seen if command == "player_queues/play_media"]
    assert played == [{"queue_id": PLAYER, "media": "deezer://track/9", "option": "next"}]
    assert [command for command, _, _ in seen][-2:] == ["player_queues/play_media", "player_queues/next"]


async def test_a_track_already_in_the_queue_is_never_chosen():
    seen = []
    similar = [{"uri": "library://track/1", "name": "Blue", "artists": []}, HEJIRA]

    await music(player_at(2, **{"music/tracks/similar_tracks": similar}), seen).next()

    played = [args["media"] for command, args, _ in seen if command == "player_queues/play_media"]
    assert played == ["deezer://track/9"]


async def test_when_the_last_track_has_no_similar_one_the_last_three_are_tried_newest_first():
    seen = []
    client = music(player_at(2, **{"music/tracks/similar_tracks": []}), seen)

    text = await client.next()

    assert text == "No next track is queued, and nothing similar to the last tracks was found."
    seeds = [args["item_id"] for command, args, _ in seen if command == "music/tracks/similar_tracks"]
    assert seeds == ["3", "2", "1"]
    assert not [command for command, _, _ in seen if command == "player_queues/play_media"]


async def test_the_last_three_tracks_are_the_seeds_not_the_whole_queue():
    seen = []
    long_queue = [queued(f"library://track/{n}", f"Track {n}") for n in range(1, 8)]
    answers = player_at(6, **{"music/tracks/similar_tracks": []})  # the last of the seven
    answers["player_queues/items"] = long_queue

    await music(answers, seen).next()

    seeds = [args["item_id"] for command, args, _ in seen if command == "music/tracks/similar_tracks"]
    assert seeds == ["7", "6", "5"]


async def test_a_refused_autoplay_does_not_stop_the_skip():
    answers = player_at(0, **{"player_queues/autoplay": (500, "autoplay is not available")})

    assert await music(answers).next() == "Skipped to the next track."


async def test_an_empty_queue_is_said_so():
    assert await music(player_at(0, **{"player_queues/items": []})).next() == "Nothing is queued on your player."


async def test_the_tool_is_offered_and_runs_through_the_persons_own_token(memory):
    seen = []
    toolbox = default_toolbox(music=music(player_at(0), seen), accounts=accounts_with(memory, "erwan"))

    assert "music_next" in toolbox.names
    assert not toolbox.parallel("music_next")
    assert await toolbox.arun("music_next", context_of(memory, "erwan"), {}) == "Skipped to the next track."
    assert {auth for _, _, auth in seen} == {"Bearer token"}


async def test_the_tool_needs_a_chosen_player(memory):
    toolbox = default_toolbox(music=music({}), accounts=accounts_with(memory))

    assert await toolbox.arun("music_next", context_of(memory, "anna"), {}) == (
        "Error: Music is not set up for you yet: choose your player and token on the Music page."
    )
