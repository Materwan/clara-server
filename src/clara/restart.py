"""Restarting the server from the web site (or `/restart`): pull, update, stop the careful way, start again.

    1. while the old server still runs: `git pull --ff-only`, then (when the code changed) `pip install -e`,
       then `.env` brought up to date with `.env.example`, keeping its values. A step that fails stops it all
       and nothing is stopped: the server goes on, and the output of the step is the answer.
    2. the server stops like `/stop` does (running answers finish, clients are told),
    3. once the HTTP server is closed, `relaunch` starts it again with the same arguments.

The new process finds `data/restart.json` (written in step 1) and tells whoever asked that it is back: the web
page waits for `/health` to say `restarted: <id>`. What would be worth a restart (a changed `.env`, a new
version of the code on disk or on the remote) is listed by `status`, which the web site shows as a badge.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import dotenv_values, find_dotenv

from .lifecycle import Lifecycle

log = logging.getLogger(__name__)

PACKAGE_ROOT = Path(__file__).resolve().parents[2]  # where pyproject.toml and .env.example are
GIT_TIMEOUT = 60
INSTALL_TIMEOUT = 600
REMOTE_CHECK_SECONDS = 600  # how long what the remote has is remembered
OUTPUT_LINES = 40  # of a failed step, shown to the person who asked


class RestartError(Exception):
    """The restart did not start: the message says why (and, for a failed step, shows what it printed)."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


@dataclass
class Step:
    name: str
    ok: bool
    output: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "output": self.output}


def _tail(text: str, lines: int = OUTPUT_LINES) -> str:
    kept = text.strip().splitlines()[-lines:]
    return "\n".join(kept)


def _run(command: list[str], cwd: Path, timeout: float) -> tuple[bool, str]:
    """(did it succeed, what it printed)."""
    try:
        done = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=timeout, errors="replace",
            stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        return False, f"{' '.join(command[:3])} took more than {timeout:g} seconds"
    except OSError as error:
        return False, f"{command[0]}: {error}"
    return done.returncode == 0, _tail(done.stdout + done.stderr)


def git_head(root: Path = PACKAGE_ROOT) -> str | None:
    """The commit the code on disk is at, or None when it is not a git checkout (or git is missing)."""
    ok, output = _run(["git", "rev-parse", "HEAD"], root, 10)
    return output.strip() if ok and re.fullmatch(r"[0-9a-f]{40}", output.strip()) else None


def commits_behind(root: Path = PACKAGE_ROOT) -> int | None:
    """How many commits the remote has that this checkout has not (fetches first); None when it cannot tell."""
    ok, _ = _run(["git", "fetch", "--quiet"], root, GIT_TIMEOUT)
    if not ok:
        return None
    ok, output = _run(["git", "rev-list", "--count", "HEAD..@{upstream}"], root, 10)
    return int(output) if ok and output.strip().isdigit() else None


# ---- .env -----------------------------------------------------------------------------------------------------

_SETTING = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=")
_EXAMPLE = re.compile(r"^[ \t]*#?[ \t]*([A-Za-z_][A-Za-z0-9_]*)=")


def merged_env(old: str, example: str) -> str:
    """`.env.example` with the value of every setting of `old`: new options appear, nothing set is lost. A
    setting `old` has and the example no longer has is kept at the end (as update-and-run.sh does)."""
    values: dict[str, str] = {}  # the last one wins, as in dotenv
    for line in old.replace("\r", "").split("\n"):
        found = _SETTING.match(line)
        if found:
            values[found.group(1)] = re.sub(r"^[ \t]*(export[ \t]+)?", "", line)
    done: set[str] = set()
    out: list[str] = []
    for line in example.replace("\r", "").split("\n"):
        found = _EXAMPLE.match(line)
        if found and found.group(1) in values and found.group(1) not in done:
            done.add(found.group(1))
            out.append(values[found.group(1)])
        else:
            out.append(line)
    kept = [line for key, line in values.items() if key not in done]
    text = "\n".join(out)
    if kept:
        text = text.rstrip("\n") + "\n\n# Kept from the previous .env (not in .env.example any more)\n" + "\n".join(kept) + "\n"
    return text


