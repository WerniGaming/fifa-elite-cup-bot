"""
Aushilfen-System: Spieler ohne Team koennen sich als Ersatzspieler ("Aushilfe")
anbieten (Positionen, Cup-Erfahrung, Liga-Erfahrung), Teams koennen gezielt
danach suchen ODER selbst eine Anfrage stellen ("Wir suchen eine Aushilfe fuer
Position X"). Kontakt laeuft ueber eine DM-Vorstellung, kein direkter
Nummern-/Handle-Austausch im oeffentlichen Kanal noetig.

Alles in EINEM sich selbst aktualisierenden Panel (Components V2), analog zum
Freundschaftsspiel-Panel - keine Nachrichtenflut im Kanal.
"""
from __future__ import annotations
import os

import discord
from discord import app_commands
from discord.ext import commands

from db import get_pool
from cogs.team_manager import get_team_for_user
from ui_helpers import success_embed, error_embed, info_embed

ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")

POSITIONS = [
    ("tw", "🧤 Torwart"),
    ("iv", "🛡️ Innenverteidiger"),
    ("av", "↔️ Außenverteidiger"),
    ("zdm", "⚓ Sechser (ZDM)"),
    ("zm", "🎯 Zentrales Mittelfeld"),
    ("zom", "🎨 Zehner (ZOM)"),
    ("fl", "🏃 Flügel"),
    ("st", "⚽ Stürmer"),
]
POSITION_LABELS = dict(POSITIONS)

EXPERIENCE_LEVELS = [
    ("keine", "🆕 Noch keine"),
    ("wenig", "🌱 Ein bis zwei"),
    ("mittel", "📈 Drei bis fünf"),
    ("viel", "🏆 Mehr als fünf"),
]
EXPERIENCE_LABELS = dict(EXPERIENCE_LEVELS)

MAX_LISTED_OFFERS = 20


def position_text(positions: list[str]) -> str:
    return " · ".join(POSITION_LABELS.get(p, p) for p in positions)


async def get_active_offers(guild_id: int) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT * FROM substitute_offers WHERE guild_id = $1 AND active = true ORDER BY created_at DESC LIMIT $2",
        guild_id, MAX_LISTED_OFFERS,
    )
    return [dict(r) for r in rows]


