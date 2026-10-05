"""Clara's requests for permission, as private messages with Approve and Deny buttons.

When a request that nobody answered in time reaches the Discord surface (the server pushes it to the person's other
surfaces), the relay sends it as a DM. The buttons carry the request's number in their id, so they keep working after
the bot restarts; a click is sent to the server as that Discord account, and the server only accepts it from the person
the request was asked of. Whoever answers first decides: the message is edited when the request is settled elsewhere.
"""

from __future__ import annotations

import logging
import re

import discord

from .backend import ClaraError
from .texts import ENGLISH, t

log = logging.getLogger(__name__)

TEMPLATE = r"clara-approval:(?P<id>[0-9]+):(?P<action>approve|deny)"
REASON_SHOWN = 200


def ask_text(event: dict, lang: str) -> str:
    """The private message that asks for the permission."""
    reason = str(event.get("reason") or "").strip()
    return t(
        lang, "approval_ask", summary=event.get("summary", ""), resource=event.get("resource", ""),
        level=t(lang, f"level_{event.get('level', 'destructive')}"),
        reason=t(lang, "approval_reason", reason=reason[:REASON_SHOWN]) if reason else "",
    )


def outcome_text(approval: dict, lang: str) -> str:
    """How a settled request ends its message."""
    status = approval.get("status")
    key = {"done": "approval_done", "failed": "approval_failed", "denied": "approval_denied", "expired": "approval_expired"}.get(
        str(status), "approval_late"
    )
    if key == "approval_late":
        return t(lang, key)
    return t(lang, key, summary=approval.get("summary", ""), result=str(approval.get("result", ""))[:800]).strip()


class ApprovalButton(discord.ui.DynamicItem[discord.ui.Button], template=TEMPLATE):
    def __init__(self, approval_id: int, action: str, lang: str = ENGLISH):
        approve = action == "approve"
        super().__init__(discord.ui.Button(
            label=t(lang, "approval_approve" if approve else "approval_deny"),
            style=discord.ButtonStyle.success if approve else discord.ButtonStyle.secondary,
            custom_id=f"clara-approval:{approval_id}:{action}",
        ))
        self.approval_id = approval_id
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: re.Match[str], /):
        return cls(int(match["id"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        lang = bot.accounts.language(interaction.user.id)
        await interaction.response.defer()  # approving runs the action: it may take a few seconds
        try:
            settled = await bot.api.decide_approval(interaction.user.id, self.approval_id, self.action == "approve")
        except ClaraError as error:
            if error.status == 409:
                text = t(lang, "approval_late")
            elif error.status == 404:
                text = t(lang, "approval_unknown")
            else:
                text = t(lang, "approval_error", detail=error.detail)
            await interaction.followup.send(text, ephemeral=True)
            if error.status == 409:
                await self._clear(interaction)
            return
        await self._clear(interaction, outcome_text(settled, lang))

    @staticmethod
    async def _clear(interaction: discord.Interaction, text: str | None = None) -> None:
        """The buttons go (answered): the message says how it ended."""
        try:
            await interaction.message.edit(content=text or interaction.message.content, view=None)
        except discord.HTTPException as error:
            log.warning("cannot edit the request message: %s", error)


def approval_view(approval_id: int, lang: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ApprovalButton(approval_id, "approve", lang))
    view.add_item(ApprovalButton(approval_id, "deny", lang))
    return view
