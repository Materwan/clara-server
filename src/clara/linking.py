"""Proof that a client controls an account before it is attached to another person.

Without it, any chat client could name `discord:victim` and pull the victim's memories
into an account it holds. The account being attached asks for a short-lived code
(`issue`); whoever knows that code may attach this account (`redeem`), once.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable

CODE_LIFETIME = 600.0  # seconds


class LinkCodes:
    def __init__(self, lifetime: float = CODE_LIFETIME, clock: Callable[[], float] = time.monotonic):
        self.lifetime = lifetime
        self._clock = clock
        self._codes: dict[tuple[str, str], tuple[str, float]] = {}  # account -> (code, expires)

    def issue(self, surface: str, external_id: str) -> str:
        """A new code for the account (it replaces the previous one)."""
        self._forget_expired()
        code = secrets.token_urlsafe(9)
        self._codes[(surface, external_id)] = (code, self._clock() + self.lifetime)
        return code

    def redeem(self, surface: str, external_id: str, code: str) -> bool:
        """True, once, if `code` is the live code of the account."""
        self._forget_expired()
        entry = self._codes.get((surface, external_id))
        if entry is None or not secrets.compare_digest(entry[0].encode(), code.encode()):
            return False
        del self._codes[(surface, external_id)]
        return True

    def _forget_expired(self) -> None:
        now = self._clock()
        for account in [a for a, (_, expires) in self._codes.items() if expires <= now]:
            del self._codes[account]
