"""The routes of projects (projects.py): a person's files and repositories, that the conversations of the project
use. Like the conversations, they are asked for an account (`surface`, `user_id`); a project belongs to the
account's person, so it is the same on every surface of theirs.

    GET    /v1/projects                         ?surface=&user_id=  the person's projects
    POST   /v1/projects                         {surface, user_id, name, description?, instructions?}
    GET    /v1/projects/{id}                    the project, its repositories, its files (paths and sizes)
    PATCH  /v1/projects/{id}                    {name?, description?, instructions?, pinned?}: only pinned projects have
                                                their conversations in the history of the web site
    DELETE /v1/projects/{id}                    its files go, its conversations stay (in no project)
    POST   /v1/projects/{id}/files              {files: [{path, data (base64)}]}: text, code, PDF, .docx, .zip
    GET    /v1/projects/{id}/file               ?path=  a file's text
    DELETE /v1/projects/{id}/files              ?path=&folder=  a file, or all those of a folder
    POST   /v1/projects/{id}/github             {repo, ref?}: download a repository's text files
    POST   /v1/projects/{id}/sources/{sid}/sync download it again
    DELETE /v1/projects/{id}/sources/{sid}      remove it and its files

A conversation is put in a project by the first message sent with `project` (POST /v1/chat), and moved with
PATCH /v1/conversations/{id} {project}; GET /v1/conversations?project= lists those of a project.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from .auth import Client, require_account
from .github import GitHubError, check_ref, parse_repo
from .ingest import ExtractedFile, Skipped, expand, unpack
from .memory import Person
from .projects import MAX_DESCRIPTION, MAX_INSTRUCTIONS, MAX_NAME, Added, Project, ProjectError, Projects, Source

log = logging.getLogger("clara")

router = APIRouter()

MAX_UPLOAD_BYTES = 80_000_000  # one request of files (base64 makes it a third bigger than the files)
MAX_FILES_PER_UPLOAD = 1_000
SKIPPED_SHOWN = 200  # files left out that an answer names

Surface = Annotated[str, Query(pattern=r"^[a-z0-9_-]{1,32}$")]
ExternalId = Annotated[str, Query(min_length=1, max_length=128)]


class _Account(BaseModel):
    surface: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    user_id: str = Field(min_length=1, max_length=128)

    @field_validator("user_id", mode="before")
    @classmethod
    def _ids_may_be_numbers(cls, value):
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


class NewProject(_Account):
    user_name: str | None = Field(default=None, max_length=80)
    name: str = Field(min_length=1, max_length=MAX_NAME)
    description: str = Field(default="", max_length=MAX_DESCRIPTION)
    instructions: str = Field(default="", max_length=MAX_INSTRUCTIONS)


class ProjectPatch(_Account):
    name: str | None = Field(default=None, max_length=MAX_NAME)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION)
    instructions: str | None = Field(default=None, max_length=MAX_INSTRUCTIONS)
    pinned: bool | None = None  # pinned projects have their conversations in the history


class UploadedFile(BaseModel):
    path: str = Field(min_length=1, max_length=400)
    data: str  # base64


class Upload(_Account):
    files: list[UploadedFile] = Field(min_length=1, max_length=MAX_FILES_PER_UPLOAD)


class RepoBody(_Account):
    repo: str = Field(min_length=3, max_length=300)
    ref: str = Field(default="", max_length=200)


def describe(project: Project, projects: Projects, window: int) -> dict:
    tokens = projects.weight(project.id) if project.files else 0
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "instructions": project.instructions,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "pinned": project.pinned,
        "files": project.files,
        "size": project.size,
        "conversations": project.conversations,
        # how the files reach the model with the provider in use now: whole in the prompt, or read with tools
        "context": {
            "tokens": tokens,
            "window": window,
            "percent": round(100 * tokens / window, 1) if window else 0,
            "inline": tokens <= projects.inline_percent * window / 100,
            "inline_percent": projects.inline_percent,
        },
        "limits": {"size": projects.max_bytes, "files": projects.max_files},
    }


def describe_source(source: Source) -> dict:
    return {
        "id": source.id, "kind": source.kind, "repo": source.repo, "ref": source.ref, "folder": source.folder,
        "commit": source.commit_sha, "synced_at": source.synced_at, "files": source.files, "size": source.size,
        "skipped": source.skipped, "problem": source.problem,
    }


def describe_added(result: Added) -> dict:
    return {
        "added": result.added,
        "replaced": result.replaced,
        "skipped": [{"path": s.path, "reason": s.reason} for s in result.skipped[:SKIPPED_SHOWN]],
        "skipped_count": len(result.skipped),
    }


def _window(request: Request) -> int:
    return request.app.state.providers.context_window


def _person(request: Request, client: str, surface: str, user_id: str) -> Person | None:
    require_account(request, client, surface, user_id)
    return request.app.state.memory.find_person(surface, user_id)


def own_project(request: Request, client: str, surface: str, user_id: str, project_id: int) -> Project:
    """The project, if it is the account's person's; else 404."""
    person = _person(request, client, surface, user_id)
    project = request.app.state.projects.get(project_id)
    if person is None or project is None or project.person_id != person.id:
        raise HTTPException(404, "No such project of yours")
    return project


def _details(request: Request, project: Project) -> dict:
    projects: Projects = request.app.state.projects
    project = projects.get(project.id) or project
    return {
        **describe(project, projects, _window(request)),
        "sources": [describe_source(s) for s in projects.sources(project.id)],
        "file_list": [
            {"path": f.path, "kind": f.kind, "size": f.size, "source": f.source_id, "added_at": f.added_at}
            for f in projects.files(project.id)
        ],
    }


@router.get("/v1/projects")
async def list_projects(client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    person = _person(request, client, surface, user_id)
    projects: Projects = request.app.state.projects
    found = projects.of(person.id) if person else []
    window = _window(request)
    return {"projects": [describe(project, projects, window) for project in found]}


@router.post("/v1/projects", status_code=201)
async def create_project(body: NewProject, client: Client, request: Request) -> dict:
    require_account(request, client, body.surface, body.user_id)
    person = request.app.state.memory.resolve(body.surface, body.user_id, body.user_name)
    try:
        project = request.app.state.projects.create(person.id, body.name, body.description, body.instructions)
    except ProjectError as error:
        raise HTTPException(422, str(error)) from None
    log.info("%s made the project %d (%s)", client, project.id, project.name)
    return _details(request, project)


@router.get("/v1/projects/{project_id}")
async def get_project(project_id: int, client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    return _details(request, own_project(request, client, surface, user_id, project_id))


@router.patch("/v1/projects/{project_id}")
async def update_project(project_id: int, body: ProjectPatch, client: Client, request: Request) -> dict:
    project = own_project(request, client, body.surface, body.user_id, project_id)
    projects = request.app.state.projects
    try:
        if body.pinned is not None:
            projects.pin(project.id, body.pinned)
        if any(v is not None for v in (body.name, body.description, body.instructions)):
            projects.update(project.id, body.name, body.description, body.instructions)
    except ProjectError as error:
        raise HTTPException(422, str(error)) from None
    return _details(request, projects.get(project.id) or project)


@router.delete("/v1/projects/{project_id}")
async def delete_project(project_id: int, client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    project = own_project(request, client, surface, user_id, project_id)
    moved = request.app.state.projects.delete(project.id)
    log.info("%s deleted the project %d (%s)", client, project.id, project.name)
    return {"ok": True, "conversations_moved": moved}


# ----------------------------------------------------------------------
# Files
# ----------------------------------------------------------------------
def _read_upload(files: list[UploadedFile]) -> tuple[list[ExtractedFile], list[Skipped]]:
    extracted: list[ExtractedFile] = []
    skipped: list[Skipped] = []
    for item in files:
        try:
            data = base64.b64decode(item.data, validate=True)
        except (binascii.Error, ValueError):
            skipped.append(Skipped(item.path, "not sent as base64"))
            continue
        found, left_out = expand(item.path, data)
        extracted += found
        skipped += left_out
    return extracted, skipped


@router.post("/v1/projects/{project_id}/files")
async def upload_files(project_id: int, body: Upload, client: Client, request: Request) -> dict:
    declared = request.headers.get("content-length") or "0"
    if declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Send at most {MAX_UPLOAD_BYTES // 1_000_000} MB at once.")
    project = own_project(request, client, body.surface, body.user_id, project_id)
    extracted, skipped = await asyncio.to_thread(_read_upload, body.files)
    # up to many MB written: in a thread, so that the answers being streamed meanwhile do not stall
    result = await asyncio.to_thread(request.app.state.projects.add, project.id, extracted)
    result = Added(result.added, result.replaced, skipped + result.skipped)
    return {**describe_added(result), "project": _details(request, project)}


@router.get("/v1/projects/{project_id}/file")
async def read_file(
    project_id: int, client: Client, request: Request, surface: Surface, user_id: ExternalId,
    path: Annotated[str, Query(min_length=1, max_length=400)],
) -> dict:
    project = own_project(request, client, surface, user_id, project_id)
    found = request.app.state.projects.file(project.id, path)
    if found is None:
        raise HTTPException(404, "No such file in the project")
    info, content = found
    return {"path": info.path, "kind": info.kind, "size": info.size, "source": info.source_id, "content": content}


@router.delete("/v1/projects/{project_id}/files")
async def remove_files(
    project_id: int, client: Client, request: Request, surface: Surface, user_id: ExternalId,
    path: Annotated[str, Query(max_length=400)], folder: bool = False,
) -> dict:
    project = own_project(request, client, surface, user_id, project_id)
    if not path.strip("/") and not folder:
        raise HTTPException(422, "Which file?")
    removed = request.app.state.projects.remove(project.id, path, folder)
    if not removed:
        raise HTTPException(404, "No such file in the project")
    return {"removed": removed, "project": _details(request, project)}


# ----------------------------------------------------------------------
# GitHub repositories
# ----------------------------------------------------------------------
def _download_token(request: Request, client: str, project: Project) -> str:
    """The token a download for this project may use ("": none, public repositories only). The operator's
    GITHUB_TOKEN is the operator's: an administrator may use it, everybody else only when the server says so
    (CLARA_GITHUB_TOKEN_SHARED). Otherwise it is the GitHub account the project's owner connected."""
    state = request.app.state
    caller = getattr(client, "user", None)
    if state.settings.github_token_shared or (caller is not None and caller.is_admin) or getattr(client, "admin_token", False):
        shared = state.github.server_token
        if shared:
            return shared
    integrations = state.integrations
    if not integrations.store.type_enabled("github", project.person_id):
        return ""
    for account in integrations.store.accounts_of(project.person_id):
        if account.kind == "github" and account.status == "ok":
            try:
                return integrations.vault.open(account.secret)
            except Exception:  # the key changed: the person connects it again
                return ""
    return ""


