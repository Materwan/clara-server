"""The routes of the integrations (integrations/): what a person connected (accounts, repositories, Drive folders,
folders), where it is attached (a project, a conversation), and the requests for permission Clara made.

As everywhere else, a caller is asked for an account (`surface`, `user_id`); everything belongs to that account's
person, so it is the same on every surface of theirs.

    GET    /v1/integrations                             accounts, resources, which kinds are on, the person's settings
    PUT    /v1/integrations/settings                    {approval_notify_after}
    DELETE /v1/integrations/accounts/{id}               disconnect (its resources go too)
    PATCH  /v1/integrations/accounts/{id}               {levels}: the default permission of its resources
    GET    /v1/integrations/browse/server               ?path=  the folders of the server a person may pick
    POST   /v1/integrations/resources                   add a repository, a Drive folder or file, a folder
    PATCH  /v1/integrations/resources/{id}              {label?, levels?}
    DELETE /v1/integrations/resources/{id}
    GET    /v1/integrations/attachments                 ?project= or ?conversation=  what is attached there
    PUT    /v1/integrations/attachments                 {resource, project | conversation, levels?}
    DELETE /v1/integrations/attachments/{id}
    GET    /v1/approvals                                ?status=pending|all&conversation=
    POST   /v1/approvals/{id}/decide                    {approve, remember?}
    GET    /v1/admin/integrations                       the administrator's settings
    PUT    /v1/admin/integrations                       {enabled?, disabled_users?, roots?, ceiling?}
    GET    /v1/admin/integrations/log                   what Clara did and was asked
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from .auth import Admin, Client, require_account, require_conversation
from .github import GitHubError, parse_repo
from .integrations import permissions
from .integrations.approvals import AlreadyAnswered
from .integrations.connectors.base import ConnectorError, Target
from .integrations.connectors.gdrive import FOLDER, GoogleDrive, authorize_url, quoted
from .integrations.connectors.github_live import GitHubLive
from .integrations.service import Integrations
from .integrations.store import PENDING, Account, Approval, Attached, Resource, StoreError
from .integrations.vault import VaultError
from .memory import MAX_NOTIFY_AFTER, Person
from .projectapi import own_project

log = logging.getLogger("clara")

router = APIRouter()

Surface = Annotated[str, Query(pattern=r"^[a-z0-9_-]{1,32}$")]
ExternalId = Annotated[str, Query(min_length=1, max_length=128)]
Levels = dict[str, str]


class _Account(BaseModel):
    surface: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    user_id: str = Field(min_length=1, max_length=128)

    @field_validator("user_id", mode="before")
    @classmethod
    def _ids_may_be_numbers(cls, value):
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


class SettingsBody(_Account):
    approval_notify_after: int | None = Field(ge=0, le=MAX_NOTIFY_AFTER)  # seconds; 0: never pushed; null: default


class AccountPatch(_Account):
    levels: Levels


class GoogleStart(_Account):
    levels: Levels | None = None


class ConnectGitHub(_Account):
    token: str = Field(min_length=10, max_length=500)  # a fine-grained personal access token
    levels: Levels | None = None


class NewResource(_Account):
    kind: str = Field(max_length=32)
    account: int | None = None  # the connected account it is reached through (GitHub, Drive)
    label: str = Field(default="", max_length=200)
    levels: Levels | None = None
    path: str = Field(default="", max_length=1000)  # server_path: the folder
    repo: str = Field(default="", max_length=300)  # github_repo: owner/name or its address
    ref: str = Field(default="", max_length=200)  # github_repo: a branch (default: the repository's)
    file_id: str = Field(default="", max_length=200)  # drive_folder, drive_file
    device: str = Field(default="", max_length=100)  # computer_path: which computer
    alias: str = Field(default="", max_length=100)  # computer_path: the folder, as the app names it


class ResourcePatch(_Account):
    label: str | None = Field(default=None, max_length=200)
    levels: Levels | None = None


class AttachBody(_Account):
    resource: int
    project: int | None = Field(default=None, ge=1)
    conversation: str | None = Field(default=None, min_length=1, max_length=200)
    levels: Levels | None = None


class JobResult(_Account):
    ok: bool
    text: str = Field(default="", max_length=2_000_000)


class DecideBody(_Account):
    approve: bool
    remember: str = Field(default="", max_length=20)  # "": just this one; "conversation" or "resource": do not ask again


class PolicyBody(BaseModel):
    enabled: dict[str, bool] | None = None
    disabled_users: dict[str, list[int]] | None = None  # person ids
    roots: list[str] | None = Field(default=None, max_length=50)
    ceiling: dict[str, Levels] | None = None


def svc(request: Request) -> Integrations:
    return request.app.state.integrations


def _checked_levels(levels: Levels | None) -> Levels | None:
    if levels is None:
        return None
    try:
        return permissions.clean(levels)
    except permissions.SettingError as error:
        raise HTTPException(422, str(error)) from None


def _person(request: Request, client: str, surface: str, user_id: str, create: bool = False) -> Person | None:
    require_account(request, client, surface, user_id)
    memory = request.app.state.memory
    return memory.resolve(surface, user_id, None) if create else memory.find_person(surface, user_id)


def _own_person(request: Request, client: str, surface: str, user_id: str) -> Person:
    person = _person(request, client, surface, user_id)
    if person is None:
        raise HTTPException(404, "Nobody known as that account")
    return person


def _own_resource(request: Request, person: Person, resource_id: int) -> Resource:
    resource = svc(request).store.resource(resource_id)
    if resource is None or resource.person_id != person.id:
        raise HTTPException(404, "No such resource of yours")
    return resource


# ----------------------------------------------------------------------
# What it looks like
# ----------------------------------------------------------------------
def describe_account(account: Account) -> dict:
    """An account for a client: never its secret."""
    return {
        "id": account.id, "kind": account.kind, "label": account.label, "status": account.status,
        "levels": account.levels, "created_at": account.created_at,
    }


def describe_resource(request: Request, resource: Resource) -> dict:
    store = svc(request).store
    account = store.account(resource.account_id) if resource.account_id else None
    locator = dict(resource.locator)
    return {
        "id": resource.id, "kind": resource.kind, "type": resource.type, "label": resource.label,
        "account": resource.account_id, "locator": locator, "levels": resource.levels,
        "effective": permissions.effective(
            resource.levels, account.levels if account else None,
            ceiling=store.policy()["ceiling"].get(resource.type),
        ),
        "attachments": store.attachment_count(resource.id), "created_at": resource.created_at,
    }


def describe_attached(request: Request, item: Attached) -> dict:
    return {
        "attachment": item.attachment_id, "scope": item.scope, "levels": item.levels,
        "effective": svc(request).broker.levels(item), "resource": describe_resource(request, item.resource),
    }


def describe_approval(request: Request, approval: Approval) -> dict:
    resource = svc(request).store.resource(approval.resource_id) if approval.resource_id else None
    return {
        "id": approval.id, "conversation": approval.conversation, "resource": resource.label if resource else "",
        "resource_id": approval.resource_id, "op": approval.op, "level": approval.level, "summary": approval.summary,
        "reason": approval.reason, "status": approval.status, "result": approval.result[:2000],
        "created_at": approval.created_at, "decided_at": approval.decided_at, "decided_on": approval.decided_on,
    }


def type_state(request: Request, person_id: int) -> list[dict]:
    service = svc(request)
    policy = service.store.policy()
    configured = {"gdrive": bool(service.settings.google_client_id and service.settings.google_client_secret)}
    states = []
    for kind in permissions.TYPES:
        enabled = service.store.type_enabled(kind, person_id)
        states.append({
            "id": kind, "name": permissions.TYPE_NAMES[kind], "enabled": enabled,
            "available": kind in service.connectors and configured.get(kind, True),
            "ceiling": policy["ceiling"][kind], "defaults": permissions.DEFAULTS,
        })
    return states


# ----------------------------------------------------------------------
# Overview, settings, accounts
# ----------------------------------------------------------------------
@router.get("/v1/integrations")
async def overview(client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    person = _person(request, client, surface, user_id)
    service = svc(request)
    if person is None:
        return {"types": type_state(request, 0), "accounts": [], "resources": [], "roots": [], "pending": 0,
                "settings": {"approval_notify_after": None, "default": service.approvals.default_notify_after}}
    return {
        "types": type_state(request, person.id),
        "accounts": [describe_account(a) for a in service.store.accounts_of(person.id)],
        "resources": [describe_resource(request, r) for r in service.store.resources_of(person.id)],
        "roots": service.store.policy()["roots"] if service.store.type_enabled(permissions.SERVER, person.id) else [],
        "pending": len(service.store.approvals_of(person.id, (PENDING,))),
        "settings": {
            "approval_notify_after": request.app.state.memory.approval_notify_after(person.id),
            "default": service.approvals.default_notify_after,
            "expire_after": service.approvals.expire_after,
        },
    }


@router.put("/v1/integrations/settings")
async def put_settings(body: SettingsBody, client: Client, request: Request) -> dict:
    person = _person(request, client, body.surface, body.user_id, create=True)
    assert person is not None
    request.app.state.memory.set_approval_notify_after(person.id, body.approval_notify_after)
    return {"approval_notify_after": body.approval_notify_after}


@router.patch("/v1/integrations/accounts/{account_id}")
async def patch_account(account_id: int, body: AccountPatch, client: Client, request: Request) -> dict:
    person = _own_person(request, client, body.surface, body.user_id)
    store = svc(request).store
    account = store.account(account_id)
    if account is None or account.person_id != person.id:
        raise HTTPException(404, "No such account of yours")
    return describe_account(store.update_account(account_id, levels=_checked_levels(body.levels)))


@router.delete("/v1/integrations/accounts/{account_id}")
async def delete_account(account_id: int, client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    person = _own_person(request, client, surface, user_id)
    store = svc(request).store
    account = store.account(account_id)
    if account is None or account.person_id != person.id:
        raise HTTPException(404, "No such account of yours")
    removed = store.delete_account(account_id)
    log.info("%s disconnected the %s account %s", client, account.kind, account.label)
    return {"ok": True, "resources_removed": removed}


# ----------------------------------------------------------------------
# GitHub
# ----------------------------------------------------------------------
def _github(request: Request) -> GitHubLive:
    connector = svc(request).connectors.get(permissions.GITHUB)
    if not isinstance(connector, GitHubLive):
        raise HTTPException(422, "GitHub is not available on this server")
    return connector


def _own_account(request: Request, person: Person, account_id: int | None, kind: str) -> tuple[Account, str]:
    """The person's account of that kind (the only one when none is named) and its secret, opened."""
    store = svc(request).store
    if account_id is None:
        mine = [a for a in store.accounts_of(person.id) if a.kind == kind]
        if len(mine) != 1:
            raise HTTPException(422, "Say which account" if mine else f"Connect a {kind} account first")
        account = mine[0]
    else:
        found = store.account(account_id)
        if found is None or found.person_id != person.id or found.kind != kind:
            raise HTTPException(404, "No such account of yours")
        account = found
    if account.status != "ok":
        raise HTTPException(409, "This account needs to be connected again")
    try:
        return account, svc(request).vault.open(account.secret)
    except Exception as error:  # the key changed: the person connects it again
        raise HTTPException(409, str(error)) from None


