"""
Umfrage-Cog: schneller Interesse-Check ("Habt ihr Bock auf einen Cup am X?").
Admin stellt per Slash-Command eine Frage, jeder klickt Interessiert/Nicht
interessiert - die Karte zeigt live, wer (per Mention) mit dabei waere.
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
POLL_BANNER_PATH = os.path.join(ASSETS_DIR, "umfrage_banner.jpg")

MAX_NAMES_SHOWN = 25


def truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


async def fetch_poll(pool, poll_id: int) -> dict | None:
    row = await pool.fetchrow("SELECT * FROM polls WHERE id = $1", poll_id)
    return dict(row) if row else None


def build_poll_container(poll: dict, interested: list[int], not_interested: list[int]) -> discord.ui.Container:
    lines = [f"# 📊 {poll['question']}"]
    if poll["description"]:
        lines.append(poll["description"])
    lines.append("")

    def name_block(ids: list[int]) -> str:
        if not ids:
            return "_niemand bisher_"
        shown = ids[:MAX_NAMES_SHOWN]
        text = " ".join(f"<@{i}>" for i in shown)
        if len(ids) > MAX_NAMES_SHOWN:
            text += f" _und {len(ids) - MAX_NAMES_SHOWN} weitere_"
        return text

    lines.append(f"### ✅ Interessiert ({len(interested)})")
    lines.append(name_block(interested))
    lines.append("")
    lines.append(f"### ❌ Nicht interessiert ({len(not_interested)})")
    lines.append(name_block(not_interested))
    lines.append("")
    status = "🔒 Umfrage geschlossen" if poll["closed"] else "Klickt unten, um abzustimmen (Stimme kann geändert werden)"
    lines.append(f"-# {status} · erstellt von <@{poll['created_by']}> · Umfrage #{poll['id']}")

    items: list = [
        discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://umfrage_banner.jpg")),
        discord.ui.TextDisplay("\n".join(lines)),
    ]
    if not poll["closed"]:
        items.append(discord.ui.ActionRow(
            discord.ui.Button(label="Interessiert", emoji="✅", style=discord.ButtonStyle.success, custom_id=f"poll:vote:{poll['id']}:interested"),
            discord.ui.Button(label="Nicht interessiert", emoji="❌", style=discord.ButtonStyle.danger, custom_id=f"poll:vote:{poll['id']}:not_interested"),
            discord.ui.Button(label="Schließen", emoji="🔒", style=discord.ButtonStyle.secondary, custom_id=f"poll:close:{poll['id']}"),
        ))
    accent = discord.Color.greyple() if poll["closed"] else discord.Color.gold()
    return discord.ui.Container(*items, accent_color=accent)


async def refresh_poll_message(bot: commands.Bot, poll_id: int):
    pool = get_pool()
    poll = await fetch_poll(pool, poll_id)
    if not poll or not poll["message_id"] or not poll["channel_id"]:
        return
    votes = await pool.fetch("SELECT discord_id, choice FROM poll_votes WHERE poll_id = $1 ORDER BY voted_at", poll_id)
    interested = [v["discord_id"] for v in votes if v["choice"] == "interested"]
    not_interested = [v["discord_id"] for v in votes if v["choice"] == "not_interested"]

    channel = bot.get_channel(poll["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(poll["channel_id"])
        except discord.HTTPException:
            return
    try:
        msg = await channel.fetch_message(poll["message_id"])
    except discord.HTTPException:
        return
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(build_poll_container(poll, interested, not_interested))
    try:
        await msg.edit(view=view)
    except discord.HTTPException:
        pass


class PollModal(discord.ui.Modal, title="Umfrage erstellen"):
    question_input = discord.ui.TextInput(label="Frage", max_length=200, placeholder="Habt ihr Bock auf einen Cup am 15.09.?")
    description_input = discord.ui.TextInput(
        label="Beschreibung (optional)", style=discord.TextStyle.paragraph, required=False, max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction):
        pool = get_pool()
        row = await pool.fetchrow(
            "INSERT INTO polls (guild_id, question, description, created_by, channel_id) VALUES ($1, $2, $3, $4, $5) RETURNING *",
            interaction.guild_id, self.question_input.value, self.description_input.value or None,
            interaction.user.id, interaction.channel_id,
        )
        poll = dict(row)
        banner_file = discord.File(POLL_BANNER_PATH, filename="umfrage_banner.jpg")
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(build_poll_container(poll, [], []))
        await interaction.response.send_message(view=view, files=[banner_file])
        msg = await interaction.original_response()
        await pool.execute("UPDATE polls SET message_id = $1 WHERE id = $2", msg.id, poll["id"])


class PollsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("poll:"):
            return

        parts = custom_id.split(":")
        action = parts[1]
        poll_id = int(parts[2])
        pool = get_pool()
        poll = await fetch_poll(pool, poll_id)
        if not poll:
            await interaction.response.send_message(view=error_embed("Diese Umfrage existiert nicht mehr."), ephemeral=True)
            return

        if action == "vote":
            if poll["closed"]:
                await interaction.response.send_message(view=error_embed("Diese Umfrage ist bereits geschlossen."), ephemeral=True)
                return
            choice = parts[3]
            await pool.execute(
                "INSERT INTO poll_votes (poll_id, discord_id, choice) VALUES ($1, $2, $3) "
                "ON CONFLICT (poll_id, discord_id) DO UPDATE SET choice = $3, voted_at = now()",
                poll_id, interaction.user.id, choice,
            )
            votes = await pool.fetch("SELECT discord_id, choice FROM poll_votes WHERE poll_id = $1 ORDER BY voted_at", poll_id)
            interested = [v["discord_id"] for v in votes if v["choice"] == "interested"]
            not_interested = [v["discord_id"] for v in votes if v["choice"] == "not_interested"]
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(build_poll_container(poll, interested, not_interested))
            await interaction.response.edit_message(view=view)

        elif action == "close":
            is_owner = interaction.user.id == poll["created_by"]
            if not is_owner and not await is_tournament_admin(interaction.user):
                await interaction.response.send_message(view=error_embed("Nur der Ersteller oder ein Admin kann die Umfrage schließen."), ephemeral=True)
                return
            await pool.execute("UPDATE polls SET closed = true WHERE id = $1", poll_id)
            votes = await pool.fetch("SELECT discord_id, choice FROM poll_votes WHERE poll_id = $1 ORDER BY voted_at", poll_id)
            interested = [v["discord_id"] for v in votes if v["choice"] == "interested"]
            not_interested = [v["discord_id"] for v in votes if v["choice"] == "not_interested"]
            poll["closed"] = True
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(build_poll_container(poll, interested, not_interested))
            await interaction.response.edit_message(view=view)

    @app_commands.command(name="umfrage", description="Stellt eine Interessens-Umfrage (Admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def umfrage(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PollModal())


async def setup(bot: commands.Bot):
    await bot.add_cog(PollsCog(bot))
