"""A daily limit of credits for each person.

What is counted is what the model reports for every round of an answer (the prompt it was given and what it
wrote), multiplied by the weight of the model that answered (models.py: a big model costs more credits per
token than a small one; a model of weight 1 costs one credit per token), added up per person and per calendar
day (UTC). The day starts again at midnight UTC.

Who may use how much:

* an administrator (a user flagged administrator) has no limit, whatever is set;
* another user has the limit an administrator set for them, or the server's default when none is set
  (`CLARA_DEFAULT_DAILY_TOKENS`, changed at run time with `/limit default`); the numbers are credits, whatever
  the names of the variable and of the database columns still say;
* a person with no user (a terminal or an app that signs in with a client token) follows the default;
* 0 means no limit, wherever it is written.

The limit is checked when an answer starts: the answer that crosses it is finished, the next one is refused.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

from .memory import Memory

DEFAULT_OPTION = "usage.default_daily_tokens"
KEEP_DAYS = 400  # days of usage kept
LIMIT_MARK = "credits for today"  # in the text of a refusal: the clients that only have the text look for it


class UsageLimitReached(Exception):
    """The person used all their credits for today; the text says when they have some again."""

    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after  # seconds until the next day


@dataclass(frozen=True)
class Quota:
    used: int  # credits used today
    limit: int | None  # credits a day; None: no limit
    resets_at: str  # when today ends (UTC, ISO 8601)

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.used)

    @property
    def reached(self) -> bool:
        return self.limit is not None and self.used >= self.limit

    def describe(self) -> dict:
        return {"used": self.used, "limit": self.limit, "remaining": self.remaining, "resets_at": self.resets_at}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_limit(text: str) -> int:
    """A limit as an operator writes it: `500000`, `500k`, `2m`, or `off`/`none`/`unlimited` (0)."""
    word = text.strip().lower().replace("_", "").replace(",", "")
    if word in ("off", "none", "no", "unlimited", "infinite", "0"):
        return 0
    factor = 1
    if word.endswith("k"):
        word, factor = word[:-1], 1_000
    elif word.endswith("m"):
        word, factor = word[:-1], 1_000_000
    try:
        number = Decimal(word) * factor
    except InvalidOperation:
        number = Decimal(0)
    if not number.is_finite() or number < 1 or number != number.to_integral_value() or number > 10**13:
        raise ValueError(f"{text!r} is not a number of credits (e.g. 500000, 500k, 2m, or off).")
    return int(number)


def show_limit(limit: int | None) -> str:
    return "no limit" if not limit else f"{limit:,} credits a day"


def credits_for(tokens: int, weight: float = 1.0) -> int:
    """What `tokens` cost on a model of this weight: at least one credit for any use at all."""
    if tokens <= 0:
        return 0
    return max(1, math.ceil(tokens * max(weight, 0.0)))


class UsageLimits:
    def __init__(self, memory: Memory, default: int = 0, clock: Callable[[], datetime] = _now):
        self._memory = memory
        self._initial_default = max(0, default)  # what CLARA_DEFAULT_DAILY_TOKENS says; `/limit default` overrides it
        self._clock = clock
        self._pruned_on = ""

    # ------------------------------------------------------------------
    # Limits
    # ------------------------------------------------------------------
    def default(self) -> int:
        """Credits a day for those with no limit of their own (0: no limit)."""
        stored = self._memory.option(DEFAULT_OPTION)
        try:
            return max(0, int(stored)) if stored is not None else self._initial_default
        except ValueError:
            return self._initial_default

    def set_default(self, tokens: int) -> None:
        if tokens < 0:
            raise ValueError("A limit is a number of credits (0: no limit).")
        self._memory.set_option(DEFAULT_OPTION, str(tokens))

    def limit_of(self, person_id: int) -> int | None:
        """Credits this person may use a day, None when they have no limit."""
        with self._memory.lock:
            rows = self._memory.database.execute(
                "SELECT is_admin, disabled, daily_token_limit FROM users WHERE person_id = ? ORDER BY name",
                (person_id,),
            ).fetchall()
        if any(row["is_admin"] and not row["disabled"] for row in rows):
            return None
        own = next((row["daily_token_limit"] for row in rows if row["daily_token_limit"] is not None), None)
        limit = self.default() if own is None else own
        return limit or None

    def limit_for_user(self, is_admin: bool, own: int | None) -> int | None:
        """The same, for a user already read (the administration's list)."""
        if is_admin:
            return None
        return (self.default() if own is None else own) or None

    # ------------------------------------------------------------------
    # Usage
    # ------------------------------------------------------------------
    def _today(self) -> str:
        return self._clock().date().isoformat()

    def _tomorrow(self) -> datetime:
        now = self._clock()
        return datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1)

    def used(self, person_id: int, day: str | None = None) -> int:
        with self._memory.lock:
            row = self._memory.database.execute(
                "SELECT tokens FROM usage WHERE person_id = ? AND day = ?", (person_id, day or self._today())
            ).fetchone()
        return row["tokens"] if row else 0

    def quota(self, person_id: int) -> Quota:
        return Quota(self.used(person_id), self.limit_of(person_id), self._tomorrow().isoformat(timespec="seconds"))

    def quota_for_user(self, person_id: int, is_admin: bool, own: int | None) -> Quota:
        return Quota(
            self.used(person_id), self.limit_for_user(is_admin, own), self._tomorrow().isoformat(timespec="seconds")
        )

    def record(self, person_id: int, tokens: int, weight: float = 1.0) -> int:
        """Count what `tokens` of a model of this `weight` cost this person, today. Returns the credits."""
        credits = credits_for(tokens, weight)
        if credits <= 0:
            return 0
        today = self._today()
        with self._memory.lock, self._memory.database as db:
            db.execute(
                "INSERT INTO usage (person_id, day, tokens) VALUES (?, ?, ?)"
                " ON CONFLICT (person_id, day) DO UPDATE SET tokens = usage.tokens + excluded.tokens",
                (person_id, today, credits),
            )
            if self._pruned_on != today:  # once a day is enough
                oldest = (self._clock() - timedelta(days=KEEP_DAYS)).date().isoformat()
                db.execute("DELETE FROM usage WHERE day < ?", (oldest,))
                self._pruned_on = today
        return credits

    def check(self, person_id: int) -> Quota:
        """The person's quota, or UsageLimitReached when they have used it all."""
        quota = self.quota(person_id)
        if quota.reached:
            wait = max(1, int((self._tomorrow() - self._clock()).total_seconds()))
            hours, minutes = divmod(wait // 60, 60)
            when = f"{hours} h {minutes:02d} min" if hours else f"{max(1, minutes)} min"
            raise UsageLimitReached(
                f"You have used your {quota.limit:,} {LIMIT_MARK}. You can talk to Clara again in {when} "
                "(the day starts again at midnight UTC), or ask an administrator to raise your limit.",
                wait,
            )
        return quota
