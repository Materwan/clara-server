"""The vault_* tools: how Clara reads and writes the second brain (see vault/vault.py).

They are offered only to the people allowed to use the vault (settings `CLARA_VAULT_OWNERS`), and not in group
conversations, where what a tool returns would be shown to everybody present. Git and embeddings can take seconds,
so every tool runs in a thread and does not hold up the server.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .tools import Tool, ToolContext, _flag, _whole_number
from .vault import Vault

LIST_LIMIT = {"type": "integer", "description": "How many to show (default 10-50, at most 200)."}
NOTE = {
    "type": "string",
    "description": "The note: its path (5-knowledge/Idea.md), its name (Idea) or a link ([[Idea]]).",
}
TYPES_HELP = "inbox, idea, project, area, knowledge, source, person, note, map"


def _vault(context: ToolContext) -> Vault:
    if context.vault is None:
        raise ValueError("The vault is not available.")
    return context.vault


def _items(value: Any) -> list[str]:
    """A list of texts: the model may send a list, or one text with commas."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError("expected a list of texts.")


def _mapping(value: Any, name: str) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except ValueError:
            loaded = None
        if isinstance(loaded, dict):
            return loaded
    raise ValueError(f"{name} must be an object like {{\"key\": \"value\"}}.")


def _count(value: Any, default: int, name: str = "limit") -> int:
    return default if value is None or value == "" else _whole_number(value, f"{name} must be a number.")


async def _in_thread(function, *args, **kwargs) -> str:
    return await asyncio.to_thread(function, *args, **kwargs)


# --- reading ----------------------------------------------------------------------------------------------


async def _overview(context: ToolContext, conventions: Any = False) -> str:
    return await _in_thread(_vault(context).overview, _flag(conventions))


async def _list(
    context: ToolContext, folder: str = "", type: str = "", status: str = "", tag: str = "", recursive: Any = True,
    sort: str = "updated", limit: Any = None,
) -> str:
    return await _in_thread(
        _vault(context).list_notes, folder, type, status, tag, recursive is not False and str(recursive).lower() != "false",
        sort, _count(limit, 50),
    )


async def _read(context: ToolContext, note: str, start_line: Any = 1) -> str:
    return await _in_thread(_vault(context).read, note, _count(start_line, 1, "start_line"))


async def _search(
    context: ToolContext, query: str, folder: str = "", type: str = "", status: str = "", tag: str = "",
    limit: Any = None,
) -> str:
    return await _in_thread(_vault(context).search, query, folder, type, status, tag, _count(limit, 10))


async def _semantic(
    context: ToolContext, query: str, folder: str = "", type: str = "", tag: str = "", limit: Any = None
) -> str:
    return await _in_thread(_vault(context).semantic_search, query, folder, type, tag, _count(limit, 8))


async def _query(
    context: ToolContext, type: str = "", status: str = "", tags: Any = None, any_tag: Any = False, folder: str = "",
    where: Any = None, text: str = "", created_after: str = "", created_before: str = "", updated_after: str = "",
    updated_before: str = "", sort: str = "updated", limit: Any = None, show: Any = None,
) -> str:
    return await _in_thread(
        _vault(context).query, type, status, _items(tags), _flag(any_tag), folder, _mapping(where, "where"), text,
        created_after, created_before, updated_after, updated_before, sort, _count(limit, 50), _items(show),
    )


async def _tags(context: ToolContext, tag: str = "", limit: Any = None) -> str:
    return await _in_thread(_vault(context).tags, tag, _count(limit, 60))


async def _properties(context: ToolContext, key: str = "") -> str:
    return await _in_thread(_vault(context).properties, key)


async def _links(context: ToolContext, note: str, unlinked: Any = False) -> str:
    return await _in_thread(_vault(context).links, note, _flag(unlinked))


async def _related(context: ToolContext, note: str, limit: Any = None) -> str:
    return await _in_thread(_vault(context).related, note, _count(limit, 10))


async def _health(context: ToolContext, limit: Any = None) -> str:
    return await _in_thread(_vault(context).health, _count(limit, 15))


async def _tasks(
    context: ToolContext, status: str = "open", folder: str = "", tag: str = "", due_before: str = "",
    text: str = "", limit: Any = None,
) -> str:
    return await _in_thread(_vault(context).tasks, status, folder, tag, due_before, text, _count(limit, 50))


async def _templates(context: ToolContext) -> str:
    return await _in_thread(_vault(context).templates)


