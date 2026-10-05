"""The routes of the markdown files Clara writes (markdownfiles.py). Like projects, they are asked for an account
(`surface`, `user_id`); a file belongs to the account's person, so it is the same on every surface of theirs.

    GET    /v1/markdown-files           ?surface=&user_id=  the person's files (names, sizes, dates), newest first
    GET    /v1/markdown-files/{id}      ?surface=&user_id=  one file, with its text
    DELETE /v1/markdown-files/{id}      ?surface=&user_id=
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request

from .auth import Client, require_account
from .markdownfiles import MarkdownFile, MarkdownFiles
from .memory import Person

log = logging.getLogger("clara")

router = APIRouter()

Surface = Annotated[str, Query(pattern=r"^[a-z0-9_-]{1,32}$")]
ExternalId = Annotated[str, Query(min_length=1, max_length=128)]


def describe(file: MarkdownFile) -> dict:
    return {
        "id": file.id, "name": file.name, "size": file.size, "created_at": file.created_at,
        "updated_at": file.updated_at,
    }


def _person(request: Request, client: str, surface: str, user_id: str) -> Person | None:
    require_account(request, client, surface, user_id)
    return request.app.state.memory.find_person(surface, user_id)


@router.get("/v1/markdown-files")
async def list_files(client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    person = _person(request, client, surface, user_id)
    files: MarkdownFiles = request.app.state.markdown
    return {"files": [describe(file) for file in files.of(person.id)] if person else []}


@router.get("/v1/markdown-files/{file_id}")
async def get_file(file_id: int, client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    person = _person(request, client, surface, user_id)
    found = request.app.state.markdown.get(person.id, file_id) if person else None
    if found is None:
        raise HTTPException(404, "No such file of yours")
    file, content = found
    return {**describe(file), "content": content}


@router.delete("/v1/markdown-files/{file_id}")
async def delete_file(file_id: int, client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    person = _person(request, client, surface, user_id)
    if person is None or not request.app.state.markdown.delete(person.id, file_id):
        raise HTTPException(404, "No such file of yours")
    log.info("%s deleted the markdown file %d", client, file_id)
    return {"ok": True}


def install(app: FastAPI) -> None:
    app.include_router(router)
