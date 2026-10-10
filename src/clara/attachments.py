"""The files a chat message carries: pictures the model looks at, and documents read here (text, code, PDF, Word).

The web site and the Discord bot send a message's files alike: `attachments` of a chat request, each one a name, a
MIME type (a hint only: the bytes decide) and the bytes in base64. `prepare` turns them into what the model gets: the
message, with each document in it the way the web site puts documents (`<document>` blocks), and the pictures, kept
apart as base64 for the model to look at. An animated GIF or WebP gives the model a few of its frames (spread over
the animation), since a single picture cannot show what moves.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO

from PIL import Image as Raster

from . import ingest
from .ingest import ExtractedFile, IngestError

MAX_ATTACHMENTS = 4  # files one message carries
MAX_PICTURES = 12  # pictures the model is shown with one message (an animation gives several)
MAX_PICTURE_BYTES = 8_000_000  # a still picture
MAX_ANIMATION_BYTES = 30_000_000  # an animated GIF or WebP: only some of its frames go to the model
FRAMES_SHOWN = 6  # the frames of an animation the model sees, spread over it (the first and the last among them)
FRAME_SIDE = 768  # a frame is shrunk to this many pixels on its longest side
MAX_DOCUMENT_BYTES = 30_000_000  # a PDF, a Word document or a text file
MAX_DOCUMENT_CHARS = 150_000  # the text of all the documents of one message (the web site's limit too)
MAX_NAME = 200
IMAGE_TOKENS = 1_200  # what a picture is counted as in a prompt (an estimate: providers do not say)
ANIMATED = ("image/gif", "image/webp")  # the types that can hold an animation


class FileError(ValueError):
    """A file that cannot be used: the message says which one, and why."""


@dataclass(frozen=True)
class Attached:
    name: str
    data: bytes


@dataclass(frozen=True)
class Image:
    name: str
    mime: str
    data: str  # base64, as the model takes it


@dataclass(frozen=True)
class Prepared:
    message: str  # the message as the model reads it
    images: tuple[Image, ...] = ()


def image_type(data: bytes) -> str | None:
    """The MIME type of a picture the models read, from its first bytes (None: it is not one)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _name(name: str) -> str:
    """A file's name as the message shows it: one line, short, no quotes (it sits in an attribute)."""
    return " ".join(name.split())[:MAX_NAME].replace('"', "'") or "file"


def _spread(count: int, shown: int) -> list[int]:
    """`shown` frame numbers spread over `count` frames, the first and the last among them."""
    if count <= shown:
        return list(range(count))
    return sorted({round(number * (count - 1) / (shown - 1)) for number in range(shown)})


def picture_frames(data: bytes) -> tuple[list[tuple[str, bytes]], int]:
    """The pictures the model is shown for one picture file (each as its MIME type and bytes), and how many frames
    the file has. A still picture is shown as it is; an animation as some of its frames (PNG, on white)."""
    mime = image_type(data) or ""
    with Raster.open(BytesIO(data)) as picture:
        count = getattr(picture, "n_frames", 1)
        if count <= 1:
            return [(mime, data)], 1
        frames: list[tuple[str, bytes]] = []
        for index in _spread(count, FRAMES_SHOWN):
            picture.seek(index)  # ascending: the frames are decoded in order
            frame = picture.convert("RGBA")
            flat = Raster.new("RGB", frame.size, (255, 255, 255))
            flat.paste(frame, mask=frame.getchannel("A"))
            flat.thumbnail((FRAME_SIDE, FRAME_SIDE))
            buffer = BytesIO()
            flat.save(buffer, format="PNG")
            frames.append(("image/png", buffer.getvalue()))
        return frames, count


def document_block(file: ExtractedFile) -> str:
    """A document as the web site puts it in a message: its text in a `<document>` block (code in a fence)."""
    kind = file.kind or "text"
    if file.kind in ("pdf", "docx"):
        body = file.text
    else:
        longest = max((len(run) for run in re.findall(r"`+", file.text)), default=0)
        fence = "`" * max(3, longest + 1)  # longer than any run in the text: it cannot be closed early
        body = f"{fence}{file.kind}\n{file.text.rstrip()}\n{fence}"
    return f'<document name="{_name(file.path)}" type="{kind}">\n{body}\n</document>'


def prepare(message: str, files: Sequence[Attached]) -> Prepared:
    """What the model gets for a message and its files: the pictures, and the message with the documents in it.
    FileError says which file cannot be used, and why."""
    if len(files) > MAX_ATTACHMENTS:
        raise FileError(f"A message carries at most {MAX_ATTACHMENTS} files.")
    images: list[Image] = []
    labels: list[str] = []  # how the message names the pictures
    documents: list[ExtractedFile] = []
    for file in files:
        shown = _name(file.name)
        mime = image_type(file.data)
        if mime is not None:
            limit = MAX_ANIMATION_BYTES if mime in ANIMATED else MAX_PICTURE_BYTES
            if len(file.data) > limit:
                raise FileError(f"{shown} is too big: a picture may be {limit // 1_000_000} MB at most.")
            try:
                frames, count = picture_frames(file.data)
            except Exception as error:  # Pillow's errors: a picture it cannot decode is refused, with the reason
                raise FileError(f"{shown} cannot be read ({error}).") from None
            labels.append(shown if count == 1 else f"{shown} ({count} frames, {len(frames)} shown in order)")
            images.extend(Image(shown, frame_mime, base64.b64encode(frame).decode()) for frame_mime, frame in frames)
            continue
        if len(file.data) > MAX_DOCUMENT_BYTES:
            raise FileError(f"{shown} is too big: a document may be {MAX_DOCUMENT_BYTES // 1_000_000} MB at most.")
        try:
            documents.append(ingest.extract(file.name, file.data))
        except IngestError as error:
            raise FileError(
                f"{shown} cannot be read ({error}). Clara reads text, code, PDF, Word (.docx) and "
                "PNG, JPEG, GIF or WebP pictures."
            ) from None
    if len(images) > MAX_PICTURES:
        raise FileError(f"A message carries at most {MAX_PICTURES} pictures (an animation counts its frames).")
    blocks = [document_block(document) for document in documents]
    if sum(len(block) for block in blocks) > MAX_DOCUMENT_CHARS:
        raise FileError(f"The documents are too long in all: at most {MAX_DOCUMENT_CHARS:,} characters.")
    text = message.strip()
    parts: list[str] = []
    if labels:
        parts.append(f"(Attached: {', '.join(labels)})")
    if text:
        parts.append(text)
    if documents:
        names = ", ".join(_name(document.path) for document in documents)
        parts.append(f"(Attached: {names})" if text else f"Here {'is' if len(documents) == 1 else 'are'}: {names}.")
        parts.extend(blocks)
    return Prepared("\n\n".join(parts), tuple(images))