async def build_substitute_panel(guild: discord.Guild) -> discord.ui.LayoutView:
    offers = await get_active_offers(guild.id)

    intro = discord.ui.TextDisplay(
        "# 🔄 Aushilfen-Börse\n"
        "Kein Team, aber Bock zu spielen? Biete dich als Aushilfe an. Team braucht kurzfristig "
        "Verstärkung? Sucht gezielt oder stellt eine Anfrage — der Kontakt läuft diskret per DM."
    )

    if offers:
        lines = [f"### 🙋 Aktuell verfügbar ({len(offers)})"]
        for o in offers:
            exp = f"Cup: {EXPERIENCE_LABELS.get(o['cup_experience'], '?')} · Liga: {EXPERIENCE_LABELS.get(o['league_experience'], '?')}"
            lines.append(f"> <@{o['discord_id']}> — {position_text(o['positions'])}\n> -# {exp}")
        offers_block = discord.ui.TextDisplay("\n".join(lines))
    else:
        offers_block = discord.ui.TextDisplay("### 🙋 Aktuell verfügbar\n_Gerade bietet sich niemand an — sei der/die Erste!_")

    actions = discord.ui.ActionRow(
        discord.ui.Button(label="Als Aushilfe anbieten", emoji="🙋", style=discord.ButtonStyle.success, custom_id="sub:offer_start"),
        discord.ui.Button(label="Aushilfe finden", emoji="🔍", style=discord.ButtonStyle.primary, custom_id="sub:find_start"),
    )
    actions2 = discord.ui.ActionRow(
        discord.ui.Button(label="Team sucht Aushilfe", emoji="📢", style=discord.ButtonStyle.secondary, custom_id="sub:request_start"),
        discord.ui.Button(label="Mein Angebot zurückziehen", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="sub:withdraw"),
    )

    container = discord.ui.Container(
        intro,
        discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
        offers_block,
        discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
        actions,
        actions2,
        accent_color=discord.Color.blurple(),
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


async def refresh_substitute_panel(bot: commands.Bot, guild: discord.Guild):
    pool = get_pool()
    row = await pool.fetchrow("SELECT substitute_channel_id, substitute_panel_message_id FROM guild_settings WHERE guild_id = $1", guild.id)
    if not row or not row["substitute_channel_id"] or not row["substitute_panel_message_id"]:
        return
    channel = bot.get_channel(row["substitute_channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(row["substitute_channel_id"])
        except discord.HTTPException:
            return
    try:
        msg = await channel.fetch_message(row["substitute_panel_message_id"])
    except discord.HTTPException:
        return
    view = await build_substitute_panel(guild)
    try:
        await msg.edit(view=view)
    except discord.HTTPException:
        pass


class PositionExperienceSelectView(discord.ui.View):
    """Zwischenschritt vor jedem Modal: Discord-Modals koennen keine Select-Menus enthalten,
    deshalb hier erst Positionen + Erfahrung per Select waehlen, dann per Button ins Modal."""

    def __init__(self, *, ask_experience: bool, continue_label: str):
        super().__init__(timeout=180)
        self.positions: list[str] = []
        self.cup_experience: str | None = None
        self.league_experience: str | None = None
        self.ask_experience = ask_experience

        pos_select = discord.ui.Select(
            placeholder="Position(en) wählen...",
            min_values=1, max_values=len(POSITIONS),
            options=[discord.SelectOption(label=lbl, value=key) for key, lbl in POSITIONS],
        )
        pos_select.callback = self._on_positions
        self.add_item(pos_select)

        if ask_experience:
            cup_select = discord.ui.Select(
                placeholder="Cup-Erfahrung (wie viele Cups schon gespielt?)...",
                options=[discord.SelectOption(label=lbl, value=key) for key, lbl in EXPERIENCE_LEVELS],
            )
            cup_select.callback = self._on_cup_exp
            self.add_item(cup_select)

            league_select = discord.ui.Select(
                placeholder="Liga-Erfahrung...",
                options=[discord.SelectOption(label=lbl, value=key) for key, lbl in EXPERIENCE_LEVELS],
            )
            league_select.callback = self._on_league_exp
            self.add_item(league_select)

        self.continue_button = discord.ui.Button(label=continue_label, style=discord.ButtonStyle.success, disabled=True)
        self.continue_button.callback = self._on_continue
        self.add_item(self.continue_button)

    def _check_ready(self):
        ready = bool(self.positions) and (not self.ask_experience or (self.cup_experience and self.league_experience))
        self.continue_button.disabled = not ready

    async def _on_positions(self, interaction: discord.Interaction):
        self.positions = interaction.data["values"]
        self._check_ready()
        await interaction.response.edit_message(view=self)

    async def _on_cup_exp(self, interaction: discord.Interaction):
        self.cup_experience = interaction.data["values"][0]
        self._check_ready()
        await interaction.response.edit_message(view=self)

    async def _on_league_exp(self, interaction: discord.Interaction):
        self.league_experience = interaction.data["values"][0]
        self._check_ready()
        await interaction.response.edit_message(view=self)

    async def _on_continue(self, interaction: discord.Interaction):
        if self.ask_experience:
            await interaction.response.send_modal(OfferNoteModal(self.positions, self.cup_experience, self.league_experience))
        else:
            await interaction.response.send_modal(RequestDetailsModal(self.positions))


class OfferNoteModal(discord.ui.Modal, title="Als Aushilfe anbieten"):
    note_input = discord.ui.TextInput(
        label="Verfügbarkeit / Anmerkung (optional)", style=discord.TextStyle.paragraph,
        required=False, max_length=300, placeholder="z.B. nur abends, aktuell auf PS5, usw.",
    )

    def __init__(self, positions: list[str], cup_experience: str, league_experience: str):
        super().__init__()
        self.positions = positions
        self.cup_experience = cup_experience
        self.league_experience = league_experience

    async def on_submit(self, interaction: discord.Interaction):
        pool = get_pool()
        await pool.execute("UPDATE substitute_offers SET active = false WHERE guild_id = $1 AND discord_id = $2", interaction.guild_id, interaction.user.id)
        await pool.execute(
            """
            INSERT INTO substitute_offers (guild_id, discord_id, positions, cup_experience, league_experience, note)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            interaction.guild_id, interaction.user.id, self.positions, self.cup_experience, self.league_experience,
            self.note_input.value or None,
        )
        await refresh_substitute_panel(interaction.client, interaction.guild)
        await interaction.response.send_message(
            view=success_embed("Du bist jetzt als Aushilfe gelistet!", f"Positionen: {position_text(self.positions)}"),
            ephemeral=True,
        )


class RequestDetailsModal(discord.ui.Modal, title="Aushilfe gesucht"):
    description_input = discord.ui.TextInput(
        label="Kurze Beschreibung", style=discord.TextStyle.paragraph, max_length=300,
        placeholder="z.B. für heute Abend, dringend, welches Turnier, usw.",
    )

    def __init__(self, positions: list[str]):
        super().__init__()
        self.positions = positions

    async def on_submit(self, interaction: discord.Interaction):
        team = await get_team_for_user(interaction.guild_id, interaction.user.id)
        team_name = team["name"] if team else interaction.user.display_name

        pool = get_pool()
        row = await pool.fetchrow("SELECT substitute_channel_id FROM guild_settings WHERE guild_id = $1", interaction.guild_id)
        channel = interaction.channel
        if row and row["substitute_channel_id"]:
            ch = interaction.client.get_channel(row["substitute_channel_id"])
            if ch:
                channel = ch

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                f"### 📢 {team_name} sucht eine Aushilfe\n"
                f"**Position(en):** {position_text(self.positions)}\n"
                f"{self.description_input.value}\n\n"
                f"-# Gesucht von <@{interaction.user.id}>"
            ),
            discord.ui.ActionRow(discord.ui.Button(
                label="Ich hab Interesse", emoji="🙋", style=discord.ButtonStyle.success,
                custom_id=f"sub:respond:{interaction.user.id}",
            )),
            accent_color=discord.Color.orange(),
        ))
        await channel.send(view=view)
        await interaction.response.send_message(view=success_embed("Anfrage gepostet!"), ephemeral=True)
        await refresh_substitute_panel(interaction.client, interaction.guild)


class SubstituteFindSelect(discord.ui.View):
    def __init__(self, offers: list[dict]):
        super().__init__(timeout=180)
        select = discord.ui.Select(
            placeholder="Aushilfe auswählen, um Kontakt aufzunehmen...",
            options=[
                discord.SelectOption(
                    label=f"Aushilfe #{o['id']}",
                    description=f"{position_text(o['positions'])[:90]}",
                    value=str(o["id"]),
                )
                for o in offers
            ],
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        offer_id = int(interaction.data["values"][0])
        pool = get_pool()
        offer = await pool.fetchrow("SELECT * FROM substitute_offers WHERE id = $1 AND active = true", offer_id)
        if not offer:
            await interaction.response.edit_message(content="Dieses Angebot ist nicht mehr aktiv.", view=None)
            return

        team = await get_team_for_user(interaction.guild_id, interaction.user.id)
        team_name = team["name"] if team else interaction.user.display_name

        try:
            target_user = await interaction.client.fetch_user(offer["discord_id"])
            await target_user.send(
                view=info_embed(
                    "🙋 Ein Team hat Interesse an dir!",
                    f"**{team_name}** möchte dich als Aushilfe kontaktieren.\n"
                    f"Melde dich direkt bei <@{interaction.user.id}>!",
                )
            )
            sent = True
        except discord.HTTPException:
            sent = False

        if sent:
            await interaction.response.edit_message(
                content=f"✅ <@{offer['discord_id']}> wurde per DM benachrichtigt — meldet sich bei dir!", view=None
            )
        else:
            await interaction.response.edit_message(
                content=f"⚠️ DM konnte nicht zugestellt werden. Versuch's direkt: <@{offer['discord_id']}>", view=None
            )


class SubstitutesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="aushilfen_setup", description="Postet die Aushilfen-Börse in diesem Kanal (Admin)")
    async def aushilfen_setup(self, interaction: discord.Interaction):
        from permissions import is_tournament_admin
        if not await is_tournament_admin(interaction.user):
            await interaction.response.send_message(view=error_embed("Nur Admins können die Aushilfen-Börse einrichten."), ephemeral=True)
            return
        view = await build_substitute_panel(interaction.guild)
        await interaction.response.send_message(view=view)
        msg = await interaction.original_response()
        pool = get_pool()
        await pool.execute(
            "INSERT INTO guild_settings (guild_id, substitute_channel_id, substitute_panel_message_id) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id) DO UPDATE SET substitute_channel_id = $2, substitute_panel_message_id = $3",
            interaction.guild_id, interaction.channel_id, msg.id,
        )

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("sub:"):
            return

        parts = custom_id.split(":")
        action = parts[1]

        if action == "offer_start":
            await interaction.response.send_message(
                content="Welche Position(en) kannst du spielen, und wie viel Erfahrung bringst du mit?",
                view=PositionExperienceSelectView(ask_experience=True, continue_label="Weiter"),
                ephemeral=True,
            )

        elif action == "request_start":
            await interaction.response.send_message(
                content="Für welche Position(en) sucht ihr eine Aushilfe?",
                view=PositionExperienceSelectView(ask_experience=False, continue_label="Weiter"),
                ephemeral=True,
            )

        elif action == "find_start":
            offers = await get_active_offers(interaction.guild_id)
            if not offers:
                await interaction.response.send_message(view=error_embed("Aktuell bietet sich niemand als Aushilfe an."), ephemeral=True)
                return
            await interaction.response.send_message(
                content="Wen möchtest du kontaktieren?", view=SubstituteFindSelect(offers), ephemeral=True
            )

        elif action == "withdraw":
            pool = get_pool()
            result = await pool.execute(
                "UPDATE substitute_offers SET active = false WHERE guild_id = $1 AND discord_id = $2 AND active = true",
                interaction.guild_id, interaction.user.id,
            )
            if result.endswith(" 0"):
                await interaction.response.send_message(view=error_embed("Du hast gerade kein aktives Angebot."), ephemeral=True)
                return
            await refresh_substitute_panel(interaction.client, interaction.guild)
            await interaction.response.send_message(view=success_embed("Dein Angebot wurde zurückgezogen."), ephemeral=True)

        elif action == "respond":
            requester_id = int(parts[2])
            team = await get_team_for_user(interaction.guild_id, interaction.user.id)
            team_name = team["name"] if team else None
            offer = await get_pool().fetchrow(
                "SELECT * FROM substitute_offers WHERE guild_id = $1 AND discord_id = $2 AND active = true",
                interaction.guild_id, interaction.user.id,
            )
            extra = f" ({position_text(offer['positions'])})" if offer else ""
            try:
                requester = await interaction.client.fetch_user(requester_id)
                await requester.send(
                    view=info_embed(
                        "🙋 Jemand hat sich gemeldet!",
                        f"<@{interaction.user.id}>{extra} hat Interesse an deiner Aushilfen-Anfrage. Meldet euch!",
                    )
                )
                await interaction.response.send_message(view=success_embed("Gemeldet! Das Team wurde per DM informiert."), ephemeral=True)
            except discord.HTTPException:
                await interaction.response.send_message(
                    view=info_embed("Konnte keine DM senden", f"Meld dich direkt bei <@{requester_id}>."), ephemeral=True
                )


async def setup(bot: commands.Bot):
    await bot.add_cog(SubstitutesCog(bot))
