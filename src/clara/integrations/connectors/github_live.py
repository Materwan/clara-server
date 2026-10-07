"""A person's GitHub repository, worked on live with their own token (a fine-grained personal access token).

The resource is a repository (and optionally a branch). Everything goes through GitHub's REST API, so a change is a
real commit made as the person, never a local copy:

    read    list a folder, read a file, search the code, list branches
    write   a commit that adds or changes a file on a branch; a branch; a pull request; an issue or a comment
    destructive   a commit to the repository's default branch, deleting a file, merging or closing a pull request,
                  closing an issue, deleting a branch

There is no force-push and nothing that rewrites history. (github.py, the project snapshots, is unrelated.)
"""

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import quote

import httpx

from ...httpclient import SharedClient
from ...ingest import IngestError, extract
from ...projects import read_lines
from ..permissions import DESTRUCTIVE, GITHUB, READ, WRITE
from .base import LIST_LIMIT, SEARCH_LIMIT, Connector, ConnectorError, Target, base_level, check_mode, check_text

API = "https://api.github.com"
TIMEOUT = httpx.Timeout(20.0, read=60.0)
MAX_FILE_BYTES = 30_000_000
MAX_BODY = 20_000  # characters of a pull request or an issue
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_BRANCH = re.compile(r"^[^\s~^:?*\[\\]{1,200}$")


def _text(value: Any, what: str, limit: int = 300) -> str:
    text = str(value or "").strip()
    if not text:
        raise ConnectorError(f"{what} is needed.")
    if len(text) > limit:
        raise ConnectorError(f"{what} is too long.")
    return text


def _branch(value: Any, what: str = "branch") -> str:
    name = _text(value, what, 200)
    if not _BRANCH.match(name) or name.startswith("/") or name.endswith("/") or ".." in name:
        raise ConnectorError(f"{name!r} is not a valid {what} name.")
    return name


def _path(value: Any) -> str:
    """A path inside a repository: no `..`, no leading slash; "" is its top."""
    parts = [part for part in str(value or "").replace("\\", "/").split("/") if part not in ("", ".")]
    if ".." in parts:
        raise ConnectorError("A path may not go up (..).")
    return "/".join(parts)


