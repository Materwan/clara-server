"""The list of conversations: what a client shows as its history, titled by Clara, renamed, pinned, searched."""

from dataclasses import replace
from datetime import datetime

import pytest
from conftest import FakeBackend, fake_providers, say
from fastapi.testclient import TestClient

from clara.agent import Agent, NothingToTitle, clean_title
from clara.memory import TurnRow
from clara.prompt import SystemPrompt
from clara.server import create_app
from clara.tools import default_toolbox

AUTH = {"Authorization": "Bearer secret-cli"}
PC = {"surface": "app", "user_id": "pc"}


@pytest.fixture
def erwan(memory):
    return memory.resolve("app", "pc", "Erwan")


def listed(memory, person, query="", surface="app") -> list[str]:
    return [info.conversation for info in memory.conversations_of(person.id, surface, query)]


# --- the memory ------------------------------------------------------------------------------


def test_a_conversation_is_listed_from_its_first_turn(memory, erwan):
    assert listed(memory, erwan) == []
    memory.add_exchange("app:pc:1", erwan.id, "How do pointers work in C?", "They hold addresses.")
    info = memory.conversation_info("app:pc:1")
    assert (info.person_id, info.surface, info.title, info.pinned) == (erwan.id, "app", "", False)
    assert info.preview == "How do pointers work in C?"
    datetime.fromisoformat(info.updated_at)
    assert listed(memory, erwan) == ["app:pc:1"]
    assert listed(memory, erwan, surface="cli") == []  # only the surface asked for


def test_pinned_ones_come_first_then_the_last_written_in(memory, erwan):
    for name in ("a", "b", "c"):
        memory.add_exchange(f"app:pc:{name}", erwan.id, name, "ok")
    memory.add_exchange("app:pc:a", erwan.id, "again", "ok")  # same second: the rowid breaks the tie
    with memory._db:  # make the order of the times certain
        for name, moment in (("a", "2026-10-03T10:00:00"), ("b", "2026-10-03T09:00:00"), ("c", "2026-10-03T11:00:00")):
            memory._db.execute("UPDATE conversations SET updated_at = ? WHERE conversation = ?", (moment, f"app:pc:{name}"))
    assert listed(memory, erwan) == ["app:pc:c", "app:pc:a", "app:pc:b"]
    memory.update_conversation("app:pc:b", pinned=True)
    assert listed(memory, erwan) == ["app:pc:b", "app:pc:c", "app:pc:a"]


def test_search_looks_in_titles_messages_and_summaries(memory, erwan):
    memory.add_exchange("app:pc:1", erwan.id, "Set a tea timer", "Done.")
    memory.add_exchange("app:pc:2", erwan.id, "Explain malloc", "It allocates 100% of nothing_much.")
    memory.add_exchange("app:pc:3", erwan.id, "hello", "hi")
    memory.update_conversation("app:pc:3", title="Greetings")
    memory.set_summary("app:pc:1", "The user brews oolong.", 0, 0)
    assert listed(memory, erwan, "TEA") == ["app:pc:1"]
    assert listed(memory, erwan, "malloc") == ["app:pc:2"]
    assert listed(memory, erwan, "greet") == ["app:pc:3"]
    assert listed(memory, erwan, "oolong") == ["app:pc:1"]
    assert listed(memory, erwan, "100%") == ["app:pc:2"]  # % and _ are taken as they are
    assert listed(memory, erwan, "ing%much") == []  # a wildcard would have matched
    assert listed(memory, erwan, "nothin__much") == []
    assert listed(memory, erwan, "nothing_much") == ["app:pc:2"]


def test_tool_calls_and_results_do_not_match_a_search_nor_show(memory, erwan):
    rows = [
        TurnRow("assistant", "", [{"function": {"name": "remember", "arguments": {"text": "secret plan"}}}]),
        TurnRow("tool", "Remembered: secret plan", tool_name="remember"),
        TurnRow("assistant", "Noted."),
    ]
    memory.add_turn("app:pc:1", erwan.id, "Remember this", rows)
    assert listed(memory, erwan, "secret") == []
    shown, more = memory.transcript("app:pc:1")
    assert [(m.role, m.content) for m in shown] == [("user", "Remember this"), ("assistant", "Noted.")]
    assert not more


