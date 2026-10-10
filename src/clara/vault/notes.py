"""A parsed note, and the lexical (BM25) search over a set of notes."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .markdown import (
    Heading,
    Link,
    Task,
    aliases_of,
    find_headings,
    find_links,
    find_tags,
    find_tasks,
    fold,
    parse_properties,
    split_frontmatter,
    tags_of,
)

WORD_RE = re.compile(r"\w+", re.UNICODE)


def words(text: str) -> list[str]:
    """The words of a text as compared by the search: no accents, no case."""
    return WORD_RE.findall(fold(text))


@dataclass
class Note:
    path: str  # "5-knowledge/Zettelkasten.md", always with "/"
    mtime: float
    size: int
    text: str  # the whole file
    props: dict[str, Any]
    body: str
    body_line: int
    title: str
    tags: list[str]
    aliases: list[str]
    links: list[Link]
    headings: list[Heading]
    tasks: list[Task]
    has_front: bool = False
    _words: Counter = field(default_factory=Counter, repr=False)

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1][: -len(".md")]

    @property
    def folder(self) -> str:
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""

    @property
    def type(self) -> str:
        return str(self.props.get("type") or "").strip().lower()

    @property
    def status(self) -> str:
        return str(self.props.get("status") or "").strip().lower()

    @property
    def summary(self) -> str:
        return str(self.props.get("summary") or "").strip()

    def prop_text(self, key: str) -> str:
        value = self.props.get(key)
        return "" if value is None or isinstance(value, (list, dict)) else str(value)


def parse_note(path: str, text: str, mtime: float) -> Note:
    parts = split_frontmatter(text)
    props = parse_properties(parts.front)
    body = parts.body
    headings = find_headings(body, parts.body_line)
    name = path.rsplit("/", 1)[-1][: -len(".md")]
    first = next((heading for heading in headings if heading.level == 1), None)
    tags = list(dict.fromkeys(tags_of(props) + find_tags(body)))
    return Note(
        path, mtime, len(text), text, props, body, parts.body_line, first.text if first else name, tags,
        aliases_of(props), find_links(body, parts.body_line), headings, find_tasks(body, parts.body_line),
        has_front=parts.front is not None,
    )


# --- BM25 ----------------------------------------------------------------------------------------------

K1 = 1.4
B = 0.75


def weighted_words(note: Note) -> Counter:
    """Words of a note with their weight: those of the name and title count most, then tags and headings."""
    counts: Counter = Counter()
    for word in words(note.name) + words(note.title):
        counts[word] += 4
    for item in [*note.tags, *note.aliases]:
        for word in words(item):
            counts[word] += 3
    for heading in note.headings:
        for word in words(heading.text):
            counts[word] += 2
    for word in words(note.body):
        counts[word] += 1
    return counts


class Bm25:
    def __init__(self, notes: list[Note]):
        self.docs: dict[str, Counter] = {}
        self.length: dict[str, int] = {}
        self.postings: dict[str, dict[str, int]] = {}
        for note in notes:
            counts = weighted_words(note)
            self.docs[note.path] = counts
            self.length[note.path] = sum(counts.values()) or 1
            for word, count in counts.items():
                self.postings.setdefault(word, {})[note.path] = count
        self.average = (sum(self.length.values()) / len(self.length)) if self.length else 1.0

    def expand(self, word: str) -> list[tuple[str, float]]:
        """The indexed words a query word stands for: itself, and (from 4 letters) the words that begin with it."""
        found = [(word, 1.0)] if word in self.postings else []
        if len(word) >= 4:
            stem = word[:-1] if word.endswith("s") else word
            found += [(term, 0.6) for term in self.postings if term != word and term.startswith(stem)][:20]
        return found

    def search(self, query: str, allowed: set[str] | None = None) -> list[tuple[str, float]]:
        terms = list(dict.fromkeys(words(query)))
        if not terms:
            return []
        total = max(len(self.docs), 1)
        scores: Counter = Counter()
        matched: dict[str, set[str]] = {}
        for term in terms:
            for indexed, weight in self.expand(term):
                posting = self.postings[indexed]
                idf = math.log(1 + (total - len(posting) + 0.5) / (len(posting) + 0.5))
                for path, count in posting.items():
                    if allowed is not None and path not in allowed:
                        continue
                    norm = count * (K1 + 1) / (count + K1 * (1 - B + B * self.length[path] / self.average))
                    scores[path] += idf * norm * weight
                    matched.setdefault(path, set()).add(term)
        # a note that holds every word of the query is worth more than one holding a single rare word
        for path, found in matched.items():
            scores[path] *= 1 + len(found) / len(terms)
        return scores.most_common()


def snippets(note: Note, query: str, limit: int = 2, width: int = 160) -> list[tuple[int, str]]:
    """The lines of the note (with their number) that hold the most words of the query."""
    terms = set(words(query))
    phrase = fold(" ".join(query.split()))
    scored: list[tuple[float, int, str]] = []
    for number, line in enumerate(note.text.split("\n"), 1):
        if number < note.body_line or not line.strip():
            continue
        folded = fold(line)
        hits = sum(1 for term in terms if term in folded)
        if hits or (phrase and phrase in folded):
            bonus = 2 if phrase and phrase in folded else 0
            scored.append((hits + bonus, number, line))
    scored.sort(key=lambda item: (-item[0], item[1]))
    out = []
    for _, number, line in sorted(scored[:limit], key=lambda item: item[1]):
        line = line.strip()
        out.append((number, line if len(line) <= width else line[: width - 1] + "…"))
    return out
