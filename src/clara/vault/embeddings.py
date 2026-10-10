"""Semantic search: the notes are cut in passages, each passage is turned into a vector by an embedding model (Ollama's
`/api/embed`), and a question is answered with the passages whose vector is closest to the question's.

The index is a SQLite file in the server's data directory (never in the vault: it is a cache, it can be deleted).
It follows the notes by content digest and is brought up to date when somebody searches, within a time budget, so a
first search over a big vault gives partial results rather than a timeout.
"""

from __future__ import annotations

import hashlib
import logging
import math
import sqlite3
import threading
import time
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from .markdown import HEADING_RE, mask_code
from .notes import Note

log = logging.getLogger(__name__)

CHUNK_CHARS = 1200
BATCH = 16
VERSION = "1"  # bump when the way notes are cut changes: everything is indexed again

try:  # Python 3.12
    from math import sumprod as _dot
except ImportError:  # pragma: no cover
    def _dot(a: Sequence[float], b: Sequence[float]) -> float:
        return sum(x * y for x, y in zip(a, b))


class EmbeddingError(Exception):
    pass


class OllamaEmbedder:
    """Embeddings from an Ollama server (`ollama pull nomic-embed-text`)."""

    def __init__(self, host: str, model: str, timeout: float = 60.0, client: httpx.Client | None = None):
        self.host = host.rstrip("/")
        self.model = model
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=3.0))  # a dead host fails fast

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            response = self._client.post(f"{self.host}/api/embed", json={"model": self.model, "input": texts})
        except httpx.HTTPError as error:
            raise EmbeddingError(f"the embedding server is unreachable ({error.__class__.__name__})") from None
        if response.status_code == 404:
            raise EmbeddingError(f"the model {self.model!r} is not installed on the embedding server (ollama pull {self.model})")
        if response.status_code >= 400:
            raise EmbeddingError(f"the embedding server answered {response.status_code}")
        vectors = response.json().get("embeddings")
        if not vectors or len(vectors) != len(texts):
            raise EmbeddingError("the embedding server returned no vectors")
        return vectors

    def close(self) -> None:
        self._client.close()


Embed = Callable[[list[str]], list[list[float]]]


@dataclass(frozen=True)
class Chunk:
    heading: str
    line: int
    text: str  # what is shown
    embedded: str  # what is embedded: the text with its title and heading in front


@dataclass(frozen=True)
class Hit:
    path: str
    score: float
    heading: str
    line: int
    text: str


@dataclass(frozen=True)
class SyncResult:
    indexed: int  # notes embedded now
    pending: int  # notes still to embed
    removed: int
    error: str = ""


def chunks_of(note: Note, limit: int = CHUNK_CHARS) -> list[Chunk]:
    """The passages of a note: its sections, cut at paragraphs, each led by the note's title and heading."""
    masked = mask_code(note.body).split("\n")
    lines = note.body.split("\n")
    sections: list[tuple[str, int, list[str]]] = [("", note.body_line, [])]
    for number, line in enumerate(lines):
        match = HEADING_RE.match(masked[number]) if number < len(masked) else None
        if match:
            sections.append((HEADING_RE.match(line).group(2), note.body_line + number, []))
        else:
            sections[-1][2].append(line)
    chunks: list[Chunk] = []
    meta = f"{note.type}. " if note.type else ""
    lead = f"{note.title}. {meta}{' '.join('#' + tag for tag in note.tags[:6])}".strip()
    for heading, line, content in sections:
        paragraphs = [p.strip() for p in "\n".join(content).split("\n\n") if p.strip()]
        current = ""
        for paragraph in paragraphs:
            if current and len(current) + len(paragraph) + 2 > limit:
                chunks.append(_chunk(lead, heading, line, current))
                current = ""
            while len(paragraph) > limit:
                chunks.append(_chunk(lead, heading, line, paragraph[:limit]))
                paragraph = paragraph[limit:]
            current = f"{current}\n\n{paragraph}".strip()
        if current:
            chunks.append(_chunk(lead, heading, line, current))
    if not chunks:
        chunks.append(_chunk(lead, "", note.body_line, ""))
    return chunks


def _chunk(lead: str, heading: str, line: int, text: str) -> Chunk:
    where = f"{lead} > {heading}" if heading else lead
    return Chunk(heading, line, text, f"{where}\n{text}".strip())