def test_a_transcript_read_with_calls_gives_the_tools_an_answer_called(memory, erwan):
    rows = [
        TurnRow("assistant", "Let me look.", [{"function": {"name": "web_search", "arguments": {"query": "tea"}}}]),
        TurnRow("tool", "results", tool_name="web_search"),
        TurnRow("assistant", "", [{"function": {"name": "create_markdown_file", "arguments": {"name": "a.md", "content": "x" * 1000}}}]),
        TurnRow("tool", "Created", tool_name="create_markdown_file"),
        TurnRow("assistant", "", [{"function": {"name": "read_markdown_file", "arguments": {"name": "b.md"}}}]),
        TurnRow("tool", "y" * 9000, tool_name="read_markdown_file"),
        TurnRow("assistant", "Done."),
    ]
    memory.add_turn("app:pc:1", erwan.id, "Look up tea", rows)
    shown, _ = memory.transcript("app:pc:1", calls=True)
    assert [(m.role, m.content, [c["name"] for c in m.calls]) for m in shown] == [
        ("user", "Look up tea", []),
        ("assistant", "Let me look.", ["web_search"]),
        ("assistant", "", ["create_markdown_file"]),
        ("assistant", "", ["read_markdown_file"]),
        ("assistant", "Done.", []),
    ]
    assert shown[1].calls[0]["arguments"] == {"query": "tea"}
    assert len(shown[2].calls[0]["arguments"]["content"]) == 1000
    assert (shown[1].calls[0]["result"], shown[1].calls[0]["truncated"]) == ("results", False)  # what it gave back
    assert shown[2].calls[0]["result"] == "Created"
    assert len(shown[3].calls[0]["result"]) == 8000 and shown[3].calls[0]["truncated"]  # cut, and says so


def test_a_long_conversation_gives_its_last_messages(memory, erwan):
    for number in range(5):
        memory.add_exchange("app:pc:1", erwan.id, f"q{number}", f"a{number}")
    shown, more = memory.transcript("app:pc:1", limit=3)
    assert [m.content for m in shown] == ["a3", "q4", "a4"] and more


def test_rename_pin_and_untitle(memory, erwan):
    memory.add_exchange("app:pc:1", erwan.id, "hi", "hello")
    assert memory.update_conversation("app:pc:1", title="  My   chat ", pinned=True)
    info = memory.conversation_info("app:pc:1")
    assert (info.title, info.titled_by, info.pinned) == ("My chat", "person", True)
    memory.update_conversation("app:pc:1", title="")
    assert (memory.conversation_info("app:pc:1").title, memory.conversation_info("app:pc:1").titled_by) == ("", "")
    assert not memory.update_conversation("app:pc:unknown", title="x")


def test_clara_does_not_overwrite_the_persons_title(memory, erwan):
    memory.add_exchange("app:pc:1", erwan.id, "hi", "hello")
    assert memory.title_if_untitled("app:pc:1", "Greetings") == "Greetings"
    assert memory.conversation_info("app:pc:1").titled_by == "clara"
    memory.update_conversation("app:pc:1", title="Mine")
    assert memory.title_if_untitled("app:pc:1", "Other") == "Mine"


def test_forgetting_a_conversation_takes_it_off_the_list(memory, erwan):
    memory.add_exchange("app:pc:1", erwan.id, "hi", "hello")
    memory.update_conversation("app:pc:1", title="Kept?")
    memory.clear_conversation("app:pc:1")
    assert listed(memory, erwan) == [] and memory.conversation_info("app:pc:1") is None
    memory.add_exchange("app:pc:1", erwan.id, "again", "ok")  # starts afresh, untitled
    assert memory.conversation_info("app:pc:1").title == ""


def test_conversations_follow_a_merge_and_go_with_their_person(memory, erwan):
    other = memory.resolve("app", "laptop", "Erwan")
    memory.add_exchange("app:laptop:1", other.id, "from the laptop", "ok")
    memory.link_account("app", "laptop", erwan)
    assert listed(memory, erwan) == ["app:laptop:1"]

    bob = memory.resolve("discord", "bob", "Bob")
    memory.add_exchange("discord:channel", erwan.id, "hi all", "hello")  # started by erwan, shared
    memory.add_exchange("discord:channel", bob.id, "hi erwan", "hello bob")
    memory.delete_person(erwan.id)
    assert memory.conversation_info("app:laptop:1") is None
    assert memory.conversation_info("discord:channel").person_id is None  # stays, for bob, ownerless


