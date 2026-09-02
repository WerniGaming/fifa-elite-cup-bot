"""
Feedback-Cog: Kategorie-Buttons im Feedback-Kanal -> Modal -> postet eine
Feedback-Karte (Components V2) mit Upvote-Button, damit die Community sehen
kann, was andere am wichtigsten finden, plus eine Admin-Statuspflege
(Offen/In Bearbeitung/Umgesetzt/Abgelehnt) ueber ein ephemerales Select-Menu
statt eines oeffentlichen Selects (Discord kann Komponenten nicht pro
Nutzer ausblenden, ein Admin-Select waere sonst fuer jeden klickbar
sichtbar - die Berechtigung wird stattdessen im Callback geprueft).
"""
from __future__ import annotations
import os

import discord
from discord import app_commands
from discord.ext import commands

from db import get_pool
from permissions import is_tournament_admin
from ui_helpers import error_embed

ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
FEEDBACK_BANNER_PATH = os.path.join(ASSETS_DIR, "feedback_banner.jpg")

CATEGORIES = {
    "bug": ("🐛", "Bug", discord.Color.red()),
    "vorschlag": ("💡", "Vorschlag", discord.Color.gold()),
    "lob": ("⭐", "Lob", discord.Color.green()),
    "beschwerde": ("😕", "Beschwerde", discord.Color.orange()),
}

STATUS = {
    "open": ("🕓", "Offen", discord.Color.greyple()),
    "in_progress": ("🔧", "In Bearbeitung", discord.Color.blue()),
    "done": ("✅", "Umgesetzt", discord.Color.green()),
    "rejected": ("❌", "Abgelehnt", discord.Color.red()),
}


def build_feedback_container(feedback: dict, vote_count: int) -> discord.ui.Container:
    emoji, label, _color = CATEGORIES.get(feedback["category"], ("💬", "Feedback", discord.Color.gold()))
    s_emoji, s_label, s_color = STATUS.get(feedback["status"], STATUS["open"])
    text = f"### {emoji} {label} — {feedback['title']}\n"
    if feedback["description"]:
        text += f"{feedback['description']}\n\n"
    text += f"**Status:** {s_emoji} {s_label}\n"
    text += f"-# von <@{feedback['author_discord_id']}> · #{feedback['id']}"

    row = discord.ui.ActionRow(
        discord.ui.Button(
            label=f"Hilfreich ({vote_count})", emoji="👍", style=discord.ButtonStyle.secondary,
            custom_id=f"feedback:vote:{feedback['id']}",
        ),
        discord.ui.Button(
            label="Status", emoji="🔧", style=discord.ButtonStyle.secondary,
            custom_id=f"feedback:managestatus:{feedback['id']}",
        ),
    )
    return discord.ui.Container(discord.ui.TextDisplay(text), row, accent_color=s_color)


async def get_vote_count(feedback_id: int) -> int:
    pool = get_pool()
    row = await pool.fetchrow("SELECT COUNT(*) AS c FROM feedback_votes WHERE feedback_id = $1", feedback_id)
    return row["c"] if row else 0


