"""The routes of what the server announces to a person: reminders, notifications, and the stream of events that
carries them (see notifications.py).

    POST   /v1/reminders                set a reminder: a text and a moment, announced to the person who set it
    GET    /v1/reminders                the reminders a person set that have not fired
    DELETE /v1/reminders/{id}           cancel one
    POST   /v1/notifications            send a notification to a person, now
    GET    /v1/notifications/stream     Server-Sent Events of an account: `reminder`, `notification`, `server`
                                        (also served as /v1/reminders/stream)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from .apicommon import AccountBody, ExternalId, Surface, event_stream, known_person, log, sse, sse_lines
from .auth import Client, require_account, require_conversation
from .memory import Memory
from .notifications import MAX_TARGETS, MAX_TEXT, MAX_TITLE, SURFACE_RE, NotificationError, Notifier, RateLimited
from .reminders import ReminderError, ReminderService, describe
from .settings import Settings

router = APIRouter()


class ReminderBody(AccountBody):
    user_name: str | None = Field(default=None, max_length=80)
    text: str = Field(min_length=1, max_length=2000)
    at: str = Field(min_length=1, max_length=64)  # ISO 8601, e.g. 2026-10-05T09:00 or ...T09:00+02:00
    repeat: str = Field(default="", max_length=16)  # "", "daily", "weekly" or "monthly"
    timezone: str | None = Field(default=None, max_length=64)  # IANA name; read a time without offset in it
    conversation: str | None = Field(default=None, min_length=1, max_length=200)  # where Clara announces it
    targets: list[Surface] = Field(default_factory=list, max_length=MAX_TARGETS)  # surfaces shown on; []: all


class NotificationBody(AccountBody):  # the account of the person to notify
    user_name: str | None = Field(default=None, max_length=80)
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    title: str = Field(default="", max_length=MAX_TITLE)
    targets: list[Surface] = Field(default_factory=list, max_length=MAX_TARGETS)  # surfaces shown on; []: all
    conversation: str | None = Field(default=None, min_length=1, max_length=200)  # what it is about, if any


# ----------------------------------------------------------------------
# Reminders
# ----------------------------------------------------------------------
@router.post("/v1/reminders", status_code=201)
async def add_reminder(body: ReminderBody, client: Client, request: Request) -> dict:
    require_account(request, client, body.surface, body.user_id)
    if body.conversation:
        require_conversation(request, client, body.conversation)
    person = request.app.state.memory.resolve(body.surface, body.user_id, body.user_name)
    origin = (body.surface, body.user_id, body.conversation or f"{body.surface}:{body.user_id}")
    reminders: ReminderService = request.app.state.reminders
    try:
        reminder = reminders.create(person, body.text, body.at, body.repeat, body.timezone, origin, body.targets)
    except ReminderError as error:
        raise HTTPException(422, str(error)) from None
    return describe(reminder)


@router.get("/v1/reminders")
async def list_reminders(client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    require_account(request, client, surface, user_id)
    person = request.app.state.memory.find_person(surface, user_id)
    reminders: ReminderService = request.app.state.reminders
    return {"reminders": [describe(r) for r in reminders.upcoming(person)] if person else []}


@router.delete("/v1/reminders/{reminder_id}")
async def cancel_reminder(
    reminder_id: int, client: Client, request: Request, surface: Surface, user_id: ExternalId
) -> dict:
    require_account(request, client, surface, user_id)
    person = known_person(request, surface, user_id)
    if not request.app.state.reminders.cancel(person, reminder_id):
        raise HTTPException(404, "No such reminder of yours")
    return {"deleted": reminder_id}


# ----------------------------------------------------------------------
# Notifications
# ----------------------------------------------------------------------
@router.post("/v1/notifications", status_code=201)
async def send_notification(body: NotificationBody, client: Client, request: Request) -> dict:
    """A notification for the person behind an account, on some of their surfaces (any, not only the
    client's own: it is the same person)."""
    require_account(request, client, body.surface, body.user_id)
    if body.conversation:
        require_conversation(request, client, body.conversation)
    person = request.app.state.memory.resolve(body.surface, body.user_id, body.user_name)
    notifier: Notifier = request.app.state.notifier
    try:
        event = notifier.notify(person.id, body.text, body.title, body.targets, client, body.conversation or "")
    except RateLimited as error:
        raise HTTPException(429, str(error)) from None
    except NotificationError as error:
        raise HTTPException(422, str(error)) from None
    return {"id": event.id, "sent_at": event.fired_at, "targets": list(event.targets)}


def recipients(request: Request, surface: str) -> Callable[[int], list[str]]:
    """The accounts of a person on a surface that can be sent something (signed in, on a login surface)."""
    memory: Memory = request.app.state.memory
    settings: Settings = request.app.state.settings

    def accounts(person_id: int) -> list[str]:
        mine = [external for s, external in memory.accounts_of(person_id) if s == surface]
        if surface in settings.login_surfaces:
            mine = [external for external in mine if request.app.state.users.account_user(surface, external) is not None]
        return mine

    return accounts


def _event_stream(
    client: Client, request: Request, surface: str | None, user_id: str | None, every_account: bool = False
) -> StreamingResponse:
    """What the server announces to an account: its person's reminders and notifications (for its
    surface), and the state of the server. Without an account: only the state of the server and what
    is for everybody. `every_account` (a client of a whole surface): what is for any of its accounts."""
    settings: Settings = request.app.state.settings
    notifier: Notifier = request.app.state.notifier
    if every_account:
        if not surface or user_id or not SURFACE_RE.match(surface):
            raise HTTPException(422, "all=true needs a surface and no user_id")
        if getattr(client, "user", None) is not None:
            raise HTTPException(403, "Only a client may listen for a whole surface")
        allowed = settings.client_surfaces.get(client)
        if allowed is not None and surface not in allowed:
            raise HTTPException(403, f"This client may not use the surface {surface!r}")
    elif bool(surface) != bool(user_id):
        raise HTTPException(422, "Give both surface and user_id, or neither")
    elif surface is not None:
        if not SURFACE_RE.match(surface) or not 1 <= len(user_id or "") <= 128:
            raise HTTPException(422, "Bad surface or user_id")
        require_account(request, client, surface, user_id)

    async def lines() -> AsyncIterator[str]:
        try:
            if every_account:
                stream = notifier.surface_events(client, surface, recipients(request, surface))
            else:
                stream = notifier.events(client, surface, user_id)
            async for line in sse_lines(stream):
                yield line
        except Exception:
            log.exception("event stream failed (client=%s)", client)
            yield sse({"type": "error", "message": "The event stream failed"})

    return event_stream(lines())


@router.get("/v1/notifications/stream")
async def notification_stream(
    client: Client, request: Request, surface: str | None = None, user_id: str | None = None,
    all: bool = False,  # noqa: A002 - the name of the query parameter
) -> StreamingResponse:
    return _event_stream(client, request, surface, user_id, all)


@router.get("/v1/reminders/stream")
async def reminder_stream(
    client: Client, request: Request, surface: str | None = None, user_id: str | None = None
) -> StreamingResponse:
    """The same stream as /v1/notifications/stream (its earlier name)."""
    return _event_stream(client, request, surface, user_id)


def install(app: FastAPI) -> None:
    app.include_router(router)
