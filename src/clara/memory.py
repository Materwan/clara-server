"""The one memory, in one SQLite file. Only the server process opens it.

    people(id, name, relation, notify_after)
                                            one row per real person; relation: Clara's relationship with them
                                            (0-100, NULL: none yet); notify_after: seconds a task takes before
                                            the person is notified when it is done (0: never, NULL: the server's)
    accounts(surface, external_id, person)  "discord:1234" and "cli:erwan" can be the same person
    facts(id, person, text, text_key)       what Clara knows about a person (shared by every surface);
                                            text_key is the text folded for comparison (no duplicates)
    messages(id, conversation, person, role, content)
                                            conversation history, one thread per conversation id
    reminders(id, person, text, due_at, ...)    what is still to be announced (see reminders.py)
    reminder_events(id, person, kind, text, ...)
                                            what was announced: reminders that came due and notifications
                                            (see notifications.py), each for one person (NULL: everybody)
                                            and some of their surfaces ('': all of them)
    reminder_cursors(client, last_event_id)     how far each listener (client + account) has read
    tasks(id, person, title, description, due_at, status, reminders_sent, ...)
    task_reminders(id, task, at)                a person's to-do list, and the reminders still to come of each
                                                task (see taskstore.py and tasks.py)
    conversations(conversation, person, title, pinned, ...)
                                            the conversations a client can list: who started each, its
                                            title, when it was last written in (from its first turn on)
    account_logins(surface, external_id, user)
                                            accounts a client (the Discord bot) signed in for a user: on the
                                            surfaces of CLARA_LOGIN_SURFACES only these may talk to Clara
    spaces(id, surface, name, chime, present)
                                            the group places a client is in (a Discord server): whether
                                            Clara may answer messages there that were not addressed to her
    options(key, value)                     small settings changed at run time (the default of `chime`)
    usage_log(id, at, person, kind, surface, model_ref, prompt_tokens, completion_tokens, ...)
                                            one row per answer (or compaction, title): who, where, which model,
                                            tokens in and out; for the administration (see usagelog.py)
    user_api_keys(person, provider, secret, hint)
                                            the API keys people saved for a provider, encrypted (see userkeys.py)

Facts follow the *person*, history follows the *conversation*: Clara knows you
are the same human on every surface, but a Discord channel and a terminal
session stay separate threads.

The connection is shared by all requests, guarded by a lock; every operation
is a few milliseconds, so it is called directly from async code.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .qcm import forms_in

MAX_FACT_LENGTH = 300
MAX_NAME_LENGTH = 80
MAX_TITLE_LENGTH = 100
MAX_NOTIFY_AFTER = 7 * 86400  # seconds: the longest threshold a person can set
RELATION_START = 50  # where a relationship starts when it first moves
CHIME_OPTION = "chime_default"
PREVIEW_LENGTH = 300  # characters of a conversation's first message given with a list of them
ANY_PROJECT = "any"  # conversations_of: in a project or not

_SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    surface     TEXT NOT NULL,
    external_id TEXT NOT NULL,
    person_id   INTEGER NOT NULL REFERENCES people (id),
    PRIMARY KEY (surface, external_id)
);
CREATE INDEX IF NOT EXISTS idx_accounts_person ON accounts (person_id);
CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  INTEGER NOT NULL REFERENCES people (id),
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    text_key   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation TEXT NOT NULL,
    person_id    INTEGER REFERENCES people (id),
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    prefix       TEXT NOT NULL DEFAULT '',
    tool_calls   TEXT,
    tool_name    TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages (conversation, id);
CREATE INDEX IF NOT EXISTS idx_messages_person ON messages (person_id);
CREATE TABLE IF NOT EXISTS conversation_state (
    conversation   TEXT PRIMARY KEY,
    summary        TEXT NOT NULL DEFAULT '',
    upto_id        INTEGER NOT NULL DEFAULT 0,
    context_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  INTEGER NOT NULL REFERENCES people (id),
    text       TEXT NOT NULL,
    due_at     TEXT NOT NULL,
    anchor_at  TEXT NOT NULL,
    repeat     TEXT NOT NULL DEFAULT '',
    timezone   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (due_at);
CREATE TABLE IF NOT EXISTS reminder_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER REFERENCES people (id),
    text      TEXT NOT NULL,
    due_at    TEXT NOT NULL,
    fired_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminder_events_fired ON reminder_events (fired_at);
CREATE TABLE IF NOT EXISTS reminder_cursors (
    client        TEXT PRIMARY KEY,
    last_event_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    conversation TEXT PRIMARY KEY,
    person_id    INTEGER REFERENCES people (id),
    surface      TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    titled_by    TEXT NOT NULL DEFAULT '',
    pinned       INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_person ON conversations (person_id, surface, updated_at);
CREATE TABLE IF NOT EXISTS users (
    name          TEXT PRIMARY KEY,
    person_id     INTEGER NOT NULL REFERENCES people (id),
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_person ON users (person_id);
CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT NOT NULL UNIQUE,
    user         TEXT NOT NULL REFERENCES users (name) ON DELETE CASCADE,
    surface      TEXT NOT NULL,
    device       TEXT NOT NULL DEFAULT '',
    address      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    last_used_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user);
CREATE TABLE IF NOT EXISTS account_logins (
    surface     TEXT NOT NULL,
    external_id TEXT NOT NULL,
    user        TEXT NOT NULL REFERENCES users (name) ON DELETE CASCADE,
    client      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    PRIMARY KEY (surface, external_id)
);
CREATE INDEX IF NOT EXISTS idx_account_logins_user ON account_logins (user);
CREATE TABLE IF NOT EXISTS spaces (
    id      TEXT PRIMARY KEY,
    surface TEXT NOT NULL,
    name    TEXT NOT NULL DEFAULT '',
    chime   INTEGER,
    present INTEGER NOT NULL DEFAULT 1,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS options (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id    INTEGER NOT NULL REFERENCES people (id),
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_person ON projects (person_id);
CREATE TABLE IF NOT EXISTS project_sources (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    repo       TEXT NOT NULL,
    ref        TEXT NOT NULL DEFAULT '',
    folder     TEXT NOT NULL,
    commit_sha TEXT NOT NULL DEFAULT '',
    synced_at  TEXT,
    skipped    INTEGER NOT NULL DEFAULT 0,
    problem    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_sources_project ON project_sources (project_id);
CREATE TABLE IF NOT EXISTS project_files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    source_id  INTEGER REFERENCES project_sources (id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT '',
    content    TEXT NOT NULL,
    size       INTEGER NOT NULL,
    added_at   TEXT NOT NULL,
    UNIQUE (project_id, path)
);
CREATE INDEX IF NOT EXISTS idx_project_files_source ON project_files (source_id);
CREATE TABLE IF NOT EXISTS usage (
    person_id INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    day       TEXT NOT NULL,
    tokens    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (person_id, day)
);
CREATE TABLE IF NOT EXISTS usage_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,  -- UTC, ISO 8601
    person_id     INTEGER REFERENCES people (id) ON DELETE CASCADE,  -- NULL: a conversation of several people
    kind          TEXT NOT NULL,  -- message, scheduled, compaction, title
    surface       TEXT NOT NULL,
    conversation  TEXT NOT NULL DEFAULT '',
    model_ref     TEXT NOT NULL DEFAULT '',  -- "provider:model" ('' with no catalogue)
    model         TEXT NOT NULL DEFAULT '',
    provider      TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    credits       INTEGER NOT NULL DEFAULT 0,  -- what was counted against the daily limit
    rounds        INTEGER NOT NULL DEFAULT 1,  -- model rounds of the answer (tool loop steps)
    estimated     INTEGER NOT NULL DEFAULT 0,  -- the model reported nothing: the tokens are estimated
    cached_tokens INTEGER NOT NULL DEFAULT 0  -- of the prompt tokens, those the provider read from its prompt cache
);
CREATE INDEX IF NOT EXISTS idx_usage_log_person ON usage_log (person_id, at);
CREATE INDEX IF NOT EXISTS idx_usage_log_at ON usage_log (at);
CREATE TABLE IF NOT EXISTS user_api_keys (
    person_id  INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    provider   TEXT NOT NULL,  -- providers.py id: cloud, gemini, deepseek, mistral
    secret     TEXT NOT NULL,  -- encrypted (integrations/vault.py), never given back
    hint       TEXT NOT NULL DEFAULT '',  -- the last characters of the key, to recognise it
    created_at TEXT NOT NULL,
    PRIMARY KEY (person_id, provider)
);
CREATE TABLE IF NOT EXISTS user_music (
    person_id  INTEGER PRIMARY KEY REFERENCES people (id) ON DELETE CASCADE,
    player     TEXT NOT NULL,  -- the Music Assistant id of the player of the PC this person controls (musicaccounts.py)
    token      TEXT NOT NULL,  -- their Music Assistant token, encrypted (integrations/vault.py), never given back
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS models (
    ref     TEXT PRIMARY KEY,  -- "provider:model", as models.py names it
    enabled INTEGER NOT NULL DEFAULT 0,  -- may users choose it?
    weight  REAL,  -- credits per token an administrator set (NULL: worked out from the size)
    size_b  REAL  -- billions of parameters, as the provider last said (NULL: unknown)
);
CREATE TABLE IF NOT EXISTS model_choices (
    person_id INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    surface   TEXT NOT NULL,
    model     TEXT NOT NULL,
    PRIMARY KEY (person_id, surface)
);
CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id      INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    title          TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    due_at         TEXT,  -- the deadline, UTC (NULL: none)
    status         TEXT NOT NULL DEFAULT 'open',  -- "open" or "done"
    reminders_sent INTEGER NOT NULL DEFAULT 0,
    timezone       TEXT NOT NULL DEFAULT '',  -- the person's clock when it was set: IANA name or "+02:00"
    surface        TEXT NOT NULL DEFAULT '',  -- where it was set: the model of that surface follows it up
    user_id        TEXT NOT NULL DEFAULT '',
    conversation   TEXT NOT NULL DEFAULT '',
    targets        TEXT NOT NULL DEFAULT '',  -- surfaces its reminders are shown on, "|"-separated ('': all)
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    done_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_person ON tasks (person_id, status);
CREATE TABLE IF NOT EXISTS task_reminders (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    at      TEXT NOT NULL  -- when it fires, UTC
);
CREATE INDEX IF NOT EXISTS idx_task_reminders_at ON task_reminders (at);
CREATE INDEX IF NOT EXISTS idx_task_reminders_task ON task_reminders (task_id);
CREATE TABLE IF NOT EXISTS schedules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id    INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    prompt       TEXT NOT NULL DEFAULT '',
    documents    TEXT NOT NULL DEFAULT '[]',  -- JSON: [{"name", "kind", "text"}], put after the prompt
    surface      TEXT NOT NULL,  -- the account it runs as
    user_id      TEXT NOT NULL,
    conversation TEXT NOT NULL,  -- one conversation for every run
    project_id   INTEGER,
    repeat       TEXT NOT NULL DEFAULT '',  -- '' (once), daily, weekly, monthly
    days         TEXT NOT NULL DEFAULT '',  -- weekly: the weekdays (0 = Monday), ","-separated
    start_at     TEXT NOT NULL,  -- the first moment, UTC: gives the time of day and the day of the month
    timezone     TEXT NOT NULL DEFAULT '',  -- IANA name or "+02:00"
    next_at      TEXT,  -- the next run, UTC (NULL: none to come)
    enabled      INTEGER NOT NULL DEFAULT 1,
    runs         INTEGER NOT NULL DEFAULT 0,
    last_at      TEXT,
    last_status  TEXT NOT NULL DEFAULT '',  -- ok, failed, missed
    last_summary TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_next ON schedules (next_at);
CREATE INDEX IF NOT EXISTS idx_schedules_person ON schedules (person_id);
CREATE TABLE IF NOT EXISTS markdown_files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    name       TEXT NOT NULL COLLATE NOCASE,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (person_id, name)
);
CREATE TABLE IF NOT EXISTS conversation_files (  -- what people sent in a conversation (conversationfiles.py)
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation TEXT NOT NULL,
    person_id    INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    mime         TEXT NOT NULL,
    kind         TEXT NOT NULL,  -- "picture" or "document"
    size         INTEGER NOT NULL,
    data         BLOB NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversation_files_conversation ON conversation_files (conversation);
CREATE TABLE IF NOT EXISTS integration_accounts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,  -- "github" or "gdrive"
    label      TEXT NOT NULL,  -- who it is on the other side (a GitHub login, a Google address)
    secret     TEXT NOT NULL DEFAULT '',  -- encrypted (integrations/secrets.py), never given back
    status     TEXT NOT NULL DEFAULT 'ok',  -- "ok" or "needs_reconnect"
    levels     TEXT NOT NULL DEFAULT '{}',  -- default permission of each level for its resources (JSON)
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_integration_accounts_person ON integration_accounts (person_id);
CREATE TABLE IF NOT EXISTS integration_resources (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    account_id INTEGER REFERENCES integration_accounts (id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,  -- github_repo, drive_folder, drive_file, server_path, computer_path
    label      TEXT NOT NULL,
    locator    TEXT NOT NULL,  -- JSON: what the connector needs to find it
    levels     TEXT NOT NULL DEFAULT '{}',  -- its own permissions (JSON); a level not in it follows the account's
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_integration_resources_person ON integration_resources (person_id);
CREATE TABLE IF NOT EXISTS integration_attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id     INTEGER NOT NULL REFERENCES integration_resources (id) ON DELETE CASCADE,
    project_id      INTEGER REFERENCES projects (id) ON DELETE CASCADE,
    conversation    TEXT,  -- exactly one of project_id and conversation is set
    levels          TEXT NOT NULL DEFAULT '{}',  -- permissions that apply here instead of the resource's (JSON)
    created_at      TEXT NOT NULL,
    UNIQUE (resource_id, project_id, conversation)
);
CREATE INDEX IF NOT EXISTS idx_integration_attachments_project ON integration_attachments (project_id);
CREATE INDEX IF NOT EXISTS idx_integration_attachments_conversation ON integration_attachments (conversation);
CREATE TABLE IF NOT EXISTS approvals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id    INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    conversation TEXT NOT NULL,
    surface      TEXT NOT NULL DEFAULT '',  -- where the conversation is, and as which account:
    user_id      TEXT NOT NULL DEFAULT '',  -- the follow-up turn is run there
    resource_id  INTEGER REFERENCES integration_resources (id) ON DELETE SET NULL,
    op           TEXT NOT NULL,  -- the operation asked for (write, delete...)
    level        TEXT NOT NULL,  -- read, write or destructive
    args         TEXT NOT NULL,  -- JSON, frozen when asked
    args_hash    TEXT NOT NULL,
    summary      TEXT NOT NULL,  -- what is shown to the person, written by the server from the arguments
    reason       TEXT NOT NULL DEFAULT '',  -- what the model said about it (shown as a quote only)
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending, approved, denied, expired, done, failed
    result       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    notified_at  TEXT,
    decided_at   TEXT,
    decided_on   TEXT NOT NULL DEFAULT '',  -- the surface the person answered on
    told         INTEGER NOT NULL DEFAULT 0  -- has the model been told how it ended?
);
CREATE INDEX IF NOT EXISTS idx_approvals_person ON approvals (person_id, status);
CREATE INDEX IF NOT EXISTS idx_approvals_conversation ON approvals (conversation, status);
CREATE TABLE IF NOT EXISTS integration_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER REFERENCES people (id) ON DELETE SET NULL,
    conversation TEXT NOT NULL DEFAULT '',
    resource    TEXT NOT NULL,  -- label of the resource, as it was
    op          TEXT NOT NULL,
    level       TEXT NOT NULL,
    summary     TEXT NOT NULL,
    outcome     TEXT NOT NULL,  -- done, failed, denied, asked
    approval_id INTEGER,
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_integration_log_at ON integration_log (at);
CREATE TABLE IF NOT EXISTS integration_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    device      TEXT NOT NULL,
    op          TEXT NOT NULL,
    args        TEXT NOT NULL,  -- JSON
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued, sent, done, failed
    result      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_integration_jobs_device ON integration_jobs (person_id, device, status);
"""

