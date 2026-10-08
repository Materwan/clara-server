"""GitHub repositories in a project: the server downloads a snapshot (the archive GitHub makes of a commit) and
keeps its text files. No git is needed, and nothing is kept of the history.

Public repositories need nothing; a private one needs a token: the person's own GitHub account (Integrations page)
or, for administrators (and everybody with CLARA_GITHUB_TOKEN_SHARED), the operator's GITHUB_TOKEN. The token never
leaves the server.
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


_REF = re.compile(r"^[A-Za-z0-9._+@-]+(?:/[A-Za-z0-9._+@-]+)*$")


class GitHubError(Exception):
    """The repository could not be downloaded: the message says why, for the person."""


def check_ref(ref: str) -> str:
    """A branch, tag or commit as it may go in a GitHub API path: "" (none) or names separated by `/`, never `..`
    (the HTTP client would resolve it, and the request would reach another endpoint of GitHub's API)."""
    ref = (ref or "").strip().strip("/")
    if ref and (len(ref) > 200 or not _REF.match(ref) or any(part in (".", "..") for part in ref.split("/"))):
        raise GitHubError(f"{ref[:60]!r} is not a valid branch, tag or commit name.")
    return ref


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
            ref = check_ref(match.groupdict().get("ref") or "")
            return f"{match.group('owner')}/{match.group('name')}", ref
    raise GitHubError(f"{text!r} is not a GitHub repository: give owner/name or its github.com address.")


class GitHub:
    def __init__(self, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self._token = token
        self._transport = transport  # tests answer from here

    def _client(self, token: str | None = None) -> httpx.AsyncClient:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "clara-server"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.AsyncClient(headers=headers, timeout=TIMEOUT, follow_redirects=True, transport=self._transport)

    def _problem(self, response: httpx.Response, repo: str, ref: str = "", token: str | None = None) -> GitHubError:
        if response.status_code == 404:
            if ref:
                return GitHubError(f"{repo} has no branch, tag or commit {ref!r} (or it is private).")
            hint = "" if token else ": a private repository needs a GitHub account connected on the Integrations page"
            return GitHubError(f"{repo} was not found on GitHub{hint}.")
        if response.status_code == 401:
            return GitHubError("GitHub refused the token (expired or revoked?).")
        if response.status_code in (403, 429) and response.headers.get("x-ratelimit-remaining") == "0":
            hint = "" if token else " Connect a GitHub account on the Integrations page to get more."
            return GitHubError(f"Too many requests to GitHub for now: try again later.{hint}")
        try:
            detail = response.json().get("message", "")
        except ValueError:
            detail = response.reason_phrase
        return GitHubError(f"GitHub answered HTTP {response.status_code} for {repo}: {detail}")

    @property
    def server_token(self) -> str | None:
        """The operator's GITHUB_TOKEN. It is the operator's: see `Settings.github_token_shared`."""
        return self._token

    async def snapshot(self, repo: str, ref: str = "", token: str | None = None) -> Snapshot:
        """Download a repository at `ref` (its default branch when empty), with `token` ("" or None: as anyone, public
        repositories only)."""
        ref = check_ref(ref)
        try:
            async with self._client(token) as client:
                response = await client.get(f"{API}/repos/{repo}")
                if response.is_error:
                    raise self._problem(response, repo, token=token)
                about = response.json()
                repo = about.get("full_name") or repo
                ref = ref or about.get("default_branch") or "main"
                response = await client.get(
                    f"{API}/repos/{repo}/commits/{ref}", headers={"Accept": "application/vnd.github.sha"}
                )
                if response.is_error:
                    raise self._problem(response, repo, ref, token)
                sha = response.text.strip()
                async with client.stream("GET", f"{API}/repos/{repo}/zipball/{sha}") as download:
                    if download.is_error:
                        await download.aread()
                        raise self._problem(download, repo, ref, token)
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