async def refresh_feedback_message(bot: commands.Bot, feedback_id: int):
    pool = get_pool()
    feedback = await pool.fetchrow("SELECT * FROM feedback WHERE id = $1", feedback_id)
    if not feedback or not feedback["message_id"] or not feedback["channel_id"]:
        return
    channel = bot.get_channel(feedback["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(feedback["channel_id"])
        except discord.HTTPException:
            return
    try:
        msg = await channel.fetch_message(feedback["message_id"])
    except discord.HTTPException:
        return
    vote_count = await get_vote_count(feedback_id)
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(build_feedback_container(dict(feedback), vote_count))
    try:
        await msg.edit(view=view)
    except discord.HTTPException:
        pass


class FeedbackModal(discord.ui.Modal):
    title_input = discord.ui.TextInput(label="Titel", max_length=100)
    description_input = discord.ui.TextInput(
        label="Beschreibung (optional)", style=discord.TextStyle.paragraph, required=False, max_length=1000
    )

    def __init__(self, category: str):
        super().__init__(title=f"Feedback: {CATEGORIES[category][1]}")
        self.category = category

    async def on_submit(self, interaction: discord.Interaction):
        pool = get_pool()
        row = await pool.fetchrow(
            "INSERT INTO feedback (guild_id, author_discord_id, category, title, description, channel_id) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
            interaction.guild_id, interaction.user.id, self.category,
            self.title_input.value, self.description_input.value or None, interaction.channel_id,
        )
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(build_feedback_container(dict(row), 0))
        await interaction.response.send_message(view=view)
        msg = await interaction.original_response()
        await pool.execute("UPDATE feedback SET message_id = $1 WHERE id = $2", msg.id, row["id"])


class FeedbackStatusView(discord.ui.View):
    def __init__(self, feedback_id: int):
        super().__init__(timeout=120)
        self.feedback_id = feedback_id
        select = discord.ui.Select(
            placeholder="Neuen Status wählen...",
            options=[discord.SelectOption(label=lbl, emoji=em, value=key) for key, (em, lbl, _c) in STATUS.items()],
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        new_status = interaction.data["values"][0]
        pool = get_pool()
        await pool.execute("UPDATE feedback SET status = $1 WHERE id = $2", new_status, self.feedback_id)
        await refresh_feedback_message(interaction.client, self.feedback_id)
        _emoji, label, _c = STATUS[new_status]
        await interaction.response.edit_message(content=f"Status auf **{label}** gesetzt.", view=None)


def build_feedback_panel() -> tuple[discord.ui.LayoutView, discord.File]:
    banner_file = discord.File(FEEDBACK_BANNER_PATH, filename="feedback_banner.jpg")
    intro = discord.ui.TextDisplay(
        "# 💬 Feedback\n"
        "Hast du einen Bug gefunden, einen Verbesserungsvorschlag, Lob oder eine Beschwerde? "
        "Wähl unten eine Kategorie — andere können deinem Feedback per 👍 zustimmen, "
        "und die Turnierleitung hält euch über den Status auf dem Laufenden."
    )
    row1 = discord.ui.ActionRow(
        discord.ui.Button(label="Bug melden", emoji="🐛", style=discord.ButtonStyle.danger, custom_id="feedback:new:bug"),
        discord.ui.Button(label="Vorschlag", emoji="💡", style=discord.ButtonStyle.primary, custom_id="feedback:new:vorschlag"),
    )
    row2 = discord.ui.ActionRow(
        discord.ui.Button(label="Lob", emoji="⭐", style=discord.ButtonStyle.success, custom_id="feedback:new:lob"),
        discord.ui.Button(label="Beschwerde", emoji="😕", style=discord.ButtonStyle.secondary, custom_id="feedback:new:beschwerde"),
    )
    container = discord.ui.Container(
        discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://feedback_banner.jpg")),
        intro,
        discord.ui.Separator(),
        row1,
        row2,
        accent_color=discord.Color.gold(),
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view, banner_file


class FeedbackCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("feedback:"):
            return

        parts = custom_id.split(":")
        action = parts[1]

        if action == "new":
            category = parts[2]
            await interaction.response.send_modal(FeedbackModal(category))
            return

        feedback_id = int(parts[2])

        if action == "vote":
            pool = get_pool()
            existing = await pool.fetchrow(
                "SELECT 1 FROM feedback_votes WHERE feedback_id = $1 AND discord_id = $2", feedback_id, interaction.user.id
            )
            if existing:
                await pool.execute(
                    "DELETE FROM feedback_votes WHERE feedback_id = $1 AND discord_id = $2", feedback_id, interaction.user.id
                )
            else:
                await pool.execute(
                    "INSERT INTO feedback_votes (feedback_id, discord_id) VALUES ($1, $2)", feedback_id, interaction.user.id
                )
            feedback = await pool.fetchrow("SELECT * FROM feedback WHERE id = $1", feedback_id)
            vote_count = await get_vote_count(feedback_id)
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(build_feedback_container(dict(feedback), vote_count))
            await interaction.response.edit_message(view=view)

        elif action == "managestatus":
            if not await is_tournament_admin(interaction.user):
                await interaction.response.send_message(view=error_embed("Nur Admins können den Status ändern."), ephemeral=True)
                return
            await interaction.response.send_message(
                content="Neuen Status für dieses Feedback wählen:", view=FeedbackStatusView(feedback_id), ephemeral=True
            )

    @app_commands.command(name="feedback_setup", description="Postet das Feedback-Panel in diesem Kanal (Admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def feedback_setup(self, interaction: discord.Interaction):
        view, banner_file = build_feedback_panel()
        pool = get_pool()
        await pool.execute(
            "INSERT INTO guild_settings (guild_id, feedback_channel_id) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET feedback_channel_id = $2",
            interaction.guild_id, interaction.channel_id,
        )
        await interaction.response.send_message(view=view, files=[banner_file])


async def setup(bot: commands.Bot):
    await bot.add_cog(FeedbackCog(bot))
