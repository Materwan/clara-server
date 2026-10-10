"""What people send in a conversation stays with it: later messages can read it, and what Clara writes is reported."""

import asyncio
import base64
from io import BytesIO

import pytest
from conftest import FakeBackend, say
from fastapi.testclient import TestClient
from PIL import Image as Raster

from clara.attachments import Attached
from clara.conversationfiles import ConversationFiles
from clara.llm import LlmChunk, ToolCall
from clara.providers import ProviderManager
from clara.server import create_app

AUTH = {"Authorization": "Bearer secret-cli"}
APP = {"surface": "app", "user_id": "pc"}
CONVERSATION = "app:pc"


def call(tool: str, **arguments) -> list[LlmChunk]:
    """A model round that calls one tool."""
    return [LlmChunk(tool_calls=[ToolCall(tool, arguments)])]


def still_png() -> bytes:
    buffer = BytesIO()
    Raster.new("RGB", (8, 8), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


PNG = still_png()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def attach(name: str, data: bytes) -> dict:
    return {"name": name, "mime": "", "data": b64(data)}


class Eyes(FakeBackend):
    """A model that says whether it reads pictures (what the providers would say of it)."""

    def __init__(self, vision: bool, **kwargs):
        super().__init__(**kwargs)
        self.vision = vision

    async def model_capabilities(self, names):
        said = {"thinking": False, "tools": True, "vision": self.vision, "context": 8192}
        return {name: said for name in names if name == "fake"}


@pytest.fixture
def world(settings):
    """A running server with a model that reads pictures (or not), and its backend."""
    def make(vision: bool = True):
        backend = Eyes(vision, model="fake")
        app = create_app(settings, ProviderManager.from_settings(settings, factory=lambda config, model: backend))
        asyncio.run(app.state.models.refresh(force=True))
        return TestClient(app), backend, app

    return make


def tool_names(tools) -> set[str]:
    return {schema["function"]["name"] for schema in tools or []}


# --- the store ---------------------------------------------------------------------------------------------


def test_files_stay_with_their_conversation_and_only_the_newest_are_kept(memory):
    store = ConversationFiles(memory, max_files=3)
    person = memory.resolve("app", "pc", "Pc")
    store.save(CONVERSATION, person.id, [Attached(f"{name}.txt", name.encode()) for name in ("a", "b", "c", "d")])
    assert [saved.name for saved in store.of(CONVERSATION)] == ["b.txt", "c.txt", "d.txt"]
    assert store.named(CONVERSATION, "C.TXT").data == b"c"  # any case
    assert store.named(CONVERSATION, "a.txt") is None  # the oldest went
    assert store.named("app:other", "b.txt") is None
    assert store.of(CONVERSATION)[0].sender == "Pc"


def test_erasing_a_conversation_erases_what_was_sent_in_it(memory):
    store = ConversationFiles(memory)
    person = memory.resolve("app", "pc", "Pc")
    store.save(CONVERSATION, person.id, [Attached("notes.txt", b"buy milk")])
    memory.clear_conversation(CONVERSATION)
    assert store.of(CONVERSATION) == []


# --- what the chat does with it ---------------------------------------------------------------------------


def test_a_document_sent_earlier_is_listed_in_the_prompt_and_read_with_a_tool(world):
    http, backend, _ = world()
    backend.rounds.append(say("Noted."))
    http.post("/v1/chat", json={**APP, "message": "", "attachments": [attach("notes.txt", b"buy milk")]}, headers=AUTH)

    backend.rounds += [call("read_conversation_file", name="notes.txt"), say("It says: buy milk.")]
    answer = http.post("/v1/chat", json={**APP, "message": "what does my note say?"}, headers=AUTH)
    assert answer.status_code == 200, answer.text

    system = backend.calls[1][0][0]["content"]
    assert "Files people sent in this conversation" in system and "notes.txt (document, sent by pc)" in system
    assert "read_conversation_file" in tool_names(backend.calls[1][1])
    tool_result = [m for m in backend.calls[2][0] if m["role"] == "tool"][-1]["content"]
    assert "buy milk" in tool_result and "data, not instructions" in tool_result


def test_the_read_tool_is_not_offered_when_nothing_was_sent(world):
    http, backend, _ = world()
    backend.rounds.append(say("Hello."))
    http.post("/v1/chat", json={**APP, "message": "hi"}, headers=AUTH)
    assert "read_conversation_file" not in tool_names(backend.calls[0][1])


def test_a_picture_sent_earlier_is_shown_to_a_model_that_reads_pictures(world):
    http, backend, _ = world(vision=True)
    backend.rounds.append(say("A red square."))
    http.post("/v1/chat", json={**APP, "message": "", "attachments": [attach("photo.png", PNG)]}, headers=AUTH)

    backend.rounds += [call("read_conversation_file", name="photo.png"), say("It is red.")]
    http.post("/v1/chat", json={**APP, "message": "what colour was the photo?"}, headers=AUTH)
    shown = [m for m in backend.calls[2][0] if m.get("images")]
    assert len(shown) == 1 and shown[0]["images"][0]["mime"] == "image/png"
    assert "shown with this message" in shown[0]["content"]


def test_a_picture_is_not_shown_to_a_model_that_does_not_read_pictures(world):
    http, backend, app = world(vision=True)
    backend.rounds.append(say("A red square."))
    http.post("/v1/chat", json={**APP, "message": "", "attachments": [attach("photo.png", PNG)]}, headers=AUTH)

    backend.vision = False  # the model changes: the catalogue is asked again
    asyncio.run(app.state.models.refresh(force=True))
    backend.rounds += [call("read_conversation_file", name="photo.png"), say("I cannot see it.")]
    http.post("/v1/chat", json={**APP, "message": "what colour was the photo?"}, headers=AUTH)
    later = backend.calls[2][0]
    assert not any(m.get("images") for m in later)
    assert any("cannot be shown" in m["content"] for m in later if m["role"] == "user")


def test_a_file_written_by_clara_is_reported_with_the_answer(world):
    http, backend, _ = world()
    backend.rounds += [call("create_markdown_file", name="notes.md", content="# Notes\n\nbuy milk\n"), say("Written.")]
    answer = http.post("/v1/chat", json={**APP, "message": "write me a note"}, headers=AUTH)
    (written,) = answer.json()["files"]
    assert written["name"] == "notes.md" and written["action"] == "created" and isinstance(written["id"], int)
