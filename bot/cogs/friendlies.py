"""
Freundschaftsspiel-Cog: Vereinsmanager koennen im Freundschaftsspiel-Kanal ein
Gesuch posten (Datum/Uhrzeit + Notiz), andere Vereinsmanager sagen per Button
zu - der Bot vermittelt dann per DM zwischen beiden, damit sie die Details
selbst klaeren (kein eigenes Chat-System noetig).

Gleiches Baukasten-Prinzip wie ueberall sonst: persistentes Panel mit
Buttons, dynamische custom_ids ("friendly:<action>:<id>"), Routing ueber
einen generischen on_interaction-Listener statt gebundener View-Callbacks -
funktioniert dadurch auch nach einem Bot-Neustart ohne State-Verlust.
"""
from __future__ import annotations
import os

import discord
from discord import app_commands
from discord.ext import commands

from db import get_pool
from permissions import is_tournament_admin
from ui_helpers import error_embed
from cogs.team_manager import get_team_for_user

ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
FRIENDLY_BANNER_PATH = os.path.join(ASSETS_DIR, "friendly_banner.jpg")

STATUS_COLOR = {
    "open": discord.Color.gold(),
    "matched": discord.Color.green(),
    "withdrawn": discord.Color.greyple(),
}


def build_request_container(req: dict) -> discord.ui.Container:
    if req["status"] == "open":
        text = (
            f"### 🤝 {req['team_name']} sucht ein Freundschaftsspiel\n"
            f"🗓️ **Wann:** {req['proposed_time']}\n"
        )
        if req["note"]:
            text += f"📝 {req['note']}\n"
        text += f"\n-# Angefragt von <@{req['requested_by_discord_id']}> · Anfrage #{req['id']}"
        row = discord.ui.ActionRow(
            discord.ui.Button(label="Zusagen", emoji="✅", style=discord.ButtonStyle.success, custom_id=f"friendly:accept:{req['id']}"),
            discord.ui.Button(label="Zurückziehen", emoji="🗑️", style=discord.ButtonStyle.secondary, custom_id=f"friendly:withdraw:{req['id']}"),
        )
        items = [discord.ui.TextDisplay(text), row]
    elif req["status"] == "matched":
        text = (
            f"### ✅ Freundschaftsspiel vereinbart\n"
            f"**{req['team_name']}** 🆚 **{req['matched_team_name']}**\n"
            f"🗓️ **Wann:** {req['proposed_time']}\n"
        )
        if req["note"]:
            text += f"📝 {req['note']}\n"
        text += f"\n-# Details wurden per DM ausgetauscht · Anfrage #{req['id']}"
        items = [discord.ui.TextDisplay(text)]
    else:
        text = f"### 🗑️ Zurückgezogen\n~~{req['team_name']} suchte ein Freundschaftsspiel~~\n-# Anfrage #{req['id']}"
        items = [discord.ui.TextDisplay(text)]

    return discord.ui.Container(*items, accent_color=STATUS_COLOR.get(req["status"], discord.Color.gold()))


