"""Folders on the machine the server runs on.

Only inside the roots an administrator listed (integrations policy `roots`): a person picks a folder under one of them,
and Clara can reach nothing outside that folder. Every path is resolved (symlinks followed, `..` removed) and checked
to be inside the folder again, at each call, so a link that points out of it, or a root that was removed, stops working.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath

from ...ingest import IGNORED_DIRS, IngestError, decode_text, extract, ignored
from ...projects import SEARCH_MAX_MATCHES, matching_lines, read_lines, search_pattern
from ..permissions import DESTRUCTIVE, SERVER, WRITE
from .base import (
    LIST_LIMIT,
    Connector,
    ConnectorError,
    Target,
    base_level,
    check_mode,
    check_text,
    cut,
)

MAX_READ_BYTES = 30_000_000  # a file bigger than this is not read
MAX_SEARCH_FILE_BYTES = 2_000_000
MAX_SEARCH_FILES = 5_000


def relative(path: object) -> str:
    """A path inside a resource as the model wrote it, made safe: "" is the resource itself. Absolute paths,
    drive letters and `..` are refused (the resolved path is checked again against the folder)."""
    text = str(path or "").strip().replace("\\", "/")
    if text in ("", ".", "/"):
        return ""
    if PureWindowsPath(text).drive or PurePosixPath(text).is_absolute():
        raise ConnectorError("Give a path inside the folder, not an absolute one.")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if ".." in parts or any("\x00" in part for part in parts):
        raise ConnectorError("A path may not go up (..).")
    return "/".join(parts)


def list_roots(roots: Callable[[], list[str]]) -> list[Path]:
    found = []
    for root in roots():
        try:
            found.append(Path(root).resolve())
        except (OSError, RuntimeError):
            continue
    return found


class ServerFolders(Connector):
    type = SERVER
    ops = frozenset({"list", "read", "search", "write", "delete", "move"})

    def __init__(self, roots: Callable[[], list[str]]):
        self._roots = roots  # the administrator's allowed roots, read at each call

    # -- where --------------------------------------------------------------------------------- #
    def base(self, target: Target) -> Path:
        """The folder of the resource, if it is still inside an allowed root."""
        try:
            base = Path(str(target.locator.get("path", ""))).resolve()
        except (OSError, RuntimeError):
            raise ConnectorError(f"{target.label}: the folder cannot be reached.") from None
        if not any(base == root or base.is_relative_to(root) for root in list_roots(self._roots)):
            raise ConnectorError(f"{target.label} is not inside a folder the administrator allows any more.")
        if not base.is_dir():
            raise ConnectorError(f"{target.label}: the folder does not exist (any more).")
        return base

    def inside(self, target: Target, path: object) -> tuple[Path, Path, str]:
        """(folder, resolved path, clean relative path); the path must stay inside the folder."""
        base = self.base(target)
        rel = relative(path)
        try:
            resolved = (base / rel).resolve() if rel else base
        except (OSError, RuntimeError):
            raise ConnectorError(f"{rel}: not a usable path.") from None
        if resolved != base and not resolved.is_relative_to(base):
            raise ConnectorError(f"{rel}: that leads outside the folder.")
        return base, resolved, rel

    # -- levels -------------------------------------------------------------------------------- #
    async def level(self, op: str, target: Target, args: dict) -> str:
        if op != "write":
            return base_level(op)
        mode = check_mode(args.get("mode"))
        _, path, _ = self.inside(target, args.get("path"))
        if mode == "overwrite" and await asyncio.to_thread(path.exists):
            return DESTRUCTIVE  # what was there is lost
        return WRITE

    # -- operations ---------------------------------------------------------------------------- #
    async def op_list(self, target: Target, args: dict) -> str:
        _, path, rel = self.inside(target, args.get("path"))
        if not path.is_dir():
            raise ConnectorError(f"{rel or '/'} is not a folder.")

        def work() -> str:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            lines = []
            for entry in entries[:LIST_LIMIT]:
                if entry.is_dir():
                    lines.append(f"{entry.name}/")
                else:
                    try:
                        lines.append(f"{entry.name} ({entry.stat().st_size:,} bytes)")
                    except OSError:
                        lines.append(entry.name)
            if len(entries) > LIST_LIMIT:
                lines.append(f"[{len(entries) - LIST_LIMIT} more]")
            return "\n".join(lines) or "(empty)"

        return await asyncio.to_thread(work)

    async def op_read(self, target: Target, args: dict) -> str:
        _, path, rel = self.inside(target, args.get("path"))
        if not path.is_file():
            raise ConnectorError(f"{rel or '/'} is not a file.")

        def work() -> str:
            if path.stat().st_size > MAX_READ_BYTES:
                raise ConnectorError(f"{rel} is too big to read ({path.stat().st_size // 1_000_000} MB).")
            try:
                found = extract(rel or path.name, path.read_bytes())
            except IngestError as error:
                raise ConnectorError(f"{rel}: {error}.") from None
            return read_lines(rel, found.text, int(args.get("start_line") or 1), int(args["end_line"]) if args.get("end_line") else None)

        return await asyncio.to_thread(work)

    async def op_search(self, target: Target, args: dict) -> str:
        base, path, rel = self.inside(target, args.get("path"))
        try:
            pattern = search_pattern(str(args.get("query", "")), bool(args.get("regex")))
        except ValueError as error:
            raise ConnectorError(str(error)) from None

        def work() -> str:
            matches: list[str] = []
            files = scanned = 0
            for folder, dirs, names in os.walk(path):
                dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and not d.endswith(".egg-info"))
                for name in sorted(names):
                    file = Path(folder, name)
                    short = file.relative_to(base).as_posix()
                    if ignored(short) or file.is_symlink():
                        continue
                    scanned += 1
                    if scanned > MAX_SEARCH_FILES:
                        break
                    try:
                        if file.stat().st_size > MAX_SEARCH_FILE_BYTES:
                            continue
                        text = decode_text(file.read_bytes())
                    except (OSError, IngestError):
                        continue
                    found = matching_lines(short, text, pattern, SEARCH_MAX_MATCHES - len(matches))
                    if found:
                        files += 1
                        matches += found
                    if len(matches) >= SEARCH_MAX_MATCHES:
                        break
                if len(matches) >= SEARCH_MAX_MATCHES or scanned > MAX_SEARCH_FILES:
                    break
            if not matches:
                return f"No match for {args.get('query')!r}."
            note = f"\n[only the first {SEARCH_MAX_MATCHES} shown]" if len(matches) >= SEARCH_MAX_MATCHES else ""
            return f"Matches in {files} file{'s' if files != 1 else ''}:\n" + "\n".join(matches) + note

        return cut(await asyncio.to_thread(work))

    async def op_write(self, target: Target, args: dict) -> str:
        mode = check_mode(args.get("mode"))
        content = check_text(args.get("content"))
        _, path, rel = self.inside(target, args.get("path"))
        if not rel:
            raise ConnectorError("Give the path of the file to write.")

        def work() -> str:
            exists = path.exists()
            if path.is_dir():
                raise ConnectorError(f"{rel} is a folder.")
            if mode == "create" and exists:
                raise ConnectorError(f"{rel} already exists: use mode overwrite or append.")
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a" if mode == "append" else "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
            verb = {"create": "Created", "overwrite": "Replaced" if exists else "Created", "append": "Added to"}[mode]
            return f"{verb} {rel} ({len(content):,} characters)."

        return await asyncio.to_thread(work)

    async def op_delete(self, target: Target, args: dict) -> str:
        _, path, rel = self.inside(target, args.get("path"))
        if not rel:
            raise ConnectorError("The folder itself cannot be deleted.")

        def work() -> str:
            if path.is_symlink() or path.is_file():
                path.unlink()
                return f"Deleted {rel}."
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    raise ConnectorError(f"{rel} is a folder with something in it: delete its files first.") from None
                return f"Deleted the empty folder {rel}."
            raise ConnectorError(f"No such file: {rel}.")

        return await asyncio.to_thread(work)

    async def op_move(self, target: Target, args: dict) -> str:
        _, source, rel = self.inside(target, args.get("path"))
        _, dest, to = self.inside(target, args.get("dest"))
        if not rel or not to:
            raise ConnectorError("Give the path to move and where to.")

        def work() -> str:
            if not source.exists():
                raise ConnectorError(f"No such file: {rel}.")
            if dest.exists():
                raise ConnectorError(f"{to} already exists: nothing was moved.")
            dest.parent.mkdir(parents=True, exist_ok=True)
            source.rename(dest)
            return f"Moved {rel} to {to}."

        return await asyncio.to_thread(work)

