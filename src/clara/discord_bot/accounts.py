"""Who is signed in, as far as the bot knows, and the language each person uses.

The server is the authority (it refuses a message from an account that is not signed in); this copy saves a
request per message and tells the bot which members to list to Clara. It is filled at start, updated by the
bot's own /register, /login and /logout, and read again every few minutes (an administrator may have signed
someone out, or disabled them, from the web site).
"""

from __future__ import annotations

import logging
import time

from .backend import ClaraBackend, ClaraError
from .texts import ENGLISH

log = logging.getLogger(__name__)

REFRESH_SECONDS = 300
HINT_EVERY = 600.0  # seconds between two "create an account" hints to the same person


class Accounts:
    def __init__(self, api: ClaraBackend, clock=time.monotonic):
        self._api = api
        self._clock = clock
        self._users: dict[int, str] = {}  # Discord id -> Clara user name
        self._languages: dict[int, str] = {}
        self._hinted: dict[int, float] = {}
        self.loaded = False

    async def refresh(self) -> None:
        try:
            self._users = await self._api.signed_in()
            self.loaded = True
        except ClaraError as error:
            log.warning("cannot read the signed-in accounts: %s", error.detail)

    def signed_in(self, user_id: int) -> bool:
        return user_id in self._users

    def user_of(self, user_id: int) -> str | None:
        return self._users.get(user_id)

    @property
    def ids(self) -> frozenset[int]:
        return frozenset(self._users)

    def signed_in_as(self, user_id: int, user: str) -> None:
        self._users[user_id] = user
        self._hinted.pop(user_id, None)

    def signed_out(self, user_id: int) -> None:
        self._users.pop(user_id, None)

    # -- language and hints ----------------------------------------------------------------------- #

    def remember_language(self, user_id: int, lang: str) -> None:
        self._languages[user_id] = lang

    def language(self, user_id: int, fallback: str = ENGLISH) -> str:
        return self._languages.get(user_id, fallback)

    def should_hint(self, user_id: int) -> bool:
        """Say "create an account" to this person now? Not more than once every HINT_EVERY seconds."""
        now = self._clock()
        last = self._hinted.get(user_id)
        if last is not None and now - last < HINT_EVERY:
            return False
        self._hinted[user_id] = now
        return True