# Columns added after the first release: databases created before have to get them.
_ADDED_COLUMNS = {
    "people": {
        "relation": "INTEGER",  # Clara's relationship with the person, 0-100 (NULL: none yet)
        "notify_after": "INTEGER",  # seconds a task takes before it notifies the person (0: never, NULL: default)
        "approval_notify_after": "INTEGER",  # seconds before a request for permission is pushed (NULL: default)
    },
    "spaces": {
        "instructions": "TEXT NOT NULL DEFAULT '[]'",  # JSON [{"text", "enabled"}]: who Clara is in that space
    },
    "projects": {
        "pinned": "INTEGER NOT NULL DEFAULT 0",  # only pinned projects have their conversations in the history
    },
    "conversations": {
        "project_id": "INTEGER",  # the project it belongs to (projects.py), NULL: none
    },
    "usage_log": {
        "own_key": "INTEGER NOT NULL DEFAULT 0",  # answered with the person's own API key (userkeys.py): no credits
        "cached_tokens": "INTEGER NOT NULL DEFAULT 0",  # prompt tokens the provider read from its cache (0: not said)
    },
    "users": {
        "daily_token_limit": "INTEGER",  # tokens a day (limits.py); NULL: the server's default, 0: no limit
    },
    "models": {
        "capabilities": "TEXT NOT NULL DEFAULT '{}'",  # JSON of what the provider says the model can do (models.Capabilities)
    },
    "messages": {
        "prefix": "TEXT NOT NULL DEFAULT ''",
        "tool_calls": "TEXT",
        "tool_name": "TEXT",
        "thinking": "TEXT",  # the model's reasoning before tool calls: some providers want it back (DeepSeek)
    },
    "tasks": {
        "parent_id": "INTEGER REFERENCES tasks (id) ON DELETE CASCADE",  # the task it is a sub task of (NULL: a main task)
        "position": "INTEGER NOT NULL DEFAULT 0",  # its place among the tasks with the same parent (0: no order chosen)
    },
    "reminders": {  # where the reminder was set: the answer that announces it is written there
        "surface": "TEXT NOT NULL DEFAULT ''",
        "user_id": "TEXT NOT NULL DEFAULT ''",
        "conversation": "TEXT NOT NULL DEFAULT ''",
        "targets": "TEXT NOT NULL DEFAULT ''",  # surfaces it is shown on, "|"-separated ('': all of them)
    },
    "reminder_events": {
        "message": "TEXT",  # what Clara wrote for it (NULL: just the text)
        "kind": "TEXT NOT NULL DEFAULT 'reminder'",  # "reminder" or "notification"
        "title": "TEXT NOT NULL DEFAULT ''",
        "targets": "TEXT NOT NULL DEFAULT ''",
        "source": "TEXT NOT NULL DEFAULT ''",  # who sent a notification: "clara", "server" or a client
        "conversation": "TEXT NOT NULL DEFAULT ''",  # the conversation it is about, if any
        "payload": "TEXT",  # JSON a client acts on (an approval's id and summary); NULL: none
    },
}


