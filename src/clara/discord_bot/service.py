"""The Discord bot inside clara-server: started with the server when AUTO_START_DISCORD_BOT is on, and started or
stopped by an administrator (`/discord` in the console, the web site's Discord page).

It runs on the server's own event loop and reaches Clara through LocalBackend. discord.py is optional
(`pip install clara-server[discord]`): without it this module still loads and says so.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import logging
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

# What the bot needs on a server: view channels, send messages (and in threads), embed links, read the history
INVITE_PERMISSIONS = 1024 + 2048 + 16384 + 65536 + 274877906944
INVITE_URL = "https://discord.com/oauth2/authorize?client_id={id}&scope=bot+applications.commands&permissions={perms}"
CONNECT_TIMEOUT = 60.0  # seconds a start waits for Discord before saying it is still connecting


def discord_installed() -> bool:
    return importlib.util.find_spec("discord") is not None


def application_id(token: str) -> str | None:
    """The bot's id, written (base64) in the first part of its token: enough for an invite link while it is off."""
    head = token.split(".", 1)[0]
    try:
        decoded = base64.b64decode(head + "=" * (-len(head) % 4), validate=False).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded if decoded.isdigit() else None


class DiscordService:
    def __init__(
        self,
        token: str | None,
        backend_factory: Callable[[], Any],
        invite_url: str = "",
        auto_start: bool = False,
        bot_factory: Callable[[Any], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.token = token
        self.invite_setting = invite_url
        self.auto_start = auto_start
        self._backend_factory = backend_factory
        self.bot_factory = bot_factory  # tests give a fake; else ClaraBot
        self._clock = clock
        self._bot: Any = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.started_at: float | None = None
        self.last_error = ""

    # -- state ---------------------------------------------------------------------------------------- #

    @property
    def available(self) -> bool:
        return self.bot_factory is not None or discord_installed()

    @property
    def state(self) -> str:
        """unavailable (no discord.py), no-token, stopped, starting, running, error."""
        if not self.available:
            return "unavailable"
        if not self.token:
            return "no-token"
        if self._task is None:
            return "error" if self.last_error else "stopped"
        if self._task.done():
            return "error" if self.last_error else "stopped"
        return "running" if self._bot is not None and self._bot.is_ready() else "starting"

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def invite_url(self) -> str:
        if self.invite_setting:
            return self.invite_setting
        user = getattr(self._bot, "user", None) if self._bot is not None else None
        bot_id = str(user.id) if user is not None else (application_id(self.token) if self.token else None)
        return INVITE_URL.format(id=bot_id, perms=INVITE_PERMISSIONS) if bot_id else ""

    def status(self) -> dict:
        bot = self._bot if self.running else None
        ready = bot is not None and bot.is_ready()
        user = bot.user if ready else None
        latency = getattr(bot, "latency", None) if ready else None
        return {
            "state": self.state,
            "available": self.available,
            "token_set": bool(self.token),
            "auto_start": self.auto_start,
            "user": str(user) if user else None,
            "user_id": str(user.id) if user else None,
            "guilds": len(bot.guilds) if ready else 0,
            "latency_ms": round(latency * 1000) if latency is not None and latency == latency else None,  # NaN early
            "uptime_seconds": int(self._clock() - self.started_at) if self.started_at is not None and self.running else None,
            "last_error": self.last_error,
            "invite_url": self.invite_url(),
        }

    def describe(self) -> str:
        """One line for the console."""
        state = self.state
        if state == "unavailable":
            return "Discord bot: discord.py is not installed (pip install clara-server[discord])."
        if state == "no-token":
            return "Discord bot: no DISCORD_BOT_TOKEN in the server's .env."
        found = self.status()
        if state == "running":
            minutes = (found["uptime_seconds"] or 0) // 60
            return (f"Discord bot: running as {found['user']}, in {found['guilds']} server(s), "
                    f"{found['latency_ms']} ms, up {minutes} min.")
        if state == "starting":
            return "Discord bot: connecting to Discord…"
        if state == "error":
            return f"Discord bot: stopped after an error: {self.last_error}"
        return "Discord bot: stopped."

    def discord_name(self, user_id: str) -> str | None:
        """The Discord name of an account, if the running bot knows it."""
        if not self.running or not user_id.isdigit():
            return None
        user = self._bot.get_user(int(user_id))
        return str(user) if user is not None else None

    def signed_out(self, user_id: str) -> None:
        """An administrator signed the account out: the running bot forgets it now, not at its next refresh."""
        if self.running and user_id.isdigit():
            self._bot.accounts.signed_out(int(user_id))

    def signed_in(self, user_id: str, user: str) -> None:
        """An administrator signed the account in: the running bot answers it now, not after its next refresh."""
        if self.running and user_id.isdigit():
            self._bot.accounts.signed_in_as(int(user_id), user)

    def members(self, query: str = "", limit: int = 20) -> list[dict]:
        """The people (not bots) in the servers of the running bot whose name contains `query`, or whose id
        starts with it, ignoring case: `[{user_id, name, display_name}]`."""
        bot = self._bot if self.running else None
        if bot is None or not bot.is_ready():
            return []
        query = query.strip().lower().removeprefix("@")
        found: dict[int, dict] = {}
        for guild in bot.guilds:
            for member in guild.members:
                if member.bot or member.id in found:
                    continue
                names = (member.name, member.display_name, getattr(member, "global_name", None) or "")
                if query and not str(member.id).startswith(query) and not any(query in n.lower() for n in names):
                    continue
                found[member.id] = {"user_id": str(member.id), "name": member.name, "display_name": member.display_name}
        return sorted(found.values(), key=lambda m: (m["display_name"].lower(), m["user_id"]))[:limit]

    # -- start and stop ------------------------------------------------------------------------------- #

    async def start(self, wait: float = CONNECT_TIMEOUT) -> str:
        """Start the bot and wait (up to `wait` seconds) for it to be connected. Returns what happened."""
        async with self._lock:
            if self.state in ("unavailable", "no-token"):
                return self.describe()
            if self.running:
                return "Discord bot: already running."
            if self.bot_factory is not None:
                bot = self.bot_factory(self._backend_factory())
            else:
                from .bot import ClaraBot

                bot = ClaraBot(self._backend_factory())
            self._bot = bot
            self.last_error = ""
            self.started_at = self._clock()
            self._task = asyncio.create_task(self._run(bot))
        deadline = self._clock() + wait
        while self.running and not bot.is_ready() and self._clock() < deadline:
            await asyncio.sleep(0.2)
        log.info(self.describe())
        return self.describe()

    async def _run(self, bot: Any) -> None:
        try:
            await bot.start(self.token)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # a wrong token, intents not enabled, the network...
            self.last_error = _explain(error)
            log.error("Discord bot stopped: %s", self.last_error)
        finally:
            with contextlib.suppress(Exception):
                if not bot.is_closed():
                    await bot.close()
            with contextlib.suppress(Exception):
                await bot.api.close()

    async def stop(self) -> str:
        async with self._lock:
            if not self.running:
                return "Discord bot: not running."
            bot, task = self._bot, self._task
            with contextlib.suppress(Exception):
                await bot.close()
            try:
                await asyncio.wait_for(asyncio.shield(task), 15)
            except asyncio.TimeoutError:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            self.last_error = ""
            log.info("Discord bot stopped")
            return "Discord bot: stopped."

    async def restart(self) -> str:
        await self.stop()
        return await self.start()


def _explain(error: Exception) -> str:
    name = type(error).__name__
    if name == "LoginFailure":
        return "Discord refused the token (DISCORD_BOT_TOKEN): check it in the developer portal."
    if name == "PrivilegedIntentsRequired":
        return ("The Message Content and Server Members intents are not enabled: turn them on in the developer "
                "portal (Bot page).")
    return f"{name}: {error}"[:300]
