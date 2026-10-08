"""Erasing a person: what the memory holds, and what the traffic log kept of them.

The traffic log (traffic.py) writes what people say and the prompts that carry what Clara knows about them, so erasing
the memory alone would leave all of it for the days the log is kept.
"""

from __future__ import annotations

import logging
import re

from .memory import Footprint, Memory
from .traffic import TrafficLog

log = logging.getLogger(__name__)


def traffic_pattern(accounts: list[tuple[str, str]], user_names: list[str], person_name: str) -> re.Pattern[str] | None:
    """The lines of the traffic log that are about these accounts (`web:erwan`, a conversation `web:erwan:2`, a query
    `user_id=erwan`, a request of `user:erwan@web`) or that tell the model who it talks to."""
    parts = []
    edge = r"(?![\w.-])"
    for surface, external in accounts:
        parts.append(rf"(?<![\w.-]){re.escape(surface)}:{re.escape(external)}{edge}")
        parts.append(rf'"user_id": "{re.escape(external)}"')
        parts.append(rf"user_id={re.escape(external)}(?![\w.%-])")
    parts += [rf"user:{re.escape(name)}@" for name in user_names]
    if person_name.strip():
        parts.append(rf"talking to: {re.escape(person_name)} \(")
    return re.compile("|".join(parts)) if parts else None


def erase_person(memory: Memory, traffic: TrafficLog | None, person_id: int) -> tuple[Footprint, int]:
    """Erase the person from the memory and from the traffic log. Returns what went from the memory, and how many lines
    of the log."""
    person = memory.person_by_id(person_id)
    accounts = memory.accounts_of(person_id)
    with memory.lock:
        users = [row["name"] for row in memory.database.execute("SELECT name FROM users WHERE person_id = ?", (person_id,))]
    found = memory.delete_person(person_id)
    removed = 0
    if traffic is not None:
        pattern = traffic_pattern(accounts, users, person.name if person else "")
        if pattern is not None:
            removed = traffic.erase(pattern)
    log.info("erased person %s: %d lines of the traffic log", person_id, removed)
    return found, removed
