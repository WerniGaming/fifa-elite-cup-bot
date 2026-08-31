"""
Willkommens-Panel-Cog: postet eine einmalige, schoen gestaltete Eingangs-
Nachricht (Components V2) fuer neue Mitglieder.
"""
from __future__ import annotations
import os
import discord
from discord import app_commands
from discord.ext import commands

from permissions import is_tournament_admin
from ui_helpers import error_embed, success_embed

BANNER_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "welcome_banner.jpg")


class WelcomePanel(discord.ui.LayoutView):
    def __init__(self, team_manager_channel_id: int | None = None):
        super().__init__(timeout=None)
        team_manager_mention = f"<#{team_manager_channel_id}>" if team_manager_channel_id else "#team-manager"

        intro = discord.ui.TextDisplay(
            "# ⚽ Willkommen im FIFA Elite Cup\n"
            "Gut, dass du da bist! Hier findest du alles rund um unsere Turniere — Anmeldung, "
            "Ergebnisse, Live-Spielpläne und die ganze Community."
        )
        team_block = discord.ui.TextDisplay(
            "### 🧢 Dein Team\n"
            f"Alles zu deinem Verein läuft über {team_manager_mention}:\n"
            "> Team gründen und Stammdaten pflegen\n"
            "> Logo und Stream-Link hinterlegen\n"
            "> mit dem Team an einem offenen Turnier anmelden"
        )
        manager_block = discord.ui.TextDisplay(
            "### 👤 Vereinsmanager\n"
            "> du vertrittst dein Team nach außen\n"
            "> Ergebnisse laufen ausschließlich über den Bot, nie manuell\n"
            "> bis zu 2 Co-Manager möglich — die dürfen genauso Ergebnisse eintragen und das Team anmelden"
        )
        stats_block = discord.ui.TextDisplay(
            "### 📊 Statistik\n"
            "> `/club_stats` zeigt die aktuelle Form eines Teams\n"
            "> Titel, Bilanz und Tordifferenz bleiben dauerhaft gespeichert"
        )
        rules_block = discord.ui.TextDisplay(
            "### ⚠️ Bevor es losgeht\n"
            "> fairer Umgang miteinander ist Grundvoraussetzung\n"
            "> Entscheidungen der Turnierleitung sind final\n"
            "-# FIFA Elite Cup"
        )

        media_items = []
        if os.path.exists(BANNER_PATH):
            self.banner_file = discord.File(BANNER_PATH, filename="welcome_banner.jpg")
            media_items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(media=self.banner_file)))

        container = discord.ui.Container(
            *media_items,
            intro,
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
            team_block,
            discord.ui.Separator(),
            manager_block,
            discord.ui.Separator(),
            stats_block,
            discord.ui.Separator(),
            rules_block,
            accent_color=discord.Color.gold(),
        )
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
        await interaction.response.send_message(view=success_embed("Willkommens-Nachricht wird gepostet..."), ephemeral=True)
        panel = WelcomePanel(channel_id)
        if hasattr(panel, "banner_file"):
            await interaction.channel.send(view=panel, files=[panel.banner_file])
        else:
            await interaction.channel.send(view=panel)


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeCog(bot))
