"""Clara over HTTP, for a bot run on its own (`clara-discord`, possibly on another machine).

The bot authenticates with one client token and says, in each request, which Discord account speaks
(`surface=discord`, `user_id=<the Discord id>`). The server refuses (403) an account that is not signed in.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx

from .backend import ClaraError, Reply

log = logging.getLogger(__name__)

SURFACE = "discord"
CHAT_TIMEOUT = httpx.Timeout(connect=10, read=900, write=30, pool=30)  # a model can think for minutes
SHORT_TIMEOUT = httpx.Timeout(30)


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, list):  # FastAPI validation errors
        detail = "; ".join(str(item.get("msg", item)) for item in detail)
    return str(detail or response.text[:300] or response.reason_phrase)


class RemoteBackend:
    def __init__(self, url: str, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self.url = url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.url, headers={"Authorization": f"Bearer {token}"}, timeout=SHORT_TIMEOUT, transport=transport
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **options: Any) -> Any:
        try:
            response = await self._client.request(method, path, **options)
        except httpx.HTTPError as error:
            raise ClaraError(0, f"{type(error).__name__}: {error}") from None
        if response.status_code >= 400:
            raise ClaraError(response.status_code, _detail(response))
        return response.json() if response.content else {}

    @staticmethod
    def _account(user_id: int | str) -> dict[str, str]:
        return {"surface": SURFACE, "user_id": str(user_id)}

    # -- accounts ----------------------------------------------------------------------------------- #

    async def register(self, user_id: int, display_name: str, username: str, password: str) -> dict:
        body = {**self._account(user_id), "user_name": display_name, "username": username, "password": password}
        return await self._call("POST", "/v1/accounts/register", json=body)

    async def login(self, user_id: int, display_name: str, username: str, password: str) -> dict:
        body = {**self._account(user_id), "user_name": display_name, "username": username, "password": password}
        return await self._call("POST", "/v1/accounts/login", json=body)

    async def logout(self, user_id: int) -> bool:
        return (await self._call("POST", "/v1/accounts/logout", json=self._account(user_id)))["signed_out"]

    async def me(self, user_id: int) -> dict:
        return await self._call("GET", "/v1/accounts/me", params=self._account(user_id))

    async def signed_in(self) -> dict[int, str]:
        """Every Discord account that is signed in: id -> user name."""
        found = await self._call("GET", "/v1/accounts/signed-in", params={"surface": SURFACE})
        return {int(entry["user_id"]): entry["user"] for entry in found["accounts"] if entry["user_id"].isdigit()}

    # -- memory --------------------------------------------------------------------------------------- #

    async def add_fact(self, user_id: int, text: str) -> dict:
        return await self._call("POST", "/v1/memory/facts", json={**self._account(user_id), "text": text})

    async def delete_fact(self, user_id: int, fact_id: int) -> None:
        await self._call("DELETE", f"/v1/memory/facts/{fact_id}", params=self._account(user_id))

    async def clear_conversation(self, conversation: str) -> int:
        found = await self._call("DELETE", f"/v1/conversations/{quote(conversation, safe=':')}")
        return found.get("deleted_messages", 0)

    # -- the to-do list ------------------------------------------------------------------------------- #

    async def tasks(self, user_id: int, status: str = "open") -> list[dict]:
        found = await self._call("GET", "/v1/tasks", params={**self._account(user_id), "status": status})
        return found["tasks"]

    async def task(self, user_id: int, task_id: int) -> dict:
        return await self._call("GET", f"/v1/tasks/{task_id}", params=self._account(user_id))

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
        body: dict[str, Any] = {
            **self._account(user_id), "user_name": display_name, "message": message, "mode": mode, "quiet": True,
        }
        if conversation:
            body["conversation"] = conversation
        if space:
            body.update(space=space, roster=roster or [], focus=[str(i) for i in focus or []])
        if instructions:
            body["instructions"] = instructions
        if prefix:
            body["prefix"] = prefix
        if timezone:
            body["timezone"] = timezone
        done = await self._call("POST", "/v1/chat", json=body, timeout=CHAT_TIMEOUT)
        answered = not done.get("observed") and not done.get("passed")
        return Reply(done.get("reply", "") if answered else "", done.get("conversation", ""), answered)

    # -- spaces and events ---------------------------------------------------------------------------- #

    async def sync_spaces(self, spaces: list[tuple[str, str]]) -> dict:
        body = {"surface": SURFACE, "spaces": [{"id": space_id, "name": name} for space_id, name in spaces]}
        return await self._call("PUT", "/v1/spaces", json=body)

    async def events(self) -> AsyncIterator[dict]:
        """The server's events for every Discord account (reminders, notifications, server state), until the
        connection ends. ClaraError if it cannot be opened."""
        params = {"surface": SURFACE, "all": "true"}
        timeout = httpx.Timeout(connect=10, read=60, write=10, pool=10)  # a keepalive comes every 15 s
        try:
            async with self._client.stream("GET", "/v1/notifications/stream", params=params, timeout=timeout) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise ClaraError(response.status_code, _detail(response))
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        try:
                            yield json.loads(line[6:])
                        except ValueError:
                            log.warning("unreadable event: %s", line[:200])
        except httpx.HTTPError as error:
            raise ClaraError(0, f"{type(error).__name__}: {error}") from None
