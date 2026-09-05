"""
Staff-Uebersicht: postet ein persistentes Panel, das alle Mitglieder der 5
Staff-Raenge (Trial Moderator, Moderator, Head Moderator, Administrator,
Verwaltung) auflistet. Aktualisiert sich automatisch, sobald sich bei
irgendjemandem eine dieser Rollen aendert - kein manuelles Neu-Posten noetig.

Raenge werden ueber ihren NAMEN aufgeloest (nicht ueber eine gespeicherte
Rollen-ID), damit ein versehentlich geloeschtes/neu erstelltes Rollen-Objekt
(andere ID) automatisch weiter funktioniert.
"""
from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands

from db import get_pool
from permissions import is_tournament_admin
from ui_helpers import success_embed, error_embed

STAFF_RANKS = ["Verwaltung", "Administrator", "Head Moderator", "Moderator", "Trial Moderator"]


async def build_staff_overview_view(guild: discord.Guild) -> discord.ui.LayoutView:
    blocks = ["# 👥 Staff-Übersicht", "-# Aktualisiert sich automatisch bei Rollenänderungen", ""]
    for rank_name in STAFF_RANKS:
        role = discord.utils.get(guild.roles, name=rank_name)
        blocks.append(f"### {rank_name}")
        if not role or not role.members:
            blocks.append("_niemand_")
        else:
            blocks.append(", ".join(m.mention for m in role.members))
        blocks.append("")

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay("\n".join(blocks)), accent_color=discord.Color.gold()))
    return view


async def refresh_staff_overview(bot: commands.Bot, guild: discord.Guild):
    pool = get_pool()
    row = await pool.fetchrow("SELECT * FROM staff_overview_panel WHERE guild_id = $1", guild.id)
    if not row:
        return
    channel = bot.get_channel(row["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(row["channel_id"])
        except discord.HTTPException:
            return
    try:
        msg = await channel.fetch_message(row["message_id"])
    except discord.HTTPException:
        return
    view = await build_staff_overview_view(guild)
    try:
        await msg.edit(view=view)
    except discord.HTTPException:
        pass


class StaffOverviewCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="staff_overview_setup", description="Postet die auto-aktualisierende Staff-Übersicht in diesem Kanal (Admin)")
    async def staff_overview_setup(self, interaction: discord.Interaction):
        if not await is_tournament_admin(interaction.user):
            await interaction.response.send_message(view=error_embed("Nur Admins können die Staff-Übersicht einrichten."), ephemeral=True)
            return
        await interaction.response.send_message(view=success_embed("Staff-Übersicht wird gepostet..."), ephemeral=True)
        view = await build_staff_overview_view(interaction.guild)
        msg = await interaction.channel.send(view=view)
        pool = get_pool()
        await pool.execute(
            "INSERT INTO staff_overview_panel (guild_id, channel_id, message_id) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id) DO UPDATE SET channel_id = $2, message_id = $3",
            interaction.guild_id, interaction.channel_id, msg.id,
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if {r.id for r in before.roles} == {r.id for r in after.roles}:
            return
        tracked_role_ids = {r.id for r in after.guild.roles if r.name in STAFF_RANKS}
        if not tracked_role_ids:
            return
        before_ids, after_ids = {r.id for r in before.roles}, {r.id for r in after.roles}
        if (before_ids ^ after_ids) & tracked_role_ids:
            await refresh_staff_overview(self.bot, after.guild)


async def setup(bot: commands.Bot):
    await bot.add_cog(StaffOverviewCog(bot))