async def fetch_request_view_data(pool, request_id: int) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT fr.*, t.name AS team_name, mt.name AS matched_team_name
        FROM friendly_requests fr
        JOIN teams t ON t.id = fr.team_id
        LEFT JOIN teams mt ON mt.id = fr.matched_team_id
        WHERE fr.id = $1
        """,
        request_id,
    )
    return dict(row) if row else None


async def refresh_request_message(bot: commands.Bot, request_id: int):
    pool = get_pool()
    req = await fetch_request_view_data(pool, request_id)
    if not req or not req["message_id"] or not req["channel_id"]:
        return
    channel = bot.get_channel(req["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(req["channel_id"])
        except discord.HTTPException:
            return
    try:
        msg = await channel.fetch_message(req["message_id"])
    except discord.HTTPException:
        return
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(build_request_container(req))
    try:
        await msg.edit(view=view)
    except discord.HTTPException:
        pass


class FriendlyRequestModal(discord.ui.Modal, title="Freundschaftsspiel suchen"):
    time_input = discord.ui.TextInput(label="Wann? (z.B. Sa. 20:00 Uhr)", max_length=100)
    note_input = discord.ui.TextInput(
        label="Notiz (optional)", style=discord.TextStyle.paragraph, required=False, max_length=300,
        placeholder="z.B. Liga, Best-of, gesuchtes Niveau...",
    )

    def __init__(self, team_id: int):
        super().__init__()
        self.team_id = team_id

    async def on_submit(self, interaction: discord.Interaction):
        pool = get_pool()
        row = await pool.fetchrow(
            "INSERT INTO friendly_requests (guild_id, team_id, requested_by_discord_id, proposed_time, note, channel_id) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            interaction.guild_id, self.team_id, interaction.user.id,
            self.time_input.value, self.note_input.value or None, interaction.channel_id,
        )
        request_id = row["id"]
        req = await fetch_request_view_data(pool, request_id)
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(build_request_container(req))
        await interaction.response.send_message(view=view)
        msg = await interaction.original_response()
        await pool.execute("UPDATE friendly_requests SET message_id = $1 WHERE id = $2", msg.id, request_id)


def build_friendly_panel() -> tuple[discord.ui.LayoutView, discord.File]:
    banner_file = discord.File(FRIENDLY_BANNER_PATH, filename="friendly_banner.jpg")
    intro = discord.ui.TextDisplay(
        "# 🤝 Freundschaftsspiele\n"
        "Sucht dein Team ein Testspiel? Poste ein Gesuch mit Wunschtermin - sobald ein anderer "
        "Vereinsmanager zusagt, vermittelt der Bot euch per DM, damit ihr die Details klären könnt."
    )
    row = discord.ui.ActionRow(
        discord.ui.Button(label="Freundschaftsspiel suchen", emoji="🤝", style=discord.ButtonStyle.primary, custom_id="friendly:new"),
    )
    container = discord.ui.Container(
        discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://friendly_banner.jpg")),
        intro,
        discord.ui.Separator(),
        row,
        accent_color=discord.Color.gold(),
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view, banner_file


class FriendliesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("friendly:"):
            return

        parts = custom_id.split(":")
        action = parts[1]

        if action == "new":
            team = await get_team_for_user(interaction.guild_id, interaction.user.id)
            if not team:
                await interaction.response.send_message(
                    view=error_embed("Du hast noch kein Team.", "Registriere zuerst dein Team im Team-Manager-Panel."),
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(FriendlyRequestModal(team["id"]))
            return

        request_id = int(parts[2])
        pool = get_pool()
        req = await fetch_request_view_data(pool, request_id)
        if not req:
            await interaction.response.send_message(view=error_embed("Diese Anfrage existiert nicht mehr."), ephemeral=True)
            return

        if action == "withdraw":
            is_owner = interaction.user.id == req["requested_by_discord_id"]
            if not is_owner and not await is_tournament_admin(interaction.user):
                await interaction.response.send_message(view=error_embed("Nur der Ersteller kann diese Anfrage zurückziehen."), ephemeral=True)
                return
            if req["status"] != "open":
                await interaction.response.send_message(view=error_embed("Diese Anfrage ist nicht mehr offen."), ephemeral=True)
                return
            await pool.execute("UPDATE friendly_requests SET status = 'withdrawn' WHERE id = $1", request_id)
            req = await fetch_request_view_data(pool, request_id)
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(build_request_container(req))
            await interaction.response.edit_message(view=view)

        elif action == "accept":
            if req["status"] != "open":
                await interaction.response.send_message(view=error_embed("Diese Anfrage ist nicht mehr offen."), ephemeral=True)
                return
            accepting_team = await get_team_for_user(interaction.guild_id, interaction.user.id)
            if not accepting_team:
                await interaction.response.send_message(
                    view=error_embed("Du hast noch kein Team.", "Registriere zuerst dein Team im Team-Manager-Panel."),
                    ephemeral=True,
                )
                return
            if accepting_team["id"] == req["team_id"]:
                await interaction.response.send_message(view=error_embed("Du kannst nicht deiner eigenen Anfrage zusagen."), ephemeral=True)
                return

            await pool.execute(
                "UPDATE friendly_requests SET status = 'matched', matched_team_id = $1, matched_by_discord_id = $2 WHERE id = $3",
                accepting_team["id"], interaction.user.id, request_id,
            )
            req = await fetch_request_view_data(pool, request_id)
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(build_request_container(req))
            await interaction.response.edit_message(view=view)

            requester = interaction.guild.get_member(req["requested_by_discord_id"])
            dm_text = (
                f"🤝 **Freundschaftsspiel vereinbart!**\n"
                f"**{req['team_name']}** 🆚 **{accepting_team['name']}**\n"
                f"🗓️ Wunschtermin: {req['proposed_time']}\n"
                + (f"📝 Notiz: {req['note']}\n" if req["note"] else "")
                + f"\nSprecht die Details (Uhrzeit, Plattform, Format) am besten direkt hier ab."
            )
            for user_id in {req["requested_by_discord_id"], interaction.user.id}:
                try:
                    user = interaction.guild.get_member(user_id) or await interaction.client.fetch_user(user_id)
                    other_mention = f"<@{interaction.user.id}>" if user_id == req["requested_by_discord_id"] else f"<@{req['requested_by_discord_id']}>"
                    await user.send(dm_text + f"\nAnsprechpartner: {other_mention}")
                except discord.HTTPException:
                    pass

    @app_commands.command(name="friendly_setup", description="Postet das Freundschaftsspiel-Panel in diesem Kanal (Admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def friendly_setup(self, interaction: discord.Interaction):
        view, banner_file = build_friendly_panel()
        pool = get_pool()
        await pool.execute(
            "INSERT INTO guild_settings (guild_id, friendly_channel_id) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET friendly_channel_id = $2",
            interaction.guild_id, interaction.channel_id,
        )
        await interaction.response.send_message(view=view, files=[banner_file])


async def setup(bot: commands.Bot):
    await bot.add_cog(FriendliesCog(bot))
