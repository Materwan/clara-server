"""What every connector (GitHub, Google Drive, a folder of the server, a folder of the person's computer) offers.

The model sees one small set of operations, whatever is behind a resource:

    list    the files and folders at a path of the resource
    read    a file's text, by lines
    search  find words in the resource's files
    write   put text in a file (`mode`: create, overwrite or append)
    delete  remove a file
    move    rename or move a file

A connector says, for each call, the *level* of the action (permissions.py) before it runs, since the same operation
can be harmless or not: writing a file that does not exist is `write`, overwriting one that does is `destructive`.
A connector that cannot tell must answer `destructive`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...projects import READ_MAX_CHARS
from ..permissions import DESTRUCTIVE, READ, WRITE

OPS = ("list", "read", "search", "write", "delete", "move")
WRITE_MODES = ("create", "overwrite", "append")
MAX_WRITE_CHARS = 1_000_000  # a text the model may write in one call
LIST_LIMIT = 300  # entries a listing gives
SEARCH_LIMIT = 60  # matching lines a search gives
RESULT_LIMIT = READ_MAX_CHARS  # characters of anything given back to the model


class ConnectorError(Exception):
    """The operation could not be done: the message says why, and goes back to the model as it is. `reconnect`: the
    account's credentials are refused, so the person has to connect it again."""

    def __init__(self, message: str, reconnect: bool = False):
        super().__init__(message)
        self.reconnect = reconnect


@dataclass(frozen=True)
class Target:
    """The resource an operation is about, with what the connector needs to reach it."""

    resource_id: int
    label: str
    locator: dict
    token: str = ""  # the account's secret, opened (never shown, never logged)
    person_id: int = 0
    account_id: int | None = None


def base_level(op: str) -> str:
    """The level of an operation that does not depend on what it finds: reads are `read`, a delete or a move is
    `destructive`. (`write` depends on whether the file exists, so the connector decides.)"""
    if op in ("list", "read", "search"):
        return READ
    if op in ("delete", "move"):
        return DESTRUCTIVE
    return WRITE


def check_text(text: object, what: str = "content") -> str:
    if not isinstance(text, str):
        raise ConnectorError(f"{what} must be text.")
    if len(text) > MAX_WRITE_CHARS:
        raise ConnectorError(f"{what} is too long: at most {MAX_WRITE_CHARS:,} characters at once.")
    return text


def check_mode(mode: object) -> str:
    mode = str(mode or "create")
    if mode not in WRITE_MODES:
        raise ConnectorError(f"mode must be one of: {', '.join(WRITE_MODES)}.")
    return mode


def cut(text: str, limit: int = RESULT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Connector:
    """Subclasses fill in what they support; an operation they do not know is refused."""

    type = ""
    ops: frozenset[str] = frozenset()

    async def level(self, op: str, target: Target, args: dict) -> str:
        return base_level(op)

    def summary(self, op: str, target: Target, args: dict) -> str:
        """What a person reads to decide: built here from the arguments, never from the model's own words."""
        path = args.get("path") or "/"
        if op == "write":
            return f"{args.get('mode', 'create')} {path} in {target.label} ({len(str(args.get('content', ''))):,} characters)"
        if op == "move":
            return f"move {path} to {args.get('dest', '?')} in {target.label}"
        return f"{op} {path} in {target.label}"

    async def run(self, op: str, target: Target, args: dict) -> str:
        if op not in self.ops:
            raise ConnectorError(f"{target.label} does not support {op}.")
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            raise ConnectorError(f"{target.label} does not support {op}.")
        return await handler(target, args)
