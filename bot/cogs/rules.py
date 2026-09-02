"""
Regelwerk-Cog: postet die Community- und Cup-Regeln als Components-V2-Nachricht
mit Banner, im gleichen dunkel/gold-Design wie alle anderen Panels.

Bewusst als einfache on-demand Slash-Commands (kein persistentes Panel mit
Message-Tracking noetig) - ein Admin fuehrt den Befehl einmal im Regel-Kanal
aus, das Ergebnis bleibt dort stehen wie jede normale Nachricht.
"""
from __future__ import annotations
import os

import discord
from discord import app_commands
from discord.ext import commands

from permissions import is_tournament_admin
from ui_helpers import error_embed

ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
REGELN_BANNER_PATH = os.path.join(ASSETS_DIR, "regeln_banner.jpg")
CUP_REGELN_BANNER_PATH = os.path.join(ASSETS_DIR, "cup_regeln_banner.jpg")


def _section(text: str) -> discord.ui.TextDisplay:
    return discord.ui.TextDisplay(text)


def build_community_rules_view() -> tuple[discord.ui.LayoutView, discord.File]:
    banner_file = discord.File(REGELN_BANNER_PATH, filename="regeln_banner.jpg")

    items: list = [
        discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://regeln_banner.jpg")),
        _section(
            "# 📜 FIFA Elite Discord — Community-Regeln\n"
            "-# Mit dem Betreten des Servers akzeptiert jedes Mitglied diese Regeln."
        ),
        discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
        _section(
            "### 🤝 1. Respekt & Verhalten\n"
            "- Respektvoller Umgang mit allen Mitgliedern\n"
            "- Keine Beleidigungen, Provokationen, Diskriminierung oder toxisches Verhalten\n"
            "- Kritik sachlich und ruhig äußern"
        ),
        discord.ui.Separator(),
        _section(
            "### 🚫 2. Erwähnungen & Spam\n"
            "- ❌ Kein @everyone oder @here Pingen\n"
            "- Keine unnötigen Markierungen von Teamleitungen oder Admins\n"
            "- ❌ Kein Spam (Nachrichten, Emojis, GIFs, Copy-Paste)"
        ),
        discord.ui.Separator(),
        _section(
            "### 🖼️ 3. Sticker, GIFs & Medien\n"
            "- ❌ Sticker sind nicht erlaubt\n"
            "- GIFs & Bilder nur, wenn sie thematisch passen\n"
            "- Keine NSFW-, beleidigenden oder provokativen Inhalte"
        ),
        discord.ui.Separator(),
        _section(
            "### 💬 4. Chat-Nutzung\n"
            "- Nutzt die richtigen Channels für eure Anliegen\n"
            "- Keine Diskussionen über Entscheidungen der Turnierleitung im öffentlichen Chat\n"
            "- Probleme & Regelverstöße privat an die Turnierleitung"
        ),
        discord.ui.Separator(),
        _section(
            "### ⚠️ 5. Regelverstöße\n"
            "- Regelverstöße werden nicht öffentlich diskutiert\n"
            "- Meldungen bitte privat + mit Beweis (Clip/Screenshot)\n"
            "- Die Turnierleitung hat das letzte Wort"
        ),
        discord.ui.Separator(),
        _section(
            "### 🔨 6. Sanktionen\n"
            "Je nach Schwere des Verstoßes:\n"
            "- Verwarnung\n"
            "- Chat-Mute\n"
            "- Temporärer Bann\n"
            "- Permanenter Bann\n"
            "-# Verhalten einzelner Spieler kann Konsequenzen für den gesamten Verein haben"
        ),
        discord.ui.Separator(),
        _section(
            "### 🏆 7. Fairplay & Vorbildfunktion\n"
            "- Vereinsmanager tragen Verantwortung für ihr Team\n"
            "- Unsportliches Verhalten schadet der gesamten Community\n"
            "- Fairplay steht immer an erster Stelle"
        ),
        discord.ui.Separator(),
        _section(
            "### 🌙 8. Nachtruhe\n"
            "Ab 23:00 Uhr gilt Nachtruhe — bitte Sprachkanäle & Chat-Aktivität entsprechend anpassen."
        ),
        discord.ui.Separator(),
        _section(
            "### ✅ 9. Zustimmung\n"
            "Mit dem Betreten des Servers akzeptiert jedes Mitglied diese Regeln.\n"
            "Regeländerungen werden über <#1425113185488343110> bekannt gegeben."
        ),
        discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
        _section("-# FIFA Elite League · Respekt | Fairplay | Community 🏆⚽"),
    ]

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_color=discord.Color.gold()))
    return view, banner_file


class RulesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="regeln", description="Postet die Community-Regeln (Admin)")
    async def regeln(self, interaction: discord.Interaction):
        if not await is_tournament_admin(interaction.user):
            await interaction.response.send_message(view=error_embed("Nur Admins können die Regeln posten."), ephemeral=True)
            return
        view, banner_file = build_community_rules_view()
        await interaction.response.send_message(view=view, files=[banner_file])


async def setup(bot: commands.Bot):
    await bot.add_cog(RulesCog(bot))