def update_env(root: Path = PACKAGE_ROOT, env: Path | None = None) -> Step:
    example = root / ".env.example"
    env = env or root / ".env"
    if not example.is_file():
        return Step(".env", True, "no .env.example, .env left as it is")
    if not env.is_file() or not env.read_text(encoding="utf-8").strip():
        return Step(".env", True, ".env is empty: left as it is")
    old = env.read_text(encoding="utf-8")
    new = merged_env(old, example.read_text(encoding="utf-8"))
    if new.replace("\r", "") == old.replace("\r", ""):
        return Step(".env", True, ".env already up to date")
    backup = env.with_name(f".env.bak.{datetime.now():%Y%m%d-%H%M%S}")
    backup.write_text(old, encoding="utf-8")
    env.write_text(new, encoding="utf-8")  # written in place: keeps the permissions of .env
    return Step(".env", True, f".env updated, your values kept (previous file: {backup.name})")


def update_code(root: Path, head_at_boot: str | None) -> list[Step]:
    """Pull, and install what the new code needs. The steps that ran, the last one the one that failed if any."""
    steps: list[Step] = []
    before = git_head(root)
    if before is None:
        steps.append(Step("git pull", True, "not a git checkout: nothing to pull"))
    else:
        ok, output = _run(["git", "pull", "--ff-only"], root, GIT_TIMEOUT)
        steps.append(Step("git pull", ok, output))
        if not ok:
            return steps
    # a pull that brought something, or one made by hand since the server started: the install may be stale
    if before is None or git_head(root) != head_at_boot or importlib.util.find_spec("discord") is None:
        ok, output = _run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "-e", f"{root}[discord]"],
                          root, INSTALL_TIMEOUT)
        steps.append(Step("pip install", ok, output))
        if not ok:
            return steps
    else:
        steps.append(Step("pip install", True, "the code did not change: nothing to install"))
    try:
        steps.append(update_env(root))
    except OSError as error:
        steps.append(Step(".env", False, str(error)))
    return steps


# ---- the service ----------------------------------------------------------------------------------------------

def _digest(path: Path | None) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path else None
    except OSError:
        return None


