"""Files a chat message carries: pictures the model reads (only with a model that reads them), documents read here."""

import asyncio
import base64
from io import BytesIO

import pytest
from conftest import FakeBackend, say
from fastapi.testclient import TestClient
from PIL import Image as Raster

from clara.attachments import (
    MAX_ANIMATION_BYTES,
    MAX_ATTACHMENTS,
    MAX_PICTURE_BYTES,
    Attached,
    FileError,
    Image,
    image_type,
    prepare,
)
from clara.llm import OllamaBackend, OpenAIBackend
from clara.providers import ProviderManager
from clara.server import create_app
from clara.traffic import without_pictures


def encoded(frames: int = 1, size: tuple[int, int] = (8, 8), fmt: str = "PNG") -> bytes:
    """A real picture (still, or animated with `frames` frames of different colours)."""
    colours = [(200, 30, 30), (30, 30, 200), (30, 200, 30), (200, 200, 30), (200, 30, 200)]
    images = [Raster.new("RGB", size, colours[number % len(colours)]) for number in range(frames)]
    buffer = BytesIO()
    if frames == 1:
        images[0].save(buffer, format=fmt)
    else:
        images[0].save(buffer, format=fmt, save_all=True, append_images=images[1:], duration=100, loop=0)
    return buffer.getvalue()


PNG = encoded()
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
AUTH = {"Authorization": "Bearer secret-cli"}
APP = {"surface": "app", "user_id": "pc"}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def attach(name: str, data: bytes, mime: str = "") -> dict:
    return {"name": name, "mime": mime, "data": b64(data)}


# --- what is prepared -----------------------------------------------------------------------------------------


def test_a_picture_is_told_by_its_bytes_not_its_name():
    assert [image_type(data) for data in (PNG, JPEG, b"GIF89a....", b"RIFF\x00\x00\x00\x00WEBPVP8 ", b"hello")] == [
        "image/png", "image/jpeg", "image/gif", "image/webp", None,
    ]


def test_a_picture_goes_to_the_model_and_the_message_names_it():
    prepared = prepare("what is this?", [Attached("photo.png", PNG)])
    assert prepared.message == "(Attached: photo.png)\n\nwhat is this?"
    assert prepared.images == (Image("photo.png", "image/png", b64(PNG)),)


def test_a_document_is_put_in_the_message_the_way_the_web_site_puts_it():
    prepared = prepare("", [Attached("notes.py", b"print('hi')\n")])
    assert prepared.message == (
        "Here is: notes.py.\n\n<document name=\"notes.py\" type=\"python\">\n```python\nprint('hi')\n```\n</document>"
    )
    assert prepared.images == ()


def test_a_message_with_a_text_file_and_a_picture_keeps_the_order_the_web_site_uses():
    prepared = prepare("look at this", [Attached("a.txt", b"x")])
    assert prepared.message == (
        "look at this\n\n(Attached: a.txt)\n\n<document name=\"a.txt\" type=\"text\">\n```\nx\n```\n</document>"
    )
    both = prepare("what is this?", [Attached("photo.png", PNG), Attached("a.txt", b"x")])
    assert both.message.startswith("(Attached: photo.png)\n\nwhat is this?\n\n(Attached: a.txt)\n\n<document")
    assert [image.name for image in both.images] == ["photo.png"]


def test_a_fence_longer_than_any_run_of_backticks_in_the_text_cannot_be_closed_early():
    prepared = prepare("", [Attached("tricky.md", b"a ``` b")])
    assert "````markdown\na ``` b\n````" in prepared.message


@pytest.mark.parametrize(
    "files, words",
    [
        ([Attached(f"{number}.png", PNG) for number in range(MAX_ATTACHMENTS + 1)], "at most"),
        ([Attached("tool.bin", b"\x7fELF\x00\x00")], "cannot be read"),
        ([Attached("photo.png", PNG + b"0" * MAX_PICTURE_BYTES)], "too big"),
        ([Attached("empty.txt", b"  \n")], "cannot be read"),
        ([Attached("broken.pdf", b"%PDF-1.4 not really")], "cannot be read"),
        ([Attached(f"doc{number}.txt", b"a" * 50_000) for number in range(4)], "too long"),
    ],
)
def test_a_file_that_cannot_be_used_is_refused_with_the_reason(files, words):
    with pytest.raises(FileError, match=words):
        prepare("", files)


# --- the chat route -------------------------------------------------------------------------------------------


class Eyes(FakeBackend):
    """A model that says whether it reads pictures (what the providers would say of it)."""

    def __init__(self, vision: bool, **kwargs):
        super().__init__(**kwargs)
        self.vision = vision

    async def model_capabilities(self, names):
        said = {"thinking": False, "tools": True, "vision": self.vision, "context": 8192}
        return {name: said for name in names if name == "fake"}


@pytest.fixture
def chat(settings):
    def make(vision: bool):
        backend = Eyes(vision, model="fake")
        app = create_app(settings, ProviderManager.from_settings(settings, factory=lambda config, model: backend))
        asyncio.run(app.state.models.refresh(force=True))  # what the providers say of their models, now
        return TestClient(app), backend

    return make


def test_a_model_that_cannot_read_pictures_is_not_sent_them(chat):
    http, backend = chat(vision=False)
    response = http.post("/v1/chat", json={**APP, "message": "what is this?", "attachments": [attach("photo.png", PNG)]},
                         headers=AUTH)
    assert response.status_code == 422 and "cannot read pictures" in response.text
    assert backend.calls == []  # no model was asked