class GitHubLive(Connector):
    type = GITHUB
    ops = frozenset({"list", "read", "search", "write", "delete", "branch", "pr", "issue"})

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._http = SharedClient(timeout=TIMEOUT, follow_redirects=True, transport=transport)  # transport: tests

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- talking to GitHub ----------------------------------------------------------------------- #
    async def request(
        self, token: str, method: str, url: str, *, accept: str = "application/vnd.github+json", **options: Any
    ) -> httpx.Response:
        headers = {
            "Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "clara-server",
            "Authorization": f"Bearer {token}",
        }
        try:
            response = await self._http.get().request(method, f"{API}{url}", headers=headers, **options)
        except httpx.HTTPError as error:
            raise ConnectorError(f"GitHub could not be reached: {error}") from None
        if response.is_error:
            raise self.problem(response)
        return response

    @staticmethod
    def problem(response: httpx.Response) -> ConnectorError:
        status = response.status_code
        try:
            detail = response.json().get("message", "")
        except ValueError:
            detail = response.reason_phrase
        if status == 401:
            return ConnectorError("GitHub refused the account's token (expired or revoked): connect it again.", True)
        if status in (403, 429) and response.headers.get("x-ratelimit-remaining") == "0":
            return ConnectorError("GitHub says too many requests for now: try again later.")
        if status == 403:
            return ConnectorError(f"GitHub does not let this token do that ({detail}). Its permissions may be too small.")
        if status == 404:
            return ConnectorError("GitHub cannot find that (or the token has no access to it).")
        if status == 409:
            return ConnectorError(f"GitHub refused: {detail} (the file changed meanwhile? read it again).")
        if status == 422:
            return ConnectorError(f"GitHub refused it: {detail}")
        return ConnectorError(f"GitHub answered HTTP {status}: {detail}")

    async def get_json(self, token: str, url: str, **options: Any) -> Any:
        return (await self.request(token, "GET", url, **options)).json()

    # -- the resource -------------------------------------------------------------------------- #
    @staticmethod
    def repo(target: Target) -> str:
        repo = str(target.locator.get("repo", ""))
        if not _NAME.match(repo):
            raise ConnectorError(f"{target.label}: not a GitHub repository.")
        return repo

    async def default_branch(self, target: Target) -> str:
        about = await self.get_json(target.token, f"/repos/{self.repo(target)}")
        return about.get("default_branch") or "main"

    async def branch_of(self, target: Target, args: dict) -> str:
        """The branch an operation is about: the one asked for, else the resource's, else the default."""
        wanted = args.get("branch") or target.locator.get("ref")
        return _branch(wanted) if wanted else await self.default_branch(target)

    def contents_url(self, target: Target, path: str) -> str:
        return f"/repos/{self.repo(target)}/contents/{quote(path)}" if path else f"/repos/{self.repo(target)}/contents"

    # -- levels ------------------------------------------------------------------------------- #
    async def level(self, op: str, target: Target, args: dict) -> str:
        if op in ("list", "read", "search"):
            return READ
        if op in ("write", "delete"):
            if op == "write":
                check_mode(args.get("mode"))
            if op == "delete":
                return DESTRUCTIVE
            branch = await self.branch_of(target, args)
            return DESTRUCTIVE if branch == await self.default_branch(target) else WRITE
        action = str(args.get("action") or "")
        if op == "branch":
            return {"list": READ, "create": WRITE, "delete": DESTRUCTIVE}.get(action, DESTRUCTIVE)
        if op == "pr":
            return {"list": READ, "open": WRITE, "comment": WRITE, "merge": DESTRUCTIVE, "close": DESTRUCTIVE}.get(
                action, DESTRUCTIVE
            )
        if op == "issue":
            return {"list": READ, "create": WRITE, "comment": WRITE, "close": DESTRUCTIVE}.get(action, DESTRUCTIVE)
        return base_level(op)

    def summary(self, op: str, target: Target, args: dict) -> str:
        repo = target.locator.get("repo", target.label)
        where = args.get("branch") or target.locator.get("ref") or "the default branch"
        if op == "write":
            return f"commit ({args.get('mode', 'create')}) {args.get('path')} to {where} of {repo}"
        if op == "delete":
            return f"delete {args.get('path')} from {where} of {repo} (a commit)"
        if op in ("branch", "pr", "issue"):
            detail = args.get("title") or args.get("name") or (f"#{args['number']}" if args.get("number") else "")
            return f"{op} {args.get('action')} {detail} in {repo}".replace("  ", " ")
        return super().summary(op, target, args)

    # -- reading -------------------------------------------------------------------------------- #
    async def op_list(self, target: Target, args: dict) -> str:
        path = _path(args.get("path"))
        ref = {"ref": args["branch"]} if args.get("branch") else ({"ref": target.locator["ref"]} if target.locator.get("ref") else {})
        found = await self.get_json(target.token, self.contents_url(target, path), params=ref)
        if not isinstance(found, list):
            raise ConnectorError(f"{path or '/'} is a file, not a folder.")
        found.sort(key=lambda item: (item.get("type") != "dir", str(item.get("name", "")).lower()))
        lines = [
            f"{item['name']}/" if item.get("type") == "dir" else f"{item['name']} ({item.get('size', 0):,} bytes)"
            for item in found[:LIST_LIMIT]
        ]
        if len(found) > LIST_LIMIT:
            lines.append(f"[{len(found) - LIST_LIMIT} more]")
        return "\n".join(lines) or "(empty)"

    async def op_read(self, target: Target, args: dict) -> str:
        path = _path(args.get("path"))
        if not path:
            raise ConnectorError("Give the path of a file.")
        ref = args.get("branch") or target.locator.get("ref")
        response = await self.request(
            target.token, "GET", self.contents_url(target, path), accept="application/vnd.github.raw+json",
            params={"ref": ref} if ref else {},
        )
        if response.headers.get("content-type", "").startswith("application/json"):
            raise ConnectorError(f"{path} is a folder: list it.")
        if len(response.content) > MAX_FILE_BYTES:
            raise ConnectorError(f"{path} is too big to read.")
        try:
            found = extract(path, response.content)
        except IngestError as error:
            raise ConnectorError(f"{path}: {error}.") from None
        end = int(args["end_line"]) if args.get("end_line") else None
        return read_lines(path, found.text, int(args.get("start_line") or 1), end)

    async def op_search(self, target: Target, args: dict) -> str:
        """GitHub's code search: the default branch only, words not regular expressions."""
        query = _text(args.get("query"), "query", 256)
        folder = _path(args.get("path"))
        q = f"{query} repo:{self.repo(target)}" + (f" path:{folder}" if folder else "")
        response = await self.request(
            target.token, "GET", "/search/code", accept="application/vnd.github.text-match+json",
            params={"q": q, "per_page": 30},
        )
        items = response.json().get("items", [])
        if not items:
            return f"No match for {query!r} (GitHub searches the default branch only)."
        lines = []
        for item in items[:SEARCH_LIMIT]:
            fragment = next((m.get("fragment", "") for m in item.get("text_matches", []) if m.get("fragment")), "")
            first = next((line.strip() for line in fragment.splitlines() if query.lower() in line.lower()), "")
            lines.append(f"{item['path']}: {first[:200]}" if first else item["path"])
        return f"Matches in {len(items)} file(s) (default branch):\n" + "\n".join(lines)

    # -- changing files ------------------------------------------------------------------------ #
    async def existing(self, target: Target, path: str, branch: str) -> dict | None:
        """The file's record (with its `sha`) on the branch, or None when it is not there."""
        try:
            found = await self.get_json(target.token, self.contents_url(target, path), params={"ref": branch})
        except ConnectorError as error:
            if "cannot find" in str(error):
                return None
            raise
        if isinstance(found, list):
            raise ConnectorError(f"{path} is a folder.")
        return found

    async def op_write(self, target: Target, args: dict) -> str:
        mode = check_mode(args.get("mode"))
        content = check_text(args.get("content"))
        path = _path(args.get("path"))
        if not path:
            raise ConnectorError("Give the path of the file to write.")
        branch = await self.branch_of(target, args)
        current = await self.existing(target, path, branch)
        if mode == "create" and current is not None:
            raise ConnectorError(f"{path} already exists on {branch}: use mode overwrite or append.")
        if mode == "append" and current is not None:
            old = await self.request(
                target.token, "GET", self.contents_url(target, path), accept="application/vnd.github.raw+json",
                params={"ref": branch},
            )
            content = old.text + content
        message = _text(args.get("message") or f"Clara: {mode} {path}", "message", 300)
        body: dict[str, Any] = {
            "message": message, "content": base64.b64encode(content.encode("utf-8")).decode(), "branch": branch,
        }
        if current is not None:
            body["sha"] = current["sha"]
        response = await self.request(target.token, "PUT", self.contents_url(target, path), json=body)
        commit = response.json().get("commit", {}).get("sha", "")[:7]
        verb = "Updated" if current is not None else "Created"
        return f"{verb} {path} on {branch} (commit {commit})."

    async def op_delete(self, target: Target, args: dict) -> str:
        path = _path(args.get("path"))
        if not path:
            raise ConnectorError("Give the path of the file to delete.")
        branch = await self.branch_of(target, args)
        current = await self.existing(target, path, branch)
        if current is None:
            raise ConnectorError(f"No such file on {branch}: {path}.")
        message = _text(args.get("message") or f"Clara: delete {path}", "message", 300)
        response = await self.request(
            target.token, "DELETE", self.contents_url(target, path),
            json={"message": message, "sha": current["sha"], "branch": branch},
        )
        return f"Deleted {path} from {branch} (commit {response.json().get('commit', {}).get('sha', '')[:7]})."

    # -- branches, pull requests, issues --------------------------------------------------------- #
    async def op_branch(self, target: Target, args: dict) -> str:
        repo = self.repo(target)
        action = str(args.get("action") or "")
        if action == "list":
            found = await self.get_json(target.token, f"/repos/{repo}/branches", params={"per_page": 100})
            return "\n".join(item["name"] for item in found[:LIST_LIMIT]) or "(no branch)"
        name = _branch(args.get("name"))
        if action == "create":
            base = _branch(args["from"]) if args.get("from") else await self.default_branch(target)
            head = await self.get_json(target.token, f"/repos/{repo}/git/ref/heads/{quote(base, safe='/')}")
            await self.request(
                target.token, "POST", f"/repos/{repo}/git/refs",
                json={"ref": f"refs/heads/{name}", "sha": head["object"]["sha"]},
            )
            return f"Created the branch {name} from {base}."
        if action == "delete":
            if name == await self.default_branch(target):
                raise ConnectorError("The default branch cannot be deleted.")
            await self.request(target.token, "DELETE", f"/repos/{repo}/git/refs/heads/{quote(name, safe='/')}")
            return f"Deleted the branch {name}."
        raise ConnectorError("action must be list, create or delete.")

    async def op_pr(self, target: Target, args: dict) -> str:
        repo = self.repo(target)
        action = str(args.get("action") or "")
        if action == "list":
            found = await self.get_json(target.token, f"/repos/{repo}/pulls", params={"state": "open", "per_page": 30})
            return "\n".join(f"#{p['number']} {p['title']} ({p['head']['ref']} -> {p['base']['ref']})" for p in found) or "No open pull request."
        if action == "open":
            body = {
                "title": _text(args.get("title"), "title"),
                "head": _branch(args.get("head"), "head branch"),
                "base": _branch(args["base"]) if args.get("base") else await self.default_branch(target),
                "body": str(args.get("body") or "")[:MAX_BODY],
            }
            made = (await self.request(target.token, "POST", f"/repos/{repo}/pulls", json=body)).json()
            return f"Opened pull request #{made['number']}: {made['html_url']}"
        number = int(args.get("number") or 0)
        if number <= 0:
            raise ConnectorError("number is needed.")
        if action == "comment":
            await self.request(
                target.token, "POST", f"/repos/{repo}/issues/{number}/comments",
                json={"body": _text(args.get("body"), "body", MAX_BODY)},
            )
            return f"Commented on #{number}."
        if action == "merge":
            await self.request(target.token, "PUT", f"/repos/{repo}/pulls/{number}/merge", json={})
            return f"Merged pull request #{number}."
        if action == "close":
            await self.request(target.token, "PATCH", f"/repos/{repo}/pulls/{number}", json={"state": "closed"})
            return f"Closed pull request #{number}."
        raise ConnectorError("action must be list, open, comment, merge or close.")

    async def op_issue(self, target: Target, args: dict) -> str:
        repo = self.repo(target)
        action = str(args.get("action") or "")
        if action == "list":
            found = await self.get_json(target.token, f"/repos/{repo}/issues", params={"state": "open", "per_page": 30})
            return "\n".join(f"#{i['number']} {i['title']}" for i in found if "pull_request" not in i) or "No open issue."
        if action == "create":
            body = {"title": _text(args.get("title"), "title"), "body": str(args.get("body") or "")[:MAX_BODY]}
            made = (await self.request(target.token, "POST", f"/repos/{repo}/issues", json=body)).json()
            return f"Opened issue #{made['number']}: {made['html_url']}"
        number = int(args.get("number") or 0)
        if number <= 0:
            raise ConnectorError("number is needed.")
        if action == "comment":
            await self.request(
                target.token, "POST", f"/repos/{repo}/issues/{number}/comments",
                json={"body": _text(args.get("body"), "body", MAX_BODY)},
            )
            return f"Commented on #{number}."
        if action == "close":
            await self.request(target.token, "PATCH", f"/repos/{repo}/issues/{number}", json={"state": "closed"})
            return f"Closed issue #{number}."
        raise ConnectorError("action must be list, create, comment or close.")