class RestartService:
    def __init__(self, data_dir: Path, lifecycle: Lifecycle, root: Path = PACKAGE_ROOT,
                 env_file: Path | None = None, clock: Callable[[], float] = time.monotonic):
        self.lifecycle = lifecycle
        self.root = root
        self.requested = False  # set once the server is on its way down to start again
        self.running = False  # the update is in progress
        self._file = data_dir / "restart.json"
        self._clock = clock
        self.boot_id = uuid.uuid4().hex
        found = find_dotenv()  # the file `load_dotenv` read at the start
        self._env_file = env_file or (Path(found) if found else None)
        self._env_digest = _digest(self._env_file)
        # What `load_dotenv` took from the file (a setting the environment already had is not replaced by it, nor
        # will it be on the next start): the settings `relaunch` renews.
        loaded = dotenv_values(self._env_file) if self._env_file else {}
        self.env_keys = {key for key, value in loaded.items() if value is not None and os.environ.get(key) == value}
        self._head_at_boot: str | None = None  # see `record_boot`
        self._behind: int | None = None
        self._checked_at: float | None = None
        self.last = self._finish_previous()  # what the restart that started this process said

    def record_boot(self) -> None:
        """Note the commit the code is at: what is compared with later. Called by `main`, once the server is built
        (asking git costs a process, which the tests that build a server need not pay)."""
        self._head_at_boot = git_head(self.root)

    def _finish_previous(self) -> dict[str, Any] | None:
        """The restart that brought this process up, now complete: it is written back, which `/health` shows."""
        try:
            record = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict) or record.get("done_at"):
            return record if isinstance(record, dict) else None
        record["done_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._write(record)
        log.info("restarted (asked by %s)", record.get("by"))
        return record

    def _write(self, record: dict[str, Any]) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps(record, indent=2), encoding="utf-8")
        except OSError:
            log.exception("could not write %s", self._file)

    @property
    def restarted(self) -> str | None:
        """The id of the restart that started this process, for `/health`: whoever asked it looks for that."""
        return self.last.get("id") if self.last and self.last.get("done_at") else None

    # -- what is worth a restart ------------------------------------------------------------------------------

    async def status(self, refresh: bool = False) -> dict[str, Any]:
        reasons: list[dict[str, str]] = []
        if _digest(self._env_file) != self._env_digest:
            reasons.append({"id": "env", "text": "The .env file changed since the server started."})
        head = await asyncio.to_thread(git_head, self.root)
        if head and self._head_at_boot and head != self._head_at_boot:
            reasons.append({"id": "code", "text": "The code on disk is newer than the code the server runs."})
        stale = self._checked_at is None or self._clock() - self._checked_at > REMOTE_CHECK_SECONDS
        if (refresh or stale) and self._head_at_boot:
            self._behind = await asyncio.to_thread(commits_behind, self.root)
            self._checked_at = self._clock()
        if self._behind:
            plural = "s" if self._behind > 1 else ""
            reasons.append({"id": "update", "text": f"An update is available: {self._behind} new commit{plural}."})
        return {
            "needed": bool(reasons),
            "reasons": reasons,
            "in_progress": self.running or self.requested,
            "last": self.last,
        }

    # -- restarting -------------------------------------------------------------------------------------------

    async def restart(self, by: str, now: bool = False) -> dict[str, Any]:
        """Update, then begin stopping so that the server starts again. Raises RestartError, the server going
        on, when a step fails."""
        if self.running or self.requested or self.lifecycle.stopping:
            raise RestartError("The server is already restarting or stopping.")
        self.running = True
        try:
            steps = await asyncio.to_thread(update_code, self.root, self._head_at_boot)
        finally:
            self.running = False
        failed = next((step for step in steps if not step.ok), None)
        if failed:
            log.warning("restart asked by %s refused: %s failed: %s", by, failed.name, failed.output)
            raise RestartError(f"{failed.name} failed, the server was not restarted.\n{failed.output}", 500)
        if self.lifecycle.stopping:  # somebody stopped it meanwhile
            raise RestartError("The server is stopping.")
        record = {
            "id": uuid.uuid4().hex,
            "by": by,
            "requested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "steps": [step.as_dict() for step in steps],
        }
        self._write(record)
        self.requested = True
        log.info("restarting, asked by %s", by)
        message = self.lifecycle.request_stop(force=now)
        return {"id": record["id"], "steps": record["steps"], "message": message}


def relaunch(argv: list[str], old_keys: set[str], console: bool, env_file: Path | None = None) -> None:
    """Start the server again in place of this process. What was loaded from `.env` at the start (`old_keys`) is
    renewed from the file as it is now, because `dotenv` never replaces a setting the environment already has."""
    found = find_dotenv()
    env_file = env_file or (Path(found) if found else None)
    fresh = {key: value for key, value in dotenv_values(env_file).items() if value is not None} if env_file else {}
    for key in old_keys:
        if key in fresh:
            os.environ[key] = fresh[key]
        else:
            os.environ.pop(key, None)
    args = [sys.executable, "-m", "clara", *(arg for arg in argv if arg != "--test")]  # the check was done once
    logging.shutdown()
    if os.name == "nt":  # no exec on Windows: a new process, with its own window when it has a console
        flags = subprocess.CREATE_NEW_CONSOLE if console else subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(args, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL if not console else None)
        return
    os.execv(sys.executable, args)