async def _history(context: ToolContext, note: str, limit: Any = None) -> str:
    return await _in_thread(_vault(context).history, note, _count(limit, 15))


async def _daily_read(context: ToolContext, date: str = "today") -> str:
    return await _in_thread(_vault(context).daily_read, date, context.timezone)


# --- writing ----------------------------------------------------------------------------------------------


async def _create(
    context: ToolContext, title: str, type: str, content: str = "", tags: Any = None, status: str = "",
    summary: str = "", folder: str = "", parent: str = "", properties: Any = None, aliases: Any = None,
    overwrite: Any = False,
) -> str:
    return await _in_thread(
        _vault(context).create, title, type, content, tags=_items(tags), status=status, summary=summary,
        folder=folder, parent=parent, properties=_mapping(properties, "properties"), aliases=_items(aliases),
        tz=context.timezone, overwrite=_flag(overwrite),
    )


async def _capture(context: ToolContext, text: str, title: str = "", source: str = "", tags: Any = None) -> str:
    return await _in_thread(_vault(context).capture, text, title, source, _items(tags), context.timezone)


async def _edit(context: ToolContext, note: str, old_text: str, new_text: str, replace_all: Any = False) -> str:
    return await _in_thread(_vault(context).edit, note, old_text, new_text, _flag(replace_all), context.timezone)


async def _append(context: ToolContext, note: str, text: str, heading: str = "") -> str:
    return await _in_thread(_vault(context).append, note, text, heading, context.timezone)


async def _rewrite(context: ToolContext, note: str, content: str) -> str:
    return await _in_thread(_vault(context).rewrite, note, content, context.timezone)


async def _set_properties(
    context: ToolContext, note: str, set: Any = None, remove: Any = None, add_tags: Any = None,
    remove_tags: Any = None,
) -> str:
    return await _in_thread(
        _vault(context).set_properties, note, _mapping(set, "set"), _items(remove), _items(add_tags),
        _items(remove_tags), context.timezone,
    )


async def _move(context: ToolContext, note: str, to: str) -> str:
    return await _in_thread(_vault(context).move, note, to, context.timezone)


async def _delete(context: ToolContext, note: str) -> str:
    return await _in_thread(_vault(context).delete, note, context.timezone)


async def _restore(context: ToolContext, note: str, commit: str) -> str:
    return await _in_thread(_vault(context).restore, note, str(commit), context.timezone)


async def _daily_append(context: ToolContext, text: str, section: str = "Log", date: str = "today") -> str:
    return await _in_thread(_vault(context).daily_append, text, section or "Log", date, context.timezone)


async def _set_task(context: ToolContext, note: str, task: Any, done: Any = True) -> str:
    return await _in_thread(
        _vault(context).set_task, note, str(task), done is not False and str(done).lower() != "false", context.timezone
    )


async def _update_index(context: ToolContext, folder: str = "") -> str:
    return await _in_thread(_vault(context).update_index, folder, context.timezone)


async def _sync(context: ToolContext) -> str:
    return await _in_thread(_vault(context).sync)


async def _reindex(context: ToolContext, full: Any = False) -> str:
    return await _in_thread(_vault(context).reindex, _flag(full), 120.0)


