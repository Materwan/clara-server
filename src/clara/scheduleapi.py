"""The routes of the scheduled tasks (schedule.py). A person is asked for with an account (`surface`, `user_id`).

    GET    /v1/schedules             ?surface=&user_id=  their scheduled tasks: when each runs next, how the last run
                                     went (`last_status` ok / failed / missed, `last_summary`), `resources`: the ids of
                                     the integrations attached to it
    POST   /v1/schedules             {surface, user_id, name, prompt, documents?, at, timezone?, repeat?, days?,
                                     project?, resources?}: plan one. `at` is the first run, local ISO 8601; `repeat`
                                     daily, weekly (on `days`, 0 is Monday) or monthly, none: once. `documents` are
                                     {name, kind, text}, put after the prompt
    PATCH  /v1/schedules/{id}        {surface, user_id, ...}: only what is given changes; `enabled` pauses and resumes it,
                                     a document without `text` keeps the one of that name
    POST   /v1/schedules/{id}/run    {surface, user_id}: run it now (202), besides its times
    DELETE /v1/schedules/{id}        ?surface=&user_id=  delete it (its conversation stays)
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .auth import Client, require_account
from .memory import Person
from .projectapi import own_project
from .schedule import MAX_NAME, MAX_PER_PERSON, MAX_PROMPT, REPEATS, ScheduleError, ScheduleService

router = APIRouter()

SURFACE = r"^[a-z0-9_-]{1,32}$"
Surface = Annotated[str, Query(pattern=SURFACE)]
ExternalId = Annotated[str, Query(min_length=1, max_length=128)]


class Document(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(default="", max_length=30)
    text: str | None = Field(default=None, max_length=2_000_000)  # None: the one kept under that name


class ScheduleBody(BaseModel):
    surface: str = Field(pattern=SURFACE)
    user_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=MAX_NAME)
    prompt: str = Field(default="", max_length=MAX_PROMPT)
    documents: list[Document] = Field(default_factory=list, max_length=20)
    at: str = Field(max_length=64)
    timezone: str | None = Field(default=None, max_length=64)
    repeat: str = Field(default="", max_length=10)
    days: list[int] = Field(default_factory=list, max_length=7)
    project: int | None = Field(default=None, ge=1)
    resources: list[int] = Field(default_factory=list, max_length=50)


class SchedulePatch(BaseModel):
    surface: str = Field(pattern=SURFACE)
    user_id: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME)
    prompt: str | None = Field(default=None, max_length=MAX_PROMPT)
    documents: list[Document] | None = Field(default=None, max_length=20)
    at: str | None = Field(default=None, max_length=64)
    timezone: str | None = Field(default=None, max_length=64)
    repeat: str | None = Field(default=None, max_length=10)
    days: list[int] | None = Field(default=None, max_length=7)
    project: int | None = Field(default=None, ge=1)  # given as null: no project
    resources: list[int] | None = Field(default=None, max_length=50)
    enabled: bool | None = None


class Who(BaseModel):
    surface: str = Field(pattern=SURFACE)
    user_id: str = Field(min_length=1, max_length=128)


def _service(request: Request) -> ScheduleService:
    return request.app.state.schedules


def _person(request: Request, client: str, surface: str, user_id: str, create: bool = False) -> Person:
    require_account(request, client, surface, user_id)
    memory = request.app.state.memory
    person = memory.resolve(surface, user_id, None) if create else memory.find_person(surface, user_id)
    if person is None:
        raise HTTPException(404, f"Nobody known as {surface}:{user_id}")
    return person


def _failed(error: ScheduleError) -> HTTPException:
    return HTTPException(404 if "No such" in str(error) else 422, str(error))


def _own_resources(request: Request, person: Person, ids: list[int]) -> list[int]:
    store = request.app.state.integrations.store
    for resource_id in ids:
        resource = store.resource(resource_id)
        if resource is None or resource.person_id != person.id:
            raise HTTPException(404, "No such resource of yours")
    return list(dict.fromkeys(ids))


def _attach(request: Request, conversation: str, ids: list[int]) -> None:
    """Make the integrations attached to the conversation exactly `ids`."""
    store = request.app.state.integrations.store
    for item in store.attachments_of(conversation=conversation):
        if item.resource.id not in ids:
            store.detach(item.attachment_id)
    for resource_id in ids:
        store.attach(resource_id, None, conversation)


def _describe(request: Request, schedule) -> dict:
    store = request.app.state.integrations.store
    return {
        **_service(request).describe(schedule),
        "resources": [a.resource.id for a in store.attachments_of(conversation=schedule.conversation)],
    }


@router.get("/v1/schedules")
async def list_schedules(client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    require_account(request, client, surface, user_id)
    person = request.app.state.memory.find_person(surface, user_id)
    found = _service(request).store.of(person.id) if person else []
    return {"schedules": [_describe(request, s) for s in found], "repeats": list(REPEATS), "max": MAX_PER_PERSON}


@router.post("/v1/schedules", status_code=201)
async def add_schedule(body: ScheduleBody, client: Client, request: Request) -> dict:
    person = _person(request, client, body.surface, body.user_id, create=True)
    if body.project is not None:
        own_project(request, client, body.surface, body.user_id, body.project)
    resources = _own_resources(request, person, body.resources)
    try:
        schedule = _service(request).create(
            person, body.surface, body.user_id, body.name, body.prompt, [d.model_dump() for d in body.documents],
            body.at, body.timezone, body.repeat, body.days, body.project,
        )
    except ScheduleError as error:
        raise _failed(error) from None
    _attach(request, schedule.conversation, resources)
    return _describe(request, schedule)


@router.patch("/v1/schedules/{schedule_id}")
async def patch_schedule(schedule_id: int, body: SchedulePatch, client: Client, request: Request) -> dict:
    person = _person(request, client, body.surface, body.user_id)
    given = body.model_dump(exclude_unset=True)
    if given.get("project") is not None:
        own_project(request, client, body.surface, body.user_id, body.project)  # type: ignore[arg-type]
    resources = _own_resources(request, person, body.resources) if body.resources is not None else None
    fields = {k: v for k, v in given.items() if k not in ("surface", "user_id", "project", "resources")}
    if "project" in given:
        fields["project_id"] = body.project
    try:
        schedule = _service(request).update(person, schedule_id, **fields)
    except ScheduleError as error:
        raise _failed(error) from None
    if resources is not None:
        _attach(request, schedule.conversation, resources)
    return _describe(request, schedule)


@router.post("/v1/schedules/{schedule_id}/run", status_code=202)
async def run_schedule(schedule_id: int, body: Who, client: Client, request: Request) -> dict:
    person = _person(request, client, body.surface, body.user_id)
    try:
        return _describe(request, _service(request).run_now(person, schedule_id))
    except ScheduleError as error:
        raise _failed(error) from None


@router.delete("/v1/schedules/{schedule_id}")
async def delete_schedule(schedule_id: int, client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    person = _person(request, client, surface, user_id)
    if not _service(request).delete(person, schedule_id):
        raise HTTPException(404, "No such scheduled task of yours")
    return {"deleted": schedule_id}


def install(app: FastAPI) -> None:
    app.include_router(router)
