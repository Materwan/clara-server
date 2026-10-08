"""What each person's answers cost in tokens, for the administration.

One row per answer (all the model rounds of a turn added up), plus one per compaction and per title, with who asked,
on which surface, which model answered and how many tokens went in and out. It is a record only: the daily credit
limit (limits.py) is counted apart and does not read it. Rows are kept until their person is erased.

Answers run on a person's own API key (userkeys.py) are rows with `own_key` set: they cost no credits, and the
views below leave them out unless asked for them (`own_key=True`), so that the figures of the server and the figures
of a person's own key never add up together.

Discord is told apart from every other surface everywhere (`discord` / `other`), because the Discord bot is the
one that spends most and is shared by many people.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from .memory import Memory

log = logging.getLogger("clara.usage")

DISCORD = "discord"
KINDS = ("message", "scheduled", "compaction", "title")
ANSWER_KINDS = ("message", "scheduled")  # the rows that are an answer to somebody (the others are upkeep)
MAX_PAGE = 200
TOP_MODELS = 3


def _now() -> datetime:
    return datetime.now(UTC)


def group_of(surface: str) -> str:
    return "discord" if surface == DISCORD else "other"


def _zero() -> dict:
    return {"answers": 0, "prompt_tokens": 0, "completion_tokens": 0}


class UsageLog:
    def __init__(self, memory: Memory, clock: Callable[[], datetime] = _now):
        self._memory = memory
        self._clock = clock

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def record(
        self, person_id: int | None, kind: str, surface: str, conversation: str, model_ref: str, model: str,
        provider: str, prompt_tokens: int, completion_tokens: int, credits: int = 0, rounds: int = 1,
        estimated: bool = False, own_key: bool = False,
    ) -> None:
        """Keep one row. Never raises: counting must not break an answer."""
        if prompt_tokens <= 0 and completion_tokens <= 0:
            return
        try:
            with self._memory.lock, self._memory.database as db:
                db.execute(
                    "INSERT INTO usage_log (at, person_id, kind, surface, conversation, model_ref, model, provider,"
                    " prompt_tokens, completion_tokens, credits, rounds, estimated, own_key)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._clock().isoformat(timespec="seconds"), person_id, kind, surface, conversation,
                        model_ref, model, provider, max(0, prompt_tokens), max(0, completion_tokens), max(0, credits),
                        max(1, rounds), int(estimated), int(own_key),
                    ),
                )
        except Exception:
            log.exception("could not log %d+%d tokens of person %s", prompt_tokens, completion_tokens, person_id)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def _since(self, days: int) -> str:
        return (self._clock() - timedelta(days=days)).isoformat(timespec="seconds") if days > 0 else ""

    def per_user(self, days: int = 0, person_id: int | None = None, own_key: bool = False) -> list[dict]:
        """Everybody who used Clara (in the last `days` days; 0: ever; `person_id`: only them), most tokens first:
        tokens in and out,
        Discord apart from the other surfaces, each surface, and the models they use most."""
        with self._memory.lock:
            rows = self._memory.database.execute(
                "SELECT l.person_id, p.name AS person,"
                " (SELECT name FROM users WHERE person_id = l.person_id ORDER BY name LIMIT 1) AS user,"
                " EXISTS (SELECT 1 FROM users WHERE person_id = l.person_id AND is_admin = 1) AS is_admin,"
                " l.surface, l.model_ref, l.model, l.provider, l.kind,"
                " COUNT(*) AS rows, SUM(l.prompt_tokens) AS prompt, SUM(l.completion_tokens) AS completion,"
                " SUM(l.credits) AS credits, MIN(l.at) AS first_at, MAX(l.at) AS last_at"
                " FROM usage_log l JOIN people p ON p.id = l.person_id"
                " WHERE l.at >= ? AND (? IS NULL OR l.person_id = ?) AND l.own_key = ?"
                " GROUP BY l.person_id, l.surface, l.model_ref, l.model, l.provider, l.kind",
                (self._since(days), person_id, person_id, int(own_key)),
            ).fetchall()
        people: dict[int, dict] = {}
        models: dict[int, dict[str, dict]] = {}
        for row in rows:
            entry = people.setdefault(row["person_id"], {
                "person": {"id": row["person_id"], "name": row["person"]}, "user": row["user"],
                "is_admin": bool(row["is_admin"]), "prompt_tokens": 0, "completion_tokens": 0, "credits": 0,
                "answers": 0, "discord": _zero(), "other": _zero(), "surfaces": {}, "first_at": row["first_at"],
                "last_at": row["last_at"],
            })
            entry["first_at"] = min(entry["first_at"], row["first_at"])
            entry["last_at"] = max(entry["last_at"], row["last_at"])
            entry["prompt_tokens"] += row["prompt"]
            entry["completion_tokens"] += row["completion"]
            entry["credits"] += row["credits"]
            answers = row["rows"] if row["kind"] in ANSWER_KINDS else 0
            entry["answers"] += answers
            for bucket in (entry[group_of(row["surface"])], entry["surfaces"].setdefault(row["surface"], _zero())):
                bucket["answers"] += answers
                bucket["prompt_tokens"] += row["prompt"]
                bucket["completion_tokens"] += row["completion"]
            if answers:
                name = row["model_ref"] or row["model"]
                best = models.setdefault(row["person_id"], {}).setdefault(
                    name, {"model": name, "provider": row["provider"], "answers": 0, "prompt_tokens": 0,
                           "completion_tokens": 0},
                )
                best["answers"] += answers
                best["prompt_tokens"] += row["prompt"]
                best["completion_tokens"] += row["completion"]
        for person_id, entry in people.items():
            ranked = sorted(models.get(person_id, {}).values(), key=lambda m: (-m["answers"], m["model"]))
            entry["models"] = ranked[:TOP_MODELS]
        return sorted(
            people.values(),
            key=lambda e: (-(e["prompt_tokens"] + e["completion_tokens"]), e["person"]["name"].lower()),
        )

    def by_provider(self, person_id: int, days: int = 0, own_key: bool = False) -> dict[str, dict]:
        """One person's tokens per provider (`{provider: {answers, prompt_tokens, completion_tokens}}`)."""
        with self._memory.lock:
            rows = self._memory.database.execute(
                "SELECT provider, SUM(kind IN ('message', 'scheduled')) AS answers, SUM(prompt_tokens) AS prompt,"
                " SUM(completion_tokens) AS completion FROM usage_log"
                " WHERE person_id = ? AND at >= ? AND own_key = ? GROUP BY provider",
                (person_id, self._since(days), int(own_key)),
            ).fetchall()
        return {
            row["provider"]: {
                "answers": row["answers"] or 0, "prompt_tokens": row["prompt"] or 0,
                "completion_tokens": row["completion"] or 0,
            }
            for row in rows
        }

    def totals(self, days: int = 0, own_key: bool = False) -> dict:
        """The same sums for the whole server, Discord apart."""
        with self._memory.lock:
            rows = self._memory.database.execute(
                "SELECT surface = ? AS discord, SUM(kind IN ('message', 'scheduled')) AS answers,"
                " SUM(prompt_tokens) AS prompt, SUM(completion_tokens) AS completion FROM usage_log"
                " WHERE at >= ? AND own_key = ? GROUP BY discord",
                (DISCORD, self._since(days), int(own_key)),
            ).fetchall()
        out = {"discord": _zero(), "other": _zero()}
        for row in rows:
            out["discord" if row["discord"] else "other"] = {
                "answers": row["answers"] or 0, "prompt_tokens": row["prompt"] or 0,
                "completion_tokens": row["completion"] or 0,
            }
        return out

    def history(
        self, person_id: int | None = None, group: str = "", surface: str = "", kind: str = "", model: str = "",
        days: int = 0, before: int = 0, limit: int = 50, own_key: bool = False,
    ) -> dict:
        """The log, newest first: `{calls, next, totals}`. `group`: `discord` or `other`; `before`: the id the
        last page ended at (`next` of the previous one); `totals` add up everything the filters keep."""
        limit = max(1, min(limit, MAX_PAGE))
        where, args = ["l.at >= ?", "l.own_key = ?"], [self._since(days), int(own_key)]
        if person_id is not None:
            where.append("l.person_id = ?")
            args.append(person_id)
        if group == "discord":
            where.append("l.surface = ?")
            args.append(DISCORD)
        elif group == "other":
            where.append("l.surface <> ?")
            args.append(DISCORD)
        if surface:
            where.append("l.surface = ?")
            args.append(surface)
        if kind:
            where.append("l.kind = ?")
            args.append(kind)
        if model:
            where.append("(l.model_ref = ? OR l.model = ?)")
            args += [model, model]
        condition = " AND ".join(where)
        with self._memory.lock:
            db = self._memory.database
            total = db.execute(
                f"SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens), 0) AS prompt,"
                f" COALESCE(SUM(completion_tokens), 0) AS completion FROM usage_log l WHERE {condition}",
                args,
            ).fetchone()
            page_where, page_args = (condition + " AND l.id < ?", [*args, before]) if before > 0 else (condition, args)
            rows = db.execute(
                "SELECT l.*, p.name AS person,"
                " (SELECT name FROM users WHERE person_id = l.person_id ORDER BY name LIMIT 1) AS user"
                " FROM usage_log l LEFT JOIN people p ON p.id = l.person_id"
                f" WHERE {page_where} ORDER BY l.id DESC LIMIT ?",
                [*page_args, limit + 1],
            ).fetchall()
        more = len(rows) > limit
        calls = [{
            "id": row["id"], "at": row["at"], "kind": row["kind"], "surface": row["surface"],
            "group": group_of(row["surface"]), "conversation": row["conversation"],
            "person": {"id": row["person_id"], "name": row["person"]} if row["person_id"] is not None else None,
            "user": row["user"], "model_ref": row["model_ref"], "model": row["model"], "provider": row["provider"],
            "prompt_tokens": row["prompt_tokens"], "completion_tokens": row["completion_tokens"],
            "credits": row["credits"], "rounds": row["rounds"], "estimated": bool(row["estimated"]),
            "own_key": bool(row["own_key"]),
        } for row in rows[:limit]]
        return {
            "calls": calls, "next": calls[-1]["id"] if more and calls else None,
            "totals": {"calls": total["calls"], "prompt_tokens": total["prompt"], "completion_tokens": total["completion"]},
        }
