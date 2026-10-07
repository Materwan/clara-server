"""The operator commands (/provider, /status, /people...).

One registry, two front ends: the console embedded in the server and the
remote `clara-admin` console (through `POST /v1/admin/command`) run exactly
the same handlers. A handler gets the raw argument text and returns text.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .agent import Agent
from .clientapi import DISCORD
from .discord_bot.service import DiscordService
from .lifecycle import Lifecycle
from .limits import UsageLimits, parse_limit, show_limit
from .memory import Memory, Person
from .models import ModelCatalog, show_weight
from .notifications import Notifier
from .prompt import relation_label
from .providers import ProviderError, ProviderManager, make_ref
from .restart import RestartError, RestartService
from .settings import Settings
from .tailscale import Tailscale
from .users import UserError, Users, generate_password

log = logging.getLogger(__name__)

SURFACE_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")


class CommandError(Exception):
    """A mistake of the operator: shown as is, not logged."""


@dataclass(frozen=True)
class CommandResult:
    output: str = ""
    quit: bool = False  # the console should close
    sensitive: bool = False  # the output holds a password: not to be written in a log


@dataclass
class CommandContext:
    settings: Settings
    memory: Memory
    agent: Agent
    providers: ProviderManager
    started_at: float
    listen: str  # "127.0.0.1:8765"
    lifecycle: Lifecycle | None = None
    notifier: Notifier | None = None  # tells everybody when the provider or the model changes
    tailscale: Tailscale | None = None  # how the server is published, if it is
    users: Users | None = None  # people who log in with a password
    discord: DiscordService | None = None  # the Discord bot built into the server
    limits: UsageLimits | None = None  # the daily credits of each person
    models: ModelCatalog | None = None  # the models users may choose, and what they cost
    restart: RestartService | None = None  # pull, update and start again

    def tell_everybody(self, text: str) -> None:
        if self.notifier is not None:
            self.notifier.broadcast(text, "Clara changed model")


Handler = Callable[[CommandContext, str], "Awaitable[CommandResult | str]"]
Choices = Callable[[CommandContext], list[str]]


@dataclass(frozen=True)
class Command:
    name: str
    usage: str
    summary: str
    handler: Handler
    choices: Choices | None = None  # suggestions for the first argument


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def command(self, name: str, usage: str, summary: str, choices: Choices | None = None):
        def register(handler: Handler) -> Handler:
            self._commands[name] = Command(name, usage, summary, handler, choices)
            return handler

        return register

    @property
    def names(self) -> list[str]:
        return list(self._commands)

    def describe(self, context: CommandContext) -> list[dict]:
        """What a console needs to offer completion."""
        return [
            {
                "name": command.name,
                "usage": command.usage,
                "summary": command.summary,
                "choices": command.choices(context) if command.choices else [],
            }
            for command in self._commands.values()
        ]

    async def execute(self, line: str, context: CommandContext) -> CommandResult:
        parts = line.strip().lstrip("/").split(None, 1)  # the leading "/" is optional
        if not parts:
            return CommandResult()
        name, arguments = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
        command = self._commands.get(name)
        if command is None:
            return CommandResult(f"Unknown command: {name}. Type /help.")
        try:
            result = await command.handler(context, arguments)
        except CommandError as error:
            return CommandResult(f"! {error}")
        except Exception:
            log.exception("command %s failed", name)
            return CommandResult("! The command failed (details in the server log).")
        return result if isinstance(result, CommandResult) else CommandResult(result)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows)]
    lines = ["  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)).rstrip()
             for row in [headers, *rows]]
    return "\n".join(lines)


def duration(seconds: float) -> str:
    minutes, _ = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def find_person(memory: Memory, reference: str) -> Person:
    """A person from `3` (id), `discord:1234` (account) or `Erwan` (name)."""
    reference = reference.strip()
    if not reference:
        raise CommandError("Which person? Give an id, a surface:user account, or a name.")
    if reference.isdigit():
        person = memory.person_by_id(int(reference))
    elif ":" in reference:
        surface, _, external_id = reference.partition(":")
        person = memory.find_person(surface.lower(), external_id)
    else:
        matches = memory.people_named(reference)
        if len(matches) > 1:
            ids = ", ".join(str(person.id) for person in matches)
            raise CommandError(f"{len(matches)} people are called {reference}: use their id ({ids}).")
        person = matches[0] if matches else None
    if person is None:
        raise CommandError(f"Nobody matches {reference!r}. See /people.")
    return person


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------
registry = CommandRegistry()


@registry.command("help", "[command]", "List the commands", lambda ctx: registry.names)
async def help_command(ctx: CommandContext, args: str) -> str:
    descriptions = registry.describe(ctx)
    if args:
        name = args.lstrip("/").lower()
        for entry in descriptions:
            if entry["name"] == name:
                return f"/{name} {entry['usage']}".rstrip() + f"\n  {entry['summary']}"
        raise CommandError(f"No command called {name}.")
    return table(
        ["command", "what it does"],
        [[f"/{e['name']} {e['usage']}".rstrip(), e["summary"]] for e in descriptions],
    )


@registry.command("quit", "", "Close this console (the embedded one also stops the server, like /stop)")
async def quit_command(ctx: CommandContext, args: str) -> CommandResult:
    return CommandResult(quit=True)


@registry.command(
    "stop",
    "[now]",
    "Stop the server: tell every client, refuse new questions, wait for running replies and agents, then exit",
    lambda ctx: ["now"],
)
async def stop_command(ctx: CommandContext, args: str) -> str:
    if ctx.lifecycle is None:
        raise CommandError("This server cannot be stopped from here.")
    if args and args.lower() != "now":
        raise CommandError("Usage: /stop [now]   (now: do not wait for what is running)")
    return ctx.lifecycle.request_stop(force=bool(args))


@registry.command(
    "restart",
    "[now]",
    "Pull, update, stop the server the careful way (as /stop does) and start it again",
    lambda ctx: ["now"],
)
async def restart_command(ctx: CommandContext, args: str) -> str:
    if ctx.restart is None:
        raise CommandError("This server cannot be restarted from here.")
    if args and args.lower() != "now":
        raise CommandError("Usage: /restart [now]   (now: do not wait for what is running)")
    try:
        done = await ctx.restart.restart("the console", now=bool(args))
    except RestartError as error:
        raise CommandError(str(error)) from None
    steps = [f"{step['name']}: {step['output'] or 'done'}" for step in done["steps"]]
    return "\n".join([*steps, done["message"]])


@registry.command("status", "", "Provider, model, activity and memory size")
async def status_command(ctx: CommandContext, args: str) -> str:
    stats = ctx.agent.stats
    people, facts = ctx.memory.counts()
    config = ctx.providers.config
    stopping = f", STOPPING (waiting for {ctx.lifecycle.waiting_for} turn(s))" if ctx.lifecycle and ctx.lifecycle.stopping else ""
    rows = [
        ["Provider", f"{config.id} ({config.label}) at {config.host}"],
        ["Model", ctx.providers.model],
        ["Server", f"{ctx.listen}, up {duration(time.monotonic() - ctx.started_at)}" + stopping],
        ["Turns", f"{stats.active} running, {stats.turns} since start"],
        ["Tokens", f"{stats.prompt_tokens:,} prompt / {stats.completion_tokens:,} completion"],
        ["Memory", f"{people} people, {facts} facts ({ctx.settings.db_path})"],
    ]
    if ctx.tailscale is not None and ctx.tailscale.enabled:
        rows.insert(3, ["Tailscale", ctx.tailscale.describe()])
    return "\n".join(f"{label:<10}{value}" for label, value in rows)


@registry.command(
    "provider",
    "[local|cloud|gemini|deepseek|mistral]",
    "Show the providers, or switch where the model runs (saved across restarts)",
    lambda ctx: list(ctx.providers.configs),
)
async def provider_command(ctx: CommandContext, args: str) -> str:
    providers = ctx.providers
    if args:
        try:
            config = providers.switch(args)
        except ProviderError as error:
            raise CommandError(str(error)) from None
        ctx.tell_everybody(f"Clara now runs on {config.label} ({config.id}), with the model {providers.model}.")
        problem = await providers.check()
        lines = [f"Now using {config.id} ({config.label}), model {providers.model}."]
        lines.append("Reachable." if problem is None else f"! Not reachable: {problem} (still selected)")
        return "\n".join(lines)

    rows = []
    for config in providers.configs.values():
        note = "" if config.usable else f"(no {config.key_name})"
        if config.needs_key and config.usable:
            note = "(key set)"
        rows.append(
            [
                "*" if config.id == providers.active else "",
                config.id,
                config.label,
                config.host,
                providers.model_of(config.id),
                note,
            ]
        )
    return table(["", "id", "provider", "host", "model", ""], rows)


@registry.command("model", "[name]", "Show or change the model of the active provider")
async def model_command(ctx: CommandContext, args: str) -> str:
    providers = ctx.providers
    try:
        available = await providers.list_models()
    except Exception as error:
        available = None
        unreachable = f"{type(error).__name__}: {str(error)[:200]}"

    if not args:
        lines = [f"{providers.config.id} model: {providers.model}"]
        if available is None:
            lines.append(f"! Cannot list models: {unreachable}")
        else:
            lines += [("* " if name == providers.model else "  ") + name for name in available]
        return "\n".join(lines)

    if available is not None and args not in available:
        shown = ", ".join(available[:15]) + (" ..." if len(available) > 15 else "")
        raise CommandError(f"{args!r} is not offered by {providers.config.id}. Available: {shown}")
    try:
        providers.set_model(args)
    except ProviderError as error:
        raise CommandError(str(error)) from None
    ctx.tell_everybody(f"Clara now uses the model {args} ({providers.config.label}).")
    note ="" if available is not None else f"\n! Not checked, provider unreachable: {unreachable}"
    return f"{providers.config.id} now uses {args}.{note}"


MODELS_USAGE = (
    "Usage: /models [list | refresh | enable <model|provider|all> | disable <model|provider|all> | "
    "weight <model> <credits|auto> | discord [<model>|default]]"
)


def model_ref(ctx: CommandContext, text: str) -> str:
    """`provider:model`; a bare model name (`gpt-oss:120b`) is one of the provider the server runs on."""
    first = text.partition(":")[0]
    if first in ctx.providers.configs and ":" in text:
        return text
    return make_ref(ctx.providers.active, text)


@registry.command(
    "models",
    "[list | refresh | enable <model|provider|all> | disable <model|provider|all> | weight <model> <credits|auto> | "
    "discord [<model>|default]]",
    "The models of every provider: which ones users may choose (they only see those), what a token of each costs "
    "in credits (from its size, or set by hand), and the model Discord answers with",
    lambda ctx: ["list", "refresh", "enable", "disable", "weight", "discord"],
)
async def models_command(ctx: CommandContext, args: str) -> str:
    catalog = ctx.models
    if catalog is None:
        raise CommandError("This server has no model catalogue.")
    words = args.split()
    verb = words[0].lower() if words else "list"
    try:
        if verb in ("list", "refresh") and len(words) <= 1:
            found = await catalog.listing(force=verb == "refresh")
            rows = [
                [
                    "*" if info.enabled else "",
                    info.ref,
                    f"{info.size_b:g}B" if info.size_b else "?",
                    show_weight(info.weight) + ("" if info.override is None else " (set)"),
                    "" if info.listed else "not offered now",
                ]
                for info in found
            ]
            head = [
                f"Server default: {ctx.providers.default_ref}. Discord: {catalog.discord_model() or 'the server default'}.",
                "A * is a model users may choose; the weight is the credits a token costs.",
            ]
            head += [f"! {provider}: {why}" for provider, why in catalog.problems.items()]
            return "\n".join(head) + "\n" + (table(["", "model", "size", "weight", ""], rows) if rows else "(no model)")
        if verb in ("enable", "disable") and len(words) == 2:
            target = words[1]
            found = await catalog.listing()
            if target.lower() == "all":
                refs = [info.ref for info in found]
            elif target.lower() in ctx.providers.configs:
                refs = [info.ref for info in found if info.provider == target.lower()]
            else:
                refs = [model_ref(ctx, target)]
            if not refs:
                raise CommandError("No such model.")
            catalog.set_enabled(refs, verb == "enable")
            return f"{len(refs)} model(s) {'now' if verb == 'enable' else 'no longer'} selectable by users."
        if verb == "weight" and len(words) == 3:
            ref = model_ref(ctx, words[1])
            auto = words[2].lower() == "auto"
            try:
                weight = None if auto else float(words[2])
            except ValueError:
                raise CommandError("A weight is a number of credits per token (0.25, 1, 15...), or auto.") from None
            catalog.set_weight(ref, weight)
            return f"{ref} now costs {show_weight(catalog.weight(ref))} credit(s) a token" + (" (from its size)." if auto else ".")
        if verb == "discord" and len(words) <= 2:
            if len(words) == 2:
                catalog.set_discord_model(None if words[1].lower() in ("default", "off", "none") else model_ref(ctx, words[1]))
            return f"Discord answers with {catalog.discord_model() or 'the server default (' + ctx.providers.default_ref + ')'}."
    except ProviderError as error:
        raise CommandError(str(error)) from None
    raise CommandError(MODELS_USAGE)


@registry.command("people", "", "Everybody Clara knows, with their accounts")
async def people_command(ctx: CommandContext, args: str) -> str:
    summaries = ctx.memory.summaries()
    if not summaries:
        return "Nobody yet."
    return table(
        ["id", "name", "facts", "relation", "accounts"],
        [
            [str(s.person.id), s.person.name, str(s.facts), "-" if s.relation is None else str(s.relation),
             ", ".join(s.accounts)]
            for s in summaries
        ],
    )


@registry.command(
    "facts",
    "<person> [add <text> | del <id>]",
    "List, add or delete what Clara knows about a person (id, surface:user or name)",
)
async def facts_command(ctx: CommandContext, args: str) -> str:
    reference, _, action = args.partition(" ")
    person = find_person(ctx.memory, reference)
    verb, _, rest = action.strip().partition(" ")
    rest = rest.strip()

    if verb == "add":
        try:
            fact = ctx.memory.add_fact(person.id, rest)
        except ValueError as error:
            raise CommandError(str(error)) from None
        return "Stored." if fact else "Already known."
    if verb == "del":
        if not rest.isdigit():
            raise CommandError("Usage: /facts <person> del <fact id>")
        if not ctx.memory.delete_fact(person.id, int(rest)):
            raise CommandError(f"{person.name} has no fact {rest}.")
        return "Deleted."
    if verb:
        raise CommandError("Usage: /facts <person> [add <text> | del <id>]")

    facts = ctx.memory.facts(person.id, 1000)
    lines = [f"{person.name} (id {person.id})"]
    lines += [f"  [{fact.id}] {fact.text}" for fact in facts] or ["  (no facts)"]
    return "\n".join(lines)


@registry.command(
    "forget-person",
    "<person> [confirm]",
    "Erase a person: accounts, facts and what they said (shows what would go until you confirm)",
)
async def forget_person_command(ctx: CommandContext, args: str) -> str:
    reference, _, last = args.strip().rpartition(" ")
    confirmed = last.lower() == "confirm"
    person = find_person(ctx.memory, reference if confirmed else args)
    found = ctx.memory.footprint(person.id)
    summary = (
        f"{person.name} (id {person.id}): {found.accounts} accounts, {found.facts} facts, "
        f"{found.messages} messages in {found.conversations} conversations"
    )
    if not confirmed:
        return (
            f"This would erase {summary}.\nIn a conversation shared with other people only their own "
            f"messages go.\nThere is no undo; to proceed: /forget-person {person.id} confirm"
        )
    ctx.memory.delete_person(person.id)
    log.info("erased person %s (id %s)", person.name, person.id)
    return f"Erased {summary}."


@registry.command(
    "link",
    "<surface:user> <person>",
    "Make an account belong to a person (merges them if it already has its own)",
)
async def link_command(ctx: CommandContext, args: str) -> str:
    parts = args.split(None, 1)
    if len(parts) != 2 or ":" not in parts[0]:
        raise CommandError("Usage: /link <surface:user> <person>   e.g. /link discord:1234 Erwan")
    surface, _, external_id = parts[0].partition(":")
    surface = surface.lower()
    if not SURFACE_PATTERN.match(surface) or not external_id:
        raise CommandError("The account looks like surface:user, e.g. discord:1234.")
    target = find_person(ctx.memory, parts[1])
    ctx.memory.link_account(surface, external_id, target, force=True)
    accounts = ", ".join(f"{s}:{e}" for s, e in ctx.memory.accounts_of(target.id))
    return f"{target.name} now has: {accounts}"


USER_ACTIONS = ["list", "add", "passwd", "admin", "disable", "enable", "remove", "logout", "link", "unlink"]
DISCORD_REFERENCE = re.compile(r"^(?:discord:|<@!?)?([0-9]{1,20})>?$")


def discord_account(ctx: CommandContext, text: str) -> str:
    """The Discord id in `1234`, `discord:1234`, `<@1234>`, or the name of a member the running bot sees."""
    found = DISCORD_REFERENCE.match(text.strip())
    if found:
        return found.group(1)
    name = text.strip().removeprefix("@").lower()
    members = ctx.discord.members(name, limit=1000) if ctx.discord is not None else []
    exact = [m for m in members if name in (m["name"].lower(), m["display_name"].lower())]
    if len(exact) == 1:
        return exact[0]["user_id"]
    if not members:
        running = ctx.discord is not None and ctx.discord.state == "running"
        raise CommandError(
            f"No Discord member called {text.strip()!r}." if running
            else "Give the Discord id (e.g. discord:1234): the bot is not running, it cannot look names up."
        )
    choices = ", ".join(f"{m['display_name']} ({m['name']}, discord:{m['user_id']})" for m in (exact or members)[:10])
    raise CommandError(f"Which one? {choices}")


@registry.command(
    "user",
    "[list | add <name> [admin] [discord:<id>] | passwd <name> | admin <name> on|off | disable|enable|remove|logout <name>"
    " | link|unlink <name> <discord:id | member>]",
    "People who log in with a password: add one (a password is made and shown once), reset it, sign out,"
    " sign a Discord account in as them",
    lambda ctx: USER_ACTIONS,
)
async def user_command(ctx: CommandContext, args: str) -> CommandResult | str:
    users = ctx.users
    if users is None:
        raise CommandError("This server has no user accounts.")
    verb, _, rest = args.strip().partition(" ")
    verb, words = verb.lower() or "list", rest.split()

    def named() -> str:
        if not words:
            raise CommandError(f"Usage: /user {verb} <name>")
        return words[0].lower()

    try:
        if verb == "list":
            found = users.list()
            if not found:
                return "Nobody can log in yet. Add someone: /user add <name> admin"
            rows = []
            for user in found:
                person = ctx.memory.person_by_id(user.person_id)
                state = "disabled" if user.disabled else ("admin" if user.is_admin else "user")
                rows.append(
                    [user.name, state, f"{person.name} (id {person.id})" if person else "?",
                     str(len(users.sessions_of(user.name))), ", ".join(users.accounts_signed_in_as(user.name)) or "-",
                     (user.last_login_at or "never")[:16].replace("T", " ")]
                )
            return table(["name", "role", "person", "devices", "signed in", "last login"], rows)

        if verb == "add":
            name = named()
            flags = [word.lower() for word in words[1:]]
            discord = [flag for flag in flags if flag != "admin"]
            if len(discord) > 1 or (discord and not DISCORD_REFERENCE.match(discord[0])):
                raise CommandError("Usage: /user add <name> [admin] [discord:<id>]")
            password = generate_password()
            if discord:
                discord_id = discord_account(ctx, discord[0])
                user = users.create_with_account(name, password, "admin" in flags, DISCORD, discord_id, "console")
                if ctx.discord is not None:
                    ctx.discord.signed_in(discord_id, user.name)
            else:
                user = users.create(name, password, admin="admin" in flags)
            person = ctx.memory.person_by_id(user.person_id)
            return CommandResult(
                f"Created {user.name}{' (administrator)' if user.is_admin else ''}, the person {person.name} (id {person.id})"
                + (f", signed in on discord:{discord_id}" if discord else "") + ".\n"
                f"Password: {password}\n"
                "Shown once. They can change it on the web site (Account), or you can reset it: /user passwd " + user.name,
                sensitive=True,
            )

        if verb in ("link", "unlink"):
            name = named()
            if len(words) < 2:
                raise CommandError(f"Usage: /user {verb} <name> <discord:id | member name>")
            user = users.get(name)
            if user is None:
                raise CommandError(f"No user called {name}.")
            discord_id = discord_account(ctx, " ".join(words[1:]))
            if verb == "unlink":
                if f"{DISCORD}:{discord_id}" not in users.accounts_signed_in_as(user.name):
                    raise CommandError(f"discord:{discord_id} is not signed in as {user.name}.")
                users.sign_out_account(DISCORD, discord_id)
                if ctx.discord is not None:
                    ctx.discord.signed_out(discord_id)
                return f"discord:{discord_id} is signed out of {user.name}: Clara no longer answers them."
            if user.disabled:
                raise CommandError(f"{user.name} is disabled: /user enable {user.name} first.")
            previous = users.account_user(DISCORD, discord_id)
            users.attach_account(user, DISCORD, discord_id, "console")
            if ctx.discord is not None:
                ctx.discord.signed_in(discord_id, user.name)
            log.info("signed discord:%s in as %s from the console", discord_id, user.name)
            was = f" (instead of {previous.name})" if previous is not None and previous.name != user.name else ""
            return f"discord:{discord_id} is signed in as {user.name}{was}: Clara answers them now."

        if verb == "passwd":
            name = named()
            password = generate_password()
            signed_out = users.set_password(name, password)
            return CommandResult(
                f"New password for {name}: {password}\nShown once. {signed_out} device(s) were signed out.",
                sensitive=True,
            )

        if verb == "admin":
            name = named()
            if len(words) != 2 or words[1].lower() not in ("on", "off"):
                raise CommandError("Usage: /user admin <name> on|off")
            users.set_admin(name, words[1].lower() == "on")
            return f"{name} is {'now' if words[1].lower() == 'on' else 'no longer'} an administrator."

        if verb in ("disable", "enable"):
            name = named()
            users.set_disabled(name, verb == "disable")
            return f"{name} is {'disabled and signed out everywhere' if verb == 'disable' else 'enabled again'}."

        if verb == "remove":
            name = named()
            users.delete(name)
            return f"{name} can no longer log in. Their person, facts and conversations are kept (/forget-person erases them)."

        if verb == "logout":
            return f"{users.revoke_all(named())} device(s) signed out."
    except UserError as error:
        raise CommandError(str(error)) from None
    raise CommandError(f"Unknown action {verb!r}. Try /help user.")


@registry.command("remember", "<person> <text>", "Store a fact about a person (same as /facts <person> add)")
async def remember_command(ctx: CommandContext, args: str) -> str:
    reference, _, text = args.strip().partition(" ")
    if not reference or not text.strip():
        raise CommandError("Usage: /remember <person> <text>   e.g. /remember Erwan likes jazz")
    return await facts_command(ctx, f"{reference} add {text.strip()}")


@registry.command("forget", "<person> <fact id>", "Delete a fact of a person (same as /facts <person> del)")
async def forget_command(ctx: CommandContext, args: str) -> str:
    reference, _, fact_id = args.strip().rpartition(" ")
    if not reference or not fact_id.isdigit():
        raise CommandError("Usage: /forget <person> <fact id>   (the ids are shown by /facts <person>)")
    return await facts_command(ctx, f"{reference} del {fact_id}")


@registry.command(
    "relation",
    "[<person> [<0-100> | +n | -n | reset]]",
    "Clara's relationship with each person (it sets her tone); show it, set it, move it or reset it",
)
async def relation_command(ctx: CommandContext, args: str) -> str:
    if not args.strip():
        rows = [
            [str(s.person.id), s.person.name, "-" if s.relation is None else f"{s.relation}/100", relation_label(s.relation)]
            for s in ctx.memory.summaries()
        ]
        return table(["id", "name", "relation", ""], rows) if rows else "Nobody yet."
    reference, _, value = args.strip().rpartition(" ")
    if not reference or not re.fullmatch(r"[+-]?\d{1,3}|reset", value.lower()):
        reference, value = args.strip(), ""  # only a person (whose name may have spaces)
    person = find_person(ctx.memory, reference)
    if value.lower() == "reset":
        ctx.memory.set_relation(person.id, None)
        return f"{person.name}: no relationship any more (Clara is neutral and polite)."
    if value[:1] in "+-" and value:
        score = ctx.memory.adjust_relation(person.id, int(value))
    elif value:
        if not 0 <= int(value) <= 100:
            raise CommandError("A relationship goes from 0 to 100.")
        score = ctx.memory.set_relation(person.id, int(value))
    else:
        score = ctx.memory.relation(person.id)
    shown = "none yet" if score is None else f"{score}/100 ({relation_label(score)})"
    return f"{person.name} (id {person.id}): {shown}"


CHIME_VALUES = {"on": True, "off": False, "default": None}


@registry.command(
    "chime",
    "[default on|off | <space> on|off|default]",
    "Where Clara may answer messages not addressed to her (Discord servers): list, or switch it",
    lambda ctx: ["default"] + [space.id for space in ctx.memory.spaces()],
)
async def chime_command(ctx: CommandContext, args: str) -> str:
    memory = ctx.memory
    words = args.split()
    if not words:
        default = memory.chime_default()
        rows = [
            [space.id, space.name, {None: f"default ({'on' if default else 'off'})", True: "on", False: "off"}[space.chime],
             "" if space.present else "(bot not there)"]
            for space in memory.spaces()
        ]
        lines = [f"Default: {'on' if default else 'off'}"]
        lines.append(table(["space", "name", "chime in", ""], rows) if rows else "No space yet (the Discord bot lists its servers when it starts).")
        return "\n".join(lines)
    if len(words) < 2 or words[-1].lower() not in CHIME_VALUES:
        raise CommandError("Usage: /chime default on|off   or   /chime <space id or name> on|off|default")
    target, value = " ".join(words[:-1]), CHIME_VALUES[words[-1].lower()]
    if target.lower() == "default":
        if value is None:
            raise CommandError("The default is on or off.")
        memory.set_chime_default(value)
        return f"By default, Clara {'may' if value else 'may not'} answer messages that are not for her."
    matches = [s for s in memory.spaces() if s.id == target] or [
        s for s in memory.spaces() if s.id.endswith(f":{target}") or s.name.lower() == target.lower()
    ]
    if len(matches) != 1:
        raise CommandError(f"{'No' if not matches else 'More than one'} space matches {target!r}. See /chime.")
    memory.set_space_chime(matches[0].id, value)
    shown = {None: "the default", True: "on", False: "off"}[value]
    return f"{matches[0].name or matches[0].id}: chime in is now {shown}."


@registry.command(
    "discord",
    "[status | start | stop | restart]",
    "The Discord bot built into the server: its state, or start / stop it (until the server restarts)",
    lambda ctx: ["status", "start", "stop", "restart"],
)
async def discord_command(ctx: CommandContext, args: str) -> str:
    bot = ctx.discord
    if bot is None:
        raise CommandError("This server has no Discord bot.")
    action = args.strip().lower() or "status"
    if action == "status":
        found = bot.status()
        lines = [bot.describe(), f"Starts with the server: {'yes' if found['auto_start'] else 'no'} (AUTO_START_DISCORD_BOT)"]
        if found["invite_url"]:
            lines.append(f"Invite: {found['invite_url']}")
        return "\n".join(lines)
    if action == "start":
        return await bot.start()
    if action == "stop":
        return await bot.stop()
    if action == "restart":
        return await bot.restart()
    raise CommandError("Usage: /discord [status | start | stop | restart]")


@registry.command(
    "limit",
    "[list | default <credits|off> | <user> [<credits|off|default>]]",
    "Credits a day each user may use (a token costs the weight of the model, see /models; a day ends at midnight "
    "UTC; administrators have no limit): see the usage, set the default, or set one user's (credits: 500000, "
    "500k, 2m; off: no limit; default: follow the default)",
    lambda ctx: ["list", "default"] + [user.name for user in (ctx.users.list() if ctx.users else [])],
)
async def limit_command(ctx: CommandContext, args: str) -> str:
    limits, users = ctx.limits, ctx.users
    if limits is None or users is None:
        raise CommandError("This server has no usage limits.")
    words = args.split()
    verb = words[0].lower() if words else "list"

    def quota_of(user):
        return limits.quota_for_user(user.person_id, user.is_admin and not user.disabled, user.token_limit)

    def row(user) -> list[str]:
        quota = quota_of(user)
        if quota.limit is None:
            own = "admin" if user.is_admin and not user.disabled else "none"
            return [user.name, own, f"{quota.used:,}", "-", "-"]
        own = "default" if user.token_limit is None else "own"
        return [user.name, own, f"{quota.used:,}", f"{quota.limit:,}", f"{quota.remaining:,}"]

    try:
        if verb == "list" and len(words) <= 1:
            found = users.list()
            head = f"Default: {show_limit(limits.default())}. Today's usage, UTC:"
            if not found:
                return head + "\n(no user yet)"
            return head + "\n" + table(["user", "limit", "used", "credits/day", "left"], [row(user) for user in found])
        if verb == "default":
            if len(words) != 2:
                raise CommandError(f"Default: {show_limit(limits.default())}. Change it: /limit default <credits|off>")
            limits.set_default(parse_limit(words[1]))
            return f"The default is now {show_limit(limits.default())} (for users with no limit of their own)."
        user = users.get(verb)
        if user is None:
            raise CommandError(f"No user called {verb}. Usage: /limit [list | default <credits|off> | <user> [<credits|off|default>]]")
        if len(words) == 1:
            return table(["user", "limit", "used", "credits/day", "left"], [row(user)])
        if len(words) != 2:
            raise CommandError("Usage: /limit <user> <credits|off|default>")
        users.set_token_limit(user.name, None if words[1].lower() == "default" else parse_limit(words[1]))
        user = users.get(user.name)
        note = " They are an administrator: they have no limit whatever is set." if user.is_admin else ""
        if user.token_limit is None:
            return f"{user.name} follows the default ({show_limit(limits.default())}).{note}"
        return f"{user.name}: {show_limit(user.token_limit)}.{note}"
    except (ValueError, UserError) as error:
        raise CommandError(str(error)) from None
