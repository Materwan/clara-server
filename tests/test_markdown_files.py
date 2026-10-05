"""Markdown files Clara writes: the store, the tools she calls, the routes the clients read them from."""

from pathlib import Path

import pytest
from conftest import FakeBackend, fake_providers, say
from fastapi.testclient import TestClient

from clara.agent import Agent, ChatRequest
from clara.llm import LlmChunk, ToolCall
from clara.markdownfiles import MarkdownError, MarkdownFiles, clean_name
from clara.prompt import SystemPrompt
from clara.server import create_app
from clara.tools import MARKDOWN_TOOLS, default_toolbox

PASSWORD = "correct horse battery"


def use(tool: str, **arguments) -> list[LlmChunk]:
    """A round in which the model calls a tool (conftest.call() takes `name` for itself, as these tools do)."""
    return [LlmChunk(tool_calls=[ToolCall(tool, arguments)])]


@pytest.fixture
def files(memory):
    return MarkdownFiles(memory)


@pytest.fixture
def erwan(memory):
    return memory.resolve("web", "erwan", "Erwan")


# --- the store -------------------------------------------------------------------------------------------


def test_a_name_gets_its_extension_and_is_checked():
    assert clean_name("notes") == "notes.md"
    assert clean_name("  Meeting   notes.MD ") == "Meeting notes.MD"
    assert clean_name("plan (v2).md") == "plan (v2).md"
    assert clean_name("é cole.md") == "é cole.md"
    assert clean_name("two\twords") == "two words.md"  # white space is one space
    for bad in ("", "  ", "a/b.md", "..\\x.md", "../x", ".hidden.md", "-x.md", "x" * 90, "a:b.md"):
        with pytest.raises(MarkdownError):
            clean_name(bad)


def test_a_file_is_made_read_and_listed(files, erwan):
    file, new = files.create(erwan.id, "notes", "# Notes\nOne\n")
    assert new and file.name == "notes.md" and file.size == len("# Notes\nOne\n")
    found, content = files.read(erwan.id, "NOTES.md")  # the name is not case sensitive
    assert found.id == file.id and content == "# Notes\nOne\n"
    files.create(erwan.id, "later", "x")
    assert [f.name for f in files.of(erwan.id)] == ["later.md", "notes.md"]  # the newest first
    assert files.get(erwan.id, file.id)[1] == content


def test_the_text_ends_with_a_line_break_and_uses_unix_ones(files, erwan):
    files.create(erwan.id, "a", "one\r\ntwo")
    assert files.read(erwan.id, "a")[1] == "one\ntwo\n"


def test_a_file_that_exists_is_not_replaced_by_mistake(files, erwan):
    files.create(erwan.id, "a", "first")
    with pytest.raises(MarkdownError, match="edit_markdown_file"):
        files.create(erwan.id, "A.md", "second")
    assert files.read(erwan.id, "a")[1] == "first\n"
    file, new = files.create(erwan.id, "A.md", "second", overwrite=True)
    assert not new and files.read(erwan.id, "a")[1] == "second\n" and len(files.of(erwan.id)) == 1


def test_the_limits_of_a_file_and_of_a_person(memory, erwan):
    small = MarkdownFiles(memory, max_chars=20, max_files=2)
    with pytest.raises(MarkdownError, match="empty"):
        small.create(erwan.id, "a", "  \n ")
    with pytest.raises(MarkdownError, match="at most 20"):
        small.create(erwan.id, "a", "x" * 21)
    small.create(erwan.id, "a", "x")
    small.create(erwan.id, "b", "x")
    with pytest.raises(MarkdownError, match="already has 2"):
        small.create(erwan.id, "c", "x")
    small.create(erwan.id, "a", "y", overwrite=True)  # replacing one is not adding one
    with pytest.raises(MarkdownError, match="would have"):
        small.edit(erwan.id, "a", "y", "y" * 40)