@router.post("/v1/integrations/github", status_code=201)
async def connect_github(body: ConnectGitHub, client: Client, request: Request) -> dict:
    """Connect a GitHub account with a personal access token: it is checked, then kept encrypted."""
    person = _person(request, client, body.surface, body.user_id, create=True)
    assert person is not None
    service = svc(request)
    if not service.store.type_enabled(permissions.GITHUB, person.id):
        raise HTTPException(403, "GitHub is turned off")
    connector = _github(request)
    token = body.token.strip()
    try:
        user = await connector.get_json(token, "/user")
    except ConnectorError as error:
        raise HTTPException(422, f"GitHub refused this token: {error}") from None
    label = str(user.get("login") or "")[:100] or "GitHub"
    sealed = service.vault.seal(token)
    same = [a for a in service.store.accounts_of(person.id) if a.kind == "github" and a.label.lower() == label.lower()]
    if same:
        account = service.store.update_account(same[0].id, sealed=sealed, status="ok", levels=_checked_levels(body.levels))
    else:
        account = service.store.add_account(person.id, "github", label, sealed, _checked_levels(body.levels))
    log.info("%s connected the GitHub account %s", client, label)
    return describe_account(account)


@router.get("/v1/integrations/browse/github")
async def browse_github(
    client: Client, request: Request, surface: Surface, user_id: ExternalId,
    account: Annotated[int | None, Query()] = None, q: Annotated[str, Query(max_length=100)] = "",
) -> dict:
    """The repositories the account can reach (the last used first), for picking one."""
    person = _own_person(request, client, surface, user_id)
    found, token = _own_account(request, person, account, "github")
    try:
        repos = await _github(request).get_json(
            token, "/user/repos", params={"per_page": 100, "sort": "pushed", "affiliation": "owner,collaborator,organization_member"}
        )
    except ConnectorError as error:
        raise HTTPException(502, str(error)) from None
    wanted = q.strip().lower()
    return {
        "account": found.id,
        "repos": [
            {
                "full_name": r["full_name"], "private": r.get("private", False), "default_branch": r.get("default_branch", ""),
                "description": (r.get("description") or "")[:200],
            }
            for r in repos
            if not wanted or wanted in r["full_name"].lower()
        ][:100],
    }