# Indexes on columns of _ADDED_COLUMNS: created after them (an older database does not have the column at first)
_INDEXES_ON_ADDED_COLUMNS = (
    "CREATE INDEX IF NOT EXISTS idx_conversations_project ON conversations (project_id)",
)


class MergeRefused(ValueError):
    """Both people already have facts or history: merging them is irreversible."""


@dataclass(frozen=True)
class Person:
    id: int
    name: str


@dataclass(frozen=True)
class PersonSummary:
    person: Person
    accounts: list[str]  # "surface:external_id"
    facts: int
    relation: int | None = None


@dataclass(frozen=True)
class Space:
    """A group place a client is in (a Discord server)."""

    id: str  # "discord:guild:123"
    surface: str
    name: str
    chime: bool | None  # may Clara answer what was not addressed to her? None: the default
    present: bool  # the client is still in it (as it last said)
    seen_at: str  # ISO, UTC
    instructions: tuple[dict, ...] = ()  # {"text", "enabled"}: when some are on, they are Clara's personality there


@dataclass(frozen=True)
class Footprint:
    """What Clara holds about a person (and what `delete_person` removes)."""

    accounts: int
    facts: int
    messages: int  # their own messages, plus the whole of the conversations they were alone in
    conversations: int


@dataclass(frozen=True)
class Fact:
    id: int
    text: str


@dataclass(frozen=True)
class StoredMessage:
    role: str  # "user", "assistant" or "tool"
    content: str
    person_id: int | None
    author: str | None  # display name of the person who wrote it (user messages)
    id: int = 0
    prefix: str = ""  # context the client put before a user message (kept for the model, not shown)
    tool_calls: list[dict] | None = None  # assistant messages: [{"function": {"name", "arguments"}}]
    tool_name: str | None = None  # tool messages: which tool answered
    thinking: str = ""  # assistant messages that call tools: the reasoning that led to the calls


@dataclass(frozen=True)
class TurnRow:
    """A message produced during a turn, after the user's: the answer, a tool call, a result."""

    role: str
    content: str
    tool_calls: list[dict] | None = None
    tool_name: str | None = None
    thinking: str = ""  # kept with tool calls only: the provider may need it back with them


@dataclass(frozen=True)
class Reminder:
    id: int
    person_id: int
    text: str
    due_at: datetime  # next time it fires (UTC)
    anchor_at: datetime  # first time it fired or will fire (UTC): repeats are counted from here
    repeat: str  # "", "daily", "weekly" or "monthly"
    timezone: str  # IANA name or "+02:00": the clock a repeat keeps
    surface: str = ""  # where it was set, "" if unknown (reminders from before this was kept)
    user_id: str = ""
    conversation: str = ""
    targets: tuple[str, ...] = ()  # surfaces it is shown on; empty: every surface of the person


@dataclass(frozen=True)
class ReminderEvent:
    """Something announced to a person: a reminder that came due, or a notification. Kept for a while so
    that a client offline at that moment gets it later."""

    id: int
    text: str
    due_at: str  # ISO, UTC (a notification: when it was sent)
    fired_at: str  # ISO, UTC
    author: str | None  # name of the person it is for (who set the reminder)
    message: str | None = None  # what Clara wrote to announce it; None: only `text` is shown
    kind: str = "reminder"  # or "notification"
    title: str = ""
    targets: tuple[str, ...] = ()  # surfaces it is for; empty: all of them
    source: str = ""  # who sent a notification
    person_id: int | None = None  # None: for everybody
    conversation: str = ""
    payload: dict | None = None  # what a client acts on (an approval to answer, a job to run)


@dataclass(frozen=True)
class ConversationInfo:
    """A conversation as a list of them shows it."""

    conversation: str
    person_id: int | None  # who started it
    surface: str
    title: str  # "" until Clara or the person gives it one
    titled_by: str  # "", "clara" or "person"
    pinned: bool
    created_at: str  # ISO, UTC
    updated_at: str  # ISO, UTC: its last turn
    preview: str = ""  # the start of its first message still stored
    project_id: int | None = None  # the project it belongs to


SHOWN_ARGUMENT = 2000  # characters of a tool argument given back with a message
SHOWN_RESULT = 8000  # characters of what a tool gave back, given back with the message that called it


def _shown_call(call: dict, result: str | None = None) -> dict:
    function = call.get("function") or {}
    arguments = function.get("arguments")
    if isinstance(arguments, dict):
        arguments = {
            key: value[:SHOWN_ARGUMENT] if isinstance(value, str) else value for key, value in arguments.items()
        }
    shown = {"name": function.get("name", ""), "arguments": arguments if isinstance(arguments, dict) else {}}
    if result is not None:
        shown |= {"result": result[:SHOWN_RESULT], "truncated": len(result) > SHOWN_RESULT}
    return shown


@dataclass(frozen=True)
class ShownMessage:
    """A message as a person reads it back: a question or an answer (tool calls and results are left out)."""

    id: int
    role: str  # "user" or "assistant"
    content: str
    created_at: str  # ISO, UTC
    forms: tuple[dict, ...] = ()  # QCM the message asked (only when the transcript was read with `forms`)
    calls: tuple[dict, ...] = ()  # tools it called, `{"name", "arguments", "result", "truncated"}` (only when read with `calls`)


@dataclass(frozen=True)
class ConversationState:
    summary: str = ""  # replaces every message up to `upto_id`
    upto_id: int = 0
    context_tokens: int = 0  # size of the context at the end of the last turn


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def join_targets(targets: tuple[str, ...] | list[str]) -> str:
    return "|".join(sorted(set(targets)))


def split_targets(stored: str) -> tuple[str, ...]:
    return tuple(part for part in (stored or "").split("|") if part)


def escape_like(text: str) -> str:
    """`text` for a LIKE pattern, its own %, _ and \\ taken literally (with ESCAPE '\\')."""
    return re.sub(r"([\\%_])", r"\\\1", text)


def _like(text: str) -> str:
    """A LIKE pattern matching `text` anywhere."""
    return f"%{escape_like(text)}%"


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def fact_key(text: str) -> str:
    """A fact folded for comparison: "Élan " and "élan" are the same fact (SQLite's lower() only
    knows ASCII)."""
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


