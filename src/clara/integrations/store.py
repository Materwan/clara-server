"""The tables of the integrations (they live in the memory's SQLite file, like projects.py's): the accounts a person
connected, the resources they added (a repository, a Drive folder, a folder), what is attached to which project or
conversation, the requests for permission, the log of what Clara did, and the jobs waiting for the desktop app.

The administrator's settings are one JSON document in the `options` table.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..memory import Memory
from .permissions import KINDS, TYPES, clean

POLICY_OPTION = "integrations_policy"
MAX_LABEL = 200
PENDING, APPROVED, DENIED, EXPIRED, DONE, FAILED = "pending", "approved", "denied", "expired", "done", "failed"
OPEN_STATES = (PENDING, APPROVED)  # not finished: the person may still answer, or the action is running


class StoreError(ValueError):
    """A request about the integrations that cannot be done: the message says why."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json(text: str | None) -> Any:
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {}


def args_hash(op: str, resource_id: int, args: dict) -> str:
    return hashlib.sha256(json.dumps([op, resource_id, args], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class Account:
    id: int
    person_id: int
    kind: str  # "github" or "gdrive"
    label: str
    status: str  # "ok" or "needs_reconnect"
    levels: dict[str, str]
    created_at: str
    secret: str = field(default="", repr=False)  # sealed (vault.py)


@dataclass(frozen=True)
class Resource:
    id: int
    person_id: int
    account_id: int | None
    kind: str
    label: str
    locator: dict
    levels: dict[str, str]
    created_at: str

    @property
    def type(self) -> str:
        return KINDS[self.kind]


@dataclass(frozen=True)
class Attached:
    """A resource as one conversation sees it: through its own attachment, or through its project's."""

    resource: Resource
    attachment_id: int
    scope: str  # "conversation" or "project"
    levels: dict[str, str]  # the attachment's own permissions


@dataclass(frozen=True)
class Approval:
    id: int
    person_id: int
    conversation: str
    resource_id: int | None
    op: str
    level: str
    args: dict
    summary: str
    reason: str
    status: str
    result: str
    created_at: str
    notified_at: str | None
    decided_at: str | None
    decided_on: str
    told: bool
    surface: str = ""  # where the conversation is, and as which account
    user_id: str = ""


@dataclass(frozen=True)
class LogEntry:
    id: int
    person_id: int | None
    conversation: str
    resource: str
    op: str
    level: str
    summary: str
    outcome: str
    approval_id: int | None
    at: str


@dataclass(frozen=True)
class Job:
    id: int
    person_id: int
    device: str
    op: str
    args: dict
    status: str
    result: str
    created_at: str


class IntegrationStore:
    def __init__(self, memory: Memory):
        self.memory = memory
        self._db = memory.database
        self._lock = memory.lock

    # ------------------------------------------------------------------
    # The administrator's settings
    # ------------------------------------------------------------------
    def policy(self) -> dict:
        """`{"enabled": {type: bool}, "disabled_users": {type: [person ids]}, "roots": [paths],
        "ceiling": {type: {level: decision}}}`, with what was never set filled in."""
        stored = _json(self.memory.option(POLICY_OPTION))
        stored = stored if isinstance(stored, dict) else {}
        return {
            "enabled": {t: bool(stored.get("enabled", {}).get(t, t in ("github", "gdrive", "computer"))) for t in TYPES},
            "disabled_users": {t: list(stored.get("disabled_users", {}).get(t, [])) for t in TYPES},
            "roots": [str(root) for root in stored.get("roots", [])],
            "ceiling": {t: clean(stored.get("ceiling", {}).get(t), strict=False) for t in TYPES},
        }

    def set_policy(self, policy: dict) -> dict:
        self.memory.set_option(POLICY_OPTION, json.dumps(policy, ensure_ascii=False))
        return self.policy()

    def opt_out(self, person_id: int) -> None:
        """Switch every kind of integration off for this person (an administrator switches them on, one by one,
        in the administration page). For the people who made their own user: they were not vetted by anybody."""
        policy = self.policy()
        for kind in TYPES:
            if person_id not in policy["disabled_users"][kind]:
                policy["disabled_users"][kind] = sorted({*policy["disabled_users"][kind], person_id})
        self.set_policy(policy)

    def type_enabled(self, kind_type: str, person_id: int) -> bool:
        policy = self.policy()
        return policy["enabled"].get(kind_type, False) and person_id not in policy["disabled_users"].get(kind_type, [])

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------
    @staticmethod
    def _account(row) -> Account:
        return Account(
            row["id"], row["person_id"], row["kind"], row["label"], row["status"], clean(row["levels"], strict=False),
            row["created_at"], row["secret"],
        )

    def add_account(self, person_id: int, kind: str, label: str, sealed: str, levels: dict | None = None) -> Account:
        if kind not in ("github", "gdrive"):
            raise StoreError(f"No such kind of account: {kind}.")
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO integration_accounts (person_id, kind, label, secret, levels, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (person_id, kind, label[:MAX_LABEL], sealed, json.dumps(clean(levels)), _now()),
            )
        return self.account(cursor.lastrowid)  # type: ignore[return-value]

    def account(self, account_id: int) -> Account | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM integration_accounts WHERE id = ?", (account_id,)).fetchone()
        return self._account(row) if row else None

    def accounts_of(self, person_id: int) -> list[Account]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM integration_accounts WHERE person_id = ? ORDER BY id", (person_id,)
            ).fetchall()
        return [self._account(row) for row in rows]

    def update_account(
        self, account_id: int, label: str | None = None, sealed: str | None = None, status: str | None = None,
        levels: dict | None = None,
    ) -> Account:
        changes = {}
        if label is not None:
            changes["label"] = label[:MAX_LABEL]
        if sealed is not None:
            changes["secret"] = sealed
        if status is not None:
            changes["status"] = status
        if levels is not None:
            changes["levels"] = json.dumps(clean(levels))
        with self._lock, self._db:
            for key, value in changes.items():  # the keys are ours
                self._db.execute(f"UPDATE integration_accounts SET {key} = ? WHERE id = ?", (value, account_id))
        account = self.account(account_id)
        if account is None:
            raise StoreError("No such account.")
        return account

    def delete_account(self, account_id: int) -> int:
        """Disconnect: its resources go too (and their attachments). Returns how many resources there were."""
        with self._lock, self._db:
            count = self._db.execute(
                "SELECT COUNT(*) FROM integration_resources WHERE account_id = ?", (account_id,)
            ).fetchone()[0]
            self._db.execute("DELETE FROM integration_accounts WHERE id = ?", (account_id,))
        return count

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------
    @staticmethod
    def _resource(row) -> Resource:
        return Resource(
            row["id"], row["person_id"], row["account_id"], row["kind"], row["label"], _json(row["locator"]),
            clean(row["levels"], strict=False), row["created_at"],
        )

    def add_resource(
        self, person_id: int, account_id: int | None, kind: str, label: str, locator: dict, levels: dict | None = None
    ) -> Resource:
        if kind not in KINDS:
            raise StoreError(f"No such kind of resource: {kind}.")
        label = " ".join(label.split())[:MAX_LABEL]
        if not label:
            raise StoreError("A resource needs a name.")
        with self._lock, self._db:
            existing = self._db.execute(
                "SELECT id FROM integration_resources WHERE person_id = ? AND kind = ? AND locator = ?",
                (person_id, kind, json.dumps(locator, sort_keys=True)),
            ).fetchone()
            if existing:
                return self.resource(existing["id"])  # type: ignore[return-value]
            cursor = self._db.execute(
                "INSERT INTO integration_resources (person_id, account_id, kind, label, locator, levels, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (person_id, account_id, kind, label, json.dumps(locator, sort_keys=True), json.dumps(clean(levels)), _now()),
            )
        return self.resource(cursor.lastrowid)  # type: ignore[return-value]

    def resource(self, resource_id: int) -> Resource | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM integration_resources WHERE id = ?", (resource_id,)).fetchone()
        return self._resource(row) if row else None

    def resources_of(self, person_id: int) -> list[Resource]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM integration_resources WHERE person_id = ? ORDER BY id", (person_id,)
            ).fetchall()
        return [self._resource(row) for row in rows]

    def update_resource(self, resource_id: int, label: str | None = None, levels: dict | None = None) -> Resource:
        with self._lock, self._db:
            if label is not None:
                self._db.execute(
                    "UPDATE integration_resources SET label = ? WHERE id = ?",
                    (" ".join(label.split())[:MAX_LABEL] or "?", resource_id),
                )
            if levels is not None:
                self._db.execute(
                    "UPDATE integration_resources SET levels = ? WHERE id = ?", (json.dumps(clean(levels)), resource_id)
                )
        resource = self.resource(resource_id)
        if resource is None:
            raise StoreError("No such resource.")
        return resource

    def delete_resource(self, resource_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM integration_resources WHERE id = ?", (resource_id,))

    # ------------------------------------------------------------------
    # Attachments (a resource in a project or a conversation)
    # ------------------------------------------------------------------
    def attach(
        self, resource_id: int, project_id: int | None = None, conversation: str | None = None,
        levels: dict | None = None,
    ) -> int:
        if (project_id is None) == (conversation is None):
            raise StoreError("Attach to a project or to a conversation.")
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT id FROM integration_attachments WHERE resource_id = ? AND project_id IS ? AND conversation IS ?",
                (resource_id, project_id, conversation),
            ).fetchone()
            if row:
                if levels is not None:
                    self._db.execute(
                        "UPDATE integration_attachments SET levels = ? WHERE id = ?", (json.dumps(clean(levels)), row["id"])
                    )
                return row["id"]
            cursor = self._db.execute(
                "INSERT INTO integration_attachments (resource_id, project_id, conversation, levels, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (resource_id, project_id, conversation, json.dumps(clean(levels)), _now()),
            )
        return cursor.lastrowid  # type: ignore[return-value]

    def attachment_target(self, attachment_id: int) -> tuple[int, int | None, str | None] | None:
        """(resource id, project id, conversation) of an attachment."""
        with self._lock:
            row = self._db.execute(
                "SELECT resource_id, project_id, conversation FROM integration_attachments WHERE id = ?",
                (attachment_id,),
            ).fetchone()
        return (row["resource_id"], row["project_id"], row["conversation"]) if row else None

    def set_attachment_levels(self, attachment_id: int, levels: dict) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE integration_attachments SET levels = ? WHERE id = ?", (json.dumps(clean(levels)), attachment_id)
            )

    def detach(self, attachment_id: int) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM integration_attachments WHERE id = ?", (attachment_id,)).rowcount > 0

    def attached(self, conversation: str, project_id: int | None, person_id: int | None = None) -> list[Attached]:
        """What a conversation sees: its own attachments and its project's. When both name a resource, the
        conversation's permissions win. With `person_id`: only that person's resources (a conversation several
        people write in shows each of them only their own)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT a.id AS attachment_id, a.levels AS attached_levels, a.project_id, a.conversation, r.*"
                " FROM integration_attachments a JOIN integration_resources r ON r.id = a.resource_id"
                " WHERE (a.conversation = ? OR (? IS NOT NULL AND a.project_id = ?))"
                " AND (? IS NULL OR r.person_id = ?) ORDER BY r.id",
                (conversation, project_id, project_id, person_id, person_id),
            ).fetchall()
        found: dict[int, Attached] = {}
        for row in rows:
            scope = "conversation" if row["conversation"] is not None else "project"
            current = found.get(row["id"])
            if current is not None and current.scope == "conversation":
                continue
            found[row["id"]] = Attached(
                self._resource(row), row["attachment_id"], scope, clean(row["attached_levels"], strict=False)
            )
        return list(found.values())

    def attachments_of(self, project_id: int | None = None, conversation: str | None = None) -> list[Attached]:
        """The attachments of one project, or of one conversation (not both)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT a.id AS attachment_id, a.levels AS attached_levels, a.project_id, a.conversation, r.*"
                " FROM integration_attachments a JOIN integration_resources r ON r.id = a.resource_id"
                " WHERE a.project_id IS ? AND a.conversation IS ? ORDER BY a.id",
                (project_id, conversation),
            ).fetchall()
        return [
            Attached(
                self._resource(row), row["attachment_id"], "project" if project_id is not None else "conversation",
                clean(row["attached_levels"], strict=False),
            )
            for row in rows
        ]

    def attachment_count(self, resource_id: int) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM integration_attachments WHERE resource_id = ?", (resource_id,)
            ).fetchone()[0]

    # ------------------------------------------------------------------
    # Requests for permission
    # ------------------------------------------------------------------
    @staticmethod
    def _approval(row) -> Approval:
        return Approval(
            row["id"], row["person_id"], row["conversation"], row["resource_id"], row["op"], row["level"],
            _json(row["args"]), row["summary"], row["reason"], row["status"], row["result"], row["created_at"],
            row["notified_at"], row["decided_at"], row["decided_on"], bool(row["told"]), row["surface"],
            row["user_id"],
        )

    def add_approval(
        self, person_id: int, conversation: str, resource_id: int, op: str, level: str, args: dict, summary: str,
        reason: str = "", surface: str = "", user_id: str = "",
    ) -> tuple[Approval, bool]:
        """Store a request; (it, True) when new. An identical request still waiting in the conversation is that
        one (False): a model that asks twice does not make the person answer twice."""
        digest = args_hash(op, resource_id, args)
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT * FROM approvals WHERE conversation = ? AND args_hash = ? AND status = 'pending'",
                (conversation, digest),
            ).fetchone()
            if row:
                return self._approval(row), False
            cursor = self._db.execute(
                "INSERT INTO approvals (person_id, conversation, surface, user_id, resource_id, op, level, args,"
                " args_hash, summary, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    person_id, conversation, surface, user_id, resource_id, op, level,
                    json.dumps(args, ensure_ascii=False), digest, summary, reason[:500], _now(),
                ),
            )
        return self.approval(cursor.lastrowid), True  # type: ignore[return-value]

    def approval(self, approval_id: int) -> Approval | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return self._approval(row) if row else None

    def approvals_of(self, person_id: int, statuses: tuple[str, ...] | None = None, limit: int = 100) -> list[Approval]:
        """A person's requests, the newest first (`statuses`: only these)."""
        sql, values = "SELECT * FROM approvals WHERE person_id = ?", [person_id]
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            values += list(statuses)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY id DESC LIMIT ?", (*values, limit)).fetchall()
        return [self._approval(row) for row in rows]

    def approvals_in(self, conversation: str, statuses: tuple[str, ...] | None = None) -> list[Approval]:
        sql, values = "SELECT * FROM approvals WHERE conversation = ?", [conversation]
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            values += list(statuses)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY id", values).fetchall()
        return [self._approval(row) for row in rows]

    def pending_count(self, conversation: str) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM approvals WHERE conversation = ? AND status = 'pending'", (conversation,)
            ).fetchone()[0]

    def decide(self, approval_id: int, status: str, surface: str = "", result: str | None = None) -> bool:
        """Move a request from `pending` to `status`. False when it was not pending any more (someone else
        answered first, it expired): the one who gets there first decides."""
        with self._lock, self._db:
            return self._db.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, decided_on = ?, result = COALESCE(?, result)"
                " WHERE id = ? AND status = 'pending'",
                (status, _now(), surface, result, approval_id),
            ).rowcount > 0

    def finish(self, approval_id: int, status: str, result: str) -> None:
        """An approved request ran: `done` or `failed`, and what came out."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE approvals SET status = ?, result = ? WHERE id = ? AND status IN ('approved', 'pending')",
                (status, result, approval_id),
            )

    def mark_notified(self, approval_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE approvals SET notified_at = ? WHERE id = ?", (_now(), approval_id))

    def mark_told(self, approval_ids: list[int]) -> None:
        if not approval_ids:
            return
        with self._lock, self._db:
            self._db.execute(
                f"UPDATE approvals SET told = 1 WHERE id IN ({','.join('?' * len(approval_ids))})", approval_ids
            )

    def mark_untold(self, approval_ids: list[int]) -> None:
        if not approval_ids:
            return
        with self._lock, self._db:
            self._db.execute(
                f"UPDATE approvals SET told = 0 WHERE id IN ({','.join('?' * len(approval_ids))})", approval_ids
            )

    def unnotified(self) -> list[Approval]:
        """Pending requests nobody was pushed a notification for yet."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM approvals WHERE status = 'pending' AND notified_at IS NULL ORDER BY id"
            ).fetchall()
        return [self._approval(row) for row in rows]

    def pending_older_than(self, before: str) -> list[Approval]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM approvals WHERE status = 'pending' AND created_at <= ? ORDER BY id", (before,)
            ).fetchall()
        return [self._approval(row) for row in rows]

    def untold(self, conversation: str, statuses: tuple[str, ...] = (DONE, FAILED, DENIED, EXPIRED)) -> list[Approval]:
        """Requests that ended of which the model was not told yet."""
        marks = ",".join("?" * len(statuses))
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM approvals WHERE conversation = ? AND told = 0 AND status IN ({marks}) ORDER BY id",
                (conversation, *statuses),
            ).fetchall()
        return [self._approval(row) for row in rows]

    def conversations_untold(self, statuses: tuple[str, ...] = (DONE, FAILED, DENIED)) -> list[tuple[str, str, str, int]]:
        """(conversation, surface, user id, person id) of those with something to tell the model about."""
        marks = ",".join("?" * len(statuses))
        with self._lock:
            rows = self._db.execute(
                "SELECT conversation, surface, user_id, person_id FROM approvals"
                f" WHERE told = 0 AND status IN ({marks}) GROUP BY conversation",
                statuses,
            ).fetchall()
        return [(row[0], row[1], row[2], row[3]) for row in rows]

    def prune_approvals(self, before: str) -> int:
        """Forget the requests that ended before `before` (what they held, a file's text, is not kept for ever)."""
        with self._lock, self._db:
            return self._db.execute(
                "DELETE FROM approvals WHERE status NOT IN ('pending', 'approved') AND created_at < ? AND told = 1", (before,)
            ).rowcount

    def interrupted(self) -> list[Approval]:
        """Approved but never finished (the server stopped in between)."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM approvals WHERE status = 'approved' ORDER BY id").fetchall()
        return [self._approval(row) for row in rows]

    # ------------------------------------------------------------------
    # The log
    # ------------------------------------------------------------------
    def log(
        self, person_id: int | None, conversation: str, resource: str, op: str, level: str, summary: str, outcome: str,
        approval_id: int | None = None,
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO integration_log (person_id, conversation, resource, op, level, summary, outcome,"
                " approval_id, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (person_id, conversation, resource, op, level, summary[:1000], outcome, approval_id, _now()),
            )

    def log_entries(self, limit: int = 200, person_id: int | None = None, before: int | None = None) -> list[LogEntry]:
        sql, values = "SELECT * FROM integration_log WHERE 1 = 1", []
        if person_id is not None:
            sql += " AND person_id = ?"
            values.append(person_id)
        if before is not None:
            sql += " AND id < ?"
            values.append(before)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY id DESC LIMIT ?", (*values, limit)).fetchall()
        return [
            LogEntry(
                r["id"], r["person_id"], r["conversation"], r["resource"], r["op"], r["level"], r["summary"],
                r["outcome"], r["approval_id"], r["at"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Jobs for the desktop app
    # ------------------------------------------------------------------
    @staticmethod
    def _job(row) -> Job:
        return Job(
            row["id"], row["person_id"], row["device"], row["op"], _json(row["args"]), row["status"], row["result"],
            row["created_at"],
        )

    def add_job(self, person_id: int, device: str, op: str, args: dict) -> Job:
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO integration_jobs (person_id, device, op, args, created_at) VALUES (?, ?, ?, ?, ?)",
                (person_id, device, op, json.dumps(args, ensure_ascii=False), _now()),
            )
        return self.job(cursor.lastrowid)  # type: ignore[return-value]

    def job(self, job_id: int) -> Job | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM integration_jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def jobs_for(self, person_id: int, device: str | None = None, statuses: tuple[str, ...] = ("queued", "sent")) -> list[Job]:
        sql = f"SELECT * FROM integration_jobs WHERE person_id = ? AND status IN ({','.join('?' * len(statuses))})"
        values: list = [person_id, *statuses]
        if device is not None:
            sql += " AND device = ?"
            values.append(device)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY id", values).fetchall()
        return [self._job(row) for row in rows]

    def take_jobs(self, person_id: int, device: str) -> list[Job]:
        """The queued jobs of a person's computer, handed over: they are `sent` from now on (a job is given out
        once, so that a write is never done twice)."""
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT * FROM integration_jobs WHERE person_id = ? AND device = ? AND status = 'queued' ORDER BY id",
                (person_id, device),
            ).fetchall()
            self._db.executemany(
                "UPDATE integration_jobs SET status = 'sent' WHERE id = ?", [(row["id"],) for row in rows]
            )
        return [self._job(row) for row in rows]

    def expire_jobs(self, sent_before: str, queued_before: str) -> int:
        """Jobs the app took and never answered, and jobs nobody collected for long: failed."""
        with self._lock, self._db:
            return self._db.execute(
                "UPDATE integration_jobs SET status = 'failed', result = 'The computer did not answer.', finished_at = ?"
                " WHERE (status = 'sent' AND created_at < ?) OR (status = 'queued' AND created_at < ?)",
                (_now(), sent_before, queued_before),
            ).rowcount

    def set_job(self, job_id: int, status: str, result: str | None = None) -> bool:
        with self._lock, self._db:
            return self._db.execute(
                "UPDATE integration_jobs SET status = ?, result = COALESCE(?, result),"
                " finished_at = CASE WHEN ? IN ('done', 'failed') THEN ? ELSE finished_at END"
                " WHERE id = ? AND status NOT IN ('done', 'failed')",
                (status, result, status, _now(), job_id),
            ).rowcount > 0

    def prune_jobs(self, before: str) -> int:
        with self._lock, self._db:
            return self._db.execute(
                "DELETE FROM integration_jobs WHERE status IN ('done', 'failed') AND finished_at < ?", (before,)
            ).rowcount