async def _check_github_repo(request: Request, person: Person, body: NewResource) -> tuple[int | None, str, dict]:
    account, token = _own_account(request, person, body.account, "github")
    try:
        repo, ref = parse_repo(body.repo)
    except GitHubError as error:
        raise HTTPException(422, str(error)) from None
    ref = body.ref.strip() or ref
    connector = _github(request)
    try:
        about = await connector.get_json(token, f"/repos/{repo}")
        if ref:
            await connector.get_json(token, f"/repos/{repo}/branches/{ref}")
    except ConnectorError as error:
        raise HTTPException(422, f"{repo}: {error}") from None
    name = about.get("full_name") or repo
    return account.id, body.label or (f"{name}@{ref}" if ref else name), {"repo": name, "ref": ref}


# ----------------------------------------------------------------------
# Google Drive
# ----------------------------------------------------------------------
STATE_SECONDS = 600  # how long the link that sends a person to Google stays good


def _drive(request: Request) -> GoogleDrive:
    connector = svc(request).connectors.get(permissions.GDRIVE)
    if not isinstance(connector, GoogleDrive):
        raise HTTPException(
            422, "Google Drive is not set up on this server: the administrator needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET"
        )
    return connector


def _redirect_uri(request: Request) -> str:
    base = svc(request).settings.public_url or str(request.base_url).rstrip("/")
    return f"{base}/v1/integrations/google/callback"