async def _sync(request: Request, client: str, project: Project, source: Source) -> Added:
    projects: Projects = request.app.state.projects
    try:
        snapshot = await request.app.state.github.snapshot(
            source.repo, source.ref, _download_token(request, client, project)
        )
        files, skipped = await asyncio.to_thread(unpack, snapshot.archive, source.folder, True)
    except (GitHubError, ValueError) as error:
        projects.source_failed(source, str(error))
        raise HTTPException(502 if isinstance(error, GitHubError) else 422, str(error)) from None
    result = await asyncio.to_thread(projects.replace_source_files, source, files, skipped, snapshot.commit_sha)
    log.info("%s synced: %d files, %d left out", source.repo, len(result.added), len(result.skipped))
    return result


@router.post("/v1/projects/{project_id}/github")
async def add_repository(project_id: int, body: RepoBody, client: Client, request: Request) -> dict:
    project = own_project(request, client, body.surface, body.user_id, project_id)
    projects: Projects = request.app.state.projects
    try:
        repo, ref = parse_repo(body.repo)
    except GitHubError as error:
        raise HTTPException(422, str(error)) from None
    try:
        ref = check_ref(body.ref) or ref
    except GitHubError as error:
        raise HTTPException(422, str(error)) from None
    try:
        source = projects.add_source(project.id, repo, ref, repo.split("/", 1)[1])
    except ProjectError as error:
        raise HTTPException(409, str(error)) from None
    try:
        result = await _sync(request, client, project, source)
    except HTTPException:
        projects.remove_source(source)  # a repository that never came is not kept
        raise
    return {**describe_added(result), "project": _details(request, project)}


def _source_or_404(request: Request, project: Project, source_id: int) -> Source:
    source = request.app.state.projects.source(project.id, source_id)
    if source is None:
        raise HTTPException(404, "No such repository in the project")
    return source


@router.post("/v1/projects/{project_id}/sources/{source_id}/sync")
async def sync_repository(project_id: int, source_id: int, body: _Account, client: Client, request: Request) -> dict:
    project = own_project(request, client, body.surface, body.user_id, project_id)
    result = await _sync(request, client, project, _source_or_404(request, project, source_id))
    return {**describe_added(result), "project": _details(request, project)}


@router.delete("/v1/projects/{project_id}/sources/{source_id}")
async def remove_repository(
    project_id: int, source_id: int, client: Client, request: Request, surface: Surface, user_id: ExternalId
) -> dict:
    project = own_project(request, client, surface, user_id, project_id)
    removed = request.app.state.projects.remove_source(_source_or_404(request, project, source_id))
    return {"removed": removed, "project": _details(request, project)}


def install(app) -> None:
    app.include_router(router)
