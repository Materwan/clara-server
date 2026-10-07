"""The interactive prompt, shared by the embedded console and `clara-admin`.

The two only differ in `execute`: the embedded one calls the command registry
directly, the remote one sends the line to the server over HTTP.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory, InMemoryHistory

from .commands import CommandResult

Execute = Callable[[str], Awaitable[CommandResult]]


class CommandCompleter(Completer):
    """Completes `/name` (the slash is optional) then the first argument of that command."""

    def __init__(self, commands: list[dict]):
        # `commands` is CommandRegistry.describe(): name + suggestions for the first argument
        self._choices = {entry["name"]: list(entry["choices"]) for entry in commands}

    def get_completions(self, document, complete_event):
        words = document.text_before_cursor.lstrip().split(" ")
        if len(words) == 1:  # still typing the command name
            typed = words[0].lstrip("/").lower()
            for name in self._choices:
                if name.startswith(typed):
                    yield Completion(f"/{name}", start_position=-len(words[0]))
        elif len(words) == 2:  # typing the first argument
            typed = words[1].lower()
            for choice in self._choices.get(words[0].lstrip("/").lower(), []):
                if choice.lower().startswith(typed):
                    yield Completion(choice, start_position=-len(words[1]))


def build_completer(commands: list[dict]) -> CommandCompleter:
    return CommandCompleter(commands)


def make_session(commands: list[dict], history_path: Path | None, **options) -> PromptSession:
    if history_path is not None:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(history_path))
    else:
        history = InMemoryHistory()
    return PromptSession(
        history=history, completer=build_completer(commands), complete_while_typing=True, **options
    )


async def run_console(
    execute: Execute,
    commands: list[dict],
    *,
    banner: str,
    history_path: Path | None = None,
    prompt: str = "clara> ",
    session: PromptSession | None = None,
) -> None:
    """Read lines and run them until /quit, Ctrl+D or Ctrl+C.

    Without a terminal (piped input) it falls back to plain `input()`.
    """
    if session is None and sys.stdin.isatty() and sys.stdout.isatty():
        session = make_session(commands, history_path)
    print(banner)
    while True:
        try:
            if session is not None:
                line = await session.prompt_async(prompt)
            else:
                line = await asyncio.to_thread(input, prompt)
        except (EOFError, KeyboardInterrupt):
            return
        line = line.strip()
        if not line:
            continue
        result = await execute(line)
        if result.output:
            print(result.output)
        if result.quit:
            return
