"""What the bot needs from Clara, whichever way it reaches her.

Two implementations: `LocalBackend` (local.py) calls the server's own objects, for the bot that runs inside
clara-server; `RemoteBackend` (remote.py) calls the HTTP API, for a bot run on its own (`clara-discord`). Both
apply the same rules: an account that is not signed in is refused, chime in is decided by the server, and so on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from ..limits import LIMIT_MARK


class ClaraError(Exception):
    """Clara said no, or could not be reached (`status` 0). The statuses are the HTTP API's."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail

    @property
    def not_signed_in(self) -> bool:
        return self.status == 403 and "not signed in" in self.detail

    @property
    def over_limit(self) -> bool:
        """The person used all their tokens for today (the detail says when they have some again)."""
        return self.status == 429 and LIMIT_MARK in self.detail

    @property
    def unreachable(self) -> bool:
        return self.status == 0


@dataclass(frozen=True)
class Reply:
    text: str  # "" when Clara chose not to answer, or the message was only kept as context
    conversation: str
    answered: bool  # False: observed, or a "maybe" she passed on
    files: tuple[dict, ...] = ()  # the markdown files she wrote in the answer: {id, name, action}


class ClaraBackend(Protocol):
    url: str  # where Clara is, for the log ("in this server" for the local one)

    async def close(self) -> None: ...

    # accounts
    async def register(self, user_id: int, display_name: str, username: str, password: str) -> dict: ...
    async def login(self, user_id: int, display_name: str, username: str, password: str) -> dict: ...
    async def logout(self, user_id: int) -> bool: ...
    async def me(self, user_id: int) -> dict: ...
    async def signed_in(self) -> dict[int, str]: ...

    # memory
    async def add_fact(self, user_id: int, text: str) -> dict: ...
    async def delete_fact(self, user_id: int, fact_id: int) -> None: ...
    async def clear_conversation(self, conversation: str) -> int: ...

    # the to-do list
    async def tasks(self, user_id: int, status: str = "open") -> list[dict]: ...
    async def task(self, user_id: int, task_id: int) -> dict: ...
    async def file_of(self, user_id: int, file_id: int) -> dict: ...  # {name, content}: a markdown file of theirs

    # requests for permission
    async def decide_approval(self, user_id: int, approval_id: int, approve: bool) -> dict: ...

    # talking
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
        attachments: list[dict] | None = None,
    ) -> Reply: ...

    # spaces and events
    async def sync_spaces(self, spaces: list[tuple[str, str]]) -> dict: ...
    def events(self) -> AsyncIterator[dict]: ...
