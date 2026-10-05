"""Markdown files Clara writes for a person: she creates one when asked for a document (notes, a summary, a
README...), and changes it later when the person wants it different.

A file belongs to one person and is the same on every surface of theirs. It has a name (`notes.md`, no folder)
and a text. The model works on them with tools (tools.py) and the clients show them (markdownapi.py, the web
site's Files page): they are not sent in a prompt, Clara reads one when she needs it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .memory import Memory

MAX_NAME = 80
MAX_CHARS = 200_000  # characters of one file
MAX_FILES = 200  # files of one person
READ_MAX_CHARS = 40_000  # what read_markdown_file gives at once
EXTENSION = ".md"
SURFACES = frozenset({"web"})  # the clients that show a file Clara just wrote (the others point to the web site)
NAME_RE = re.compile(r"^[^\W_](?:[\w .()\-]*[\w)\-])?$")  # letters, digits, spaces, "_.()-"; not starting with a symbol


class MarkdownError(ValueError):
    """A request about a file that cannot be done: the message says why (it is given back to the model)."""


@dataclass(frozen=True)
class MarkdownFile:
    id: int
    name: str
    size: int  # characters
    created_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_name(name: str) -> str:
    """The file name as stored: `.md` added when missing, and checked."""
    name = " ".join(str(name or "").split())
    if not name:
        raise MarkdownError("Give the file a name, e.g. notes.md.")
    if not name.lower().endswith(EXTENSION):
        name += EXTENSION
    stem = name[: -len(EXTENSION)]
    if len(name) > MAX_NAME:
        raise MarkdownError(f"A file name has at most {MAX_NAME} characters.")
    if not NAME_RE.match(stem):
        raise MarkdownError(
            "A file name has letters, digits, spaces and . _ - ( ) only, with no folder, e.g. meeting-notes.md."
        )
    return name


class MarkdownFiles:
    def __init__(self, memory: Memory, max_chars: int = MAX_CHARS, max_files: int = MAX_FILES):
        self._db = memory.database
        self._lock = memory.lock
        self.max_chars = max_chars
        self.max_files = max_files

    @staticmethod
    def _file(row) -> MarkdownFile:
        return MarkdownFile(row["id"], row["name"], row["size"], row["created_at"], row["updated_at"])

    _COLUMNS = "SELECT id, name, length(content) AS size, created_at, updated_at FROM markdown_files"

    def of(self, person_id: int) -> list[MarkdownFile]:
        """A person's files, the last changed first."""
        with self._lock:
            rows = self._db.execute(
                self._COLUMNS + " WHERE person_id = ? ORDER BY updated_at DESC, id DESC", (person_id,)
            ).fetchall()
        return [self._file(row) for row in rows]

    def find(self, person_id: int, name: str) -> MarkdownFile | None:
        try:
            name = clean_name(name)
        except MarkdownError:
            return None
        with self._lock:
            row = self._db.execute(self._COLUMNS + " WHERE person_id = ? AND name = ?", (person_id, name)).fetchone()
        return self._file(row) if row else None

    def get(self, person_id: int, file_id: int) -> tuple[MarkdownFile, str] | None:
        """A file of this person and its text, or None."""
        with self._lock:
            row = self._db.execute(
                "SELECT id, name, length(content) AS size, created_at, updated_at, content FROM markdown_files"
                " WHERE person_id = ? AND id = ?",
                (person_id, file_id),
            ).fetchone()
        return (self._file(row), row["content"]) if row else None

    def read(self, person_id: int, name: str) -> tuple[MarkdownFile, str]:
        name = clean_name(name)
        with self._lock:
            row = self._db.execute(
                "SELECT id, name, length(content) AS size, created_at, updated_at, content FROM markdown_files"
                " WHERE person_id = ? AND name = ?",
                (person_id, name),
            ).fetchone()
        if row is None:
            raise MarkdownError(f"No file called {name} (list_markdown_files gives their names).")
        return self._file(row), row["content"]

    def _checked_content(self, content: str) -> str:
        content = str(content if content is not None else "").replace("\r\n", "\n")
        if not content.strip():
            raise MarkdownError("The content is empty.")
        if len(content) > self.max_chars:
            raise MarkdownError(f"A file has at most {self.max_chars:,} characters, this one has {len(content):,}.")
        return content if content.endswith("\n") else content + "\n"

    def create(self, person_id: int, name: str, content: str, overwrite: bool = False) -> tuple[MarkdownFile, bool]:
        """Make a file, or (`overwrite`) replace the text of the one with that name. Returns it and whether it was
        new."""
        name = clean_name(name)
        content = self._checked_content(content)
        now = _now()
        with self._lock, self._db:
            old = self._db.execute(
                "SELECT id FROM markdown_files WHERE person_id = ? AND name = ?", (person_id, name)
            ).fetchone()
            if old and not overwrite:
                raise MarkdownError(
                    f"{name} already exists: change it with edit_markdown_file, or give overwrite=true to replace "
                    "all of its text."
                )
            if old:
                self._db.execute(
                    "UPDATE markdown_files SET content = ?, updated_at = ? WHERE id = ?", (content, now, old["id"])
                )
            else:
                count = self._db.execute(
                    "SELECT COUNT(*) FROM markdown_files WHERE person_id = ?", (person_id,)
                ).fetchone()[0]
                if count >= self.max_files:
                    raise MarkdownError(f"This person already has {self.max_files} files: delete some first.")
                self._db.execute(
                    "INSERT INTO markdown_files (person_id, name, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (person_id, name, content, now, now),
                )
        found = self.find(person_id, name)
        assert found is not None
        return found, old is None

    def edit(
        self, person_id: int, name: str, old: str, new: str, replace_all: bool = False
    ) -> tuple[MarkdownFile, int]:
        """Replace a passage of a file (`old`, exactly as it is written there) by `new`. Without `replace_all`
        the passage must be there once, so that the model cannot change the wrong place. Returns the file and
        how many places changed."""
        name = clean_name(name)
        old = str(old if old is not None else "").replace("\r\n", "\n")
        new = str(new if new is not None else "").replace("\r\n", "\n")
        if not old:
            raise MarkdownError("old_text is empty: give the passage to replace, exactly as it is in the file.")
        if old == new:
            raise MarkdownError("old_text and new_text are the same: nothing would change.")
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT id, content FROM markdown_files WHERE person_id = ? AND name = ?", (person_id, name)
            ).fetchone()
            if row is None:
                raise MarkdownError(f"No file called {name} (list_markdown_files gives their names).")
            text = row["content"]
            found = text.count(old)
            if found == 0:
                raise MarkdownError(
                    "old_text is not in the file: read it again with read_markdown_file and copy the passage exactly "
                    "(spaces and line breaks included)."
                )
            if found > 1 and not replace_all:
                raise MarkdownError(
                    f"old_text is in the file {found} times: add some of the lines around it to make it unique, or "
                    "give replace_all=true to change every one."
                )
            changed = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            if not changed.strip():
                raise MarkdownError("The file would be empty: delete it instead.")
            if len(changed) > self.max_chars:
                raise MarkdownError(f"A file has at most {self.max_chars:,} characters, this one would have {len(changed):,}.")
            self._db.execute(
                "UPDATE markdown_files SET content = ?, updated_at = ? WHERE id = ?", (changed, _now(), row["id"])
            )
        updated = self.find(person_id, name)
        assert updated is not None
        return updated, found if replace_all else 1

    def append(self, person_id: int, name: str, text: str) -> MarkdownFile:
        """Add text right after the last line of a file (it starts with a blank line to make a new paragraph)."""
        name = clean_name(name)
        addition = str(text if text is not None else "").replace("\r\n", "\n")
        if not addition.strip():
            raise MarkdownError("There is nothing to add.")
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT id, content FROM markdown_files WHERE person_id = ? AND name = ?", (person_id, name)
            ).fetchone()
            if row is None:
                raise MarkdownError(f"No file called {name} (list_markdown_files gives their names).")
            changed = row["content"] + addition  # the stored text always ends with a line break
            changed = changed if changed.endswith("\n") else changed + "\n"
            if len(changed) > self.max_chars:
                raise MarkdownError(f"A file has at most {self.max_chars:,} characters, this one would have {len(changed):,}.")
            self._db.execute(
                "UPDATE markdown_files SET content = ?, updated_at = ? WHERE id = ?", (changed, _now(), row["id"])
            )
        updated = self.find(person_id, name)
        assert updated is not None
        return updated

    def delete(self, person_id: int, file_id: int) -> bool:
        with self._lock, self._db:
            return self._db.execute(
                "DELETE FROM markdown_files WHERE person_id = ? AND id = ?", (person_id, file_id)
            ).rowcount > 0