def test_conversations_from_before_the_list_existed_are_not_listed(memory, erwan):
    with memory._db:  # a message stored by an older server: no row in the list
        memory._db.execute(
            "INSERT INTO messages (conversation, person_id, role, content, created_at)"
            " VALUES ('app:pc', ?, 'user', 'old', '2026-01-01T00:00:00+00:00')",
            (erwan.id,),
        )
    assert listed(memory, erwan) == []


# --- titles ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "title"),
    [
        ('"Tea timer setup"', "Tea timer setup"),
        ("Title: Pointers in C.", "Pointers in C"),
        ("**Titre : Notes PDF**", "Notes PDF"),
        ("\n\n# Lists in Python\nThis conversation is about...", "Lists in Python"),
        ("word " * 30, "word word word word word word word word word word word word…"),
        ("   ", ""),
    ],
)
def test_the_title_a_model_writes_is_cleaned(written, title):
    assert clean_title(written) == title


async def test_clara_titles_a_conversation_once(memory, erwan, tmp_path):
    backend = FakeBackend(say('"Pointers ', 'in C"'))
    agent = Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"))
    memory.add_exchange("app:pc:1", erwan.id, "How do pointers work in C?", "They hold addresses.")
    assert await agent.title("app:pc:1") == "Pointers in C"
    prompt = backend.calls[0][0]
    assert "title" in prompt[0]["content"] and "How do pointers work in C?" in prompt[1]["content"]
    assert backend.calls[0][1] is None  # no tools
    assert await agent.title("app:pc:1") == "Pointers in C"  # no second model call
    assert len(backend.calls) == 1
    assert memory.history("app:pc:1", 10)[-1].content == "They hold addresses."  # nothing stored in it
    with pytest.raises(NothingToTitle):
        await agent.title("app:pc:nope")


async def test_a_purged_conversation_is_titled_from_its_summary(memory, erwan, tmp_path):
    backend = FakeBackend(say("Oolong tea"))
    agent = Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"))
    memory.add_exchange("app:pc:1", erwan.id, "q", "a")
    memory.set_summary("app:pc:1", "The user brews oolong tea.", memory.last_message_id("app:pc:1"), 0)
    memory.purge_summarised("app:pc:1", memory.last_message_id("app:pc:1"))
    assert await agent.title("app:pc:1") == "Oolong tea"
    assert "oolong" in backend.calls[0][0][1]["content"]


# --- over HTTP -----------------------------------------------------------------------------------


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def client(settings, backend):
    with TestClient(create_app(settings, fake_providers(settings, backend))) as http:
        yield http


def chat(http, backend, message, conversation, answer="Sure.", account=PC):
    backend.rounds.append(say(answer))
    body = {**account, "message": message, "conversation": conversation}
    response = http.post("/v1/chat", json=body, headers=AUTH)
    assert response.status_code == 200, response.text