class Memory:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        for table, columns in _ADDED_COLUMNS.items():
            present = {row["name"] for row in self._db.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in present:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self._migrate_fact_keys()
        for statement in _INDEXES_ON_ADDED_COLUMNS:
            self._db.execute(statement)
        self._db.commit()

    def _migrate_fact_keys(self) -> None:
        """Databases from before `text_key` compared facts with SQL lower(): give every fact its key,
        drop the duplicates that only differed by non-ASCII case (the oldest stays), then index."""
        db = self._db
        if "text_key" not in {row["name"] for row in db.execute("PRAGMA table_info(facts)")}:
            db.execute("ALTER TABLE facts ADD COLUMN text_key TEXT NOT NULL DEFAULT ''")
        db.execute("DROP INDEX IF EXISTS idx_facts_unique")  # the old index, on lower(text)
        for row in db.execute("SELECT id, text FROM facts WHERE text_key = ''").fetchall():
            db.execute("UPDATE facts SET text_key = ? WHERE id = ?", (fact_key(row["text"]), row["id"]))
        db.execute(
            "DELETE FROM facts WHERE id NOT IN (SELECT MIN(id) FROM facts GROUP BY person_id, text_key)"
        )
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_key ON facts (person_id, text_key)")

    @property
    def database(self) -> sqlite3.Connection:
        """The connection, for the stores that keep their tables in the same file (users.py)."""
        return self._db

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def close(self) -> None:
        with self._lock:
            self._db.commit()
            self._db.close()

    # ------------------------------------------------------------------
    # People and accounts
    # ------------------------------------------------------------------
    def find_person(self, surface: str, external_id: str) -> Person | None:
        with self._lock:
            row = self._db.execute(
                "SELECT p.id, p.name FROM accounts a JOIN people p ON p.id = a.person_id"
                " WHERE a.surface = ? AND a.external_id = ?",
                (surface, external_id),
            ).fetchone()
        return Person(row["id"], row["name"]) if row else None

    def resolve(self, surface: str, external_id: str, name: str | None = None) -> Person:
        """The person behind an account; a new person is created on first contact."""
        with self._lock, self._db:
            person = self.find_person(surface, external_id)
            if person:
                return person
            display = _one_line(name or "")[:MAX_NAME_LENGTH] or external_id[:MAX_NAME_LENGTH]
            created = self._db.execute(
                "INSERT INTO people (name, created_at) VALUES (?, ?)", (display, _now())
            )
            self._db.execute(
                "INSERT INTO accounts (surface, external_id, person_id) VALUES (?, ?, ?)",
                (surface, external_id, created.lastrowid),
            )
            return Person(created.lastrowid, display)

    def person_by_id(self, person_id: int) -> Person | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id, name FROM people WHERE id = ?", (person_id,)
            ).fetchone()
        return Person(row["id"], row["name"]) if row else None

    def people_named(self, name: str) -> list[Person]:
        """People whose name matches, ignoring case."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, name FROM people WHERE lower(name) = lower(?) ORDER BY id", (name,)
            ).fetchall()
        return [Person(row["id"], row["name"]) for row in rows]

    def summaries(self) -> list[PersonSummary]:
        with self._lock:
            rows = self._db.execute(
                "SELECT p.id, p.name, p.relation, (SELECT COUNT(*) FROM facts f WHERE f.person_id = p.id)"
                " AS fact_count FROM people p ORDER BY p.id"
            ).fetchall()
            return [
                PersonSummary(
                    Person(row["id"], row["name"]),
                    [f"{surface}:{external}" for surface, external in self.accounts_of(row["id"])],
                    row["fact_count"],
                    row["relation"],
                )
                for row in rows
            ]

    def counts(self) -> tuple[int, int]:
        """(people, facts)"""
        with self._lock:
            people = self._db.execute("SELECT COUNT(*) FROM people").fetchone()[0]
            facts = self._db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        return people, facts

    def accounts_of(self, person_id: int) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT surface, external_id FROM accounts WHERE person_id = ?"
                " ORDER BY surface, external_id",
                (person_id,),
            ).fetchall()
        return [(row["surface"], row["external_id"]) for row in rows]

    def has_data(self, person_id: int) -> bool:
        """Does the person own any fact or message?"""
        with self._lock:
            return self._db.execute(
                "SELECT EXISTS (SELECT 1 FROM facts WHERE person_id = ?)"
                " OR EXISTS (SELECT 1 FROM messages WHERE person_id = ?)",
                (person_id, person_id),
            ).fetchone()[0] == 1

    def link_account(
        self, surface: str, external_id: str, target: Person, force: bool = False
    ) -> Person:
        """Make an account belong to `target`.

        If the account already had its own person, that person is merged into
        `target`: facts and history move over, duplicate facts are dropped. A merge
        cannot be undone, so it is refused (MergeRefused) when both people already
        have data, unless `force` (the operator's console).
        """
        with self._lock, self._db:
            current = self.find_person(surface, external_id)
            if current is None:
                self._db.execute(
                    "INSERT INTO accounts (surface, external_id, person_id) VALUES (?, ?, ?)",
                    (surface, external_id, target.id),
                )
            elif current.id != target.id:
                if not force and self.has_data(current.id) and self.has_data(target.id):
                    raise MergeRefused(
                        "Both accounts already have memories; an operator must merge them "
                        "from the server console (/link)."
                    )
                self._merge(current.id, target.id)
        return target

    def _conversations_of(self, person_id: int) -> tuple[list[str], list[str]]:
        """(conversations only this person wrote in, conversations shared with someone else)."""
        alone: list[str] = []
        shared: list[str] = []
        for row in self._db.execute(
            "SELECT c.conversation, EXISTS (SELECT 1 FROM messages o WHERE o.conversation = c.conversation"
            " AND o.role = 'user' AND o.person_id IS NOT ?) AS others"
            " FROM (SELECT DISTINCT conversation FROM messages WHERE person_id = ?) c",
            (person_id, person_id),
        ).fetchall():
            (shared if row["others"] else alone).append(row["conversation"])
        return alone, shared

    def footprint(self, person_id: int) -> Footprint:
        with self._lock:
            alone, shared = self._conversations_of(person_id)
            # every message of the conversations they were alone in, only their own in the shared ones
            messages = self._db.execute(
                "SELECT COUNT(*) FROM messages m JOIN (SELECT value AS conversation FROM json_each(?)) a"
                " ON a.conversation = m.conversation",
                (json.dumps(alone),),
            ).fetchone()[0] + self._db.execute(
                "SELECT COUNT(*) FROM messages m JOIN (SELECT value AS conversation FROM json_each(?)) s"
                " ON s.conversation = m.conversation WHERE m.person_id = ?",
                (json.dumps(shared), person_id),
            ).fetchone()[0]
            return Footprint(
                len(self.accounts_of(person_id)), self.fact_count(person_id), messages, len(alone) + len(shared)
            )

    def delete_person(self, person_id: int) -> Footprint:
        """Erase a person: accounts, facts and what they said. A conversation only they wrote in goes
        entirely (answers and summary included); in one shared with other people only their own
        messages go, and the answers and summary that remain may still mention them."""
        with self._lock, self._db:
            found = self.footprint(person_id)
            alone, shared = self._conversations_of(person_id)
            for conversation in alone:
                self._db.execute("DELETE FROM conversation_files WHERE conversation = ?", (conversation,))
                self._db.execute("DELETE FROM messages WHERE conversation = ?", (conversation,))
                self._db.execute("DELETE FROM conversation_state WHERE conversation = ?", (conversation,))
                self._db.execute("DELETE FROM conversations WHERE conversation = ?", (conversation,))
            # the shared conversations they started stay, with nobody as their owner (and out of their projects)
            self._db.execute(
                "UPDATE conversations SET person_id = NULL, project_id = NULL WHERE person_id = ?", (person_id,)
            )
            self._db.execute("DELETE FROM projects WHERE person_id = ?", (person_id,))  # their files go with them
            self._db.execute("DELETE FROM markdown_files WHERE person_id = ?", (person_id,))
            self._db.execute("DELETE FROM usage WHERE person_id = ?", (person_id,))
            self._db.execute("DELETE FROM usage_log WHERE person_id = ?", (person_id,))
            self._db.execute("DELETE FROM model_choices WHERE person_id = ?", (person_id,))
            self._db.execute("DELETE FROM user_api_keys WHERE person_id = ?", (person_id,))
            self._db.execute("DELETE FROM tasks WHERE person_id = ?", (person_id,))  # their reminders go with them
            # their connected accounts, resources and requests go with them (the tables cascade); so does their log
            self._db.execute("DELETE FROM integration_log WHERE person_id = ?", (person_id,))
            self._db.execute("DELETE FROM messages WHERE person_id = ?", (person_id,))
            self._db.execute("DELETE FROM reminders WHERE person_id = ?", (person_id,))
            self._db.execute("DELETE FROM reminder_events WHERE person_id = ?", (person_id,))
            self._db.execute("DELETE FROM facts WHERE person_id = ?", (person_id,))
            self._db.execute(
                "DELETE FROM account_logins WHERE EXISTS (SELECT 1 FROM accounts a WHERE a.person_id = ?"
                " AND a.surface = account_logins.surface AND a.external_id = account_logins.external_id)",
                (person_id,),
            )
            self._db.execute("DELETE FROM accounts WHERE person_id = ?", (person_id,))
            self._db.execute("DELETE FROM users WHERE person_id = ?", (person_id,))  # their sessions go with them
            self._db.execute("DELETE FROM people WHERE id = ?", (person_id,))
            return found

    def purge_summarised(self, conversation: str, upto_id: int) -> int:
        """Delete the messages a summary stands for (it then is the only record of them)."""
        with self._lock, self._db:
            return self._db.execute(
                "DELETE FROM messages WHERE conversation = ? AND id <= ?", (conversation, upto_id)
            ).rowcount

    def _merge(self, source: int, target: int) -> None:
        db = self._db
        db.execute(
            "UPDATE people SET relation = COALESCE(relation, (SELECT relation FROM people WHERE id = ?)) WHERE id = ?",
            (source, target),
        )
        db.execute(
            "UPDATE people SET notify_after = COALESCE(notify_after, (SELECT notify_after FROM people WHERE id = ?))"
            " WHERE id = ?",
            (source, target),
        )
        db.execute("UPDATE OR IGNORE facts SET person_id = ? WHERE person_id = ?", (target, source))
        db.execute("DELETE FROM facts WHERE person_id = ?", (source,))  # duplicates left behind
        db.execute("UPDATE accounts SET person_id = ? WHERE person_id = ?", (target, source))
        db.execute("UPDATE messages SET person_id = ? WHERE person_id = ?", (target, source))
        db.execute("UPDATE reminders SET person_id = ? WHERE person_id = ?", (target, source))
        db.execute("UPDATE reminder_events SET person_id = ? WHERE person_id = ?", (target, source))
        db.execute("UPDATE conversations SET person_id = ? WHERE person_id = ?", (target, source))
        db.execute("UPDATE projects SET person_id = ? WHERE person_id = ?", (target, source))
        db.execute("UPDATE tasks SET person_id = ? WHERE person_id = ?", (target, source))
        db.execute(
            "INSERT INTO usage (person_id, day, tokens) SELECT ?, day, tokens FROM usage WHERE person_id = ?"
            " ON CONFLICT (person_id, day) DO UPDATE SET tokens = usage.tokens + excluded.tokens",
            (target, source),
        )
        db.execute("DELETE FROM usage WHERE person_id = ?", (source,))
        db.execute("UPDATE usage_log SET person_id = ? WHERE person_id = ?", (target, source))
        db.execute(  # the target's own choices win
            "INSERT OR IGNORE INTO model_choices (person_id, surface, model)"
            " SELECT ?, surface, model FROM model_choices WHERE person_id = ?",
            (target, source),
        )
        db.execute("DELETE FROM model_choices WHERE person_id = ?", (source,))
        db.execute(  # the same for their API keys
            "INSERT OR IGNORE INTO user_api_keys (person_id, provider, secret, hint, created_at)"
            " SELECT ?, provider, secret, hint, created_at FROM user_api_keys WHERE person_id = ?",
            (target, source),
        )
        db.execute("DELETE FROM user_api_keys WHERE person_id = ?", (source,))
        for row in db.execute("SELECT id, name FROM markdown_files WHERE person_id = ?", (source,)).fetchall():
            name, number = row["name"], 1
            while db.execute(
                "SELECT 1 FROM markdown_files WHERE person_id = ? AND name = ?", (target, name)
            ).fetchone():  # a file of the target has this name: the other one keeps its content under a new one
                number += 1
                name = f"{row['name'].removesuffix('.md')} ({number}).md"
            db.execute("UPDATE markdown_files SET person_id = ?, name = ? WHERE id = ?", (target, name, row["id"]))
        db.execute("UPDATE users SET person_id = ? WHERE person_id = ?", (target, source))
        db.execute("DELETE FROM people WHERE id = ?", (source,))

    def move_account(self, surface: str, external_id: str, target: Person) -> None:
        """Give an account to `target` without merging anybody: the person it had keeps everything else.
        For an account used by several users one after the other (a Discord account signed in as one user,
        then as another)."""
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO accounts (surface, external_id, person_id) VALUES (?, ?, ?)"
                " ON CONFLICT (surface, external_id) DO UPDATE SET person_id = excluded.person_id",
                (surface, external_id, target.id),
            )

    def create_person(self, name: str) -> Person:
        """A new person with no account (one will be given to them)."""
        display = _one_line(name)[:MAX_NAME_LENGTH] or "?"
        with self._lock, self._db:
            created = self._db.execute("INSERT INTO people (name, created_at) VALUES (?, ?)", (display, _now()))
        return Person(created.lastrowid, display)

    # ------------------------------------------------------------------
    # Relationship
    # ------------------------------------------------------------------
    def relation(self, person_id: int) -> int | None:
        """Clara's relationship with a person, 0-100; None: there is none yet."""
        with self._lock:
            row = self._db.execute("SELECT relation FROM people WHERE id = ?", (person_id,)).fetchone()
        return row["relation"] if row else None

    def set_relation(self, person_id: int, value: int | None) -> int | None:
        """Set it (clamped to 0-100), or forget it (None)."""
        value = None if value is None else max(0, min(100, int(value)))
        with self._lock, self._db:
            self._db.execute("UPDATE people SET relation = ? WHERE id = ?", (value, person_id))
        return value

    def adjust_relation(self, person_id: int, change: int) -> int:
        """Move it by `change` (a relationship that does not exist yet starts at RELATION_START)."""
        with self._lock, self._db:
            current = self.relation(person_id)
            value = max(0, min(100, (RELATION_START if current is None else current) + int(change)))
            self._db.execute("UPDATE people SET relation = ? WHERE id = ?", (value, person_id))
        return value

    # ------------------------------------------------------------------
    # Notification threshold
    # ------------------------------------------------------------------
    def notify_after(self, person_id: int) -> int | None:
        """Seconds a task of this person takes before it notifies them when done (0: never); None: the
        server's default (CLARA_NOTIFY_LONG_TURN)."""
        with self._lock:
            row = self._db.execute("SELECT notify_after FROM people WHERE id = ?", (person_id,)).fetchone()
        return row["notify_after"] if row else None

    def set_notify_after(self, person_id: int, seconds: int | None) -> int | None:
        """Set it (0: never; at most MAX_NOTIFY_AFTER), or go back to the server's default (None)."""
        return self._set_delay("notify_after", person_id, seconds)

    def _set_delay(self, column: str, person_id: int, seconds: int | None) -> int | None:
        """Store one of a person's delays (`column` is ours: notify_after or approval_notify_after)."""
        if seconds is not None:
            seconds = int(seconds)
            if not 0 <= seconds <= MAX_NOTIFY_AFTER:
                raise ValueError(f"A delay is 0 (never) to {MAX_NOTIFY_AFTER} seconds.")
        with self._lock, self._db:
            self._db.execute(f"UPDATE people SET {column} = ? WHERE id = ?", (seconds, person_id))
        return seconds

    def approval_notify_after(self, person_id: int) -> int | None:
        """Seconds a request for permission waits in its conversation before it is pushed to the person's other
        surfaces (0: never pushed); None: the server's default."""
        with self._lock:
            row = self._db.execute(
                "SELECT approval_notify_after FROM people WHERE id = ?", (person_id,)
            ).fetchone()
        return row["approval_notify_after"] if row else None

    def set_approval_notify_after(self, person_id: int, seconds: int | None) -> int | None:
        return self._set_delay("approval_notify_after", person_id, seconds)

    # ------------------------------------------------------------------
    # Spaces (the group places of a client) and run-time options
    # ------------------------------------------------------------------
    @staticmethod
    def _space(row: sqlite3.Row) -> Space:
        chime = row["chime"]
        return Space(
            row["id"], row["surface"], row["name"], None if chime is None else bool(chime), bool(row["present"]),
            row["seen_at"], tuple(json.loads(row["instructions"])),
        )

    def sync_spaces(self, surface: str, spaces: list[tuple[str, str]]) -> list[Space]:
        """A client says which spaces of `surface` it is in now (id, name): they are stored or renamed and
        marked present, the others of that surface absent (their setting is kept)."""
        now = _now()
        with self._lock, self._db:
            self._db.execute("UPDATE spaces SET present = 0 WHERE surface = ?", (surface,))
            self._db.executemany(
                "INSERT INTO spaces (id, surface, name, present, seen_at) VALUES (?, ?, ?, 1, ?)"
                " ON CONFLICT (id) DO UPDATE SET name = excluded.name, present = 1, seen_at = excluded.seen_at",
                [(space_id, surface, _one_line(name)[:MAX_NAME_LENGTH], now) for space_id, name in spaces],
            )
        return self.spaces(surface)

    def spaces(self, surface: str | None = None) -> list[Space]:
        with self._lock:
            if surface is None:
                rows = self._db.execute("SELECT * FROM spaces ORDER BY surface, present DESC, name").fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM spaces WHERE surface = ? ORDER BY present DESC, name", (surface,)
                ).fetchall()
        return [self._space(row) for row in rows]

    def space(self, space_id: str) -> Space | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
        return self._space(row) if row else None

    def set_space_chime(self, space_id: str, chime: bool | None) -> bool:
        """On, off, or back to the default (None). False if there is no such space."""
        with self._lock, self._db:
            return self._db.execute(
                "UPDATE spaces SET chime = ? WHERE id = ?", (None if chime is None else int(chime), space_id)
            ).rowcount > 0

    def set_space_instructions(self, space_id: str, instructions: list[dict]) -> bool:
        """Replace the list of instructions of a space. False if there is no such space."""
        with self._lock, self._db:
            return self._db.execute(
                "UPDATE spaces SET instructions = ? WHERE id = ?", (json.dumps(instructions), space_id)
            ).rowcount > 0

    def space_personality(self, space_id: str | None) -> list[str]:
        """The instructions that are on for a space (none: Clara keeps her personality file)."""
        space = self.space(space_id) if space_id else None
        return [item["text"] for item in space.instructions if item["enabled"]] if space else []

    def chime_default(self) -> bool:
        return self.option(CHIME_OPTION) == "on"

    def set_chime_default(self, on: bool) -> None:
        self.set_option(CHIME_OPTION, "on" if on else "off")

    def chime_allowed(self, space_id: str | None) -> bool:
        """May Clara answer, in that space, a message that was not addressed to her?"""
        if not space_id:
            return False
        space = self.space(space_id)
        if space is None or space.chime is None:
            return self.chime_default()
        return space.chime

    def option(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM options WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_option(self, key: str, value: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO options (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ------------------------------------------------------------------
    # Facts
    # ------------------------------------------------------------------
    def facts(self, person_id: int, limit: int = 100) -> list[Fact]:
        """The most recent facts of a person, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, text FROM facts WHERE person_id = ? ORDER BY id DESC LIMIT ?",
                (person_id, limit),
            ).fetchall()
        return [Fact(row["id"], row["text"]) for row in reversed(rows)]

    def add_fact(self, person_id: int, text: str) -> Fact | None:
        """Store a fact; None when the person already has it. Raises ValueError if invalid."""
        text = _one_line(text)
        if not text:
            raise ValueError("A fact cannot be empty.")
        if len(text) > MAX_FACT_LENGTH:
            raise ValueError(f"A fact is at most {MAX_FACT_LENGTH} characters long.")
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO facts (person_id, text, text_key, created_at) VALUES (?, ?, ?, ?)",
                (person_id, text, fact_key(text), _now()),
            )
            return Fact(cursor.lastrowid, text) if cursor.rowcount else None

    def fact_count(self, person_id: int) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM facts WHERE person_id = ?", (person_id,)
            ).fetchone()[0]

    def search_facts(self, person_id: int, query: str, limit: int = 10) -> list[Fact]:
        """The person's facts containing every word of `query` (any case), newest first.
        Only that person's: the search is always scoped by `person_id`."""
        terms = fact_key(query).split()
        if not terms:
            return []
        where = " AND ".join("instr(text_key, ?) > 0" for _ in terms)
        with self._lock:
            rows = self._db.execute(
                f"SELECT id, text FROM facts WHERE person_id = ? AND {where} ORDER BY id DESC LIMIT ?",
                (person_id, *terms, limit),
            ).fetchall()
        return [Fact(row["id"], row["text"]) for row in rows]

    def delete_fact(self, person_id: int, fact_id: int) -> bool:
        """Delete one of the person's facts (never someone else's)."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "DELETE FROM facts WHERE id = ? AND person_id = ?", (fact_id, person_id)
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Reminders (scheduling and delivery rules live in reminders.py)
    # ------------------------------------------------------------------
    @staticmethod
    def _stamp(moment: datetime) -> str:
        """ISO text in UTC: these strings sort in time order."""
        return moment.astimezone(UTC).isoformat(timespec="seconds")

    @staticmethod
    def _reminder(row: sqlite3.Row) -> Reminder:
        return Reminder(
            row["id"],
            row["person_id"],
            row["text"],
            datetime.fromisoformat(row["due_at"]),
            datetime.fromisoformat(row["anchor_at"]),
            row["repeat"],
            row["timezone"],
            row["surface"],
            row["user_id"],
            row["conversation"],
            split_targets(row["targets"]),
        )

    def add_reminder(
        self,
        person_id: int,
        text: str,
        due_at: datetime,
        repeat: str = "",
        zone: str = "",
        origin: tuple[str, str, str] = ("", "", ""),
        targets: tuple[str, ...] = (),
    ) -> Reminder:
        """`origin` is (surface, user_id, conversation) of where it was set: the answer that announces
        the reminder is written in that conversation. `targets`: the surfaces it is shown on (empty: all)."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO reminders (person_id, text, due_at, anchor_at, repeat, timezone, created_at,"
                " surface, user_id, conversation, targets) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    person_id, text, self._stamp(due_at), self._stamp(due_at), repeat, zone, _now(), *origin,
                    join_targets(targets),
                ),
            )
            row = self._db.execute("SELECT * FROM reminders WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._reminder(row)

    def reminders_of(self, person_id: int) -> list[Reminder]:
        """The reminders a person set that have not fired (a repeating one stays), soonest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM reminders WHERE person_id = ? ORDER BY due_at, id", (person_id,)
            ).fetchall()
        return [self._reminder(row) for row in rows]

    def reminder_count(self, person_id: int) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM reminders WHERE person_id = ?", (person_id,)
            ).fetchone()[0]

    def delete_reminder(self, person_id: int, reminder_id: int) -> bool:
        """Cancel one of the person's reminders (never someone else's)."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "DELETE FROM reminders WHERE id = ? AND person_id = ?", (reminder_id, person_id)
            )
            return cursor.rowcount > 0

    def next_reminder_due(self) -> datetime | None:
        with self._lock:
            row = self._db.execute("SELECT MIN(due_at) FROM reminders").fetchone()
        return datetime.fromisoformat(row[0]) if row[0] else None

    def due_reminders(self, now: datetime) -> list[Reminder]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM reminders WHERE due_at <= ? ORDER BY due_at, id", (self._stamp(now),)
            ).fetchall()
        return [self._reminder(row) for row in rows]

    def reminder_exists(self, reminder_id: int) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM reminders WHERE id = ?", (reminder_id,)).fetchone() is not None

    def fire_reminder(
        self, reminder: Reminder, next_due: datetime | None, now: datetime, message: str | None = None
    ) -> ReminderEvent:
        """Record that `reminder` came due (with the `message` Clara wrote for it, if any), then reschedule
        it (`next_due`) or, if it was a one-off, remove it."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO reminder_events (person_id, text, due_at, fired_at, message, kind, targets,"
                " conversation) VALUES (?, ?, ?, ?, ?, 'reminder', ?, ?)",
                (
                    reminder.person_id, reminder.text, self._stamp(reminder.due_at), self._stamp(now), message,
                    join_targets(reminder.targets), reminder.conversation,
                ),
            )
            if next_due is None:
                self._db.execute("DELETE FROM reminders WHERE id = ?", (reminder.id,))
            else:
                self._db.execute(
                    "UPDATE reminders SET due_at = ? WHERE id = ?", (self._stamp(next_due), reminder.id)
                )
            author = self._db.execute(
                "SELECT name FROM people WHERE id = ?", (reminder.person_id,)
            ).fetchone()
        return ReminderEvent(
            id=cursor.lastrowid,
            text=reminder.text,
            due_at=self._stamp(reminder.due_at),
            fired_at=self._stamp(now),
            author=author["name"] if author else None,
            message=message,
            kind="reminder",
            targets=reminder.targets,
            person_id=reminder.person_id,
            conversation=reminder.conversation,
        )

    def add_notification(
        self,
        person_id: int | None,
        text: str,
        now: datetime,
        title: str = "",
        targets: tuple[str, ...] = (),
        source: str = "",
        conversation: str = "",
        kind: str = "notification",
        payload: dict | None = None,
    ) -> ReminderEvent:
        """Store a notification for one person (None: for everybody), to be streamed to their clients. `kind`
        and `payload`: an event a client acts on (an approval to answer) rather than shows."""
        stamp = self._stamp(now)
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO reminder_events (person_id, text, due_at, fired_at, kind, title, targets, source,"
                " conversation, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    person_id, text, stamp, stamp, kind, title, join_targets(targets), source, conversation,
                    json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                ),
            )
            row = self._db.execute(self._EVENT_COLUMNS + " WHERE e.id = ?", (cursor.lastrowid,)).fetchone()
        return self._event(row)

    _EVENT_COLUMNS = (
        "SELECT e.id, e.text, e.due_at, e.fired_at, p.name, e.message, e.kind, e.title, e.targets, e.source,"
        " e.person_id, e.conversation, e.payload FROM reminder_events e LEFT JOIN people p ON p.id = e.person_id"
    )

    @staticmethod
    def _event(row: sqlite3.Row) -> ReminderEvent:
        return ReminderEvent(
            row["id"],
            row["text"],
            row["due_at"],
            row["fired_at"],
            row["name"],
            row["message"],
            row["kind"],
            row["title"],
            split_targets(row["targets"]),
            row["source"],
            row["person_id"],
            row["conversation"],
            json.loads(row["payload"]) if row["payload"] else None,
        )

    EVERYONE = object()  # reminder_events_after(): no filter on the person

    def reminder_events_after(
        self, event_id: int, limit: int = 100, person_id: int | None | object = EVERYONE
    ) -> list[ReminderEvent]:
        """The events after `event_id`, oldest first. With `person_id`: only that person's, and the ones for
        everybody (`None`: only the ones for everybody)."""
        where, values = "e.id > ?", [event_id]
        if person_id is not self.EVERYONE:
            where += " AND (e.person_id IS NULL OR e.person_id = ?)"
            values.append(person_id)
        with self._lock:
            rows = self._db.execute(
                self._EVENT_COLUMNS + f" WHERE {where} ORDER BY e.id LIMIT ?", (*values, limit)
            ).fetchall()
        return [self._event(row) for row in rows]

    def people_in_conversation(self, conversation: str) -> list[int]:
        """Who wrote in a conversation (person ids)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT person_id FROM messages WHERE conversation = ? AND role = 'user'"
                " AND person_id IS NOT NULL ORDER BY person_id",
                (conversation,),
            ).fetchall()
        return [row[0] for row in rows]

    def last_reminder_event(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COALESCE(MAX(id), 0) FROM reminder_events").fetchone()[0]

    def prune_reminder_events(self, before: datetime) -> int:
        with self._lock, self._db:
            return self._db.execute(
                "DELETE FROM reminder_events WHERE fired_at < ?", (self._stamp(before),)
            ).rowcount

    def reminder_cursor(self, client: str) -> int | None:
        """The last event the client was sent, or None if it never connected."""
        with self._lock:
            row = self._db.execute(
                "SELECT last_event_id FROM reminder_cursors WHERE client = ?", (client,)
            ).fetchone()
        return row[0] if row else None

    def set_reminder_cursor(self, client: str, event_id: int) -> None:
        """Move a client's cursor forward (never back: two connections of one client may race)."""
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO reminder_cursors (client, last_event_id) VALUES (?, ?)"
                " ON CONFLICT (client) DO UPDATE SET last_event_id = MAX(last_event_id, excluded.last_event_id)",
                (client, event_id),
            )

    # ------------------------------------------------------------------
    # Conversation history
    # ------------------------------------------------------------------
    _MESSAGE_COLUMNS = (
        "SELECT m.id, m.role, m.content, m.person_id, p.name, m.prefix, m.tool_calls, m.tool_name, m.thinking"
        " FROM messages m LEFT JOIN people p ON p.id = m.person_id"
    )

    @staticmethod
    def _stored(row: sqlite3.Row) -> StoredMessage:
        return StoredMessage(
            row["role"],
            row["content"],
            row["person_id"],
            row["name"],
            row["id"],
            row["prefix"],
            json.loads(row["tool_calls"]) if row["tool_calls"] else None,
            row["tool_name"],
            row["thinking"] or "",
        )

    def history(self, conversation: str, turns: int, after_id: int = 0) -> list[StoredMessage]:
        """The last `turns` turns of a conversation (each from a user message to the
        next one, tool calls included), oldest first, ignoring messages up to `after_id`."""
        with self._lock:
            start = self._db.execute(
                "SELECT id FROM messages WHERE conversation = ? AND role = 'user' AND id > ?"
                " ORDER BY id DESC LIMIT 1 OFFSET ?",
                (conversation, after_id, max(turns, 1) - 1),
            ).fetchone()
            rows = self._db.execute(
                self._MESSAGE_COLUMNS + " WHERE m.conversation = ? AND m.id >= ? ORDER BY m.id",
                (conversation, start["id"] if start else after_id + 1),
            ).fetchall()
        return [self._stored(row) for row in rows]

    def turns_after(self, conversation: str, after_id: int = 0) -> int:
        """How many turns (user messages) a conversation has after `after_id`."""
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation = ? AND role = 'user' AND id > ?",
                (conversation, after_id),
            ).fetchone()[0]

    def messages_after(self, conversation: str, after_id: int = 0) -> list[StoredMessage]:
        """Every message of a conversation after `after_id`, oldest first."""
        with self._lock:
            rows = self._db.execute(
                self._MESSAGE_COLUMNS + " WHERE m.conversation = ? AND m.id > ? ORDER BY m.id",
                (conversation, after_id),
            ).fetchall()
        return [self._stored(row) for row in rows]

    def message_count(self, conversation: str, after_id: int = 0) -> int:
        """How many messages (of every role) a conversation has after `after_id`."""
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation = ? AND id > ?", (conversation, after_id)
            ).fetchone()[0]

    def first_exchange(self, conversation: str) -> tuple[str, str]:
        """The first question of a conversation still stored, and the first answer that has text ("": none)."""
        first = (
            "SELECT content FROM messages WHERE conversation = ? AND role = ? AND content != '' ORDER BY id LIMIT 1"
        )
        with self._lock:
            question = self._db.execute(first, (conversation, "user")).fetchone()
            answer = self._db.execute(first, (conversation, "assistant")).fetchone()
        return (question[0] if question else "", answer[0] if answer else "")

    def last_message_id(self, conversation: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(id) FROM messages WHERE conversation = ?", (conversation,)
            ).fetchone()
        return row[0] or 0

    def add_turn(
        self,
        conversation: str,
        person_id: int,
        question: str,
        rows: list[TurnRow],
        prefix: str = "",
        project_id: int | None = None,
    ) -> None:
        """Store a question and everything that followed it (calls, results, answer), or nothing. A new
        conversation is put in `project_id`; one that exists stays where it is."""
        now = _now()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO conversations (conversation, person_id, surface, created_at, updated_at, project_id)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (conversation) DO UPDATE SET updated_at = excluded.updated_at",
                (conversation, person_id, conversation.partition(":")[0], now, now, project_id),
            )
            self._db.execute(
                "INSERT INTO messages (conversation, person_id, role, content, created_at, prefix)"
                " VALUES (?, ?, 'user', ?, ?, ?)",
                (conversation, person_id, question, now, prefix),
            )
            self._db.executemany(
                "INSERT INTO messages (conversation, person_id, role, content, created_at,"
                " tool_calls, tool_name, thinking) VALUES (?, NULL, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        conversation,
                        row.role,
                        row.content,
                        now,
                        json.dumps(row.tool_calls, ensure_ascii=False) if row.tool_calls else None,
                        row.tool_name,
                        row.thinking or None,
                    )
                    for row in rows
                ],
            )

    def add_exchange(self, conversation: str, person_id: int, question: str, answer: str) -> None:
        """Store a question and its plain answer."""
        self.add_turn(conversation, person_id, question, [TurnRow("assistant", answer)])

    def clear_conversation(self, conversation: str) -> int:
        with self._lock, self._db:
            self._db.execute("DELETE FROM integration_attachments WHERE conversation = ?", (conversation,))
            self._db.execute("DELETE FROM conversation_files WHERE conversation = ?", (conversation,))
            self._db.execute("DELETE FROM conversation_state WHERE conversation = ?", (conversation,))
            self._db.execute("DELETE FROM conversations WHERE conversation = ?", (conversation,))
            return self._db.execute(
                "DELETE FROM messages WHERE conversation = ?", (conversation,)
            ).rowcount

    # ------------------------------------------------------------------
    # The list of conversations
    # ------------------------------------------------------------------
    _CONVERSATION_COLUMNS = (
        "SELECT c.conversation, c.person_id, c.surface, c.title, c.titled_by, c.pinned, c.created_at,"
        " c.updated_at, c.project_id, (SELECT substr(m.content, 1, ?) FROM messages m WHERE m.conversation = c.conversation"
        " AND m.role = 'user' ORDER BY m.id LIMIT 1) AS preview FROM conversations c"
    )

    @staticmethod
    def _conversation(row: sqlite3.Row) -> ConversationInfo:
        return ConversationInfo(
            row["conversation"], row["person_id"], row["surface"], row["title"], row["titled_by"],
            bool(row["pinned"]), row["created_at"], row["updated_at"], row["preview"] or "", row["project_id"],
        )

    def conversation_info(self, conversation: str) -> ConversationInfo | None:
        with self._lock:
            row = self._db.execute(
                self._CONVERSATION_COLUMNS + " WHERE c.conversation = ?", (PREVIEW_LENGTH, conversation)
            ).fetchone()
        return self._conversation(row) if row else None

    def conversations_of(
        self, person_id: int, surface: str | tuple[str, ...], query: str = "", limit: int = 200,
        project: int | str | None = ANY_PROJECT,
    ) -> list[ConversationInfo]:
        """The conversations a person started on a surface (or on any of several): pinned ones first, then the
        last written in. `query` keeps those whose title, messages or summary contain it (case is ignored for
        ASCII letters); `project` those of a project (None: those in no project; ANY_PROJECT: all of them)."""
        surfaces = (surface,) if isinstance(surface, str) else tuple(surface)
        sql = self._CONVERSATION_COLUMNS + f" WHERE c.person_id = ? AND c.surface IN ({','.join('?' * len(surfaces))})"
        params: list = [PREVIEW_LENGTH, person_id, *surfaces]
        if project is None:
            sql += " AND c.project_id IS NULL"
        elif project != ANY_PROJECT:
            sql += " AND c.project_id = ?"
            params.append(project)
        if query.strip():
            pattern = _like(query.strip())
            sql += (
                " AND (c.title LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM messages m"
                " WHERE m.conversation = c.conversation AND m.role IN ('user', 'assistant')"
                " AND m.content LIKE ? ESCAPE '\\') OR EXISTS (SELECT 1 FROM conversation_state s"
                " WHERE s.conversation = c.conversation AND s.summary LIKE ? ESCAPE '\\'))"
            )
            params += [pattern, pattern, pattern]
        sql += " ORDER BY c.pinned DESC, c.updated_at DESC, c.rowid DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [self._conversation(row) for row in rows]

    def update_conversation(self, conversation: str, title: str | None = None, pinned: bool | None = None) -> bool:
        """The person renames (an empty title: back to none) and/or pins a conversation. False if it is
        not listed."""
        with self._lock, self._db:
            if self._db.execute(
                "SELECT 1 FROM conversations WHERE conversation = ?", (conversation,)
            ).fetchone() is None:
                return False
            if title is not None:
                title = _one_line(title)[:MAX_TITLE_LENGTH]
                self._db.execute(
                    "UPDATE conversations SET title = ?, titled_by = ? WHERE conversation = ?",
                    (title, "person" if title else "", conversation),
                )
            if pinned is not None:
                self._db.execute(
                    "UPDATE conversations SET pinned = ? WHERE conversation = ?", (int(pinned), conversation)
                )
            return True

    def set_conversation_project(self, conversation: str, project_id: int | None) -> bool:
        """Put a listed conversation in a project (None: in none). False if it is not listed."""
        with self._lock, self._db:
            return self._db.execute(
                "UPDATE conversations SET project_id = ? WHERE conversation = ?", (project_id, conversation)
            ).rowcount > 0

    def title_if_untitled(self, conversation: str, title: str) -> str:
        """Give Clara's title to a conversation nobody has titled yet (the person may have, meanwhile).
        Returns the title it has now."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE conversations SET title = ?, titled_by = 'clara' WHERE conversation = ? AND title = ''",
                (_one_line(title)[:MAX_TITLE_LENGTH], conversation),
            )
            row = self._db.execute(
                "SELECT title FROM conversations WHERE conversation = ?", (conversation,)
            ).fetchone()
        return row["title"] if row else ""

    def transcript(
        self, conversation: str, limit: int = 200, forms: bool = False, calls: bool = False
    ) -> tuple[list[ShownMessage], bool]:
        """The last `limit` questions and answers of a conversation still stored, oldest first, and
        whether older ones were left out. With `forms`, an answer that asked a QCM is kept even when it
        wrote nothing, and carries the QCM. With `calls`, an answer that called tools is kept as well, and
        carries them (what they were given is cut short: it says what they were about, not all of it)."""
        # a call is stored as JSON: `"name": "qcm"` is how it shows; the forms are checked below
        keep = " OR tool_calls LIKE '%\"qcm\"%'" if forms else ""
        if calls:
            keep = " OR tool_calls IS NOT NULL"
        with self._lock:
            rows = self._db.execute(
                "SELECT id, role, content, created_at, tool_calls FROM messages WHERE conversation = ?"
                f" AND role IN ('user', 'assistant') AND (content != ''{keep}) ORDER BY id DESC LIMIT ?",
                (conversation, limit + 1),
            ).fetchall()
        shown = []
        for row in rows[:limit]:
            stored = json.loads(row["tool_calls"]) if row["tool_calls"] else []
            asked = tuple(forms_in(stored)) if forms and stored else ()
            called = tuple(_shown_call(call, result) for call, result in self._results(row["id"], stored)) if calls else ()
            if row["content"] or asked or called:
                shown.append(ShownMessage(row["id"], row["role"], row["content"], row["created_at"], asked, called))
        return shown[::-1], len(rows) > limit

    def _results(self, message_id: int, stored: list[dict]) -> list[tuple[dict, str | None]]:
        """The calls of an answer, each with what the tool gave back: the messages that follow the answer, one for
        each call and in its order (None: not kept, the messages were deleted)."""
        if not stored:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT role, content FROM messages WHERE id > ? AND conversation ="
                " (SELECT conversation FROM messages WHERE id = ?) ORDER BY id LIMIT ?",
                (message_id, message_id, len(stored)),
            ).fetchall()
        kept = []
        for row in rows:
            if row["role"] != "tool":
                break
            kept.append(row["content"])
        results = kept + [None] * (len(stored) - len(kept))
        return list(zip(stored, results))

    # ------------------------------------------------------------------
    # Summary and size of a conversation
    # ------------------------------------------------------------------
    def state(self, conversation: str) -> ConversationState:
        with self._lock:
            row = self._db.execute(
                "SELECT summary, upto_id, context_tokens FROM conversation_state"
                " WHERE conversation = ?",
                (conversation,),
            ).fetchone()
        return ConversationState(*row) if row else ConversationState()

    def set_context_tokens(self, conversation: str, tokens: int) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO conversation_state (conversation, context_tokens) VALUES (?, ?)"
                " ON CONFLICT (conversation) DO UPDATE SET context_tokens = excluded.context_tokens",
                (conversation, tokens),
            )

    def set_summary(self, conversation: str, summary: str, upto_id: int, context_tokens: int) -> None:
        """From now on `summary` stands for every message up to `upto_id`."""
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO conversation_state (conversation, summary, upto_id, context_tokens)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (conversation) DO UPDATE SET"
                " summary = excluded.summary, upto_id = excluded.upto_id,"
                " context_tokens = excluded.context_tokens",
                (conversation, summary, upto_id, context_tokens),
            )
