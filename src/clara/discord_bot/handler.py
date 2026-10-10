"""A Discord message, from its arrival to Clara's answer (see routing.py for who gets one)."""

from __future__ import annotations

import base64
import io
import logging
from collections import Counter
from typing import TYPE_CHECKING

import discord
import httpx

from .backend import ClaraBackend, ClaraError
from .gifs import gif_links, gif_of
from .mentions import add_pings, readable, split_message
from .routing import Action, Incoming, conversation_of, route, space_of
from .texts import ENGLISH, language, t

if TYPE_CHECKING:
    from .accounts import Accounts

log = logging.getLogger(__name__)

QUOTE = 300  # characters of a replied-to message given to Clara
MAX_SENT_FILES = 10  # Discord takes this many files in one message


class MessageHandler:
    def __init__(
        self, bot: discord.Client, api: ClaraBackend, accounts: Accounts, timezone: str | None = None,
        gif_transport: httpx.AsyncBaseTransport | None = None,  # tests answer the GIF links from it
    ):
        self.bot = bot
        self.api = api
        self.accounts = accounts
        self.timezone = timezone
        self.gif_transport = gif_transport
        self._busy: Counter[int] = Counter()  # channel id -> messages Clara is working on there

    # -- reading the message ---------------------------------------------------------------------- #

    @property
    def me(self) -> discord.ClientUser:
        return self.bot.user  # type: ignore[return-value]

    async def replied_to(self, message: discord.Message) -> discord.Message | None:
        reference = message.reference
        if reference is None or reference.message_id is None:
            return None
        if isinstance(reference.resolved, discord.Message):
            return reference.resolved
        if isinstance(reference.resolved, discord.DeletedReferencedMessage):
            return None
        try:
            return await message.channel.fetch_message(reference.message_id)
        except discord.HTTPException:
            return None

    def addressed(self, message: discord.Message, replied: discord.Message | None) -> bool:
        """Does it mention Clara (or her bot role), or reply to her?"""
        if any(user.id == self.me.id for user in message.mentions):
            return True
        if replied is not None and replied.author.id == self.me.id:
            return True
        guild_me = message.guild.me if message.guild else None
        return guild_me is not None and any(
            role.is_bot_managed() and role in guild_me.roles for role in message.role_mentions
        )

    def text_of(self, message: discord.Message) -> str:
        guild = message.guild
        return readable(
            message.content,
            message.mentions,
            self.me.id,
            self.display_name(guild),
            {role.id: role.name for role in message.role_mentions},
            {channel.id: getattr(channel, "name", "channel") for channel in message.channel_mentions},
        )

    def display_name(self, guild: discord.Guild | None) -> str:
        return guild.me.display_name if guild is not None and guild.me is not None else self.me.display_name

    def lang(self, message: discord.Message) -> str:
        fallback = language(message.guild.preferred_locale) if message.guild else ENGLISH
        return self.accounts.language(message.author.id, fallback)

    # -- what Clara is given ------------------------------------------------------------------------ #

    def roster(self, guild: discord.Guild) -> list[dict[str, str]]:
        """The members of the server who are signed in, in a stable order (the prompt is cached)."""
        members = [guild.get_member(user_id) for user_id in self.accounts.ids]
        return [
            {"user_id": str(member.id), "name": member.display_name}
            for member in sorted((m for m in members if m is not None), key=lambda m: (m.display_name.lower(), m.id))
        ]

    def focus(self, message: discord.Message, replied: discord.Message | None) -> list[int]:
        """The people the message is about: those it mentions, and the author of the message it replies to."""
        people = [user.id for user in message.mentions if not user.bot and user.id != message.author.id]
        if replied is not None and not replied.author.bot and replied.author.id != message.author.id:
            people.append(replied.author.id)
        return list(dict.fromkeys(people))

    def instructions(self, message: discord.Message) -> str:
        style = (
            "Discord shows Markdown. Keep answers as short as a chat message allows (a long answer is split into "
            "several messages)."
        )
        if message.guild is None:
            return f"You are in a private conversation on Discord with {message.author.display_name}. {style}"
        channel = getattr(message.channel, "name", "")
        parent = getattr(getattr(message.channel, "parent", None), "name", "")
        where = f"the thread \"{channel}\" of #{parent}" if parent else f"the channel #{channel}"
        return (
            f"You are on Discord, in the server \"{message.guild.name}\", {where}. To mention one of the people "
            f"listed, write @ followed by their name exactly as listed. {style}"
        )

    def prefix(self, message: discord.Message, replied: discord.Message | None) -> str:
        if replied is None:
            return ""
        quoted = " ".join(self.text_of(replied).split())
        if len(quoted) > QUOTE:
            quoted = quoted[: QUOTE - 1] + "…"
        who = "your message" if replied.author.id == self.me.id else f"{replied.author.display_name}"
        return f"[in reply to {who}: \"{quoted}\"]"

    # -- handling ----------------------------------------------------------------------------------- #

    async def handle(self, message: discord.Message) -> None:
        if message.author.bot or message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        if not self.accounts.loaded:
            await self.accounts.refresh()
        private = message.guild is None
        replied = await self.replied_to(message) if message.reference else None
        text = self.text_of(message)
        action = route(
            Incoming(
                from_bot=message.author.bot,
                is_self=message.author.id == self.me.id,
                private=private,
                addressed=self.addressed(message, replied),
                signed_in=self.accounts.signed_in(message.author.id),
                text=text,
                channel_busy=self._busy[message.channel.id] > 0,
                files=bool(message.attachments),
            )
        )
        if action is Action.IGNORE:
            return
        if action is Action.HINT:
            if self.accounts.should_hint(message.author.id):
                await self.send(message, t(self.lang(message), "need_account"))
            return
        if action is Action.SLASH:
            await self.send(message, t(self.lang(message), "use_slash"))
            return
        await self.talk(message, replied, text, action)

    async def files_of(self, message: discord.Message) -> list[dict]:
        """The files a message carries, for the server to read (attachments.py): name, MIME type and bytes. Those
        are the attachments, and the GIFs of the links Discord's picker sent (gifs.py)."""
        files = []
        for attachment in message.attachments:
            try:
                data = await attachment.read()
            except discord.HTTPException as error:
                log.warning("message %s: cannot download %s: %s", message.id, attachment.filename, error)
                continue
            files.append({
                "name": attachment.filename, "mime": attachment.content_type or "",
                "data": base64.b64encode(data).decode(),
            })
        for link in gif_links(message.content):
            data = await gif_of(link, self.gif_transport)
            if data is not None:
                slug = link.rstrip("/").rsplit("/", 1)[-1][:60]
                files.append({"name": f"{slug}.gif", "mime": "image/gif", "data": base64.b64encode(data).decode()})
        return files

    async def talk(self, message: discord.Message, replied: discord.Message | None, text: str, action: Action) -> None:
        guild = message.guild
        channel_id = message.channel.id
        counted = action is not Action.OBSERVE
        # the files are downloaded only for a message she answers: she does not read the others
        files = await self.files_of(message) if action is Action.ANSWER else []
        if counted:
            self._busy[channel_id] += 1
        try:
            request = self.api.chat(
                message.author.id,
                message.author.display_name,
                text,
                conversation=conversation_of(channel_id, guild is None),
                space=space_of(guild.id) if guild else None,
                roster=self.roster(guild) if guild else None,
                focus=self.focus(message, replied) if guild else None,
                mode=action.value,
                instructions=self.instructions(message),
                prefix=self.prefix(message, replied),
                timezone=self.timezone,
                attachments=files,
            )
            if action is Action.ANSWER:
                async with message.channel.typing():
                    reply = await request
            else:
                reply = await request
        except ClaraError as error:
            await self.failed(message, action, error)
            return
        finally:
            if counted:
                self._busy[channel_id] -= 1
                if self._busy[channel_id] <= 0:
                    del self._busy[channel_id]
        if reply.answered and (reply.text or reply.files):
            members = guild.members if guild is not None else []
            text = add_pings(reply.text, members, self.me.id) if reply.text else ""
            await self.send(message, text, await self.made_files(message, reply.files))

    async def made_files(self, message: discord.Message, files: tuple[dict, ...]) -> list[discord.File]:
        """The markdown files Clara wrote in her answer, as Discord files (their text is read from the server)."""
        found: list[discord.File] = []
        for item in files[:MAX_SENT_FILES]:
            try:
                saved = await self.api.file_of(message.author.id, int(item["id"]))
            except ClaraError as error:
                log.warning("message %s: cannot give the file %s: %s", message.id, item.get("name"), error.detail)
                continue
            found.append(discord.File(io.BytesIO(saved["content"].encode("utf-8")), filename=saved["name"]))
        return found

    async def failed(self, message: discord.Message, action: Action, error: ClaraError) -> None:
        log.warning("message %s (%s): %s %s", message.id, action.value, error.status, error.detail)
        if error.not_signed_in:
            self.accounts.signed_out(message.author.id)
            if action is Action.ANSWER and self.accounts.should_hint(message.author.id):
                await self.send(message, t(self.lang(message), "need_account"))
            return
        if action is not Action.ANSWER:
            return  # nobody asked Clara anything: no need to say it failed
        lang = self.lang(message)
        if error.unreachable:
            text = t(lang, "unreachable")
        elif error.status == 503:
            text = t(lang, "stopping")
        elif error.status == 413:
            text = t(lang, "too_long")
        elif error.over_limit:
            text = error.detail
        elif error.status == 429:
            text = t(lang, "busy")
        else:
            text = t(lang, "failed", detail=error.detail[:200])
        await self.send(message, text)

    async def send(self, message: discord.Message, text: str, files: list[discord.File] | None = None) -> None:
        """Reply, in several messages when it is longer than Discord allows; the files come with the first."""
        first, *rest = split_message(text) or ["…" if not files else ""]
        options = {"files": files} if files else {}
        try:
            await message.reply(first or None, mention_author=False, **options)
            for chunk in rest:
                await message.channel.send(chunk)
        except discord.HTTPException as error:
            log.warning("cannot answer in channel %s: %s", message.channel.id, error)