def test_the_history_of_an_account_over_http(client, backend):
    chat(client, backend, "How do pointers work?", "app:pc:aaa", "They hold addresses.")
    chat(client, backend, "Tea timer", "app:pc:bbb")
    chat(client, backend, "From the terminal", "cli:erwan")  # another surface: not in the app's list

    listing = client.get("/v1/conversations", params=PC, headers=AUTH).json()["conversations"]
    assert [c["id"] for c in listing] == ["app:pc:bbb", "app:pc:aaa"]
    assert listing[1]["preview"] == "How do pointers work?" and listing[1]["title"] == ""

    shown = client.get("/v1/conversations/app:pc:aaa/messages", params=PC, headers=AUTH).json()
    assert [(m["role"], m["content"]) for m in shown["messages"]] == [
        ("user", "How do pointers work?"), ("assistant", "They hold addresses.")
    ]
    assert (shown["summary"], shown["earlier"]) == ("", False)

    backend.rounds.append(say("C pointers"))
    titled = client.post("/v1/conversations/app:pc:aaa/title", json=PC, headers=AUTH)
    assert titled.json() == {"id": "app:pc:aaa", "title": "C pointers"}

    changed = client.patch("/v1/conversations/app:pc:bbb", json={**PC, "title": "Tea", "pinned": True}, headers=AUTH)
    assert (changed.json()["title"], changed.json()["pinned"], changed.json()["titled_by"]) == ("Tea", True, "person")

    found = client.get("/v1/conversations", params={**PC, "q": "pointers"}, headers=AUTH).json()
    assert [c["id"] for c in found["conversations"]] == ["app:pc:aaa"]

    gone = client.delete("/v1/conversations/app:pc:bbb", params=PC, headers=AUTH)
    assert gone.json()["deleted_messages"] == 2
    listing = client.get("/v1/conversations", params=PC, headers=AUTH).json()["conversations"]
    assert [(c["id"], c["title"]) for c in listing] == [("app:pc:aaa", "C pointers")]

    # the generic route still answers for the conversation itself
    assert client.get("/v1/conversations/app:pc:aaa", headers=AUTH).json()["conversation"] == "app:pc:aaa"


def test_nobody_reaches_the_conversations_of_someone_else(client, backend):
    chat(client, backend, "my secret", "app:pc:aaa")
    chat(client, backend, "hello", "app:other:x", account={"surface": "app", "user_id": "other"})
    other = {"surface": "app", "user_id": "other"}
    assert [c["id"] for c in client.get("/v1/conversations", params=other, headers=AUTH).json()["conversations"]] == [
        "app:other:x"
    ]
    assert client.get("/v1/conversations/app:pc:aaa/messages", params=other, headers=AUTH).status_code == 404
    assert client.patch("/v1/conversations/app:pc:aaa", json={**other, "title": "x"}, headers=AUTH).status_code == 404
    assert client.post("/v1/conversations/app:pc:aaa/title", json=other, headers=AUTH).status_code == 404
    assert client.delete("/v1/conversations/app:pc:aaa", params=other, headers=AUTH).status_code == 404
    assert client.delete("/v1/conversations/app:pc:aaa", params={"surface": "app"}, headers=AUTH).status_code == 422
    nobody = {"surface": "app", "user_id": "stranger"}
    assert client.get("/v1/conversations", params=nobody, headers=AUTH).json() == {"conversations": []}
    assert client.get("/v1/conversations/app:pc:aaa/messages", params=PC, headers=AUTH).status_code == 200


def test_a_conversation_with_nothing_to_title_and_a_failing_model(client, backend):
    chat(client, backend, "hi", "app:pc:aaa")
    backend.rounds.append(say(""))
    assert client.post("/v1/conversations/app:pc:aaa/title", json=PC, headers=AUTH).status_code == 502
    assert client.get("/v1/conversations", params=PC, headers=AUTH).json()["conversations"][0]["title"] == ""


def test_the_summary_comes_with_purged_messages(client, backend):
    chat(client, backend, "old question", "app:pc:aaa")
    memory = client.app.state.memory
    upto = memory.last_message_id("app:pc:aaa")
    memory.set_summary("app:pc:aaa", "They talked about tea.", upto, 0)
    shown = client.get("/v1/conversations/app:pc:aaa/messages", params=PC, headers=AUTH).json()
    assert shown["summary"] == ""  # its messages are still there, and shown
    memory.purge_summarised("app:pc:aaa", upto)
    chat(client, backend, "new question", "app:pc:aaa")
    shown = client.get("/v1/conversations/app:pc:aaa/messages", params=PC, headers=AUTH).json()
    assert shown["summary"] == "They talked about tea."
    assert [m["content"] for m in shown["messages"]] == ["new question", "Sure."]


def test_the_history_respects_the_surface_limits(settings, backend):
    limited = replace(settings, client_surfaces={"terminal": frozenset({"cli"})})
    with TestClient(create_app(limited, fake_providers(limited, backend))) as http:
        assert http.get("/v1/conversations", params=PC, headers=AUTH).status_code == 403
        assert http.get("/v1/conversations/app:pc:1/messages", params=PC, headers=AUTH).status_code == 403
