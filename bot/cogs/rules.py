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


def build_cup_rules_view() -> tuple[discord.ui.LayoutView, discord.File]:
    banner_file = discord.File(CUP_REGELN_BANNER_PATH, filename="cup_regeln_banner.jpg")

    items: list = [
        discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://cup_regeln_banner.jpg")),
        _section(
            "# 🏆 FIFA Elite Cup — Regelwerk\n"
            "-# Mit der Anmeldung eures Teams akzeptiert ihr dieses Regelwerk."
        ),
        discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
        _section(
            "### 1️⃣ Teilnahme\n"
            "- Anmeldung nur über das Team-Manager-Panel (Team erstellen) und die "
            "Turnier-Anmeldung im jeweiligen Turnier-Kanal\n"
            "- Mindestens 6 Leute pro Team"
        ),
        discord.ui.Separator(),
        _section(
            "### 2️⃣ Allgemeines\n"
            "- Fairplay-Pflicht\n"
            "- Beleidigung = sofortige Disqualifikation\n"
            "- 5 Min (+/-) Toleranz, sonst Def-Win fürs Gegnerteam\n"
            "- Heimteam lädt ein"
        ),
        discord.ui.Separator(),
        _section(
            "### 3️⃣ Spielregeln\n"
            "- Modus: Pro Clubs (EA FC)\n"
            "- Kein Spieler auf der Torlinie bei Freistößen (gilt nicht für Bots, gilt nicht wenn "
            "es zu keinem Schuss kam - nur wenn der Spieler den gegnerischen TW behindert oder auf "
            "der eigenen TW-Linie steht und den Ball abfälscht)\n"
            "- Disconnect bei allen in der 2. Halbzeit: Halbzeit wird wiederholt (sofern zeitlich "
            "möglich), Ergebnis der 1. Halbzeit zählt weiter - muss von der Cupleitung genehmigt werden\n"
            "- Disconnect (Ingame-Minuten): 0–10 Min → Neustart bei 0:0 (Cupleitung entscheidet über "
            "Wertung) · ab 10 Min → weiterspielen"
        ),
        discord.ui.Separator(),
        _section(
            "### 4️⃣ Cup-System\n"
            "- Gruppenphase\n"
            "- Bei Unentschieden im KO: Verlängerung → Elfmeterschießen"
        ),
        discord.ui.Separator(),
        _section(
            "### 5️⃣ Ergebnisse\n"
            "- Ergebnisse werden über den Bot gemeldet (Button **Ergebnis eintragen** im Gruppen-/"
            "Bracket-Panel) - passende EA-Club-Spiele werden automatisch erkannt, sonst manuell eintragen\n"
            "- Der Gegner muss das gemeldete Ergebnis im Bot bestätigen, bevor es zählt\n"
            "- Wird kein Ergebnis gemeldet oder nicht bestätigt, entscheidet die Cupleitung über die Wertung"
        ),
        discord.ui.Separator(),
        _section(
            "### 6️⃣ Disqualifikation\n"
            "- Zu spät\n"
            "- Regelbruch\n"
            "- Mehrfaches Nichtantreten"
        ),
        discord.ui.Separator(),
        _section(
            "### 7️⃣ Kommunikation\n"
            "- Gegner frühzeitig anschreiben\n"
            "- Nur Captains/Vereinsmanager treffen Entscheidungen\n"
            "- Probleme bitte direkt an die Cupleitung"
        ),
        discord.ui.Separator(),
        _section(
            "### 8️⃣ Preis (momentan)\n"
            "🏆 FIFA Elite T-Cup Champion\n"
            "- Pokal-Grafik + Server-Ehrung"
        ),
        discord.ui.Separator(),
        _section(
            "### 9️⃣ Größenregelung (CM)\n"
            "**3er-Kette:** TW frei wählbar · IVs max. 1,87 · Rest max. 1,82\n"
            "**4er-Kette:** TW frei wählbar · 2 IV + 1 ZDM max. 1,87 · Rest max. 1,82"
        ),
        discord.ui.Separator(),
        _section(
            "### 🔟 Klarmachungen\n"
            "- Keine offiziellen EA/Publisher-Logos\n"
            "- Technische Probleme liegen außerhalb der Haftung des Veranstalters\n"
            "- Anmeldung = Zustimmung zu allen Regeln"
        ),
        discord.ui.Separator(),
        _section("➡️ Any-Pflicht\n➡️ Wechseln ist verboten\n➡️ TW-Pflicht"),
        discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
        _section("-# FIFA Elite Cup · Fairplay | Wettkampf | Community 🏆⚽"),
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

    @app_commands.command(name="cup_regeln", description="Postet das Cup-Regelwerk (Admin)")
    async def cup_regeln(self, interaction: discord.Interaction):
        if not await is_tournament_admin(interaction.user):
            await interaction.response.send_message(view=error_embed("Nur Admins können die Regeln posten."), ephemeral=True)
            return
        view, banner_file = build_cup_rules_view()
        await interaction.response.send_message(view=view, files=[banner_file])


async def setup(bot: commands.Bot):
    await bot.add_cog(RulesCog(bot))
