"""Clara inside this process, for the bot that runs in clara-server: no token, no HTTP.

Each call goes through the same functions as the HTTP routes (the checks of auth.py, clientapi.py and the chat
route of chatapi.py), with the bot as the client `discord-bot`. Their HTTP errors come back as ClaraError with the
same status, so the bot behaves the same whichever backend it has.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from .. import clientapi
from ..apicommon import model_failure
from ..auth import Caller, require_account, require_conversation
from ..chatapi import ChatBody, checked
from ..notificationapi import recipients
from .backend import ClaraError, Reply

log = logging.getLogger(__name__)

SURFACE = "discord"
CLIENT = "discord-bot"  # the name the built-in bot has as a client (traffic log, event cursor, logs)


class _Request:
    """What the route functions read of a request: the application, and no address (it is this process)."""

    def __init__(self, app: FastAPI):
        self.app = app
        self.client = None
        self.headers: dict[str, str] = {}
        self.scope: dict[str, Any] = {}


class LocalBackend:
    url = "in this server"

    def __init__(self, app: FastAPI):
        self.app = app
        self.state = app.state
        self.client = Caller(CLIENT)
        self.request = _Request(app)

    async def close(self) -> None:
        pass

    async def _guard(self, call):
        """Run a route function; its refusals become ClaraError."""
        try:
            return await call
        except HTTPException as error:
            raise ClaraError(error.status_code, str(error.detail)) from None
        except ValidationError as error:
            raise ClaraError(422, "; ".join(item["msg"] for item in error.errors())) from None

    def _account(self, user_id: int | str, signed_in: bool = True) -> str:
        try:
            require_account(self.request, self.client, SURFACE, str(user_id), signed_in=signed_in)
        except HTTPException as error:
            raise ClaraError(error.status_code, str(error.detail)) from None
        return str(user_id)

    # -- accounts ----------------------------------------------------------------------------------- #

    async def register(self, user_id: int, display_name: str, username: str, password: str) -> dict:
        body = clientapi.SignInBody(
            surface=SURFACE, user_id=str(user_id), user_name=display_name, username=username, password=password
        )
        return await self._guard(clientapi.register(body, self.client, self.request))

    async def login(self, user_id: int, display_name: str, username: str, password: str) -> dict:
        body = clientapi.SignInBody(
            surface=SURFACE, user_id=str(user_id), user_name=display_name, username=username, password=password
        )
        return await self._guard(clientapi.account_login(body, self.client, self.request))

    async def logout(self, user_id: int) -> bool:
        body = clientapi._Account(surface=SURFACE, user_id=str(user_id))
        return (await self._guard(clientapi.account_logout(body, self.client, self.request)))["signed_out"]

    async def me(self, user_id: int) -> dict:
        return await self._guard(clientapi.account_me(SURFACE, str(user_id), self.client, self.request))

    async def signed_in(self) -> dict[int, str]:
        found = await self._guard(clientapi.signed_in_accounts(SURFACE, self.client, self.request))
        return {int(entry["user_id"]): entry["user"] for entry in found["accounts"] if entry["user_id"].isdigit()}

    # -- memory --------------------------------------------------------------------------------------- #

    async def add_fact(self, user_id: int, text: str) -> dict:
        external = self._account(user_id)
        memory = self.state.memory
        try:
            fact = memory.add_fact(memory.resolve(SURFACE, external).id, text)
        except ValueError as error:
            raise ClaraError(422, str(error)) from None
        return {"created": fact is not None, "id": fact.id if fact else None}

    async def delete_fact(self, user_id: int, fact_id: int) -> None:
        external = self._account(user_id)
        memory = self.state.memory
        person = memory.find_person(SURFACE, external)
        if person is None or not memory.delete_fact(person.id, fact_id):
            raise ClaraError(404, "No such fact for this person")

    async def clear_conversation(self, conversation: str) -> int:
        try:
            require_conversation(self.request, self.client, conversation)
        except HTTPException as error:
            raise ClaraError(error.status_code, str(error.detail)) from None
        return self.state.memory.clear_conversation(conversation)

    # -- the to-do list ------------------------------------------------------------------------------- #

    async def tasks(self, user_id: int, status: str = "open") -> list[dict]:
        external = self._account(user_id)
        person = self.state.memory.find_person(SURFACE, external)
        if person is None:
            return []
        service = self.state.tasks
        return service.describe_all(person, service.tasks(person, None if status == "all" else status))

    async def task(self, user_id: int, task_id: int) -> dict:
        external = self._account(user_id)
        person = self.state.memory.find_person(SURFACE, external)
        found = self.state.tasks.store.get(person.id, task_id) if person else None
        if found is None:
            raise ClaraError(404, "No such task of yours")
        return self.state.tasks.describe(found)

    # -- talking -------------------------------------------------------------------------------------- #

    async def chat(
        self,
        user_id: int,
        display_name: str,
        message: str,
        *,
        conversation: str | None = None,
        space: str | None = None,
        roster: list[dict[str, str]] | None = None,
        focus: list[int] | None = None,
        mode: str = "answer",
        instructions: str = "",
        prefix: str = "",
        timezone: str | None = None,
    ) -> Reply:
        fields: dict[str, Any] = {
            "surface": SURFACE, "user_id": str(user_id), "user_name": display_name, "message": message, "mode": mode,
            "quiet": True, "conversation": conversation, "instructions": instructions, "prefix": prefix,
            "timezone": timezone,
        }
        if space:
            fields.update(space=space, roster=roster or [], focus=[str(i) for i in focus or []])
        try:
            request = checked(self.request, self.client, ChatBody(**fields))
        except HTTPException as error:
            raise ClaraError(error.status_code, str(error.detail)) from None
        except ValidationError as error:
            raise ClaraError(422, "; ".join(item["msg"] for item in error.errors())) from None
        done: dict = {}
        try:
            async for event in self.state.agent.turn(request, self.client):
                done = event
        except Exception as error:  # the same answer as the chat route's
            refused = model_failure(error, "chat", self.client)
            raise ClaraError(refused.status_code, str(refused.detail)) from None
        answered = not done.get("observed") and not done.get("passed")
        return Reply(done.get("reply", "") if answered else "", done.get("conversation", ""), answered)

    # -- requests for permission ---------------------------------------------------------------------- #

    async def decide_approval(self, user_id: int, approval_id: int, approve: bool) -> dict:
        from .. import integrationapi

        body = integrationapi.DecideBody(surface=SURFACE, user_id=str(user_id), approve=approve)
        return await self._guard(integrationapi.decide_approval(approval_id, body, self.client, self.request))

    # -- spaces and events ---------------------------------------------------------------------------- #

    async def sync_spaces(self, spaces: list[tuple[str, str]]) -> dict:
        body = clientapi.SpacesBody(
            surface=SURFACE, spaces=[clientapi.SpaceEntry(id=space_id, name=name) for space_id, name in spaces]
        )
        return await self._guard(clientapi.sync_spaces(body, self.client, self.request))

    async def events(self) -> AsyncIterator[dict]:
        stream = self.state.notifier.surface_events(self.client, SURFACE, recipients(self.request, SURFACE))
        try:
            async for event in stream:
                yield event
        finally:
            await stream.aclose()
