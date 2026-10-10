"""The vault is a git repository shared with the person's Obsidian (usually through the Obsidian Git plugin).

Clara pulls before she works (at most once per `pull_every` seconds for reads, always before a write), commits every
change of hers as its own commit, and pushes a few seconds after the last one. Nothing here ever force-pushes, resets
or discards: when a pull or a push cannot be done cleanly it is aborted, the problem is remembered, and the person is
told (`vault_sync`, `/vault`).
"""

from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

TIMEOUT = 90  # seconds for one git command (a push to a slow remote)
PUSH_DELAY = 5.0  # seconds after the last change before the push (several changes make one push)
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


class GitError(Exception):
    pass


@dataclass(frozen=True)
class GitStatus:
    branch: str
    remote: bool  # an `origin` exists
    ahead: int  # commits not pushed yet
    behind: int  # commits of the remote not pulled yet (as of the last fetch)
    dirty: list[str]  # files changed and not committed
    last_pull: float | None
    last_push: float | None
    error: str  # the last problem ("" : none)

    def describe(self) -> str:
        lines = [f"branch {self.branch or '(none yet)'}" + ("" if self.remote else ", no remote (local only)")]
        if self.remote:
            lines.append(f"{self.ahead} commit(s) to push, {self.behind} to pull")
        if self.dirty:
            lines.append(f"{len(self.dirty)} file(s) changed but not committed: {', '.join(self.dirty[:5])}")
        for label, moment in (("last pull", self.last_pull), ("last push", self.last_push)):
            if moment:
                lines.append(f"{label} {int(time.time() - moment)} s ago")
        if self.error:
            lines.append(f"PROBLEM: {self.error}")
        return "; ".join(lines)


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class GitSync:
    def __init__(
        self,
        root: Path,
        *,
        push: bool = True,
        pull_every: float = 30.0,
        push_delay: float = PUSH_DELAY,
        author: str = "Clara",
        email: str = "clara@localhost",
        token: str = "",
        runner: Runner = subprocess.run,
        clock: Callable[[], float] = time.time,
    ):
        self.root = root
        self.push_enabled = push
        self.pull_every = pull_every
        self.push_delay = push_delay
        self.author = author
        self.email = email
        self._token = token
        self._run = runner
        self._clock = clock
        self.lock = threading.RLock()  # the vault takes it for every write: git and the files change together
        self._last_pull: float | None = None
        self._last_pull_try = 0.0
        self._last_push: float | None = None
        self.error = ""
        self._timer: threading.Timer | None = None
        self._closed = False

    # --- running git ----------------------------------------------------------------------------------
    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "LC_ALL": "C", "GIT_EDITOR": "true"})
        if self._token:  # an access token for https remotes, passed in the environment (not visible in `ps`)
            credentials = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
            env.update({
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
            })
        return env

    def git(self, *args: str, check: bool = True, timeout: float = TIMEOUT) -> str:
        identity = ["-c", f"user.name={self.author}", "-c", f"user.email={self.email}", "-c", "core.quotepath=off"]
        try:
            done = self._run(
                ["git", *identity, *args], cwd=self.root, env=self._env(), capture_output=True, text=True,
                timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]} took more than {int(timeout)} s") from None
        except FileNotFoundError:
            raise GitError("git is not installed on the server") from None
        if check and done.returncode != 0:
            raise GitError((done.stderr or done.stdout or f"git {args[0]} failed").strip().splitlines()[-1])
        return done.stdout

    def is_repository(self) -> bool:
        try:
            return self.git("rev-parse", "--is-inside-work-tree", check=False).strip() == "true" and (
                self.root / ".git"
            ).exists()
        except GitError:
            return False

    def branch(self) -> str:
        try:
            return self.git("symbolic-ref", "--short", "HEAD").strip()
        except GitError:
            return ""

    def has_remote(self) -> bool:
        return bool(self.git("remote", check=False).split())

    def _remote_has_branch(self, branch: str) -> bool:
        try:
            return bool(self.git("ls-remote", "--heads", "origin", branch).strip())
        except GitError as error:
            self.error = f"cannot reach the remote: {error}"
            return False

    # --- status ---------------------------------------------------------------------------------------
    def status(self) -> GitStatus:
        with self.lock:
            branch = self.branch()
            remote = self.has_remote()
            ahead = behind = 0
            if branch and remote:
                counts = self.git("rev-list", "--left-right", "--count", f"HEAD...origin/{branch}", check=False).split()
                if len(counts) == 2:
                    ahead, behind = int(counts[0]), int(counts[1])
                elif self.git("rev-parse", "--verify", "-q", "HEAD", check=False).strip():
                    ahead = int(self.git("rev-list", "--count", "HEAD", check=False).strip() or 0)
            dirty = [line[3:] for line in self.git("status", "--porcelain", check=False).splitlines() if line.strip()]
            return GitStatus(branch, remote, ahead, behind, dirty, self._last_pull, self._last_push, self.error)

    # --- pull -----------------------------------------------------------------------------------------
    def pull(self, force: bool = False) -> str:
        """Bring in what the person changed. Cheap when called often: it only goes to the network every
        `pull_every` seconds, unless `force`. Returns a message when something notable happened, else ''."""
        with self.lock:
            now = self._clock()
            if not force and now - self._last_pull_try < self.pull_every:
                return ""
            self._last_pull_try = now
            branch = self.branch()
            if not self.has_remote():
                return ""
            if not branch:
                branch = "main"
            if not self._remote_has_branch(branch):
                return ""  # an empty remote: nothing to bring
            try:
                if not self.git("rev-parse", "--verify", "-q", "HEAD", check=False).strip():
                    # nothing committed here yet: take the remote as it is
                    self.git("fetch", "origin", branch)
                    self.git("checkout", "-B", branch, f"origin/{branch}")
                else:
                    self.git("pull", "--rebase", "--autostash", "origin", branch)
            except GitError as error:
                self.git("rebase", "--abort", check=False)
                self.error = f"pull failed, resolve it in the repository: {error}"
                log.warning("vault: %s", self.error)
                return self.error
            self._last_pull = self._clock()
            if self.error.startswith(("pull failed", "cannot reach")):
                self.error = ""
            return ""

    # --- commit and push ------------------------------------------------------------------------------
    def commit(self, paths: Sequence[str], message: str) -> bool:
        """Commit these paths (new, changed or deleted) as one commit. False when there was nothing to commit."""
        with self.lock:
            known = [
                path for path in dict.fromkeys(paths)
                if (self.root / path).exists() or self.git("ls-files", "--", path, check=False).strip()
            ]
            if not known:
                return False
            self.git("add", "-A", "--", *known)
            if not self.git("diff", "--cached", "--name-only").strip():
                return False
            self.git("commit", "-q", "-m", message)
        self._schedule_push()
        return True

    def _schedule_push(self) -> None:
        if not self.push_enabled or self._closed:
            return
        with self.lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.push_delay, self.push)
            self._timer.daemon = True
            self._timer.start()

    def push(self) -> str:
        """Send the commits to the remote; if the remote moved, take its changes first. '' or a problem text."""
        with self.lock:
            self._timer = None
            if not self.push_enabled or not self.has_remote():
                return ""
            branch = self.branch()
            if not branch:
                return ""
            try:
                try:
                    self.git("push", "-u", "origin", branch)
                except GitError:
                    if self._remote_has_branch(branch):
                        self.git("pull", "--rebase", "--autostash", "origin", branch)
                    self.git("push", "-u", "origin", branch)
            except GitError as error:
                self.git("rebase", "--abort", check=False)
                self.error = f"push failed, the change is saved here and will be sent later: {error}"
                log.warning("vault: %s", self.error)
                return self.error
            self._last_push = self._clock()
            if self.error.startswith(("push failed", "pull failed")):
                self.error = ""
            return ""

    def flush(self) -> str:
        """Push now (what is waiting for the delay)."""
        with self.lock:
            if self._timer is not None:
                self._timer.cancel()
            return self.push()

    def close(self) -> None:
        self._closed = True
        with self.lock:
            waiting = self._timer is not None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if waiting:
            self.push()

    # --- history --------------------------------------------------------------------------------------
    def log(self, path: str, limit: int = 15) -> list[tuple[str, str, str, str]]:
        """(commit, date, author, subject) of the changes of one file, newest first."""
        out = self.git(
            "log", f"-n{int(limit)}", "--follow", "--date=short", "--format=%h%x09%ad%x09%an%x09%s", "--", path,
            check=False,
        )
        rows = []
        for line in out.splitlines():
            parts = line.split("\t", 3)
            if len(parts) == 4:
                rows.append((parts[0], parts[1], parts[2], parts[3]))
        return rows

    def deleted_path(self, name: str) -> str:
        """The path a note called `name` had when it was deleted (the latest), or ''."""
        out = self.git("log", "--diff-filter=D", "--name-only", "--pretty=format:", "-n", "300", check=False)
        wanted = name.strip().lower()
        for line in out.splitlines():
            line = line.strip()
            if line.lower().endswith(".md") and line.rsplit("/", 1)[-1][:-3].lower() == wanted:
                return line
        return ""

    def show(self, commit: str, path: str) -> str:
        if not COMMIT_RE.match(commit):
            raise GitError("that is not a commit id (use one from the history)")
        return self.git("show", f"{commit}:{path}")

    def exists_in_head(self, path: str) -> bool:
        return bool(self.git("ls-files", "--", path, check=False).strip())


def init_repository(root: Path, branch: str = "main") -> None:
    """Make the folder a git repository (used by tests and by `/vault init`)."""
    subprocess.run(["git", "init", "-q", "-b", branch, str(root)], check=True, capture_output=True)