@router.post("/v1/integrations/google/start")
async def google_start(body: GoogleStart, client: Client, request: Request) -> dict:
    """Where to send the person to connect their Google Drive (the page opens in a new window, and comes back to the
    callback below)."""
    person = _person(request, client, body.surface, body.user_id, create=True)
    assert person is not None
    service = svc(request)
    if not service.store.type_enabled(permissions.GDRIVE, person.id):
        raise HTTPException(403, "Google Drive is turned off")
    drive = _drive(request)
    now = time.time()
    for used, until in list(service.google_states.items()):  # forget the links that ended
        if until < now:
            del service.google_states[used]
    nonce = secrets.token_urlsafe(12)
    state = service.vault.seal(json.dumps({
        "person": person.id, "until": now + STATE_SECONDS, "nonce": nonce, "levels": _checked_levels(body.levels) or {},
    }))
    return {"url": authorize_url(drive.client_id, _redirect_uri(request), state), "redirect_uri": _redirect_uri(request)}


def _page(title: str, text: str, status: int = 200) -> HTMLResponse:
    body = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<body style='font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem'>"
        f"<h1 style='font-size:1.3rem'>{html.escape(title)}</h1><p>{html.escape(text)}</p>"
    )
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _claims(request: Request, state: str) -> tuple[dict, int, str] | HTMLResponse:
    """What the sign-in link says (who it is for), or the page that explains why it is no good. Nobody is signed in
    on these pages: what proves who it is for is `state`, which only this server can have made, for one person,
    used once, within ten minutes."""
    service = svc(request)
    try:
        claims = json.loads(service.vault.open(state))
        person_id, until, nonce = int(claims["person"]), float(claims["until"]), str(claims["nonce"])
    except (VaultError, ValueError, KeyError, TypeError):
        return _page("This link is not valid", "Start again from the Integrations page.", 400)
    if until < time.time() or nonce in service.google_states:
        return _page("This link has expired", "Start again from the Integrations page.", 400)
    return claims, person_id, nonce