def test_a_passage_is_replaced_where_it_is_found_once(files, erwan):
    files.create(erwan.id, "todo", "- milk\n- eggs\n- bread\n")
    file, count = files.edit(erwan.id, "todo", "- eggs", "- eggs (6)")
    assert count == 1
    assert files.read(erwan.id, "todo")[1] == "- milk\n- eggs (6)\n- bread\n"
    files.edit(erwan.id, "todo", "- milk\n", "")  # deleting a passage
    assert files.read(erwan.id, "todo")[1] == "- eggs (6)\n- bread\n"


def test_a_passage_that_is_not_there_or_not_alone_is_refused_with_what_to_do(files, erwan):
    files.create(erwan.id, "todo", "- a\n- a\n- b\n")
    with pytest.raises(MarkdownError, match="not in the file"):
        files.edit(erwan.id, "todo", "- c", "- d")
    with pytest.raises(MarkdownError, match="2 times"):
        files.edit(erwan.id, "todo", "- a", "- z")
    with pytest.raises(MarkdownError, match="same"):
        files.edit(erwan.id, "todo", "- a", "- a")
    with pytest.raises(MarkdownError, match="empty"):
        files.edit(erwan.id, "todo", "", "x")
    with pytest.raises(MarkdownError, match="No file"):
        files.edit(erwan.id, "nothing", "a", "b")
    assert files.read(erwan.id, "todo")[1] == "- a\n- a\n- b\n"  # nothing changed
    _, count = files.edit(erwan.id, "todo", "- a", "- z", replace_all=True)
    assert count == 2 and files.read(erwan.id, "todo")[1] == "- z\n- z\n- b\n"


def test_a_file_cannot_be_emptied_by_an_edit(files, erwan):
    files.create(erwan.id, "a", "only")
    with pytest.raises(MarkdownError, match="empty"):
        files.edit(erwan.id, "a", "only\n", "")


def test_text_is_added_at_the_end(files, erwan):
    files.create(erwan.id, "a", "# A")
    files.append(erwan.id, "a", "\n## More\nText")
    assert files.read(erwan.id, "a")[1] == "# A\n\n## More\nText\n"
    with pytest.raises(MarkdownError, match="nothing to add"):
        files.append(erwan.id, "a", " ")
    with pytest.raises(MarkdownError, match="No file"):
        files.append(erwan.id, "zzz", "x")


def test_a_person_only_reaches_their_own_files(memory, files, erwan):
    other = memory.resolve("web", "alice", "Alice")
    mine, _ = files.create(erwan.id, "secret", "mine")
    files.create(other.id, "secret", "hers")  # the same name is another file
    assert files.read(other.id, "secret")[1] == "hers\n"
    assert files.get(other.id, mine.id) is None
    assert not files.delete(other.id, mine.id)
    with pytest.raises(MarkdownError):
        files.edit(other.id, "nothing", "a", "b")
    assert files.delete(erwan.id, mine.id) and files.find(erwan.id, "secret") is None


def test_erasing_a_person_erases_their_files_and_a_merge_keeps_both_sets(memory, files, erwan):
    other = memory.resolve("discord", "42", "Erwan on Discord")
    files.create(erwan.id, "notes", "web notes")
    files.create(other.id, "notes", "discord notes")
    files.create(other.id, "extra", "x")
    memory.link_account("discord", "42", erwan, force=True)
    names = sorted(f.name for f in files.of(erwan.id))
    assert names == ["extra.md", "notes (2).md", "notes.md"]  # the clash is renamed, nothing is lost
    assert files.read(erwan.id, "notes")[1] == "web notes\n"
    assert files.read(erwan.id, "notes (2)")[1] == "discord notes\n"
    memory.delete_person(erwan.id)
    assert memory.database.execute("SELECT COUNT(*) FROM markdown_files").fetchone()[0] == 0


# --- the tools -------------------------------------------------------------------------------------------


def make_agent(memory, tmp_path: Path, backend, **options) -> Agent:
    return Agent(
        memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"), markdown=MarkdownFiles(memory), **options
    )


