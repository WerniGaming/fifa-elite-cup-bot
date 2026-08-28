"""
Willkommens-Panel-Cog: postet eine einmalige, schoen gestaltete Eingangs-
Nachricht (Components V2) fuer neue Mitglieder.
"""
from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands

from permissions import is_tournament_admin
from ui_helpers import error_embed


class WelcomePanel(discord.ui.LayoutView):
    def __init__(self, team_manager_channel_id: int | None = None):
        super().__init__(timeout=None)
        team_manager_mention = f"<#{team_manager_channel_id}>" if team_manager_channel_id else "#team-manager"
        text = (
            "# 👋 WILLKOMMEN\n"
            "### Willkommen beim FIFA Elite Cup!\n"
            "Schön, dass du hier bist. Schau dich um, verfolge laufende Turniere live und werde Teil "
            "der Community.\n"
            "-----\n"
            "### » Team-Management\n"
            f"Willst du aktiv an Turnieren teilnehmen? Im Kanal {team_manager_mention} kannst du:\n"
            "› dein Team erstellen und verwalten\n"
            "› ein Logo hochladen und deinen Stream-Link hinterlegen\n"
            "› dich mit deinem Team für laufende Turniere anmelden\n"
            "-----\n"
            "### » Für Team-Manager\n"
            "› Du bist der offizielle Ansprechpartner für dein Team\n"
            "› Ergebnisse werden ausschließlich über den Bot gemeldet\n"
            "› Du kannst bis zu 2 Co-Manager ernennen, die ebenfalls Ergebnisse eintragen und "
            "dein Team anmelden dürfen\n"
            "-----\n"
            "### » Team-Statistiken\n"
            "› Jedes Team hat eine eigene Statistik-Übersicht (`/club_stats`)\n"
            "› Turniersiege, Bilanz und Torverhältnis werden dauerhaft gespeichert\n"
            "-----\n"
            "### » Wichtig\n"
            "› Respekt und Fairplay sind Pflicht\n"
            "› Entscheidungen der Turnierleitung sind verbindlich\n"
            "-# FIFA Elite Cup"
        )
        container = discord.ui.Container(discord.ui.TextDisplay(text), accent_color=discord.Color.gold())
        self.add_item(container)


class WelcomeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="welcome_panel_setup", description="Postet die Willkommens-Nachricht in diesem Kanal (Admin)")
    @app_commands.describe(team_manager_channel="Optional: Team-Manager-Kanal, wird in der Nachricht verlinkt")
    async def welcome_panel_setup(self, interaction: discord.Interaction, team_manager_channel: discord.TextChannel | None = None):
        if not await is_tournament_admin(interaction.user):
            await interaction.response.send_message(view=error_embed("Nur Admins können die Willkommens-Nachricht posten."), ephemeral=True)
            return
        channel_id = team_manager_channel.id if team_manager_channel else None
        await interaction.response.send_message(view=WelcomePanel(channel_id))


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeCog(bot))