def test_a_picture_is_read_by_a_model_that_can_and_the_history_keeps_only_its_name(chat):
    http, backend = chat(vision=True)
    backend.rounds.append(say("A cat."))
    response = http.post("/v1/chat", json={**APP, "message": "what is this?", "attachments": [attach("photo.png", PNG)]},
                         headers=AUTH)
    assert response.status_code == 200, response.text
    user = backend.calls[-1][0][-1]
    assert user["content"].endswith("(Attached: photo.png)\n\nwhat is this?")
    assert user["images"] == [{"name": "photo.png", "mime": "image/png", "data": b64(PNG)}]

    backend.rounds.append(say("Still a cat."))
    http.post("/v1/chat", json={**APP, "message": "and its colour?"}, headers=AUTH)
    earlier = [m for m in backend.calls[-1][0] if m["role"] == "user"][0]
    assert "(Attached: photo.png)" in earlier["content"] and "images" not in earlier


def test_documents_reach_a_model_that_reads_no_pictures_as_text(chat):
    http, backend = chat(vision=False)
    backend.rounds.append(say("Noted."))
    response = http.post("/v1/chat", json={**APP, "message": "", "attachments": [attach("notes.txt", b"buy milk")]},
                         headers=AUTH)
    assert response.status_code == 200, response.text
    user = backend.calls[-1][0][-1]
    assert "buy milk" in user["content"] and "images" not in user


def test_a_message_needs_text_or_a_file(chat):
    http, _ = chat(vision=True)
    assert http.post("/v1/chat", json={**APP, "message": "  "}, headers=AUTH).status_code == 422


def test_a_file_must_be_base64_and_readable(chat):
    http, backend = chat(vision=True)
    bad_base64 = {**APP, "message": "x", "attachments": [{"name": "a.png", "data": "not base64!"}]}
    assert http.post("/v1/chat", json=bad_base64, headers=AUTH).status_code == 422
    unreadable = {**APP, "message": "x", "attachments": [attach("tool.bin", b"\x7fELF\x00\x00")]}
    response = http.post("/v1/chat", json=unreadable, headers=AUTH)
    assert response.status_code == 422 and "tool.bin cannot be read" in response.text
    assert backend.calls == []


# --- what the model backends and the traffic log get ----------------------------------------------------------


def test_ollama_gets_the_bytes_of_a_picture_and_no_other_message_has_any():
    messages = [
        {"role": "user", "content": "hi", "images": [{"name": "a.png", "mime": "image/png", "data": b64(PNG)}]},
        {"role": "assistant", "content": "x", "thinking": "hmm"},
    ]
    plain = OllamaBackend._plain(messages)
    assert plain[0] == {"role": "user", "content": "hi", "images": [PNG]}
    assert plain[1] == {"role": "assistant", "content": "x"}


def test_an_openai_style_service_gets_each_picture_as_a_data_url():
    backend = OpenAIBackend("gemini-2.5-flash", host="https://example.test/v1", api_key=None)
    messages = [{"role": "user", "content": "hi", "images": [{"name": "a.png", "mime": "image/png", "data": b64(PNG)}]}]
    assert backend.convert(messages) == [{
        "role": "user",
        "content": [
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64(PNG)}"}},
        ],
    }]


def test_the_traffic_log_names_a_picture_and_does_not_write_its_bytes():
    logged = without_pictures([{"role": "user", "content": "hi", "images": [{"name": "a.png", "mime": "image/png", "data": b64(PNG)}]}])
    assert logged == [{"role": "user", "content": "hi", "images": ["a.png (image/png)"]}]
    assert b64(PNG) not in repr(logged)


# --- animations: the model sees some frames, spread over the animation --------------------------------------


def test_an_animated_gif_gives_the_model_frames_spread_over_it():
    prepared = prepare("what happens?", [Attached("anim.gif", encoded(frames=20, fmt="GIF"))])
    assert prepared.message == "(Attached: anim.gif (20 frames, 6 shown in order))\n\nwhat happens?"
    assert len(prepared.images) == 6
    assert all(image.mime == "image/png" and image.name == "anim.gif" for image in prepared.images)


def test_a_still_gif_is_shown_as_it_is():
    data = encoded(frames=1, fmt="GIF")
    prepared = prepare("", [Attached("still.gif", data)])
    assert prepared.message == "(Attached: still.gif)"
    assert prepared.images == (Image("still.gif", "image/gif", base64.b64encode(data).decode()),)


def test_frames_are_shrunk_and_flattened_on_white():
    prepared = prepare("", [Attached("wide.gif", encoded(frames=2, size=(1600, 900), fmt="GIF"))])
    frame = Raster.open(BytesIO(base64.b64decode(prepared.images[0].data)))
    assert max(frame.size) <= 768 and frame.mode == "RGB"


def test_an_animation_may_be_bigger_than_a_still_picture_but_not_than_its_own_limit():
    bigger = encoded(frames=2, fmt="GIF") + b"\x00" * MAX_PICTURE_BYTES  # trailing bytes: the size is what counts
    assert len(bigger) > MAX_PICTURE_BYTES
    assert len(prepare("", [Attached("big.gif", bigger)]).images) == 2
    with pytest.raises(FileError, match="too big"):
        prepare("", [Attached("huge.gif", b"GIF89a" + b"0" * MAX_ANIMATION_BYTES)])


def test_a_picture_that_cannot_be_decoded_is_refused_with_the_reason():
    with pytest.raises(FileError, match="cannot be read"):
        prepare("", [Attached("broken.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)])


def test_a_message_carries_at_most_twelve_pictures_counting_the_frames_of_animations():
    with pytest.raises(FileError, match="at most 12 pictures"):
        prepare("", [Attached("one.gif", encoded(frames=20, fmt="GIF")), Attached("two.gif", encoded(frames=20, fmt="GIF")),
                     Attached("a.png", PNG)])  # 6 + 6 + 1 pictures