async def run(agent: Agent, **fields) -> list[dict]:
    request = ChatRequest(**{"surface": "web", "user_id": "erwan", "user_name": "Erwan", "message": "hi", **fields})
    return [event async for event in agent.turn(request)]


async def test_clara_writes_a_file_and_the_client_is_told(memory, tmp_path):
    backend = FakeBackend(use("create_markdown_file", name="trip", content="# Trip\n- Rome\n"), say("Done."))
    agent = make_agent(memory, tmp_path, backend)
    events = await run(agent, message="write me a trip plan")
    shown = [e for e in events if e["type"] == "markdown_file"]
    assert len(shown) == 1 and shown[0]["action"] == "created"
    assert shown[0]["file"]["name"] == "trip.md" and shown[0]["file"]["size"] == len("# Trip\n- Rome\n")
    person = memory.find_person("web", "erwan")
    assert agent.markdown.read(person.id, "trip")[1] == "# Trip\n- Rome\n"
    result = next(e for e in events if e["type"] == "tool")["result"]
    assert "Created trip.md" in result and "do not paste" in result and "web site" not in result


async def test_other_clients_are_pointed_to_the_web_site(memory, tmp_path):
    backend = FakeBackend(use("create_markdown_file", name="trip", content="x"), say("Done."))
    events = await run(make_agent(memory, tmp_path, backend), surface="cli", message="write it")
    assert "Files page" in next(e for e in events if e["type"] == "tool")["result"]


async def test_clara_changes_the_file_she_made_in_an_earlier_conversation_turn(memory, tmp_path):
    backend = FakeBackend(
        use("create_markdown_file", name="todo", content="- milk\n- eggs\n"), say("Made it."),
        use("edit_markdown_file", name="todo", old_text="- eggs", new_text="- eggs\n- bread"), say("Added bread."),
        use("append_markdown_file", name="todo", text="\n## Later\n- tea"), say("Added a section."),
    )
    agent = make_agent(memory, tmp_path, backend)
    await run(agent, message="a todo list")
    edited = await run(agent, message="add bread")
    assert [e["action"] for e in edited if e["type"] == "markdown_file"] == ["updated"]
    await run(agent, message="a later section")
    person = memory.find_person("web", "erwan")
    assert agent.markdown.read(person.id, "todo")[1] == "- milk\n- eggs\n- bread\n\n## Later\n- tea\n"
    assert len(agent.markdown.of(person.id)) == 1


async def test_a_mistake_comes_back_to_the_model_as_text_to_correct(memory, tmp_path):
    backend = FakeBackend(
        use("create_markdown_file", name="a", content="one"),
        use("create_markdown_file", name="a", content="two"),
        use("edit_markdown_file", name="a", old_text="three", new_text="4"),
        use("read_markdown_file", name="nope"),
        say("I could not."),
    )
    agent = make_agent(memory, tmp_path, backend)
    events = await run(agent)
    results = [e["result"] for e in events if e["type"] == "tool"]
    assert results[0].startswith("Created")
    assert results[1].startswith("Error:") and "edit_markdown_file" in results[1]
    assert results[2].startswith("Error:") and "not in the file" in results[2]
    assert results[3].startswith("Error:") and "No file" in results[3]
    assert [e["type"] for e in events].count("markdown_file") == 1  # only what worked is shown


async def test_clara_lists_and_reads_files_in_pieces(memory, tmp_path):
    lines = "".join(f"line {number}\n" for number in range(1, 6001))  # more than one reading
    backend = FakeBackend(
        use("list_markdown_files"),
        use("read_markdown_file", name="big"),
        use("read_markdown_file", name="big", start_line=99999),
        use("list_markdown_files"),
        say("ok"),
    )
    agent = make_agent(memory, tmp_path, backend)
    agent.markdown.create(memory.resolve("web", "erwan", "Erwan").id, "big", lines)  # not through the model's own call
    results = [e["result"] for e in await run(agent) if e["type"] == "tool"]
    assert results[0].startswith("big.md (")
    assert results[1].startswith("big.md, lines 1-") and "of 6000 (read on with start_line=" in results[1]
    assert results[2] == "big.md has only 6000 lines."


