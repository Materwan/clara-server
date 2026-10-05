"""Reminders and notifications from the server, delivered as private messages.

The bot listens to one stream for the whole Discord surface; each event names the Discord accounts to deliver it
to (those of its person that are signed in). The server keeps a cursor: what fires while the bot is away is sent
when it reconnects (for a week).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from .approvals import approval_view, ask_text, outcome_text
from .backend import ClaraBackend, ClaraError
from .mentions import split_message
from .texts import t

if TYPE_CHECKING:
    from .accounts import Accounts

log = logging.getLogger(__name__)

RETRY_SECONDS = (2, 5, 15, 30, 60)
KEPT_REQUESTS = 200  # requests whose messages are remembered, to edit them when they are settled elsewhere


def text_of(event: dict, lang: str) -> str | None:
    """What to send for an event, or None for one not worth a private message."""
    if event.get("type") == "reminder":
        return t(lang, "reminder", text=event.get("message") or event.get("text", ""))
    if event.get("type") == "notification":
        # the server's notes about a Discord conversation (an answer that took long, a summary): the person sees
        # that conversation already
        if event.get("source") == "server" and str(event.get("conversation") or "").startswith("discord:"):
            return None
        title = event.get("title") or ""
        body = event.get("text", "")
        return t(lang, "notification", text=f"**{title}**\n{body}" if title else body)
    return None


class EventRelay:
    def __init__(self, bot: discord.Client, api: ClaraBackend, accounts: Accounts):
        self.bot = bot
        self.api = api
        self.accounts = accounts
        self._asked: dict[int, list[tuple[discord.Message, str]]] = {}  # request -> (its message, its language)

    async def run(self) -> None:
        """Listen for ever, reconnecting when the server goes away."""
        failures = 0
        while True:
            try:
                async for event in self.api.events():
                    failures = 0
                    await self.deliver(event)
            except ClaraError as error:
                if error.status in (401, 403, 422):
                    log.error("the event stream is refused (%s): no reminders on Discord", error.detail)
                    return
                log.info("event stream: %s", error.detail)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("event stream failed")
            wait = RETRY_SECONDS[min(failures, len(RETRY_SECONDS) - 1)]
            failures += 1
            await asyncio.sleep(wait)

    async def deliver(self, event: dict) -> None:
        if event.get("type") == "server":
            log.info("Clara: %s", event.get("message") or event.get("state"))
            return
        if event.get("type") == "approval":
            return await self.ask(event)
        if event.get("type") == "approval_resolved":
            return await self.settle(event)
        for account in event.get("accounts", []):
            if not str(account).isdigit():
                continue
            user_id = int(account)
            text = text_of(event, self.accounts.language(user_id))
            if not text:
                continue
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                for chunk in split_message(text):
                    await user.send(chunk)
            except discord.HTTPException as error:  # private messages closed, unknown user...
                log.warning("cannot send event %s to %s: %s", event.get("id"), user_id, error)

    async def ask(self, event: dict) -> None:
        """A request for permission nobody answered yet: a private message with its buttons."""
        approval_id = event.get("approval")
        if not isinstance(approval_id, int):
            return
        for account in event.get("accounts", []):
            if not str(account).isdigit():
                continue
            user_id = int(account)
            lang = self.accounts.language(user_id)
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                message = await user.send(ask_text(event, lang), view=approval_view(approval_id, lang))
            except discord.HTTPException as error:
                log.warning("cannot send request %s to %s: %s", approval_id, user_id, error)
                continue
            if message is not None:
                self._asked.setdefault(approval_id, []).append((message, lang))
        while len(self._asked) > KEPT_REQUESTS:
            self._asked.pop(next(iter(self._asked)))

    async def settle(self, event: dict) -> None:
        """A request was answered (here or elsewhere): its messages lose their buttons and say how it ended."""
        for message, lang in self._asked.pop(event.get("approval"), []):
            try:
                await message.edit(content=outcome_text(event, lang), view=None)
            except discord.HTTPException as error:
                log.warning("cannot edit the request message: %s", error)
