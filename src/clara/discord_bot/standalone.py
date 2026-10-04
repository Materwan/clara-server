"""The bot on its own, talking to a Clara server over HTTP: `clara-discord` (the bot-discord project starts this).

Use it to run the bot on another machine than the server. Do not run it and the server's built-in bot with the same
Discord token at the same time: both would answer.

    DISCORD_BOT_TOKEN   the bot's token
    CLARA_URL           the server (default http://127.0.0.1:8765)
    CLARA_TOKEN         one of the server's client tokens (CLARA_TOKENS=discord:<token>)
    CLARA_TIMEZONE      optional: the clock Clara is told (IANA name)
    CLARA_DISCORD_DATA_DIR  where the log goes (default: data)
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class SettingsError(Exception):
    """The configuration cannot work; the text says how to fix it."""


@dataclass(frozen=True)
class Settings:
    discord_token: str = field(repr=False)
    clara_url: str
    clara_token: str = field(repr=False)
    timezone: str | None = None
    data_dir: Path = Path("data")

    @property
    def log_file(self) -> Path:
        return self.data_dir / "logs" / "clara-discord.log"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        if env is None:
            load_dotenv()
            env = os.environ

        def text(key: str, default: str = "") -> str:
            return env.get(key, "").strip() or default

        discord_token = text("DISCORD_BOT_TOKEN")
        if not discord_token:
            raise SettingsError("DISCORD_BOT_TOKEN is missing: put the bot's token in .env")
        clara_token = text("CLARA_TOKEN")
        if not clara_token:
            raise SettingsError(
                "CLARA_TOKEN is missing: give the bot one of the server's client tokens (CLARA_TOKENS=discord:<token> "
                "on the server, CLARA_TOKEN=<token> here)"
            )
        timezone = text("CLARA_TIMEZONE") or None
        if timezone:
            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise SettingsError(f"CLARA_TIMEZONE: unknown timezone {timezone!r} (e.g. Europe/Paris)") from None
        return cls(
            discord_token=discord_token,
            clara_url=text("CLARA_URL", "http://127.0.0.1:8765").rstrip("/"),
            clara_token=clara_token,
            timezone=timezone,
            data_dir=Path(text("CLARA_DISCORD_DATA_DIR", "data")),
        )


def configure_logging(settings: Settings) -> None:
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    file = RotatingFileHandler(settings.log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), file],
        force=True,
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def main(settings: Settings) -> None:
    from .bot import ClaraBot
    from .remote import RemoteBackend

    api = RemoteBackend(settings.clara_url, settings.clara_token)
    bot = ClaraBot(api, settings.timezone)
    try:
        async with bot:
            await bot.start(settings.discord_token)
    finally:
        await api.close()


def run() -> None:
    try:
        settings = Settings.from_env()
    except SettingsError as error:
        raise SystemExit(str(error)) from None
    try:
        import discord  # noqa: F401
    except ImportError:
        raise SystemExit("discord.py is not installed: pip install clara-server[discord]") from None
    configure_logging(settings)
    try:
        asyncio.run(main(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