@router.get("/v1/integrations/google/callback", response_class=HTMLResponse)
async def google_callback(request: Request, code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    """Where Google sends the person back. It does not connect anything yet: a link made by someone else could have
    brought a person here to give *their* Drive to *another* Clara user, so the page says whose Drive this would be and
    asks (a form that is sent back to `confirm`)."""
    request.scope["clara_sensitive"] = True  # the code is in the address: the traffic log does not write it
    if error:
        return _page("Google Drive was not connected", f"Google said: {error}. You can close this window.", 400)
    found = _claims(request, state)
    if isinstance(found, HTMLResponse):
        return found
    _, person_id, _ = found
    person = request.app.state.memory.person_by_id(person_id)
    who = html.escape(person.name if person else "someone")
    body = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Connect Google Drive</title>"
        "<body style='font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:15vh auto;padding:0 1rem'>"
        "<h1 style='font-size:1.3rem'>Connect your Google Drive to Clara?</h1>"
        f"<p>You are about to let the Clara user <strong>{who}</strong> read and change the files of the Google account "
        "you just chose. Only continue if that is you, and you started this yourself from the Integrations page.</p>"
        "<form method=post action='/v1/integrations/google/confirm'>"
        f"<input type=hidden name=code value='{html.escape(code, quote=True)}'>"
        f"<input type=hidden name=state value='{html.escape(state, quote=True)}'>"
        "<button type=submit style='font:inherit;padding:.5rem 1.2rem'>Connect</button></form>"
        "<p style='color:#555'>If you did not ask for this, close this window: nothing was connected.</p>"
    )
    return HTMLResponse(body, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/v1/integrations/google/confirm", response_class=HTMLResponse)
async def google_confirm(request: Request) -> HTMLResponse:
    """The person confirmed: the code is exchanged, and the Google account is kept (encrypted) for the Clara user."""
    request.scope["clara_sensitive"] = True
    service = svc(request)
    form = {key: values[0] for key, values in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
    code, state = form.get("code", ""), form.get("state", "")
    found = _claims(request, state)
    if isinstance(found, HTMLResponse):
        return found
    claims, person_id, nonce = found
    service.google_states[nonce] = float(claims["until"])  # used: it cannot bring anyone here again
    drive = _drive(request)
    try:
        tokens = await drive.token_request({
            "grant_type": "authorization_code", "code": code, "redirect_uri": _redirect_uri(request),
        })
        refresh = tokens.get("refresh_token")
        if not refresh:
            raise ConnectorError("Google gave no long-lived access: remove Clara in your Google account's security page, then try again.")
        found_user = await drive.request(refresh, "GET", "https://www.googleapis.com/drive/v3/about", params={"fields": "user"})
        user = found_user.json().get("user", {})
    except ConnectorError as problem:
        return _page("Google Drive was not connected", str(problem), 502)
    label = str(user.get("emailAddress") or user.get("displayName") or "Google")[:100]
    sealed = service.vault.seal(refresh)
    same = [a for a in service.store.accounts_of(person_id) if a.kind == "gdrive" and a.label.lower() == label.lower()]
    levels = claims.get("levels") or None
    if same:
        service.store.update_account(same[0].id, sealed=sealed, status="ok", levels=levels)
    else:
        service.store.add_account(person_id, "gdrive", label, sealed, levels)
    log.info("person %d connected the Google account %s", person_id, label)
    return _page("Google Drive is connected", f"Clara can now reach {label}. You can close this window.")


@router.get("/v1/integrations/browse/drive")
async def browse_drive(
    client: Client, request: Request, surface: Surface, user_id: ExternalId, account: Annotated[int | None, Query()] = None,
    folder: Annotated[str, Query(max_length=200)] = "root", q: Annotated[str, Query(max_length=100)] = "",
) -> dict:
    """The folders and files of a Drive folder (or those whose name has `q`), for picking one."""
    person = _own_person(request, client, surface, user_id)
    found, token = _own_account(request, person, account, "gdrive")
    drive = _drive(request)
    target = Target(0, "Drive", {}, token)
    try:
        if q.strip():
            page = await drive.get(
                target, "https://www.googleapis.com/drive/v3/files",
                q=f"name contains {quoted(q.strip())} and trashed = false", fields="files(id,name,mimeType,parents)", pageSize=50,
            )
            items, current, parent = page.get("files", []), None, None
        else:
            current = await drive.get(target, f"https://www.googleapis.com/drive/v3/files/{folder}", fields="id,name,parents")
            items = await drive.children(target, current["id"])
            parent = (current.get("parents") or [None])[0]
    except ConnectorError as error:
        raise HTTPException(502, str(error)) from None
    return {
        "account": found.id, "folder": {"id": current["id"], "name": current["name"]} if current else None, "parent": parent,
        "items": [{"id": i["id"], "name": i["name"], "folder": i["mimeType"] == FOLDER, "mime": i["mimeType"]} for i in items[:200]],
    }


async def _check_drive_file(request: Request, person: Person, body: NewResource) -> tuple[int | None, str, dict]:
    account, token = _own_account(request, person, body.account, "gdrive")
    file_id = body.file_id.strip()
    if not file_id:
        raise HTTPException(422, "Say which folder or file")
    drive = _drive(request)
    try:
        meta = await drive.meta(Target(0, "Drive", {}, token), file_id)
    except ConnectorError as error:
        raise HTTPException(422, str(error)) from None
    is_folder = meta["mimeType"] == FOLDER
    if is_folder != (body.kind == "drive_folder"):
        raise HTTPException(422, "That is a folder: add it as a folder" if is_folder else "That is a file: add it as a file")
    return account.id, body.label or meta["name"], {"id": meta["id"], "name": meta["name"]}


# ----------------------------------------------------------------------
# Folders of the server
# ----------------------------------------------------------------------
def _server_roots(request: Request) -> list[Path]:
    roots = []
    for root in svc(request).store.policy()["roots"]:
        try:
            roots.append(Path(root).resolve())
        except (OSError, RuntimeError):
            continue
    return roots


def _inside_roots(request: Request, path: str) -> Path:
    """A folder of the server the administrator allows (it exists and is inside a root); 422 otherwise."""
    try:
        folder = Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(422, "Not a usable path") from None
    if not any(folder == root or folder.is_relative_to(root) for root in _server_roots(request)):
        raise HTTPException(422, "That folder is not inside one the administrator allows")
    if not folder.is_dir():
        raise HTTPException(422, "That is not a folder")
    return folder


@router.get("/v1/integrations/browse/server")
async def browse_server(
    client: Client, request: Request, surface: Surface, user_id: ExternalId, path: Annotated[str, Query(max_length=1000)] = ""
) -> dict:
    """The folders a person may pick: the allowed roots, then the sub-folders of one."""
    person = _own_person(request, client, surface, user_id)
    if not svc(request).store.type_enabled(permissions.SERVER, person.id):
        raise HTTPException(403, "Server folders are turned off")
    roots = _server_roots(request)
    if not path.strip():
        return {"path": "", "parent": None, "dirs": [str(root) for root in roots if root.is_dir()], "roots": True}
    folder = _inside_roots(request, path)

    def work() -> list[str]:
        try:
            return sorted((p.name for p in folder.iterdir() if p.is_dir() and not p.is_symlink()), key=str.lower)
        except OSError:
            return []

    names = await asyncio.to_thread(work)
    parent = None if folder in roots else str(folder.parent)
    return {"path": str(folder), "parent": parent, "dirs": names[:500], "roots": False}


# ----------------------------------------------------------------------
# Resources
# ----------------------------------------------------------------------
async def _validated(request: Request, person: Person, body: NewResource) -> tuple[int | None, str, dict]:
    """(account id, label, locator) of a resource a person asks to add, once it is checked; 422/403 otherwise."""
    if body.kind not in permissions.KINDS:
        raise HTTPException(422, f"No such kind of resource: {body.kind}")
    service = svc(request)
    kind_type = permissions.KINDS[body.kind]
    if not service.store.type_enabled(kind_type, person.id):
        raise HTTPException(403, f"{permissions.TYPE_NAMES[kind_type]} is turned off")
    if kind_type not in service.connectors:
        raise HTTPException(422, f"{permissions.TYPE_NAMES[kind_type]} is not available on this server")
    if body.kind == "server_path":
        folder = _inside_roots(request, body.path)
        return None, body.label or folder.name or str(folder), {"path": str(folder)}
    if body.kind == "github_repo":
        return await _check_github_repo(request, person, body)
    if body.kind in ("drive_folder", "drive_file"):
        return await _check_drive_file(request, person, body)
    if body.kind == "computer_path":
        if body.surface != "app":
            raise HTTPException(403, "Folders of a computer are added from the Clara desktop app")
        if not body.device.strip() or not body.alias.strip():
            raise HTTPException(422, "Say which computer and which folder")
        name = body.label or body.alias.strip()
        return None, name, {"device": body.device.strip(), "alias": body.alias.strip()}
    raise HTTPException(422, f"{body.kind} cannot be added yet")


@router.post("/v1/integrations/resources", status_code=201)
async def add_resource(body: NewResource, client: Client, request: Request) -> dict:
    person = _person(request, client, body.surface, body.user_id, create=True)
    assert person is not None
    levels = _checked_levels(body.levels)
    account_id, label, locator = await _validated(request, person, body)
    store = svc(request).store
    try:
        resource = store.add_resource(person.id, account_id, body.kind, label, locator, levels)
    except StoreError as error:
        raise HTTPException(422, str(error)) from None
    log.info("%s added the %s %s", client, body.kind, resource.label)
    return describe_resource(request, resource)


@router.patch("/v1/integrations/resources/{resource_id}")
async def patch_resource(resource_id: int, body: ResourcePatch, client: Client, request: Request) -> dict:
    person = _own_person(request, client, body.surface, body.user_id)
    resource = _own_resource(request, person, resource_id)
    try:
        resource = svc(request).store.update_resource(resource.id, body.label, _checked_levels(body.levels))
    except StoreError as error:
        raise HTTPException(422, str(error)) from None
    return describe_resource(request, resource)


@router.delete("/v1/integrations/resources/{resource_id}")
async def delete_resource(resource_id: int, client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    person = _own_person(request, client, surface, user_id)
    resource = _own_resource(request, person, resource_id)
    svc(request).store.delete_resource(resource.id)
    return {"ok": True}


# ----------------------------------------------------------------------
# Jobs for the desktop app (folders of the person's computer)
# ----------------------------------------------------------------------
JOB_RESULT_SHOWN = 100_000


@router.get("/v1/integrations/jobs")
async def take_jobs(
    client: Client, request: Request, surface: Surface, user_id: ExternalId, device: Annotated[str, Query(min_length=1, max_length=100)],
) -> dict:
    """What Clara asked of this computer's folders and the app has not fetched yet. Each job is given out once."""
    if surface != "app":
        raise HTTPException(403, "Only the Clara desktop app does these jobs")
    person = _own_person(request, client, surface, user_id)
    found = svc(request).store.take_jobs(person.id, device)
    return {"jobs": [{"id": j.id, "op": j.op, "alias": j.args.get("alias", ""), "args": j.args.get("args", {}), "created_at": j.created_at} for j in found]}


@router.post("/v1/integrations/jobs/{job_id}/result")
async def job_result(job_id: int, body: JobResult, client: Client, request: Request) -> dict:
    if body.surface != "app":
        raise HTTPException(403, "Only the Clara desktop app does these jobs")
    person = _own_person(request, client, body.surface, body.user_id)
    store = svc(request).store
    job = store.job(job_id)
    if job is None or job.person_id != person.id:
        raise HTTPException(404, "No such job of yours")
    if not store.set_job(job_id, "done" if body.ok else "failed", body.text[:JOB_RESULT_SHOWN]):
        raise HTTPException(409, "This job is already finished")
    return {"ok": True}


# ----------------------------------------------------------------------
# Attachments
# ----------------------------------------------------------------------
def _own_conversation(request: Request, client: str, person: Person, conversation: str) -> str:
    """The conversation, if it is the person's (or new, and of a surface the caller may use)."""
    require_conversation(request, client, conversation)
    info = request.app.state.memory.conversation_info(conversation)
    if info is not None and info.person_id != person.id:
        raise HTTPException(404, "No such conversation of yours")
    return conversation


@router.get("/v1/integrations/attachments")
async def list_attachments(
    client: Client, request: Request, surface: Surface, user_id: ExternalId,
    project: Annotated[int | None, Query(ge=1)] = None,
    conversation: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
) -> dict:
    """What is attached to a project, or to a conversation (with the project's, which it inherits)."""
    if (project is None) == (conversation is None):
        raise HTTPException(422, "Ask for a project or for a conversation")
    person = _own_person(request, client, surface, user_id)
    store = svc(request).store
    if project is not None:
        own_project(request, client, surface, user_id, project)
        found = store.attachments_of(project_id=project)
        return {"attachments": [describe_attached(request, a) for a in found], "inherited": []}
    assert conversation is not None
    _own_conversation(request, client, person, conversation)
    info = request.app.state.memory.conversation_info(conversation)
    own = store.attachments_of(conversation=conversation)
    inherited = [
        a for a in store.attached(conversation, info.project_id if info else None, person.id) if a.scope == "project"
    ]
    return {
        "attachments": [describe_attached(request, a) for a in own if a.resource.person_id == person.id],
        "inherited": [describe_attached(request, a) for a in inherited],
    }


@router.put("/v1/integrations/attachments")
async def put_attachment(body: AttachBody, client: Client, request: Request) -> dict:
    if (body.project is None) == (body.conversation is None):
        raise HTTPException(422, "Attach to a project or to a conversation")
    person = _person(request, client, body.surface, body.user_id, create=True)
    assert person is not None
    resource = _own_resource(request, person, body.resource)
    store = svc(request).store
    if body.project is not None:
        own_project(request, client, body.surface, body.user_id, body.project)
    else:
        assert body.conversation is not None
        _own_conversation(request, client, person, body.conversation)
    try:
        attachment = store.attach(resource.id, body.project, body.conversation, _checked_levels(body.levels))
    except StoreError as error:
        raise HTTPException(422, str(error)) from None
    found = [
        a for a in store.attachments_of(project_id=body.project, conversation=body.conversation)
        if a.attachment_id == attachment
    ]
    return describe_attached(request, found[0])


@router.delete("/v1/integrations/attachments/{attachment_id}")
async def delete_attachment(attachment_id: int, client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    person = _own_person(request, client, surface, user_id)
    store = svc(request).store
    target = store.attachment_target(attachment_id)
    resource = store.resource(target[0]) if target else None
    if resource is None or resource.person_id != person.id:
        raise HTTPException(404, "No such attachment of yours")
    store.detach(attachment_id)
    return {"ok": True}


# ----------------------------------------------------------------------
# Requests for permission
# ----------------------------------------------------------------------
@router.get("/v1/approvals")
async def list_approvals(
    client: Client, request: Request, surface: Surface, user_id: ExternalId,
    status: Annotated[str, Query(pattern="^(pending|all)$")] = "pending",
    conversation: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
) -> dict:
    person = _person(request, client, surface, user_id)
    if person is None:
        return {"approvals": []}
    store = svc(request).store
    found = store.approvals_of(person.id, (PENDING,) if status == "pending" else None)
    if conversation is not None:
        found = [a for a in found if a.conversation == conversation]
    return {"approvals": [describe_approval(request, a) for a in found]}


@router.post("/v1/approvals/{approval_id}/decide")
async def decide_approval(approval_id: int, body: DecideBody, client: Client, request: Request) -> dict:
    """Approve (the action runs now, and Clara goes on) or deny. Only the person it was asked of can answer."""
    person = _own_person(request, client, body.surface, body.user_id)
    try:
        settled = await svc(request).approvals.decide(person.id, approval_id, body.approve, body.surface, body.remember)
    except AlreadyAnswered as error:
        raise HTTPException(409, str(error)) from None
    except StoreError as error:
        raise HTTPException(404 if "No such" in str(error) else 422, str(error)) from None
    log.info("%s %s the request %d", client, "approved" if body.approve else "denied", approval_id)
    return describe_approval(request, settled)


# ----------------------------------------------------------------------
# Administration
# ----------------------------------------------------------------------
def describe_policy(request: Request) -> dict:
    service = svc(request)
    return {
        **service.store.policy(),
        "types": [{"id": t, "name": permissions.TYPE_NAMES[t], "available": t in service.connectors} for t in permissions.TYPES],
        "google_configured": bool(service.settings.google_client_id and service.settings.google_client_secret),
        "public_url": service.settings.public_url,
    }


@router.get("/v1/admin/integrations")
async def admin_integrations(admin: Admin, request: Request) -> dict:
    return describe_policy(request)


@router.put("/v1/admin/integrations")
async def set_admin_integrations(body: PolicyBody, admin: Admin, request: Request) -> dict:
    store = svc(request).store
    policy = store.policy()
    for name in (body.enabled or {}) | (body.disabled_users or {}) | (body.ceiling or {}):
        if name not in permissions.TYPES:
            raise HTTPException(422, f"No such integration: {name}")
    if body.enabled is not None:
        policy["enabled"].update(body.enabled)
    if body.disabled_users is not None:
        policy["disabled_users"].update({k: sorted(set(v)) for k, v in body.disabled_users.items()})
    if body.ceiling is not None:
        for name, levels in body.ceiling.items():
            policy["ceiling"][name] = _checked_levels(levels) or {}
    if body.roots is not None:
        roots: list[str] = []
        for root in body.roots:
            try:
                folder = Path(root).resolve()
            except (OSError, RuntimeError, ValueError):
                raise HTTPException(422, f"Not a usable path: {root}") from None
            if not folder.is_dir():
                raise HTTPException(422, f"Not a folder on the server: {root}")
            if str(folder) not in roots:
                roots.append(str(folder))
        policy["roots"] = roots
    log.info("%s changed the integrations settings", admin)
    store.set_policy(policy)
    return describe_policy(request)


@router.get("/v1/admin/integrations/log")
async def admin_integrations_log(
    admin: Admin, request: Request, limit: Annotated[int, Query(ge=1, le=500)] = 200,
    before: Annotated[int | None, Query(ge=1)] = None,
) -> dict:
    memory = request.app.state.memory
    entries: list[dict[str, Any]] = []
    for entry in svc(request).store.log_entries(limit, None, before):
        person = memory.person_by_id(entry.person_id) if entry.person_id else None
        entries.append({
            "id": entry.id, "at": entry.at, "person": person.name if person else "", "conversation": entry.conversation,
            "resource": entry.resource, "op": entry.op, "level": entry.level, "summary": entry.summary,
            "outcome": entry.outcome, "approval": entry.approval_id,
        })
    return {"entries": entries}


def install(app) -> None:
    app.include_router(router)
