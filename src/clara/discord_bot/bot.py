"""The Discord client: gateway events in, the handler, the commands and the event relay out."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import discord
from discord import app_commands

from .accounts import REFRESH_SECONDS, Accounts
from .backend import ClaraBackend, ClaraError
from .commands import FrenchTranslator, register_commands
from .events import EventRelay
from .handler import MessageHandler
from .routing import space_of

log = logging.getLogger(__name__)


class ClaraBot(discord.Client):
    def __init__(self, api: ClaraBackend, timezone: str | None = None):
        intents = discord.Intents.default()
        intents.message_content = True  # to read what is said, not only what is said to Clara
        intents.members = True  # to know the members of each server (who to list to Clara, who to ping)
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False, replied_user=True),
        )
        self.api = api
        self.accounts = Accounts(api)
        self.handler = MessageHandler(self, api, self.accounts, timezone)
        self.relay = EventRelay(self, api, self.accounts)
        self.tree = app_commands.CommandTree(self)
        self.tree.on_error = self.on_command_error
        register_commands(self.tree, self)
        self._tasks: list[asyncio.Task] = []

    async def setup_hook(self) -> None:
        await self.tree.set_translator(FrenchTranslator())
        try:
            synced = await self.tree.sync()  # one bulk update of the global commands
            log.info("slash commands published: %s", ", ".join(command.name for command in synced))
        except discord.HTTPException as error:
            log.error("could not publish the slash commands: %s", error)
        await self.accounts.refresh()
        self._tasks = [asyncio.create_task(self.relay.run()), asyncio.create_task(self._refresh_accounts())]

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        await super().close()

    async def _refresh_accounts(self) -> None:
        while True:
            await asyncio.sleep(REFRESH_SECONDS)
            await self.accounts.refresh()

    async def sync_spaces(self) -> None:
        """Tell Clara which Discord servers the bot is in (an administrator lets her chime in there)."""
        try:
            await self.api.sync_spaces([(space_of(guild.id), guild.name) for guild in self.guilds])
        except ClaraError as error:
            log.warning("cannot list the servers to Clara: %s", error.detail)

    # -- gateway events ----------------------------------------------------------------------------- #

    async def on_ready(self) -> None:
        log.info("Discord: connected as %s (%s), in %d server(s), Clara %s", self.user,
                 self.user.id if self.user else "?", len(self.guilds), self.api.url)
        await self.sync_spaces()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Discord: joined the server %s (%s)", guild.name, guild.id)
        await self.sync_spaces()

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("Discord: left the server %s (%s)", guild.name, guild.id)
        await self.sync_spaces()

    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        if before.name != after.name:
            await self.sync_spaces()

    async def on_message(self, message: discord.Message) -> None:
        await self.handler.handle(message)

    async def on_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        command = interaction.command.name if interaction.command else "?"
        log.error("/%s failed", command, exc_info=error)
        with contextlib.suppress(discord.HTTPException):
            text = "Something went wrong."
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        log.exception("Discord: error in %s", event_method)
