"""What Clara may do with something a person attached, decided per *level* of action.

    read          look: list, read, search
    write         add something new, or change something that stays recoverable (a new file, a commit on a branch)
    destructive   lose or replace what was there, or change what is shared (overwrite, delete, move, commit to the
                  default branch, merge)

Each level is `allow` (just do it), `ask` (wait for the person) or `deny`. Where the answer comes from, from the most
specific: the attachment (this project or conversation), the resource, the account, the default of the type. An
administrator's ceiling then caps it: a ceiling of `ask` means `allow` cannot be given.
"""

from __future__ import annotations

import json

READ, WRITE, DESTRUCTIVE = "read", "write", "destructive"
LEVELS = (READ, WRITE, DESTRUCTIVE)
ALLOW, ASK, DENY = "allow", "ask", "deny"
DECISIONS = (ALLOW, ASK, DENY)
_OPEN = {DENY: 0, ASK: 1, ALLOW: 2}  # how much each lets through

# The integration types (what an administrator switches on and off), and the kinds of resource each has
GITHUB, GDRIVE, SERVER, COMPUTER = "github", "gdrive", "server", "computer"
TYPES = (GITHUB, GDRIVE, SERVER, COMPUTER)
TYPE_NAMES = {GITHUB: "GitHub", GDRIVE: "Google Drive", SERVER: "Server folders", COMPUTER: "Computer folders"}
KINDS = {
    "github_repo": GITHUB,
    "drive_folder": GDRIVE,
    "drive_file": GDRIVE,
    "server_path": SERVER,
    "computer_path": COMPUTER,
}
DEFAULTS = {READ: ALLOW, WRITE: ASK, DESTRUCTIVE: ASK}  # what a type gives until someone chooses


class SettingError(ValueError):
    """A permission setting that makes no sense."""


def clean(levels: object, strict: bool = True) -> dict[str, str]:
    """Levels as stored: only known levels with known decisions. `strict`: refuse the rest instead of dropping it."""
    if levels is None or levels == "":
        return {}
    if isinstance(levels, str):
        try:
            levels = json.loads(levels)
        except ValueError:
            raise SettingError("Permissions are not valid JSON.") from None
    if not isinstance(levels, dict):
        raise SettingError("Permissions are a level -> allow, ask or deny.")
    kept: dict[str, str] = {}
    for level, decision in levels.items():
        if level not in LEVELS or decision not in DECISIONS:
            if strict:
                raise SettingError(
                    f"{level!r}: {decision!r} is not a permission (levels: {', '.join(LEVELS)}; "
                    f"decisions: {', '.join(DECISIONS)})."
                )
            continue
        kept[level] = decision
    return kept


def capped(decision: str, ceiling: str | None) -> str:
    """`decision`, no more open than `ceiling`."""
    if ceiling is None or ceiling not in _OPEN:
        return decision
    return decision if _OPEN[decision] <= _OPEN[ceiling] else ceiling


def effective(*layers: dict[str, str] | None, ceiling: dict[str, str] | None = None) -> dict[str, str]:
    """Each level's decision. `layers`: the most specific first (attachment, resource, account); what none of them
    says is DEFAULTS; the `ceiling` is applied last."""
    result: dict[str, str] = {}
    for level in LEVELS:
        decision = next((layer[level] for layer in layers if layer and level in layer), DEFAULTS[level])
        result[level] = capped(decision, (ceiling or {}).get(level))
    return result


def describe(levels: dict[str, str]) -> str:
    """For the system prompt: "read: allow, write: ask, destructive: ask"."""
    return ", ".join(f"{level}: {levels[level]}" for level in LEVELS)
