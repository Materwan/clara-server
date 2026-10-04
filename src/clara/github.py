"""GitHub repositories in a project: the server downloads a snapshot (the archive GitHub makes of a commit) and
keeps its text files. No git is needed, and nothing is kept of the history.

Public repositories need nothing; private ones need GITHUB_TOKEN in the server's environment (a fine-grained
token with read access to the repositories' contents is enough). The token never leaves the server.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

API = "https://api.github.com"
MAX_ARCHIVE_BYTES = 300_000_000  # a repository's archive bigger than this is not downloaded
TIMEOUT = httpx.Timeout(20.0, read=120.0)

_NAME = r"[A-Za-z0-9_.-]{1,100}"
_URL = re.compile(
    rf"^(?:https?://)?(?:www\.)?github\.com[/:](?P<owner>{_NAME})/(?P<name>{_NAME}?)(?:\.git)?"
    r"(?:/(?:tree|commit|blob)/(?P<ref>[^?#]+?))?/?(?:[?#].*)?$"
)
_SSH = re.compile(rf"^git@github\.com:(?P<owner>{_NAME})/(?P<name>{_NAME}?)(?:\.git)?$")
_SHORT = re.compile(rf"^(?P<owner>{_NAME})/(?P<name>{_NAME}?)(?:\.git)?(?:@(?P<ref>\S+))?$")


class GitHubError(Exception):
    """The repository could not be downloaded: the message says why, for the person."""


@dataclass(frozen=True)
class Snapshot:
    repo: str  # owner/name, as GitHub spells it
    ref: str  # the branch or tag downloaded
    commit_sha: str
    archive: bytes  # a .zip, every file in one top folder


def parse_repo(text: str) -> tuple[str, str]:
    """`(owner/name, ref)` from what a person types: `owner/name`, `owner/name@branch`, a github.com URL (with
    `/tree/<branch>` or not) or a git@github.com address. `ref` is "" when none is given."""
    text = text.strip()
    for pattern in (_URL, _SSH, _SHORT):
        match = pattern.match(text)
        if match and match.group("name"):
            ref = (match.groupdict().get("ref") or "").strip("/")
            return f"{match.group('owner')}/{match.group('name')}", ref
    raise GitHubError(f"{text!r} is not a GitHub repository: give owner/name or its github.com address.")


class GitHub:
    def __init__(self, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self._token = token
        self._transport = transport  # tests answer from here

    def _client(self) -> httpx.AsyncClient:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "clara-server"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return httpx.AsyncClient(headers=headers, timeout=TIMEOUT, follow_redirects=True, transport=self._transport)

    def _problem(self, response: httpx.Response, repo: str, ref: str = "") -> GitHubError:
        if response.status_code == 404:
            if ref:
                return GitHubError(f"{repo} has no branch, tag or commit {ref!r} (or it is private).")
            hint = "" if self._token else ": a private repository needs GITHUB_TOKEN in the server's .env"
            return GitHubError(f"{repo} was not found on GitHub{hint}.")
        if response.status_code == 401:
            return GitHubError("GitHub refused the server's GITHUB_TOKEN (expired or revoked?).")
        if response.status_code in (403, 429) and response.headers.get("x-ratelimit-remaining") == "0":
            hint = "" if self._token else " Set GITHUB_TOKEN in the server's .env to get more."
            return GitHubError(f"Too many requests to GitHub for now: try again later.{hint}")
        try:
            detail = response.json().get("message", "")
        except ValueError:
            detail = response.reason_phrase
        return GitHubError(f"GitHub answered HTTP {response.status_code} for {repo}: {detail}")

    async def snapshot(self, repo: str, ref: str = "") -> Snapshot:
        """Download a repository at `ref` (its default branch when empty)."""
        try:
            async with self._client() as client:
                response = await client.get(f"{API}/repos/{repo}")
                if response.is_error:
                    raise self._problem(response, repo)
                about = response.json()
                repo = about.get("full_name") or repo
                ref = ref or about.get("default_branch") or "main"
                response = await client.get(
                    f"{API}/repos/{repo}/commits/{ref}", headers={"Accept": "application/vnd.github.sha"}
                )
                if response.is_error:
                    raise self._problem(response, repo, ref)
                sha = response.text.strip()
                async with client.stream("GET", f"{API}/repos/{repo}/zipball/{sha}") as download:
                    if download.is_error:
                        await download.aread()
                        raise self._problem(download, repo, ref)
                    declared = int(download.headers.get("content-length") or 0)
                    if declared > MAX_ARCHIVE_BYTES:
                        raise GitHubError(f"{repo} is too big to download ({declared // 1_000_000} MB).")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in download.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_ARCHIVE_BYTES:
                            raise GitHubError(f"{repo} is too big to download (over {MAX_ARCHIVE_BYTES // 1_000_000} MB).")
                        chunks.append(chunk)
        except httpx.HTTPError as error:
            raise GitHubError(f"GitHub could not be reached: {error}") from None
        return Snapshot(repo, ref, sha, b"".join(chunks))