def _unit(vector: Sequence[float]) -> array:
    norm = math.sqrt(_dot(vector, vector)) or 1.0
    return array("f", (value / norm for value in vector))


class EmbeddingIndex:
    def __init__(self, path: Path, embed: Embed, model: str, clock: Callable[[], float] = time.monotonic):
        self.model = model
        self._embed = embed
        self._clock = clock
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS indexed (path TEXT PRIMARY KEY, digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chunks (
                path TEXT NOT NULL, idx INTEGER NOT NULL, heading TEXT NOT NULL, line INTEGER NOT NULL,
                text TEXT NOT NULL, vector BLOB NOT NULL, PRIMARY KEY (path, idx)
            );
            """
        )

    def _digest(self, note: Note) -> str:
        return hashlib.sha1(f"{VERSION}|{self.model}|{note.text}".encode()).hexdigest()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def stats(self) -> tuple[int, int]:
        """(notes indexed, passages)"""
        with self._lock:
            notes = self._db.execute("SELECT COUNT(*) FROM indexed").fetchone()[0]
            passages = self._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return notes, passages

    def clear(self) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM indexed")
            self._db.execute("DELETE FROM chunks")

    def pending(self, notes: Sequence[Note]) -> list[Note]:
        with self._lock:
            known = dict(self._db.execute("SELECT path, digest FROM indexed").fetchall())
        return [note for note in notes if known.get(note.path) != self._digest(note)]

    def sync(self, notes: Sequence[Note], budget: float = 25.0) -> SyncResult:
        """Embed the notes that are new or changed (as many as the time budget allows) and forget those that are
        gone. A failing embedding server stops the work with its message; what is done stays."""
        with self._lock:
            paths = {note.path for note in notes}
            known = dict(self._db.execute("SELECT path, digest FROM indexed").fetchall())
            gone = [path for path in known if path not in paths]
            with self._db:
                for path in gone:
                    self._db.execute("DELETE FROM indexed WHERE path = ?", (path,))
                    self._db.execute("DELETE FROM chunks WHERE path = ?", (path,))
            todo = [note for note in notes if known.get(note.path) != self._digest(note)]
            started = self._clock()
            done = 0
            error = ""
            for note in todo:
                if self._clock() - started > budget:
                    break
                try:
                    self._index(note)
                except EmbeddingError as problem:
                    error = str(problem)
                    break
                done += 1
            return SyncResult(done, len(todo) - done, len(gone), error)

    def _index(self, note: Note) -> None:
        chunks = chunks_of(note)
        vectors: list[list[float]] = []
        for start in range(0, len(chunks), BATCH):
            vectors += self._embed([chunk.embedded for chunk in chunks[start : start + BATCH]])
        with self._db:
            self._db.execute("DELETE FROM chunks WHERE path = ?", (note.path,))
            self._db.executemany(
                "INSERT INTO chunks (path, idx, heading, line, text, vector) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (note.path, number, chunk.heading, chunk.line, chunk.text, _unit(vector).tobytes())
                    for number, (chunk, vector) in enumerate(zip(chunks, vectors))
                ],
            )
            self._db.execute(
                "INSERT INTO indexed (path, digest) VALUES (?, ?) ON CONFLICT (path) DO UPDATE SET digest = excluded.digest",
                (note.path, self._digest(note)),
            )

    def search(self, query: str, limit: int = 10, allowed: set[str] | None = None) -> list[Hit]:
        """The best passage of each note, the closest first. Raises EmbeddingError if the question cannot be embedded."""
        vector = _unit(self._embed([query])[0])
        best: dict[str, Hit] = {}
        with self._lock:
            rows = self._db.execute("SELECT path, heading, line, text, vector FROM chunks").fetchall()
        for path, heading, line, text, blob in rows:
            if allowed is not None and path not in allowed:
                continue
            stored = array("f")
            stored.frombytes(blob)
            if len(stored) != len(vector):
                continue  # made by another model
            score = _dot(vector, stored)
            if path not in best or score > best[path].score:
                best[path] = Hit(path, score, heading, line, text)
        return sorted(best.values(), key=lambda hit: -hit.score)[:limit]

    def vector_of(self, path: str) -> list[array]:
        with self._lock:
            rows = self._db.execute("SELECT vector FROM chunks WHERE path = ?", (path,)).fetchall()
        out = []
        for (blob,) in rows:
            vector = array("f")
            vector.frombytes(blob)
            out.append(vector)
        return out