def vault_tools() -> list[Tool]:
    """Offered, with a vault, to the people who may use it."""
    return [
        Tool(
            name="vault_overview",
            description=(
                "Your map of the second brain: number of notes per folder and type, inbox to sort, active projects, "
                "top tags, recent changes, sync state. Call it first when you do not know the vault yet, or to orient "
                "yourself before filing something."
            ),
            function=_overview,
            parameters={"conventions": {"type": "boolean", "description": "true: also give the written conventions."}},
        ),
        Tool(
            name="vault_list",
            description="List notes (path, type/status, last update, summary), optionally of one folder, type, status or tag.",
            function=_list,
            parameters={
                "folder": {"type": "string", "description": "Folder, e.g. 3-projects or 5-knowledge/programming."},
                "type": {"type": "string", "description": f"One of: {TYPES_HELP}, daily."},
                "status": {"type": "string"},
                "tag": {"type": "string", "description": "Tag without #; sub tags (a/b) match their parent (a)."},
                "recursive": {"type": "boolean", "description": "false: only this folder, and list its sub folders."},
                "sort": {"type": "string", "enum": ["updated", "created", "name", "oldest"]},
                "limit": LIST_LIMIT,
            },
        ),
        Tool(
            name="vault_read",
            description=(
                "Read a note, with line numbers (about 20,000 characters at once; read on with start_line). The "
                "header gives its type, status, tags and how many notes link to it. Read a note before changing it."
            ),
            function=_read,
            parameters={"note": NOTE, "start_line": {"type": "integer", "description": "First line (default 1)."}},
            required=("note",),
        ),
        Tool(
            name="vault_search",
            description=(
                "Keyword search over all notes (accents and case ignored, titles and tags weigh most): ranked notes "
                "with the matching lines. Search before creating a note, to extend an existing one instead of "
                "duplicating it, and to find what the person wrote about a subject."
            ),
            function=_search,
            parameters={
                "query": {"type": "string", "description": "Words to look for."},
                "folder": {"type": "string"},
                "type": {"type": "string"},
                "status": {"type": "string"},
                "tag": {"type": "string"},
                "limit": LIST_LIMIT,
            },
            required=("query",),
        ),
        Tool(
            name="vault_semantic_search",
            description=(
                "Search by meaning rather than words ('what did I think about learning fast' finds notes that never "
                "use those words). Use it when vault_search finds nothing, or for a vague question. It falls back to "
                "keywords when the embedding model is not available."
            ),
            function=_semantic,
            parameters={
                "query": {"type": "string", "description": "A sentence or question."},
                "folder": {"type": "string"},
                "type": {"type": "string"},
                "tag": {"type": "string"},
                "limit": LIST_LIMIT,
            },
            required=("query",),
        ),
        Tool(
            name="vault_query",
            description=(
                "Filter notes like a database (Dataview): by type, status, tags (all of them, or any), folder, "
                "properties and dates. `where` maps a property to a value: 'active', '*' (is set), '!done' (not), "
                "'>=2026-01-01', '<5', '~word' (contains); a list property matches if it holds the value. "
                "Example: all open ideas tagged #ai updated since September."
            ),
            function=_query,
            parameters={
                "type": {"type": "string"},
                "status": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "any_tag": {"type": "boolean", "description": "true: at least one tag, not all."},
                "folder": {"type": "string"},
                "where": {"type": "object", "description": "Property conditions, e.g. {\"project\": \"Clara\", \"priority\": \">=2\"}."},
                "text": {"type": "string", "description": "Notes whose text contains this."},
                "created_after": {"type": "string", "description": "YYYY-MM-DD"},
                "created_before": {"type": "string"},
                "updated_after": {"type": "string"},
                "updated_before": {"type": "string"},
                "sort": {"type": "string", "description": "updated, created, oldest, name, or a property name."},
                "limit": LIST_LIMIT,
                "show": {"type": "array", "items": {"type": "string"}, "description": "Properties to show on each line."},
            },
        ),
        Tool(
            name="vault_tags",
            description="List the tags in use with their number of notes, or the notes that carry one tag.",
            function=_tags,
            parameters={"tag": {"type": "string", "description": "Omit to list the tags."}, "limit": LIST_LIMIT},
        ),
        Tool(
            name="vault_properties",
            description=(
                "The front matter properties in use (with examples), or the values of one. Look at it before "
                "inventing a property, so that names stay consistent."
            ),
            function=_properties,
            parameters={"key": {"type": "string", "description": "A property name, to see its values."}},
        ),
        Tool(
            name="vault_links",
            description=(
                "The links of a note: the notes it points to, the broken ones, and the notes that link to it "
                "(backlinks, with the line). unlinked=true also finds notes that mention its name without linking: "
                "candidates for a link."
            ),
            function=_links,
            parameters={"note": NOTE, "unlinked": {"type": "boolean"}},
            required=("note",),
        ),
        Tool(
            name="vault_related",
            description=(
                "Notes close to a given note (linked, sharing tags or links, similar words or meaning), with the "
                "reason. Use it to find what a new note should link to."
            ),
            function=_related,
            parameters={"note": NOTE, "limit": LIST_LIMIT},
            required=("note",),
        ),
        Tool(
            name="vault_health",
            description=(
                "A check-up of the vault: broken links, notes without type or in the wrong folder, isolated notes, "
                "empty notes, old inbox items, stale projects, duplicate names. Use it for a review or when asked to "
                "tidy; fix what is clearly yours to fix and ask about the rest."
            ),
            function=_health,
            parameters={"limit": {"type": "integer", "description": "Items per problem (default 15)."}},
        ),
        Tool(
            name="vault_tasks",
            description=(
                "The checkbox tasks written in the notes ('- [ ] text 📅 2026-10-31'), by due date: open, done or "
                "all, optionally of a folder or tag, due before a date, or containing words."
            ),
            function=_tasks,
            parameters={
                "status": {"type": "string", "enum": ["open", "done", "all"]},
                "folder": {"type": "string"},
                "tag": {"type": "string"},
                "due_before": {"type": "string", "description": "YYYY-MM-DD"},
                "text": {"type": "string"},
                "limit": LIST_LIMIT,
            },
        ),
        Tool(
            name="vault_templates",
            description=(
                "The kinds of note, where each lives, its statuses and its template (the sections to write). Read "
                "it before creating a type of note you have not made yet."
            ),
            function=_templates,
            parameters={},
        ),
        Tool(
            name="vault_history",
            description="The git history of a note (commit, date, who, what): who changed it and when.",
            function=_history,
            parameters={"note": NOTE, "limit": LIST_LIMIT},
            required=("note",),
        ),
        Tool(
            name="vault_daily_read",
            description="Read a daily note (today by default).",
            function=_daily_read,
            parameters={"date": {"type": "string", "description": "today, yesterday, tomorrow or YYYY-MM-DD."}},
        ),
        Tool(
            name="vault_create_note",
            description=(
                "Create a note of a type, in the right folder with its front matter and template. Types: "
                "idea (draft thoughts), project and area (a folder + hub note), knowledge (atomic evergreen note), "
                "source (book, article, video: needs properties.source_type), person, note (a working note inside a "
                "project or area: needs `parent`), map. The title is the file name and must be unique. `content` is "
                "the markdown body: follow the template's sections (vault_templates), write in the person's "
                "language, and link related notes with [[wikilinks]]. Search first: extend an existing note rather "
                "than duplicate. Not sure where it belongs? Use vault_capture."
            ),
            function=_create,
            parameters={
                "title": {"type": "string", "description": "The note's title (becomes its file name)."},
                "type": {"type": "string", "enum": ["idea", "project", "area", "knowledge", "source", "person", "note", "map", "inbox"]},
                "content": {"type": "string", "description": "The markdown body, without front matter."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Without #; use a/b for sub tags."},
                "status": {"type": "string", "description": "Default depends on the type (idea: seed, project: active...)."},
                "summary": {"type": "string", "description": "One line that says what the note is (shown in lists)."},
                "folder": {"type": "string", "description": "Optional sub folder of the type's folder (5-knowledge/programming)."},
                "parent": {"type": "string", "description": "type note only: the project or area it belongs to."},
                "properties": {"type": "object", "description": "More front matter, e.g. {\"source_type\": \"book\", \"author_name\": \"...\"}."},
                "aliases": {"type": "array", "items": {"type": "string"}, "description": "Other names for [[links]]."},
                "overwrite": {"type": "boolean", "description": "Rarely: replace a note of the same path."},
            },
            required=("title", "type"),
        ),
        Tool(
            name="vault_capture",
            description=(
                "Drop anything in the inbox, fast, without deciding where it goes: a thought, a link, a quote, a "
                "to-do, something the person said they want to keep. It is sorted later. Prefer this to losing it "
                "or to guessing a place."
            ),
            function=_capture,
            parameters={
                "text": {"type": "string", "description": "What to keep, as the person said it."},
                "title": {"type": "string", "description": "Optional short title (else the first line)."},
                "source": {"type": "string", "description": "Optional: a URL or where it came from."},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            required=("text",),
        ),
        Tool(
            name="vault_edit_note",
            description=(
                "Change a note: replace a passage by another (only that passage is sent). old_text must be in the "
                "note exactly as written (without the line numbers vault_read shows) and appear once, unless replace_all. Empty new_text deletes the passage. "
                "Read the note first. For adding at the end or in a section use vault_append_note."
            ),
            function=_edit,
            parameters={
                "note": NOTE,
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            required=("note", "old_text", "new_text"),
        ),
        Tool(
            name="vault_append_note",
            description=(
                "Add text at the end of a note, or at the end of one of its sections (`heading`, created if missing). "
                "Use it to log progress on a project, add an item to a list, extend an idea."
            ),
            function=_append,
            parameters={
                "note": NOTE,
                "text": {"type": "string", "description": "Markdown to add."},
                "heading": {"type": "string", "description": "Section to add it to, e.g. 'Next steps'."},
            },
            required=("note", "text"),
        ),
        Tool(
            name="vault_rewrite_note",
            description=(
                "Replace the whole text of a note (its front matter is kept; the old version stays in git). Only for "
                "a deep restructure; otherwise vault_edit_note."
            ),
            function=_rewrite,
            parameters={"note": NOTE, "content": {"type": "string", "description": "The new markdown body."}},
            required=("note", "content"),
        ),
        Tool(
            name="vault_set_properties",
            description=(
                "Change a note's front matter without touching its text: set properties (status, summary, "
                "priority, a link like \"[[Project]]\"...), remove some, add or remove tags. Statuses are checked "
                "against the note's type. Use it to move an idea from seed to growing, close a project, tag a note."
            ),
            function=_set_properties,
            parameters={
                "note": NOTE,
                "set": {"type": "object", "description": "Properties to set, e.g. {\"status\": \"done\"}."},
                "remove": {"type": "array", "items": {"type": "string"}, "description": "Property names to remove."},
                "add_tags": {"type": "array", "items": {"type": "string"}},
                "remove_tags": {"type": "array", "items": {"type": "string"}},
            },
            required=("note",),
        ),
        Tool(
            name="vault_move_note",
            description=(
                "Move and/or rename a note; every [[link]] to it in the vault is updated. `to` is a new name "
                "('Better title'), a folder ending with / ('8-archive/'), or a full path. Use it to file an inbox "
                "note, to archive something finished (and set its status), or to rename."
            ),
            function=_move,
            parameters={"note": NOTE, "to": {"type": "string"}},
            required=("note", "to"),
        ),
        Tool(
            name="vault_delete_note",
            description=(
                "Delete a note: it goes to the trash and stays in git history, and the answer says which notes still "
                "link to it. Only when the person asked, or for a clear duplicate or empty capture you processed."
            ),
            function=_delete,
            parameters={"note": NOTE},
            required=("note",),
        ),
        Tool(
            name="vault_restore_note",
            description=(
                "Bring a note back as it was in an earlier commit (from vault_history), also a deleted one. The "
                "current version stays in the history."
            ),
            function=_restore,
            parameters={"note": NOTE, "commit": {"type": "string", "description": "A commit id from vault_history."}},
            required=("note", "commit"),
        ),
        Tool(
            name="vault_daily_append",
            description=(
                "Add to a daily note (today by default; created from the template if it does not exist) under a "
                "section: Log (what happened, with the time), Tasks ('- [ ] ...'), Notes. Use it for the journal "
                "side of the brain: what the person did, decided, felt, learned today."
            ),
            function=_daily_append,
            parameters={
                "text": {"type": "string", "description": "Markdown to add; start list items with '- '."},
                "section": {"type": "string", "description": "Heading to add under (default Log)."},
                "date": {"type": "string", "description": "today, yesterday, tomorrow or YYYY-MM-DD."},
            },
            required=("text",),
        ),
        Tool(
            name="vault_set_task",
            description="Check or uncheck a task ('- [ ] ...') of a note, found by its line number or words of its text.",
            function=_set_task,
            parameters={
                "note": NOTE,
                "task": {"type": "string", "description": "The task's line number, or words of its text."},
                "done": {"type": "boolean", "description": "Default true; false to uncheck."},
            },
            required=("note", "task"),
        ),
        Tool(
            name="vault_update_index",
            description=(
                "Refresh the automatic list of a folder's index note (a map of content: every note with its status "
                "and summary), or of the Home note (folder empty). Text the person wrote in the index is kept. Do it "
                "after filing several notes, or in a review."
            ),
            function=_update_index,
            parameters={"folder": {"type": "string", "description": "e.g. 2-ideas; empty for Home."}},
        ),
        Tool(
            name="vault_sync",
            description=(
                "Pull what the person changed in Obsidian, push your commits, and report the state of the sync (any "
                "problem such as a conflict). Notes are synced automatically; call this when asked, or when a "
                "warning about the sync appeared."
            ),
            function=_sync,
            parameters={},
        ),
        Tool(
            name="vault_reindex",
            description=(
                "Build or refresh the index of semantic search (embeddings). It is brought up to date by "
                "vault_semantic_search too; use this after many notes were added, or full=true to start over."
            ),
            function=_reindex,
            parameters={"full": {"type": "boolean"}},
        ),
    ]


VAULT_TOOLS = frozenset(tool.name for tool in vault_tools())
