"""A person's Google Drive: a folder (with everything under it) or a single file, worked on live with their own account.

The person connects once with Google's OAuth (the administrator's Google client is in the server's `.env`); Clara keeps
the refresh token, encrypted, and asks Google for short-lived access tokens when she needs one. Everything is limited to
the folder (or file) that was attached: a file outside it is refused, even by its id.

    read    list a folder, read a file (Google Docs, Sheets and Slides are exported as text), search
    write   create a file or a folder, append to a file
    destructive   replace a file's content, rename or move, move to the trash

"Delete" puts a file in Drive's trash: it can be restored there. Native Google Docs, Sheets and Slides cannot be
replaced as plain text; Clara writes a new file next to them instead.
"""

from __future__ import annotations

import json
import mimetypes
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from ...ingest import IngestError, extract
from ...projects import read_lines
from ..permissions import DESTRUCTIVE, GDRIVE, WRITE
from .base import LIST_LIMIT, SEARCH_LIMIT, Connector, ConnectorError, Target, base_level, check_mode, check_text

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
SCOPE = "https://www.googleapis.com/auth/drive"
TIMEOUT = httpx.Timeout(20.0, read=90.0)
FOLDER = "application/vnd.google-apps.folder"
NATIVE = "application/vnd.google-apps."
EXPORTS = {  # native types that can be read as text
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
MAX_FILE_BYTES = 30_000_000
SEARCH_FOLDERS = 40  # sub-folders a search looks in
DEPTH = 20  # levels up a file is followed to check it is under the attached folder
FIELDS = "id,name,mimeType,size,parents,trashed"


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Where the person goes to allow Clara to use their Drive."""
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": f"{SCOPE} email",
        "access_type": "offline", "prompt": "consent", "state": state, "include_granted_scopes": "true",
    })


def quoted(text: str) -> str:
    """A value inside a Drive query: between single quotes, with ' and \\ escaped."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def segments(path: object) -> list[str]:
    """The names of a path inside the attached folder (an `id:...` segment names a file by its id)."""
    parts = [part.strip() for part in str(path or "").replace("\\", "/").split("/") if part.strip() not in ("", ".")]
    if ".." in parts:
        raise ConnectorError("A path may not go up (..).")
    return parts


class GoogleDrive(Connector):
    type = GDRIVE
    ops = frozenset({"list", "read", "search", "write", "delete", "move"})

    def __init__(self, client_id: str, client_secret: str, transport: httpx.AsyncBaseTransport | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self._transport = transport  # tests answer from here
        self._access: dict[str, tuple[str, float]] = {}  # refresh token -> (access token, expires at)

    # -- talking to Google ----------------------------------------------------------------------- #
    async def _send(self, method: str, url: str, headers: dict, **options: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, transport=self._transport) as client:
                return await client.request(method, url, headers=headers, **options)
        except httpx.HTTPError as error:
            raise ConnectorError(f"Google could not be reached: {error}") from None

    async def token_request(self, data: dict) -> dict:
        """The token endpoint: a code or a refresh token against tokens."""
        response = await self._send(
            "POST", TOKEN_URL, {}, data={**data, "client_id": self.client_id, "client_secret": self.client_secret}
        )
        try:
            found = response.json()
        except ValueError:
            found = {}
        if response.is_error:
            if found.get("error") == "invalid_grant":
                raise ConnectorError(
                    "Google no longer accepts this connection (the access was removed, or the Google app is still in "
                    "'Testing' mode, where it ends after 7 days): connect it again.", True,
                )
            raise ConnectorError(f"Google refused: {found.get('error_description') or found.get('error') or response.status_code}")
        return found

    async def access_token(self, refresh_token: str) -> str:
        cached = self._access.get(refresh_token)
        if cached and cached[1] > time.monotonic() + 60:
            return cached[0]
        found = await self.token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
        token = found["access_token"]
        self._access[refresh_token] = (token, time.monotonic() + int(found.get("expires_in", 3000)))
        return token

    async def request(self, refresh_token: str, method: str, url: str, **options: Any) -> httpx.Response:
        extra = options.pop("headers", {})
        access = await self.access_token(refresh_token)
        response = await self._send(method, url, {**extra, "Authorization": f"Bearer {access}"}, **options)
        if response.status_code == 401:  # the cached token ended early: once more with a fresh one
            self._access.pop(refresh_token, None)
            access = await self.access_token(refresh_token)
            response = await self._send(method, url, {**extra, "Authorization": f"Bearer {access}"}, **options)
        if response.is_error:
            raise self.problem(response)
        return response

    @staticmethod
    def problem(response: httpx.Response) -> ConnectorError:
        status = response.status_code
        try:
            error = response.json().get("error", {})
            detail = error.get("message", "") if isinstance(error, dict) else str(error)
            reason = (error.get("errors") or [{}])[0].get("reason", "") if isinstance(error, dict) else ""
        except (ValueError, AttributeError):
            detail = reason = ""
        if status == 401:
            return ConnectorError("Google refused the connection: connect it again.", True)
        if status == 403 and reason in ("accessNotConfigured", "SERVICE_DISABLED"):
            return ConnectorError("The Google Drive API is not turned on in the administrator's Google project.")
        if status == 403 and reason in ("rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded"):
            return ConnectorError("Google says too many requests for now: try again later.")
        if status == 403:
            return ConnectorError(f"Google does not allow that ({detail or 'forbidden'}).")
        if status == 404:
            return ConnectorError("Google Drive cannot find that file (or it is not shared with this account).")
        return ConnectorError(f"Google Drive answered HTTP {status}: {detail}")

    async def get(self, target: Target, url: str, **params: Any) -> Any:
        wanted = {key: value for key, value in params.items() if value is not None}  # httpx would send "key="
        return (await self.request(target.token, "GET", url, params=wanted)).json()

    # -- finding things ------------------------------------------------------------------------ #
    @staticmethod
    def root(target: Target) -> str:
        file_id = str(target.locator.get("id", ""))
        if not file_id:
            raise ConnectorError(f"{target.label}: not a Drive file.")
        return file_id

    async def meta(self, target: Target, file_id: str) -> dict:
        return await self.get(target, f"{API}/files/{file_id}", fields=FIELDS, supportsAllDrives="true")

    async def children(self, target: Target, folder_id: str, extra: str = "") -> list[dict]:
        found: list[dict] = []
        token = ""
        while len(found) < 1000:
            page = await self.get(
                target, f"{API}/files", q=f"{quoted(folder_id)} in parents and trashed = false{extra}",
                fields=f"nextPageToken,files({FIELDS})", pageSize=200, pageToken=token or None, orderBy="folder,name",
                supportsAllDrives="true", includeItemsFromAllDrives="true",
            )
            found += page.get("files", [])
            token = page.get("nextPageToken", "")
            if not token:
                break
        return found

    async def contained(self, target: Target, file_id: str) -> bool:
        """Is the file the attached folder, or under it? (Followed up through its parents.)"""
        root = self.root(target)
        current = file_id
        for _ in range(DEPTH):
            if current == root:
                return True
            parents = (await self.meta(target, current)).get("parents") or []
            if not parents:
                return False
            current = parents[0]
        return False

    async def resolve(self, target: Target, path: object, must_exist: bool = True) -> tuple[dict | None, str, str]:
        """(the file's record or None, its parent folder's id, its name) for a path inside the attached folder."""
        root = self.root(target)
        names = segments(path)
        info = await self.meta(target, root)
        if info.get("trashed"):
            raise ConnectorError(f"{target.label} is in the trash.")
        if not names:
            return info, "", info["name"]
        if info["mimeType"] != FOLDER:
            raise ConnectorError(f"{target.label} is a single file: use an empty path.")
        parent = root
        for index, name in enumerate(names):
            last = index == len(names) - 1
            if name.startswith("id:"):
                found = await self.meta(target, name[3:])
                if not await self.contained(target, found["id"]):
                    raise ConnectorError(f"{name} is not inside {target.label}.")
                if found.get("trashed"):
                    raise ConnectorError(f"{name} is in the trash.")
            else:
                matches = [
                    c for c in await self.children(target, parent, f" and name = {quoted(name)}")
                ]
                if len(matches) > 1:
                    ids = ", ".join(f"id:{m['id']}" for m in matches[:5])
                    raise ConnectorError(f"Several files are named {name!r} here: use one of {ids} in the path.")
                found = matches[0] if matches else None
                if found is None:
                    if not must_exist:  # to be made: the folders above it too, when it is written
                        return None, parent if last else "", names[-1]
                    raise ConnectorError(f"No such file or folder: {'/'.join(names[: index + 1])}.")
            if not last:
                if found["mimeType"] != FOLDER:
                    raise ConnectorError(f"{name} is not a folder.")
                parent = found["id"]
            else:
                return found, (found.get("parents") or [parent])[0], found["name"]
        raise ConnectorError("Not a usable path.")  # (unreachable: the loop returns on the last name)

    async def folder_path(self, target: Target, path: object) -> str:
        """The id of the folder at `path`, made (with the folders above it) when it does not exist."""
        parent = self.root(target)
        for name in segments(path):
            matches = await self.children(target, parent, f" and name = {quoted(name)} and mimeType = {quoted(FOLDER)}")
            if matches:
                parent = matches[0]["id"]
                continue
            made = await self.request(
                target.token, "POST", f"{API}/files", params={"supportsAllDrives": "true", "fields": "id"},
                json={"name": name, "mimeType": FOLDER, "parents": [parent]},
            )
            parent = made.json()["id"]
        return parent

    # -- levels ------------------------------------------------------------------------------- #
    async def level(self, op: str, target: Target, args: dict) -> str:
        if op != "write":
            return base_level(op)
        mode = check_mode(args.get("mode"))
        if mode != "overwrite":
            return WRITE
        found, _, _ = await self.resolve(target, args.get("path"), must_exist=False)
        return DESTRUCTIVE if found is not None else WRITE

    def summary(self, op: str, target: Target, args: dict) -> str:
        if op == "delete":
            return f"move {args.get('path') or '/'} to the trash in {target.label}"
        return super().summary(op, target, args)

    # -- operations ---------------------------------------------------------------------------- #
    @staticmethod
    def line(item: dict) -> str:
        if item["mimeType"] == FOLDER:
            return f"{item['name']}/ [id:{item['id']}]"
        size = f" ({int(item['size']):,} bytes)" if item.get("size") else ""
        kind = " (Google " + item["mimeType"].removeprefix(NATIVE) + ")" if item["mimeType"].startswith(NATIVE) else ""
        return f"{item['name']}{kind}{size} [id:{item['id']}]"

    async def op_list(self, target: Target, args: dict) -> str:
        found, _, _ = await self.resolve(target, args.get("path"))
        assert found is not None
        if found["mimeType"] != FOLDER:
            return self.line(found)
        items = await self.children(target, found["id"])
        lines = [self.line(item) for item in items[:LIST_LIMIT]]
        if len(items) > LIST_LIMIT:
            lines.append(f"[{len(items) - LIST_LIMIT} more]")
        return "\n".join(lines) or "(empty)"

    async def text_of(self, target: Target, found: dict) -> str:
        mime = found["mimeType"]
        if mime == FOLDER:
            raise ConnectorError(f"{found['name']} is a folder: list it.")
        if mime in EXPORTS:
            response = await self.request(
                target.token, "GET", f"{API}/files/{found['id']}/export", params={"mimeType": EXPORTS[mime]}
            )
            return response.text
        if mime.startswith(NATIVE):
            raise ConnectorError(f"{found['name']} is a Google {mime.removeprefix(NATIVE)}: it cannot be read as text.")
        if int(found.get("size") or 0) > MAX_FILE_BYTES:
            raise ConnectorError(f"{found['name']} is too big to read.")
        response = await self.request(target.token, "GET", f"{API}/files/{found['id']}", params={"alt": "media"})
        try:
            return extract(found["name"], response.content).text
        except IngestError as error:
            raise ConnectorError(f"{found['name']}: {error}.") from None

    async def op_read(self, target: Target, args: dict) -> str:
        found, _, _ = await self.resolve(target, args.get("path"))
        assert found is not None
        text = await self.text_of(target, found)
        end = int(args["end_line"]) if args.get("end_line") else None
        return read_lines(found["name"], text, int(args.get("start_line") or 1), end)

    async def op_search(self, target: Target, args: dict) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ConnectorError("query is empty.")
        start, _, _ = await self.resolve(target, args.get("path"))
        assert start is not None
        if start["mimeType"] != FOLDER:
            folders = []
        else:
            folders, queue = [start["id"]], [start["id"]]
            while queue and len(folders) < SEARCH_FOLDERS:
                for sub in await self.children(target, queue.pop(0), f" and mimeType = {quoted(FOLDER)}"):
                    if len(folders) < SEARCH_FOLDERS:
                        folders.append(sub["id"])
                        queue.append(sub["id"])
        if not folders:
            text = await self.text_of(target, start)
            hit = [line.strip() for line in text.splitlines() if query.lower() in line.lower()][:SEARCH_LIMIT]
            return "\n".join(f"{start['name']}: {line[:200]}" for line in hit) or f"No match for {query!r}."
        where = " or ".join(f"{quoted(f)} in parents" for f in folders)
        page = await self.get(
            target, f"{API}/files", q=f"({where}) and fullText contains {quoted(query)} and trashed = false",
            fields=f"files({FIELDS})", pageSize=SEARCH_LIMIT, supportsAllDrives="true", includeItemsFromAllDrives="true",
        )
        items = page.get("files", [])
        if not items:
            return f"No match for {query!r}."
        return f"{len(items)} file(s) containing {query!r}:\n" + "\n".join(self.line(i) for i in items)

    async def upload(self, target: Target, metadata: dict, content: str, file_id: str = "") -> dict:
        """Create (or, with `file_id`, replace the content of) a file: a multipart upload, text in, text out."""
        name = metadata.get("name", "")
        mime = metadata.get("mimeType") or mimetypes.guess_type(name)[0] or "text/plain"
        if mime.startswith("text/") or mime in ("application/json", "application/xml"):
            mime = f"{mime}; charset=UTF-8" if "charset" not in mime else mime
        boundary = "clara-boundary-8f2c"
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n{content}\r\n--{boundary}--"
        ).encode("utf-8")
        url = f"{UPLOAD}/files/{file_id}" if file_id else f"{UPLOAD}/files"
        response = await self.request(
            target.token, "PATCH" if file_id else "POST", url,
            params={"uploadType": "multipart", "fields": FIELDS, "supportsAllDrives": "true"},
            content=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        )
        return response.json()

    async def op_write(self, target: Target, args: dict) -> str:
        mode = check_mode(args.get("mode"))
        content = check_text(args.get("content"))
        names = segments(args.get("path"))
        if not names:
            raise ConnectorError("Give the path of the file to write.")
        found, parent, name = await self.resolve(target, args.get("path"), must_exist=False)
        if found is not None and found["mimeType"] == FOLDER:
            raise ConnectorError(f"{name} is a folder.")
        if found is None:
            parent = await self.folder_path(target, "/".join(names[:-1]))
        if found is not None and found["mimeType"].startswith(NATIVE):
            raise ConnectorError(
                f"{name} is a Google {found['mimeType'].removeprefix(NATIVE)}: it cannot be changed as text. "
                "Write a new file with another name instead."
            )
        if mode == "create" and found is not None:
            raise ConnectorError(f"{name} already exists: use mode overwrite or append.")
        if found is None:
            made = await self.upload(target, {"name": names[-1], "parents": [parent]}, content)
            return f"Created {made['name']} ({len(content):,} characters) [id:{made['id']}]."
        if mode == "append":
            content = await self.text_of(target, found) + content
        await self.upload(target, {}, content, found["id"])
        return f"{'Added to' if mode == 'append' else 'Replaced'} {name} ({len(content):,} characters)."

    async def op_delete(self, target: Target, args: dict) -> str:
        found, _, name = await self.resolve(target, args.get("path"))
        assert found is not None
        if found["id"] == self.root(target):
            raise ConnectorError("The attached folder itself cannot be deleted.")
        await self.request(
            target.token, "PATCH", f"{API}/files/{found['id']}", params={"supportsAllDrives": "true"}, json={"trashed": True}
        )
        return f"Moved {name} to the trash (it can be restored from Drive's trash)."

    async def op_move(self, target: Target, args: dict) -> str:
        found, parent, name = await self.resolve(target, args.get("path"))
        assert found is not None
        if found["id"] == self.root(target):
            raise ConnectorError("The attached folder itself cannot be moved.")
        new_names = segments(args.get("dest"))
        if not new_names:
            raise ConnectorError("Give the new path.")
        existing, _, _ = await self.resolve(target, args.get("dest"), must_exist=False)
        if existing is not None:
            raise ConnectorError(f"{'/'.join(new_names)} already exists: nothing was moved.")
        new_parent = await self.folder_path(target, "/".join(new_names[:-1]))
        params: dict[str, Any] = {"supportsAllDrives": "true"}
        if new_parent != parent:
            params |= {"addParents": new_parent, "removeParents": parent}
        await self.request(target.token, "PATCH", f"{API}/files/{found['id']}", params=params, json={"name": new_names[-1]})
        return f"Moved {name} to {'/'.join(new_names)}."

