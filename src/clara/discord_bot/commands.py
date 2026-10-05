"""The slash commands. Every answer is ephemeral (only the person who ran the command sees it).

    /register   a form: user name, password twice -> a Clara user (the same as on the web site), signed in
    /login      a form: user name, password -> this Discord account is signed in as that user
    /logout
    /me         the account, the relationship, what Clara remembers (with a menu to make her forget something)
    /remember   /forget   /reset   /help
    /tasks      your to-do list: each task with the reminders sent and the next one;  /task: one in full

Descriptions are in English, and in French for people whose Discord is in French (FrenchTranslator).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .backend import ClaraError
from .routing import conversation_of
from .texts import FRENCH, language, t

if TYPE_CHECKING:
    from .bot import ClaraBot

log = logging.getLogger(__name__)

FACTS_SHOWN = 25  # a select menu has at most 25 options
FIELD_LIMIT = 1024

FRENCH_DESCRIPTIONS = {
    "Create your Clara account": "Créer ton compte Clara",
    "Sign in to your Clara account": "Te connecter à ton compte Clara",
    "Sign out of Clara on Discord": "Te déconnecter de Clara sur Discord",
    "What Clara knows about you": "Ce que Clara sait de toi",
    "Make Clara remember something about you": "Faire retenir quelque chose à Clara",
    "What to remember": "Ce qu'il faut retenir",
    "Make Clara forget something": "Faire oublier quelque chose à Clara",
    "The memory to forget": "Le souvenir à oublier",
    "Clear this conversation (facts are kept)": "Effacer cette conversation (les souvenirs restent)",
    "How to talk with Clara": "Comment parler avec Clara",
    "Your to-do list": "Ta liste de tâches",
    "Which ones": "Lesquelles",
    "One of your tasks in full": "Le détail d'une de tes tâches",
    "The task": "La tâche",
}


class FrenchTranslator(app_commands.Translator):
    async def translate(self, string: app_commands.locale_str, locale: discord.Locale, context) -> str | None:
        if language(locale) != FRENCH:
            return None
        return FRENCH_DESCRIPTIONS.get(string.message)


def describe_error(lang: str, error: ClaraError) -> str:
    if error.unreachable:
        return t(lang, "unreachable")
    if error.status == 401:
        return t(lang, "wrong_password")
    if error.status == 429:
        return t(lang, "busy")
    return t(lang, "refused", detail=error.detail[:300])


def lang_of(bot: ClaraBot, interaction: discord.Interaction) -> str:
    lang = language(interaction.locale)
    bot.accounts.remember_language(interaction.user.id, lang)
    return lang


async def reply(interaction: discord.Interaction, text: str, **options) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, **options)
    else:
        await interaction.response.send_message(text, ephemeral=True, **options)


# ----------------------------------------------------------------------
# Signing in
# ----------------------------------------------------------------------
class SignInModal(discord.ui.Modal):
    def __init__(self, bot: ClaraBot, lang: str, creating: bool):
        super().__init__(title=t(lang, "register_title" if creating else "login_title"))
        self.bot, self.lang, self.creating = bot, lang, creating
        self.username = discord.ui.TextInput(
            label=t(lang, "username"), placeholder=t(lang, "username_hint"), min_length=1, max_length=32
        )
        self.password = discord.ui.TextInput(
            label=t(lang, "password"), placeholder=t(lang, "password_hint"), min_length=10 if creating else 1,
            max_length=256,
        )
        self.add_item(self.username)
        self.add_item(self.password)
        self.again: discord.ui.TextInput | None = None
        if creating:
            self.again = discord.ui.TextInput(label=t(lang, "password_again"), min_length=10, max_length=256)
            self.add_item(self.again)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.again is not None and self.again.value != self.password.value:
            await reply(interaction, t(self.lang, "password_mismatch"))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        user = interaction.user
        call = self.bot.api.register if self.creating else self.bot.api.login
        try:
            done = await call(user.id, user.display_name, self.username.value.strip().lower(), self.password.value)
        except ClaraError as error:
            await reply(interaction, describe_error(self.lang, error))
            return
        self.bot.accounts.signed_in_as(user.id, done["user"])
        log.info("%s (%s) %s as %s", user, user.id, "registered" if self.creating else "signed in", done["user"])
        await reply(interaction, t(self.lang, "registered" if self.creating else "logged_in", user=done["user"]))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("sign-in form failed", exc_info=error)
        await reply(interaction, t(self.lang, "failed", detail=type(error).__name__))


# ----------------------------------------------------------------------
# /me
# ----------------------------------------------------------------------
def me_embed(lang: str, me: dict) -> discord.Embed:
    embed = discord.Embed(title=t(lang, "me_title"), colour=discord.Colour.blurple())
    embed.add_field(name=t(lang, "me_user"), value=me.get("user") or "?", inline=True)
    relation = me.get("relation")
    embed.add_field(
        name=t(lang, "me_relation"),
        value=t(lang, "me_relation_none") if relation is None else f"{relation}/100 ({me.get('relation_label', '')})",
        inline=True,
    )
    embed.add_field(name=t(lang, "me_accounts"), value=", ".join(me.get("accounts", [])) or "-", inline=False)
    facts = me.get("facts", [])
    lines: list[str] = []
    for fact in reversed(facts):  # newest first
        line = f"`{fact['id']}` {fact['text']}"
        if sum(len(x) + 1 for x in lines) + len(line) > FIELD_LIMIT - 40:
            lines.append(t(lang, "me_more_facts", count=len(facts) - len(lines)))
            break
        lines.append(line)
    embed.add_field(name=t(lang, "me_facts"), value="\n".join(lines) or t(lang, "me_no_facts"), inline=False)
    return embed


class ForgetMenu(discord.ui.View):
    def __init__(self, bot: ClaraBot, lang: str, owner: int, facts: list[dict]):
        super().__init__(timeout=300)
        self.bot, self.lang, self.owner = bot, lang, owner
        newest = list(reversed(facts))[:FACTS_SHOWN]
        if not newest:
            return
        select = discord.ui.Select(
            placeholder=t(lang, "me_forget_placeholder"),
            options=[
                discord.SelectOption(label=(fact["text"][:97] + "…") if len(fact["text"]) > 100 else fact["text"],
                                     value=str(fact["id"]))
                for fact in newest
            ],
        )
        select.callback = self.forget
        self.select = select
        self.add_item(select)

    async def forget(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner:
            return
        fact_id = int(self.select.values[0])
        try:
            await self.bot.api.delete_fact(self.owner, fact_id)
            me = await self.bot.api.me(self.owner)
        except ClaraError as error:
            await reply(interaction, describe_error(self.lang, error))
            return
        await interaction.response.edit_message(
            embed=me_embed(self.lang, me), view=ForgetMenu(self.bot, self.lang, self.owner, me.get("facts", []))
        )


# ----------------------------------------------------------------------
# /tasks, /task
# ----------------------------------------------------------------------
TASKS_SHOWN = 20


def moment(text: str, style: str = "f") -> str:
    """A moment as Discord shows it, on the clock of whoever reads it."""
    return f"<t:{int(datetime.fromisoformat(text).timestamp())}:{style}>"


def task_text(lang: str, task: dict, detail: bool = False) -> str:
    """A task: its number, title, deadline, reminders sent and the next reminder (and, in full, the rest)."""
    parts = [f"`{task['id']}` **{task['title']}**"]
    if task["status"] == "done":
        parts.append(t(lang, "task_done"))
    if task.get("due_at"):
        parts.append(t(lang, "task_due", when=moment(task["due_at"])))
    sent = task["reminders_sent"]
    parts.append(t(lang, "task_sent_one") if sent == 1 else t(lang, "task_sent", count=sent))
    if task["status"] == "open":
        parts.append(
            t(lang, "task_next", when=moment(task["next_reminder"])) if task["next_reminder"] else t(lang, "task_no_next")
        )
    text = " · ".join(parts)
    if detail:
        text += f"\n{t(lang, 'task_description')}: {task['description'] or t(lang, 'task_no_description')}"
        if len(task["reminders"]) > 1:
            text += f"\n{t(lang, 'task_reminders')}: " + ", ".join(moment(at) for at in task["reminders"])
    return text


def tasks_embed(lang: str, tasks: list[dict], status: str) -> discord.Embed:
    title = t(lang, "tasks_done_title" if status == "done" else "tasks_title")
    embed = discord.Embed(title=title, colour=discord.Colour.blurple())
    if not tasks:
        embed.description = t(lang, "tasks_none")
        return embed
    lines = [task_text(lang, task) for task in tasks[:TASKS_SHOWN]]
    if len(tasks) > TASKS_SHOWN:
        lines.append(t(lang, "tasks_more", count=len(tasks) - TASKS_SHOWN))
    embed.description = "\n".join(lines)
    embed.set_footer(text=t(lang, "tasks_footer").replace("`", ""))
    return embed


# ----------------------------------------------------------------------
# The commands
# ----------------------------------------------------------------------
def register_commands(tree: app_commands.CommandTree, bot: ClaraBot) -> None:
    describe = app_commands.locale_str

    @tree.command(name="register", description=describe("Create your Clara account"))
    async def register(interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(SignInModal(bot, lang_of(bot, interaction), creating=True))

    @tree.command(name="login", description=describe("Sign in to your Clara account"))
    async def login(interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(SignInModal(bot, lang_of(bot, interaction), creating=False))

    @tree.command(name="logout", description=describe("Sign out of Clara on Discord"))
    async def logout(interaction: discord.Interaction) -> None:
        lang = lang_of(bot, interaction)
        try:
            done = await bot.api.logout(interaction.user.id)
        except ClaraError as error:
            await reply(interaction, describe_error(lang, error))
            return
        bot.accounts.signed_out(interaction.user.id)
        await reply(interaction, t(lang, "logged_out" if done else "not_logged_in"))

    @tree.command(name="me", description=describe("What Clara knows about you"))
    async def me(interaction: discord.Interaction) -> None:
        lang = lang_of(bot, interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            found = await bot.api.me(interaction.user.id)
        except ClaraError as error:
            await reply(interaction, describe_error(lang, error))
            return
        if not found.get("signed_in"):
            await reply(interaction, t(lang, "need_account"))
            return
        await interaction.followup.send(
            embed=me_embed(lang, found), view=ForgetMenu(bot, lang, interaction.user.id, found.get("facts", [])),
            ephemeral=True,
        )

    @tree.command(name="remember", description=describe("Make Clara remember something about you"))
    @app_commands.describe(text=describe("What to remember"))
    async def remember(interaction: discord.Interaction, text: app_commands.Range[str, 1, 300]) -> None:
        lang = lang_of(bot, interaction)
        try:
            done = await bot.api.add_fact(interaction.user.id, text)
        except ClaraError as error:
            await reply(interaction, t(lang, "need_account") if error.not_signed_in else describe_error(lang, error))
            return
        await reply(interaction, t(lang, "remembered" if done.get("created") else "already_known"))

    @tree.command(name="forget", description=describe("Make Clara forget something"))
    @app_commands.describe(memory=describe("The memory to forget"))
    async def forget(interaction: discord.Interaction, memory: int) -> None:
        lang = lang_of(bot, interaction)
        try:
            found = await bot.api.me(interaction.user.id)
            text = next((f["text"] for f in found.get("facts", []) if f["id"] == memory), None)
            if text is None:
                await reply(interaction, t(lang, "no_such_fact"))
                return
            await bot.api.delete_fact(interaction.user.id, memory)
        except ClaraError as error:
            await reply(interaction, t(lang, "need_account") if error.not_signed_in else describe_error(lang, error))
            return
        await reply(interaction, t(lang, "forgotten", text=text))

    @forget.autocomplete("memory")
    async def forget_choices(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        try:
            facts = (await bot.api.me(interaction.user.id)).get("facts", [])
        except ClaraError:
            return []
        wanted = current.lower().strip()
        matching = [f for f in reversed(facts) if wanted in f["text"].lower() or wanted == str(f["id"])]
        return [app_commands.Choice(name=f["text"][:100], value=f["id"]) for f in matching[:25]]

    @tree.command(name="tasks", description=describe("Your to-do list"))
    @app_commands.describe(which=describe("Which ones"))
    @app_commands.choices(
        which=[
            app_commands.Choice(name="open", value="open"),
            app_commands.Choice(name="done", value="done"),
        ]
    )
    async def tasks(interaction: discord.Interaction, which: app_commands.Choice[str] | None = None) -> None:
        lang = lang_of(bot, interaction)
        status = which.value if which else "open"
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            found = await bot.api.tasks(interaction.user.id, status)
        except ClaraError as error:
            await reply(interaction, t(lang, "need_account") if error.not_signed_in else describe_error(lang, error))
            return
        await interaction.followup.send(embed=tasks_embed(lang, found, status), ephemeral=True)

    @tree.command(name="task", description=describe("One of your tasks in full"))
    @app_commands.describe(task=describe("The task"))
    async def task_command(interaction: discord.Interaction, task: int) -> None:
        lang = lang_of(bot, interaction)
        try:
            found = await bot.api.task(interaction.user.id, task)
        except ClaraError as error:
            if error.status == 404:
                await reply(interaction, t(lang, "no_such_task"))
            else:
                await reply(interaction, t(lang, "need_account") if error.not_signed_in else describe_error(lang, error))
            return
        await reply(interaction, task_text(lang, found, detail=True))

    @task_command.autocomplete("task")
    async def task_choices(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        try:
            found = await bot.api.tasks(interaction.user.id, "all")
        except ClaraError:
            return []
        wanted = current.lower().strip()
        matching = [x for x in found if wanted in x["title"].lower() or wanted == str(x["id"])]
        return [app_commands.Choice(name=f"{x['id']}: {x['title']}"[:100], value=x["id"]) for x in matching[:25]]

    @tree.command(name="reset", description=describe("Clear this conversation (facts are kept)"))
    async def reset(interaction: discord.Interaction) -> None:
        lang = lang_of(bot, interaction)
        private = interaction.guild is None
        if not private and not interaction.permissions.manage_guild:
            await reply(interaction, t(lang, "reset_admins_only"))
            return
        conversation = conversation_of(interaction.channel_id or 0, private) or f"discord:{interaction.user.id}"
        try:
            count = await bot.api.clear_conversation(conversation)
        except ClaraError as error:
            await reply(interaction, describe_error(lang, error))
            return
        log.info("%s (%s) cleared %s", interaction.user, interaction.user.id, conversation)
        await reply(interaction, t(lang, "reset_done", count=count))

    @tree.command(name="help", description=describe("How to talk with Clara"))
    async def help_command(interaction: discord.Interaction) -> None:
        await reply(interaction, t(lang_of(bot, interaction), "help"))