async def test_the_tools_are_not_offered_without_a_store(memory, tmp_path):
    backend = FakeBackend(say("ok"))
    agent = Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md"))
    await run(agent)
    offered = {schema["function"]["name"] for schema in backend.calls[0][1]}
    assert not offered & MARKDOWN_TOOLS and "remember" in offered
    backend = FakeBackend(say("ok"))
    await run(make_agent(memory, tmp_path, backend))
    assert MARKDOWN_TOOLS <= {schema["function"]["name"] for schema in backend.calls[0][1]}


async def test_the_files_of_one_person_are_not_reachable_from_a_conversation_of_another(memory, tmp_path):
    backend = FakeBackend(
        use("create_markdown_file", name="private", content="erwan only"), say("ok"),
        use("read_markdown_file", name="private"), use("edit_markdown_file", name="private", old_text="e", new_text="x"),
        say("ok"),
    )
    agent = make_agent(memory, tmp_path, backend)
    await run(agent)
    events = await run(agent, user_id="alice", user_name="Alice")
    results = [e["result"] for e in events if e["type"] == "tool"]
    assert all(result.startswith("Error:") and "No file" in result for result in results)


# --- the routes ------------------------------------------------------------------------------------------


@pytest.fixture
def http(settings):
    backend = FakeBackend(use("create_markdown_file", name="notes", content="# Notes\n"), say("ok"))
    with TestClient(create_app(settings, fake_providers(settings, backend))) as client:
        users = client.app.state.users
        users.create("erwan", PASSWORD)
        users.create("alice", PASSWORD)
        yield client


def web(http, name):
    answer = http.post(
        "/v1/auth/login", json={"username": name, "password": PASSWORD, "surface": "web"}, headers={"X-Clara-Web": "1"}
    )
    assert answer.status_code == 200
    return {"X-Clara-Web": "1"}


def test_the_web_site_lists_reads_and_deletes_the_files_of_its_user_only(http):
    headers = web(http, "erwan")
    mine = {"surface": "web", "user_id": "erwan"}
    assert http.get("/v1/markdown-files", params=mine, headers=headers).json() == {"files": []}
    assert http.post("/v1/chat", json={**mine, "message": "notes please"}, headers=headers).status_code == 200
    listed = http.get("/v1/markdown-files", params=mine, headers=headers).json()["files"]
    assert [f["name"] for f in listed] == ["notes.md"]
    one = http.get(f"/v1/markdown-files/{listed[0]['id']}", params=mine, headers=headers).json()
    assert one["content"] == "# Notes\n" and one["size"] == 8

    http.cookies.clear()  # the client keeps one session cookie: sign in as alice instead
    other = web(http, "alice")
    theirs = {"surface": "web", "user_id": "alice"}
    assert http.get("/v1/markdown-files", params=theirs, headers=other).json() == {"files": []}
    assert http.get(f"/v1/markdown-files/{listed[0]['id']}", params=theirs, headers=other).status_code == 404
    assert http.delete(f"/v1/markdown-files/{listed[0]['id']}", params=theirs, headers=other).status_code == 404
    assert http.get("/v1/markdown-files", params=mine, headers=other).status_code == 403  # not their account

    http.cookies.clear()
    headers = web(http, "erwan")
    assert http.delete(f"/v1/markdown-files/{listed[0]['id']}", params=mine, headers=headers).json() == {"ok": True}
    assert http.get("/v1/markdown-files", params=mine, headers=headers).json() == {"files": []}


def test_the_files_need_a_login(http):
    assert http.get("/v1/markdown-files", params={"surface": "web", "user_id": "erwan"}).status_code == 401
