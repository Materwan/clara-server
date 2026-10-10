"""The shape of the vault: which kinds of note exist and where each one lives.

The defaults below are the layout described in the vault's own README. A vault can change folder names, statuses and
descriptions in `_clara/schema.json` (the file shipped with the vault holds exactly these defaults).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

from .errors import VaultError

log = logging.getLogger(__name__)

SCHEMA_FILE = "_clara/schema.json"
TEMPLATES = "_templates"
TRASH = ".trash"
ATTACHMENTS = "_attachments"
CLARA = "_clara"
HIDDEN_DIRS = frozenset({".git", ".obsidian", TRASH, ".github", "node_modules"})
DAILY_FORMAT = "YYYY/YYYY-MM-DD"


@dataclass(frozen=True)
class NoteType:
    name: str
    folder: str  # where its notes live ("" : it depends, see `hub`)
    description: str
    statuses: tuple[str, ...] = ()
    default_status: str = ""
    template: str = ""  # file name (without .md) in _templates; defaults to the type's name
    hub: bool = False  # a project or an area: a folder with a note of its own name that holds the others
    sub_folders: bool = True  # may notes of this type be put in a sub folder (5-knowledge/programming)
    required: tuple[str, ...] = ()  # properties a note of this type must have besides type/created/updated

    @property
    def template_name(self) -> str:
        return self.template or self.name


DEFAULT_TYPES: tuple[NoteType, ...] = (
    NoteType(
        "inbox", "0-inbox", "Raw capture, not sorted yet: a thought, a link, a task, a voice memo. Process it later.",
        ("new", "processed"), "new", sub_folders=False,
    ),
    NoteType(
        "daily", "1-daily", "One note per day (1-daily/YYYY/YYYY-MM-DD): log, tasks, what happened.", (), "",
        sub_folders=False,
    ),
    NoteType(
        "idea", "2-ideas", "A draft idea or seed that may grow into a project or knowledge.",
        ("seed", "growing", "mature", "shelved", "done"), "seed",
    ),
    NoteType(
        "project", "3-projects", "Something with an outcome and an end: it has its own folder and a hub note.",
        ("planned", "active", "paused", "done", "cancelled"), "active", hub=True, sub_folders=False,
    ),
    NoteType(
        "area", "4-areas", "An ongoing responsibility with no end (health, money, home): a folder and a hub note.",
        ("active", "paused"), "active", hub=True, sub_folders=False,
    ),
    NoteType(
        "knowledge", "5-knowledge", "What is known: atomic, evergreen notes in your own words, linked together.",
        ("draft", "evergreen"), "draft",
    ),
    NoteType(
        "source", "6-sources", "Where knowledge comes from: a book, article, video, paper, conversation.",
        ("to-read", "reading", "done"), "to-read", required=("source_type",),
    ),
    NoteType("person", "7-people", "A person you know or follow: who they are, context, last contacts.", (), ""),
    NoteType(
        "note", "", "A working note that belongs to a project or an area (meeting, decision, spec, log): needs `parent`.",
        ("draft", "final"), "draft", sub_folders=True,
    ),
    NoteType(
        "map", "", "An index or map of content that links the notes of a folder or a theme.", ("active",), "active",
    ),
)

ARCHIVE = "8-archive"


@dataclass(frozen=True)
class Schema:
    types: dict[str, NoteType] = field(default_factory=lambda: {t.name: t for t in DEFAULT_TYPES})
    archive: str = ARCHIVE
    daily_format: str = DAILY_FORMAT

    def get(self, name: str) -> NoteType:
        found = self.types.get(name.strip().lower())
        if found is None:
            raise VaultError(f"Unknown note type {name!r}. Types: {', '.join(self.types)}.")
        return found

    @property
    def folders(self) -> dict[str, str]:
        """top folder -> the type it holds"""
        return {t.folder: t.name for t in self.types.values() if t.folder}

    def type_of_folder(self, path: str) -> str | None:
        top = path.split("/", 1)[0]
        return self.folders.get(top)

    @property
    def inbox(self) -> str:
        return self.types["inbox"].folder

    @property
    def daily_folder(self) -> str:
        return self.types["daily"].folder


def load_schema(root: Path) -> Schema:
    """The default schema, with the changes of `_clara/schema.json` when the vault has one (a broken file is
    ignored with a warning: Clara keeps working with the defaults)."""
    schema = Schema()
    path = root / SCHEMA_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return schema
    except (OSError, ValueError):
        log.warning("vault: %s cannot be read, using the default layout", SCHEMA_FILE)
        return schema
    types = dict(schema.types)
    for name, change in (data.get("types") or {}).items():
        base = types.get(name, NoteType(name, "", ""))
        try:
            types[name] = replace(
                base,
                folder=str(change.get("folder", base.folder)),
                description=str(change.get("description", base.description)),
                statuses=tuple(change.get("statuses", base.statuses)),
                default_status=str(change.get("default_status", base.default_status)),
                template=str(change.get("template", base.template)),
                hub=bool(change.get("hub", base.hub)),
                sub_folders=bool(change.get("sub_folders", base.sub_folders)),
                required=tuple(change.get("required", base.required)),
            )
        except (AttributeError, TypeError):
            log.warning("vault: the type %r of %s is malformed, ignored", name, SCHEMA_FILE)
    return Schema(types, str(data.get("archive", schema.archive)), str(data.get("daily_format", schema.daily_format)))


def schema_json(schema: Schema | None = None) -> str:
    """The schema as the JSON file a vault ships (the defaults, when no schema is given)."""
    schema = schema or Schema()
    types = {}
    for t in schema.types.values():
        types[t.name] = {
            "folder": t.folder, "description": t.description, "statuses": list(t.statuses),
            "default_status": t.default_status, "template": t.template_name, "hub": t.hub,
            "sub_folders": t.sub_folders, "required": list(t.required),
        }
    return json.dumps({"archive": schema.archive, "daily_format": schema.daily_format, "types": types}, indent=2,
                      ensure_ascii=False) + "\n"
