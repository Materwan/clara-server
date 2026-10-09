"""The discovery tools: tracks like a track, a genre of the library, and Music Assistant's recommendation rows."""

import pytest
from test_music import accounts_with, context_of, music

from clara.tools import default_toolbox

TRACK = {"uri": "library://track/1", "name": "Blue", "artists": [{"name": "Joni"}], "album": {"name": "Blue LP"}}
SIMILAR = {"uri": "deezer://track/9", "name": "River", "artists": [{"name": "Joni"}], "album": {"name": "Hejira"}}


async def test_similar_asks_for_the_tracks_of_the_same_item_and_provider_and_lists_them(memory):
    seen = []
    client = music({"music/tracks/similar_tracks": [SIMILAR]}, seen)

    text = await client.similar("library://track/1")

    assert text == "deezer://track/9 | track | River | Joni | Hejira"
    assert seen == [
        (
            "music/tracks/similar_tracks",
            {"item_id": "1", "provider_instance_id_or_domain": "library", "limit": 5, "allow_lookup": True},
            "Bearer token",
        )
    ]


async def test_similar_is_found_from_a_track_only_and_a_malformed_uri_is_refused():
    seen = []
    client = music({"music/tracks/similar_tracks": []}, seen)

    with pytest.raises(ValueError, match="uri of a track"):
        await client.similar("library://album/2")
    with pytest.raises(ValueError, match="not a music uri"):
        await client.similar("blue")
    assert seen == []


async def test_similar_says_so_when_nothing_is_like_it():
    assert await music({"music/tracks/similar_tracks": []}).similar("library://track/1") == (
        "No similar track to library://track/1."
    )


async def test_genre_takes_the_library_genre_with_the_same_name_and_lists_its_tracks_at_random():
    seen = []
    answers = {
        "music/genres/library_items": [{"item_id": 8, "name": "Bossa"}, {"item_id": 7, "name": "Bossa Nova"}],
        "music/genres/tracks": [TRACK],
    }

    text = await music(answers, seen).genre("bossa nova", 3)

    assert text == "Tracks of the genre Bossa Nova:\nlibrary://track/1 | track | Blue | Joni | Blue LP"
    assert seen[0][1] == {"search": "bossa nova", "limit": 5}
    assert seen[1][1] == {"item_id": 7, "limit": 3, "order_by": "random"}


async def test_genre_says_so_when_the_library_has_no_such_genre():
    seen = []

    assert await music({"music/genres/library_items": []}, seen).genre("polka") == (
        "No genre matching 'polka' in the library."
    )
    assert [command for command, _, _ in seen] == ["music/genres/library_items"]


async def test_recommend_without_a_row_takes_the_first_one_and_lists_its_items():
    seen = []
    rows = [
        {"provider": "library", "item_id": "recent", "name": "Recently played"},
        {"provider": "deezer", "item_id": "discover", "name": "Discover"},
    ]
    answers = {"music/recommendations": rows, "music/recommendations/items": [TRACK]}

    text = await music(answers, seen).recommend()

    assert text == "Recommended in Recently played:\nlibrary://track/1 | track | Blue | Joni | Blue LP"
    assert seen[1][1] == {"provider": "library", "item_id": "recent"}


async def test_recommend_picks_the_row_named_and_says_which_rows_exist_when_none_matches():
    seen = []
    rows = [
        {"provider": "library", "item_id": "recent", "name": "Recently played"},
        {"provider": "deezer", "item_id": "discover", "name": "Discover"},
    ]
    client = music({"music/recommendations": rows, "music/recommendations/items": [SIMILAR]}, seen)

    assert (await client.recommend("discov")).startswith("Recommended in Discover:")
    assert seen[1][1] == {"provider": "deezer", "item_id": "discover"}

    assert await client.recommend("podcasts") == (
        "No recommendation row matching 'podcasts'. The rows are: Recently played, Discover."
    )


async def test_recommend_says_so_when_music_assistant_has_no_rows():
    assert await music({"music/recommendations": []}).recommend() == "Music Assistant has no recommendations."


async def test_the_discovery_tools_are_offered_read_only_and_need_a_chosen_player(memory):
    names = {"music_similar", "music_genre", "music_recommend"}
    toolbox = default_toolbox(music=music({}), accounts=accounts_with(memory, "erwan"))

    assert names <= toolbox.names
    assert all(toolbox.parallel(name) for name in names)
    assert await toolbox.arun("music_genre", context_of(memory, "anna"), {"genre": "jazz"}) == (
        "Error: Music is not set up for you yet: choose your player and token on the Music page."
    )


async def test_the_discovery_tools_run_through_the_persons_own_token(memory):
    seen = []
    toolbox = default_toolbox(
        music=music({"music/tracks/similar_tracks": [SIMILAR]}, seen), accounts=accounts_with(memory, "erwan")
    )

    result = await toolbox.arun(
        "music_similar", context_of(memory, "erwan"), {"uri": "library://track/1", "limit": 50}
    )

    assert result == "deezer://track/9 | track | River | Joni | Hejira"
    assert seen[0][1]["limit"] == 10  # the limit is kept to what a reply can hold
    assert seen[0][2] == "Bearer token"
