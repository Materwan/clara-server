"""The routes of the to-do list (tasks.py). Like the reminders, a person is asked for with an account
(`surface`, `user_id`); the same list is seen from every surface they use.

    POST   /v1/tasks                 {surface, user_id, title, description?, due?, reminders?, timezone?, targets?}:
                                     add a task. `reminders` are local ISO 8601 times; without any, Clara picks them
    GET    /v1/tasks                 ?surface=&user_id=&status=open|done|all  the person's tasks: for each its
                                     reminders sent, the next reminder and all those to come
    GET    /v1/tasks/{id}            ?surface=&user_id=  one task, with its description
    PATCH  /v1/tasks/{id}            {surface, user_id, title?, description?, due?, reminders?, targets?, status?}:
                                     only what is given changes; `due` null removes the deadline, `reminders` replaces
                                     those to come ([]: stop reminding), `status` "done" or "open" closes or reopens it
    DELETE /v1/tasks/{id}            ?surface=&user_id=  delete it for good
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .auth import Client, require_account, require_conversation
from .memory import Memory, Person
from .notifications import MAX_TARGETS
from .tasks import MAX_DESCRIPTION, MAX_QUEUE, MAX_TITLE, NO_DUE, TaskError, TaskService

router = APIRouter()

SURFACE = r"^[a-z0-9_-]{1,32}$"
Surface = Annotated[str, Query(pattern=SURFACE)]
ExternalId = Annotated[str, Query(min_length=1, max_length=128)]


class TaskBody(BaseModel):
    surface: str = Field(pattern=SURFACE)
    user_id: str = Field(min_length=1, max_length=128)
    user_name: str | None = Field(default=None, max_length=80)
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    description: str = Field(default="", max_length=MAX_DESCRIPTION)
    due: str | None = Field(default=None, max_length=64)  # the deadline, ISO 8601
    reminders: list[str] = Field(default_factory=list, max_length=MAX_QUEUE)  # []: Clara picks them
    timezone: str | None = Field(default=None, max_length=64)  # IANA name; read the times without offset in it
    conversation: str | None = Field(default=None, min_length=1, max_length=200)
    targets: list[str] = Field(default_factory=list, max_length=MAX_TARGETS)  # surfaces shown on; []: all


class TaskPatch(BaseModel):
    surface: str = Field(pattern=SURFACE)
    user_id: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION)
    due: str | None = Field(default=None, max_length=64)  # given as null: no deadline any more
    reminders: list[str] | None = Field(default=None, max_length=MAX_QUEUE)  # given as []: stop reminding
    timezone: str | None = Field(default=None, max_length=64)
    targets: list[str] | None = Field(default=None, max_length=MAX_TARGETS)
    status: Literal["open", "done"] | None = None


def _tasks(request: Request) -> TaskService:
    return request.app.state.tasks


def _person(request: Request, surface: str, user_id: str) -> Person:
    memory: Memory = request.app.state.memory
    person = memory.find_person(surface, user_id)
    if person is None:
        raise HTTPException(404, f"Nobody known as {surface}:{user_id}")
    return person


def _failed(error: TaskError) -> HTTPException:
    return HTTPException(404 if "No such task" in str(error) else 422, str(error))


@router.post("/v1/tasks", status_code=201)
async def add_task(body: TaskBody, client: Client, request: Request) -> dict:
    require_account(request, client, body.surface, body.user_id)
    if body.conversation:
        require_conversation(request, client, body.conversation)
    person = request.app.state.memory.resolve(body.surface, body.user_id, body.user_name)
    origin = (body.surface, body.user_id, body.conversation or f"{body.surface}:{body.user_id}")
    tasks = _tasks(request)
    try:
        task = await tasks.create(
            person, body.title, body.description, body.due, body.reminders, body.timezone, origin, body.targets
        )
    except TaskError as error:
        raise _failed(error) from None
    return tasks.describe(task)


@router.get("/v1/tasks")
async def list_tasks(
    client: Client, request: Request, surface: Surface, user_id: ExternalId,
    status: Literal["open", "done", "all"] = "open",
) -> dict:
    require_account(request, client, surface, user_id)
    tasks = _tasks(request)
    person = request.app.state.memory.find_person(surface, user_id)
    found = tasks.tasks(person, None if status == "all" else status) if person else []
    return {"tasks": [tasks.describe(task) for task in found], "max_reminders": tasks.max_reminders}


@router.get("/v1/tasks/{task_id}")
async def get_task(task_id: int, client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    require_account(request, client, surface, user_id)
    tasks = _tasks(request)
    try:
        return tasks.describe(tasks.get(_person(request, surface, user_id), task_id))
    except TaskError as error:
        raise _failed(error) from None


@router.patch("/v1/tasks/{task_id}")
async def patch_task(task_id: int, body: TaskPatch, client: Client, request: Request) -> dict:
    require_account(request, client, body.surface, body.user_id)
    tasks = _tasks(request)
    person = _person(request, body.surface, body.user_id)
    try:
        task = tasks.get(person, task_id)
        reopening = body.status == "open" and task.status != "open"
        if body.status == "done":
            if body.reminders:
                raise TaskError("A task that is done is not reminded: reopen it to set reminders.")
            tasks.complete(person, task_id)
        elif reopening:
            await tasks.reopen(person, task_id, body.reminders, body.timezone)
        changes = {"title": body.title, "description": body.description, "targets": body.targets}
        due = body.due if "due" in body.model_fields_set else NO_DUE
        reminders = None if reopening or body.status == "done" else body.reminders
        if any(v is not None for v in changes.values()) or due is not NO_DUE or reminders is not None:
            tasks.update(
                person, task_id, body.title, body.description, due, reminders, body.targets, body.timezone
            )
        return tasks.describe(tasks.get(person, task_id))
    except TaskError as error:
        raise _failed(error) from None


@router.delete("/v1/tasks/{task_id}")
async def delete_task(task_id: int, client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    require_account(request, client, surface, user_id)
    if not _tasks(request).delete(_person(request, surface, user_id), task_id):
        raise HTTPException(404, "No such task of yours")
    return {"deleted": task_id}


def install(app: FastAPI) -> None:
    app.include_router(router)
