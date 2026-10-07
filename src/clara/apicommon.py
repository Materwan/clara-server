"""What the route modules of the core API share (chatapi.py, conversationapi.py, accountapi.py, notificationapi.py):
the shape of an account in a request, the server-sent-events helpers, and the HTTP answer to a model that failed."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .agent import ModelTimeout, NothingToCompact, NothingToTitle, PromptTooLarge, ServerStopping
from .limits import UsageLimitReached
from .memory import Memory, Person

log = logging.getLogger("clara")

Surface = Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,32}$")]
ExternalId = Annotated[str, Field(min_length=1, max_length=128)]


class Body(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def _ids_may_be_numbers(cls, value: Any) -> Any:
        # Discord ids are big integers; accept them without making clients stringify
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


class AccountBody(Body):
    surface: Surface
    user_id: ExternalId


def known_person(request: Request, surface: str, user_id: str) -> Person:
    """The person behind an account; 404 when the server does not know it (nothing is created)."""
    memory: Memory = request.app.state.memory
    person = memory.find_person(surface, user_id)
    if person is None:
        raise HTTPException(404, f"Nobody known as {surface}:{user_id}")
    return person


def own_project_id(request: Request, surface: str, user_id: str, project_id: int) -> int:
    """The project, if the account's person owns it; else 404."""
    person = request.app.state.memory.find_person(surface, user_id)
    project = request.app.state.projects.get(project_id)
    if person is None or project is None or project.person_id != person.id:
        raise HTTPException(404, "No such project of yours")
    return project.id


def refuse_when_stopping(request: Request) -> None:
    if request.app.state.lifecycle.stopping:
        raise HTTPException(503, "Clara is stopping and takes no new question.")


def model_failure(error: Exception, what: str, client: str) -> HTTPException:
    """The HTTP answer to a request to the model that failed with `error`. Call it from the `except` block: an
    error nobody expected is logged with its traceback, as `what` failed."""
    if isinstance(error, (NothingToCompact, NothingToTitle)):
        return HTTPException(409, str(error))
    if isinstance(error, PromptTooLarge):
        return HTTPException(413, str(error))
    if isinstance(error, UsageLimitReached):
        return HTTPException(429, str(error), headers={"Retry-After": str(error.retry_after)})
    if isinstance(error, ServerStopping):
        return HTTPException(503, str(error))
    if isinstance(error, ModelTimeout):
        return HTTPException(504, str(error))
    log.exception("%s failed (client=%s)", what, client)
    return HTTPException(502, "The language model failed")


# ----------------------------------------------------------------------
# Server-sent events
# ----------------------------------------------------------------------
KEEPALIVE_SECONDS = 15.0


def sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


async def with_keepalive(events: AsyncIterator[dict], interval: float = KEEPALIVE_SECONDS) -> AsyncIterator[dict | None]:
    """The events, plus a None every `interval` seconds of silence (a client may spend minutes
    running a tool, and idle connections get dropped by proxies)."""
    iterator = events.__aiter__()
    pending = asyncio.ensure_future(iterator.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield None
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            yield event
            pending = asyncio.ensure_future(iterator.__anext__())
    finally:
        pending.cancel()
        with contextlib.suppress(BaseException):
            await pending
        with contextlib.suppress(Exception):
            await iterator.aclose()  # type: ignore[attr-defined]


async def sse_lines(events: AsyncIterator[dict]) -> AsyncIterator[str]:
    """`events` as server-sent events, with a keepalive comment in the silences."""
    async with contextlib.aclosing(with_keepalive(events)) as stream:
        async for event in stream:
            yield ": keepalive\n\n" if event is None else sse(event)


def event_stream(lines: AsyncIterator[str]) -> StreamingResponse:
    """A response that streams server-sent events as they come (no cache, no proxy buffering)."""
    return StreamingResponse(
        lines, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
