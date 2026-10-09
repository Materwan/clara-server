"""The Music page's routes: a person chooses their player and their token, and only ever controls their own player."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_music import PC, PLAYER, music

from clara.auth import Caller, authenticate_user
from clara.integrations.vault import Vault
from clara.musicaccounts import MusicAccounts
from clara.musicapi import install as install_music

PLAYERS = [{"player_id": PLAYER, "display_name": "Erwan's PC", "available": True}]
SECRET = "ma-secret-token-1234"


def app_for(memory, client, name: str) -> tuple[TestClient, int]:
    """The routes, as `name` is signed in; the Music Assistant is `client` (None: not configured)."""
    app = FastAPI()
    install_music(app)
    app.state.music = client
    app.state.music_accounts = MusicAccounts(memory, Vault("test-secret"))
    person_id = memory.resolve("web", name, name).id
    caller = Caller(f"{name}@web")
    caller.user = SimpleNamespace(name=name, person_id=person_id)
    app.dependency_overrides[authenticate_user] = lambda: caller
    return TestClient(app), person_id


def test_choosing_a_player_checks_the_token_and_the_player_then_keeps_both(memory):
    seen = []
    client, _ = app_for(memory, music({"players/all": PLAYERS}, seen), "erwan")

    response = client.put("/v1/me/music", json={"player": PLAYER, "token": f"  {SECRET}  "})

    assert response.status_code == 200 and response.json()["player"] == PLAYER
    assert SECRET not in response.text
    assert seen == [("players/all", {}, "Bearer " + SECRET)]
    assert client.get("/v1/me/music").json()["player"] == PLAYER


def test_only_the_player_changes_when_the_saved_token_is_used(memory):
    players = [{"player_id": name, "display_name": name, "available": True} for name in ("pc_one", "pc_two")]
    seen = []
    client, _ = app_for(memory, music({"players/all": players}, seen), "erwan")
    client.put("/v1/me/music", json={"player": "pc_one", "token": SECRET})
    seen.clear()

    response = client.put("/v1/me/music", json={"player": "pc_two"})

    assert response.status_code == 200 and response.json()["player"] == "pc_two"
    assert seen == [("players/all", {}, "Bearer " + SECRET)]


def test_changing_the_player_without_a_saved_token_is_refused(memory):
    client, _ = app_for(memory, music({"players/all": PLAYERS}), "erwan")

    assert client.put("/v1/me/music", json={"player": PLAYER}).status_code == 409


def test_the_token_is_kept_encrypted_in_the_database(memory):
    client, person_id = app_for(memory, music({"players/all": PLAYERS}), "erwan")

    client.put("/v1/me/music", json={"player": PLAYER, "token": SECRET})

    stored = memory.database.execute("SELECT player, token FROM user_music WHERE person_id = ?", (person_id,)).fetchone()
    assert stored["player"] == PLAYER and SECRET not in stored["token"]


def test_a_player_that_is_not_there_is_refused(memory):
    client, _ = app_for(memory, music({"players/all": PLAYERS}), "erwan")

    response = client.put("/v1/me/music", json={"player": "someone-else", "token": SECRET})

    assert response.status_code == 422 and "no player of that id" in response.json()["detail"]
    assert client.get("/v1/me/music").json()["player"] is None


def test_a_refused_token_is_refused_with_the_reason(memory):
    client, _ = app_for(memory, music({"players/all": (401, "Authentication required")}), "erwan")

    response = client.put("/v1/me/music", json={"player": PLAYER, "token": "wrong"})

    assert response.status_code == 502 and "refused your token" in response.json()["detail"]


def test_a_person_who_chose_nothing_gets_409_and_no_music_is_sent(memory):
    seen = []
    client, _ = app_for(memory, music({"players/get": PC}, seen), "erwan")

    assert client.get("/v1/me/music/status").status_code == 409
    assert client.post("/v1/me/music/stop").status_code == 409
    assert seen == []


def test_a_server_without_music_assistant_says_so(memory):
    client, _ = app_for(memory, None, "erwan")

    response = client.get("/v1/me/music")

    assert response.json() == {"server": False, "player": None, "saved_at": None}
    assert client.put("/v1/me/music", json={"player": PLAYER, "token": SECRET}).status_code == 404


def test_search_play_queue_and_stop_act_on_the_persons_own_player(memory):
    seen = []
    answers = {
        "players/all": PLAYERS,
        "players/get": PC,
        "music/search": {"tracks": [{"uri": "library://track/1", "name": "Blue", "artists": [{"name": "Joni"}]}]},
        "player_queues/play_media": None,
        "player_queues/autoplay": None,
        "player_queues/stop": None,
    }
    client, person_id = app_for(memory, music(answers, seen), "erwan")
    client.put("/v1/me/music", json={"player": PLAYER, "token": SECRET})

    found = client.get("/v1/me/music/search", params={"q": "blue"}).json()["results"]
    played = client.post("/v1/me/music/play", json={"uri": "library://track/1"}).json()["message"]
    queued = client.post("/v1/me/music/queue", json={"uri": "library://track/1", "position": "end"}).json()["message"]
    stopped = client.post("/v1/me/music/stop").json()["message"]

    assert found == [{"uri": "library://track/1", "type": "track", "title": "Blue", "artist": "Joni", "album": "-"}]
    assert played == "Playing library://track/1 on your player, replacing its queue."
    assert queued == "Queued library://track/1 at the end of the queue on your player."
    assert stopped == "Stopped your player."
    assert {auth for _, _, auth in seen} == {"Bearer " + SECRET}
    assert {args.get("queue_id") for command, args, _ in seen if command.startswith("player_queues/")} == {PLAYER}


def test_a_queue_position_other_than_next_or_end_is_refused_before_music_assistant_is_asked(memory):
    seen = []
    client, _ = app_for(memory, music({"players/all": PLAYERS}, seen), "erwan")
    client.put("/v1/me/music", json={"player": PLAYER, "token": SECRET})
    seen.clear()

    response = client.post("/v1/me/music/queue", json={"uri": "library://track/1", "position": "middle"})

    assert response.status_code == 422
    assert seen == []


def test_forgetting_the_player_removes_the_token_too(memory):
    client, person_id = app_for(memory, music({"players/all": PLAYERS}), "erwan")
    client.put("/v1/me/music", json={"player": PLAYER, "token": SECRET})

    assert client.delete("/v1/me/music").json()["player"] is None
    assert client.delete("/v1/me/music").status_code == 404
    assert memory.database.execute("SELECT COUNT(*) FROM user_music WHERE person_id = ?", (person_id,)).fetchone()[0] == 0


def test_each_person_has_their_own_player(memory):
    players = [{"player_id": name, "display_name": name, "available": True} for name in ("erwan_pc", "anna_pc")]
    server = music({"players/all": players}, [])
    erwan, _ = app_for(memory, server, "erwan")
    anna, _ = app_for(memory, server, "anna")
    erwan.put("/v1/me/music", json={"player": "erwan_pc", "token": "erwan-token-1234"})
    anna.put("/v1/me/music", json={"player": "anna_pc", "token": "anna-token-1234"})

    assert erwan.get("/v1/me/music").json()["player"] == "erwan_pc"
    assert anna.get("/v1/me/music").json()["player"] == "anna_pc"
