"""
Aushilfen-Cog: EIN einziges, live aktualisiertes Uebersichts-Panel (gleiches Prinzip
wie das Freundschaftsspiel-Panel) statt einer eigenen Nachricht pro Angebot/Anfrage -
sonst verschwindet das Panel mit den Buttons nach oben.

Zwei Kategorien in einem Panel:
- "Aushilfen bieten sich an": Spieler ohne Team listen Position(en) + Erfahrung,
  Teams bewerben sich per Dropdown darauf.
- "Teams suchen eine Aushilfe": Teams schreiben Position(en) + Beschreibung aus,
  Spieler bewerben sich per Dropdown darauf.

In beiden Faellen waehlt der Ersteller (Spieler bzw. Team) ueber "Meine Einträge"
aus den Bewerbern eine Seite aus - danach bekommen beide eine DM mit Kontakt.
"""
from __future__ import annotations
import os

import discord
from discord import app_commands
from discord.ext import commands

from db import get_pool
from permissions import is_tournament_admin
from ui_helpers import error_embed
from cogs.team_manager import get_team_for_user, get_team_managers

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


def position_text(positions: list[str]) -> str:
    return " · ".join(POSITION_LABELS.get(p, p) for p in positions)


def truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------- Datenzugriff ----------

async def fetch_open_offers(pool, guild_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT * FROM substitute_offers WHERE guild_id = $1 AND status = 'open' ORDER BY created_at", guild_id
    )
    result = []
    for r in rows:
        count = await pool.fetchval("SELECT COUNT(*) FROM substitute_offer_candidates WHERE offer_id = $1", r["id"])
        result.append({**dict(r), "candidate_count": count})
    return result


async def fetch_open_requests(pool, guild_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT sr.*, t.name AS team_name FROM substitute_requests sr JOIN teams t ON t.id = sr.team_id "
        "WHERE sr.guild_id = $1 AND sr.status = 'open' ORDER BY sr.created_at",
        guild_id,
    )
    result = []
    for r in rows:
        count = await pool.fetchval("SELECT COUNT(*) FROM substitute_request_candidates WHERE request_id = $1", r["id"])
        result.append({**dict(r), "candidate_count": count})
    return result


# ---------- Panel-Aufbau ----------

def build_panel_view(offers: list[dict], requests: list[dict]) -> discord.ui.LayoutView:
    items: list = [
        discord.ui.TextDisplay(
            "# 🔄 Aushilfen-Börse\n"
            "-# Kein Team, aber Bock zu spielen? Oder ein Team braucht kurzfristig Verstärkung? Hier trifft man sich."
        ),
        discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
    ]

    apply_offer_options, apply_request_options = [], []

    items.append(discord.ui.TextDisplay(f"### 🙋 Aushilfen bieten sich an ({len(offers)})"))
    if not offers:
        items.append(discord.ui.TextDisplay("_Aktuell bietet sich niemand an - sei die/der Erste!_"))
    else:
        for o in offers:
            exp = f"Cup-Erfahrung: {o['cup_experience']} · Liga-Erfahrung: {o['league_experience']}"
            cand_txt = f" · {o['candidate_count']} Interessent(en)" if o["candidate_count"] else ""
            lines = [f"> <@{o['discord_id']}> — {position_text(o['positions'])}{cand_txt}", f"> -# {exp}"]
            if o["note"]:
                lines.append(f"> 📝 {o['note']}")
            items.append(discord.ui.TextDisplay("\n".join(lines)))
            apply_offer_options.append(discord.SelectOption(
                label=truncate(f"Aushilfe: {position_text(o['positions'])}", 100), value=str(o["id"]),
            ))

    items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
    items.append(discord.ui.TextDisplay(f"### 📢 Teams suchen eine Aushilfe ({len(requests)})"))
    if not requests:
        items.append(discord.ui.TextDisplay("_Aktuell sucht kein Team - schreib gern eine Anfrage aus!_"))
    else:
        for r in requests:
            cand_txt = f" · {r['candidate_count']} Bewerber" if r["candidate_count"] else ""
            lines = [f"> **{r['team_name']}** sucht {position_text(r['positions'])}{cand_txt}"]
            if r["description"]:
                lines.append(f"> 📝 {r['description']}")
            items.append(discord.ui.TextDisplay("\n".join(lines)))
            apply_request_options.append(discord.SelectOption(
                label=truncate(f"{r['team_name']} — {position_text(r['positions'])}", 100), value=str(r["id"]),
            ))

    items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
    if apply_offer_options:
        items.append(discord.ui.ActionRow(discord.ui.Select(
            placeholder="Als Team für eine Aushilfe bewerben...", custom_id="sub:apply_offer",
            options=apply_offer_options[:25],
        )))
    if apply_request_options:
        items.append(discord.ui.ActionRow(discord.ui.Select(
            placeholder="Als Spieler auf eine Team-Anfrage bewerben...", custom_id="sub:apply_request",
            options=apply_request_options[:25],
        )))
    items.append(discord.ui.ActionRow(
        discord.ui.Button(label="Als Aushilfe anbieten", emoji="🙋", style=discord.ButtonStyle.success, custom_id="sub:offer_start"),
        discord.ui.Button(label="Team sucht Aushilfe", emoji="📢", style=discord.ButtonStyle.primary, custom_id="sub:request_start"),
        discord.ui.Button(label="Meine Einträge", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="sub:mine"),
    ))

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_color=discord.Color.blurple()))
    return view


async def refresh_substitute_panel(bot: commands.Bot, guild: discord.Guild):
    pool = get_pool()
    settings = await pool.fetchrow("SELECT substitute_channel_id, substitute_panel_message_id FROM guild_settings WHERE guild_id = $1", guild.id)
    if not settings or not settings["substitute_channel_id"]:
        return
    channel = guild.get_channel(settings["substitute_channel_id"])
    if channel is None:
        try:
            channel = await guild.fetch_channel(settings["substitute_channel_id"])
        except discord.HTTPException:
            return

    if settings["substitute_panel_message_id"]:
        try:
            old_msg = await channel.fetch_message(settings["substitute_panel_message_id"])
            await old_msg.delete()
        except discord.HTTPException:
            pass

    offers = await fetch_open_offers(pool, guild.id)
    requests = await fetch_open_requests(pool, guild.id)
    view = build_panel_view(offers, requests)
    try:
        msg = await channel.send(view=view)
        await pool.execute("UPDATE guild_settings SET substitute_panel_message_id = $1 WHERE guild_id = $2", msg.id, guild.id)
    except discord.HTTPException:
        pass


# ---------- Zwischenschritt: Position(en) waehlen, dann Modal ----------

class PositionSelectView(discord.ui.View):
    """Discord-Modals koennen keine Select-Menus enthalten, deshalb hier erst Positionen
    per Select waehlen, danach per Button ins Modal (dort auch Cup-/Liga-Erfahrung als
    Freitext, damit z.B. 'VPG' oder ein konkreter Liga-/Cup-Name reinpasst)."""

    def __init__(self, *, ask_experience: bool):
        super().__init__(timeout=180)
        self.positions: list[str] = []
        self.ask_experience = ask_experience

        pos_select = discord.ui.Select(
            placeholder="Position(en) wählen...", min_values=1, max_values=len(POSITIONS),
            options=[discord.SelectOption(label=lbl, value=key) for key, lbl in POSITIONS],
        )
        pos_select.callback = self._on_positions
        self.add_item(pos_select)

        self.continue_button = discord.ui.Button(label="Weiter", style=discord.ButtonStyle.success, disabled=True)
        self.continue_button.callback = self._on_continue
        self.add_item(self.continue_button)

    async def _on_positions(self, interaction: discord.Interaction):
        self.positions = interaction.data["values"]
        self.continue_button.disabled = False
        await interaction.response.edit_message(view=self)

    async def _on_continue(self, interaction: discord.Interaction):
        if self.ask_experience:
            await interaction.response.send_modal(OfferNoteModal(self.positions))
        else:
            await interaction.response.send_modal(RequestDescriptionModal(self.positions))


class OfferNoteModal(discord.ui.Modal, title="Als Aushilfe anbieten"):
    cup_input = discord.ui.TextInput(
        label="Cup-Erfahrung", max_length=100, placeholder="z.B. 3 Cups gespielt, 1x Sieger, o.ä.",
    )
    league_input = discord.ui.TextInput(
        label="Liga-Erfahrung", max_length=100, placeholder="z.B. VPG, eigene Liga-Namen, o.ä.",
    )
    note_input = discord.ui.TextInput(
        label="Verfügbarkeit / Anmerkung (optional)", style=discord.TextStyle.paragraph,
        required=False, max_length=300, placeholder="z.B. nur abends, aktuell auf PS5, usw.",
    )

    def __init__(self, positions: list[str]):
        super().__init__()
        self.positions = positions

    async def on_submit(self, interaction: discord.Interaction):
        pool = get_pool()
        existing = await pool.fetchrow(
            "SELECT 1 FROM substitute_offers WHERE guild_id = $1 AND discord_id = $2 AND status = 'open'",
            interaction.guild_id, interaction.user.id,
        )
        if existing:
            await interaction.response.send_message(
                view=error_embed("Du hast bereits ein offenes Angebot.", "Zieh es über 'Meine Einträge' zurück, um ein neues zu erstellen."),
                ephemeral=True,
            )
            return
        await pool.execute(
            "INSERT INTO substitute_offers (guild_id, discord_id, positions, cup_experience, league_experience, note) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            interaction.guild_id, interaction.user.id, self.positions, self.cup_input.value, self.league_input.value,
            self.note_input.value or None,
        )
        await interaction.response.send_message(content=f"✅ Du bist jetzt als Aushilfe gelistet ({position_text(self.positions)}).", ephemeral=True)
        await refresh_substitute_panel(interaction.client, interaction.guild)


class RequestDescriptionModal(discord.ui.Modal, title="Aushilfe gesucht"):
    description_input = discord.ui.TextInput(
        label="Beschreibung (optional)", style=discord.TextStyle.paragraph, required=False, max_length=300,
        placeholder="z.B. für heute Abend, dringend, welches Turnier, usw.",
    )

    def __init__(self, positions: list[str]):
        super().__init__()
        self.positions = positions

    async def on_submit(self, interaction: discord.Interaction):
        team = await get_team_for_user(interaction.guild_id, interaction.user.id)
        if not team:
            await interaction.response.send_message(
                view=error_embed("Du hast noch kein Team.", "Registriere zuerst dein Team im Team-Manager-Panel."), ephemeral=True
            )
            return
        pool = get_pool()
        existing = await pool.fetchrow(
            "SELECT 1 FROM substitute_requests WHERE guild_id = $1 AND team_id = $2 AND status = 'open'",
            interaction.guild_id, team["id"],
        )
        if existing:
            await interaction.response.send_message(
                view=error_embed("Ihr habt bereits eine offene Anfrage.", "Zieh sie über 'Meine Einträge' zurück, um eine neue zu erstellen."),
                ephemeral=True,
            )
            return
        await pool.execute(
            "INSERT INTO substitute_requests (guild_id, team_id, requested_by_discord_id, positions, description) "
            "VALUES ($1, $2, $3, $4, $5)",
            interaction.guild_id, team["id"], interaction.user.id, self.positions, self.description_input.value or None,
        )
        await interaction.response.send_message(content=f"✅ Anfrage erstellt ({position_text(self.positions)}).", ephemeral=True)
        await refresh_substitute_panel(interaction.client, interaction.guild)


# ---------- "Meine Einträge" (ephemeral) ----------

class MyEntriesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @classmethod
    async def build(cls, pool, guild_id: int, user_id: int) -> "MyEntriesView | None":
        self = cls()
        has_any = False

        my_offer = await pool.fetchrow(
            "SELECT * FROM substitute_offers WHERE guild_id = $1 AND discord_id = $2 AND status = 'open'", guild_id, user_id
        )
        if my_offer:
            has_any = True
            cands = await pool.fetch(
                "SELECT soc.*, t.name AS team_name FROM substitute_offer_candidates soc JOIN teams t ON t.id = soc.team_id WHERE soc.offer_id = $1",
                my_offer["id"],
            )
            if cands:
                select = discord.ui.Select(
                    placeholder=truncate(f"Mein Angebot ({len(cands)} Interessent(en)) — Team wählen", 150),
                    options=[discord.SelectOption(label=truncate(c["team_name"], 100), value=str(c["team_id"])) for c in cands],
                )
                select.callback = self._make_choose_offer_callback(my_offer["id"])
                self.add_item(select)
            withdraw_offer = discord.ui.Select(
                placeholder="Mein Angebot zurückziehen...",
                options=[discord.SelectOption(label=truncate(f"Angebot: {position_text(my_offer['positions'])}", 100), value=str(my_offer["id"]))],
            )
            withdraw_offer.callback = self._withdraw_offer_callback
            self.add_item(withdraw_offer)

        team = await get_team_for_user(guild_id, user_id)
        my_requests = await pool.fetch(
            "SELECT * FROM substitute_requests WHERE guild_id = $1 AND team_id = $2 AND status = 'open'", guild_id, team["id"]
        ) if team else []
        for req in my_requests:
            has_any = True
            cands = await pool.fetch(
                "SELECT * FROM substitute_request_candidates WHERE request_id = $1", req["id"]
            )
            if cands:
                select = discord.ui.Select(
                    placeholder=truncate(f"Anfrage ({len(cands)} Bewerber) — Spieler wählen", 150),
                    options=[discord.SelectOption(label=f"<@{c['discord_id']}>"[:100] or str(c["discord_id"]), value=str(c["discord_id"])) for c in cands],
                )
                select.callback = self._make_choose_request_callback(req["id"])
                self.add_item(select)
            withdraw_req = discord.ui.Select(
                placeholder="Anfrage zurückziehen...",
                options=[discord.SelectOption(label=truncate(f"Anfrage: {position_text(req['positions'])}", 100), value=str(req["id"]))],
            )
            withdraw_req.callback = self._withdraw_request_callback
            self.add_item(withdraw_req)

        return self if has_any else None

    def _make_choose_offer_callback(self, offer_id: int):
        async def callback(interaction: discord.Interaction):
            team_id = int(interaction.data["values"][0])
            pool = get_pool()
            candidate = await pool.fetchrow("SELECT * FROM substitute_offer_candidates WHERE offer_id = $1 AND team_id = $2", offer_id, team_id)
            team = await pool.fetchrow("SELECT * FROM teams WHERE id = $1", team_id)
            await pool.execute("UPDATE substitute_offers SET status = 'matched', matched_team_id = $1 WHERE id = $2", team_id, offer_id)
            await refresh_substitute_panel(interaction.client, interaction.guild)

            managers = await get_team_managers(team_id)
            contact = "\n".join(f"- <@{m['discord_id']}>" for m in managers) or f"<@{candidate['discord_id']}>"
            try:
                await interaction.user.send(f"🔄 Du hast dich für **{team['name']}** entschieden!\n\n**Kontakt:**\n{contact}")
            except discord.HTTPException:
                pass
            for m in managers:
                try:
                    user = interaction.guild.get_member(m["discord_id"]) or await interaction.client.fetch_user(m["discord_id"])
                    await user.send(f"🔄 Eure Bewerbung um eine Aushilfe war erfolgreich! Kontakt: <@{interaction.user.id}>")
                except discord.HTTPException:
                    pass
            await interaction.response.edit_message(content=f"✅ **{team['name']}** wurde ausgewählt.", view=None)
        return callback

    def _make_choose_request_callback(self, request_id: int):
        async def callback(interaction: discord.Interaction):
            player_id = int(interaction.data["values"][0])
            pool = get_pool()
            request = await pool.fetchrow("SELECT * FROM substitute_requests WHERE id = $1", request_id)
            team = await pool.fetchrow("SELECT * FROM teams WHERE id = $1", request["team_id"])
            await pool.execute("UPDATE substitute_requests SET status = 'matched', matched_discord_id = $1 WHERE id = $2", player_id, request_id)
            await refresh_substitute_panel(interaction.client, interaction.guild)

            managers = await get_team_managers(request["team_id"])
            contact = "\n".join(f"- <@{m['discord_id']}>" for m in managers)
            try:
                player = interaction.guild.get_member(player_id) or await interaction.client.fetch_user(player_id)
                await player.send(f"🔄 **{team['name']}** möchte dich als Aushilfe!\n\n**Kontakt:**\n{contact}")
            except discord.HTTPException:
                pass
            await interaction.response.edit_message(content=f"✅ <@{player_id}> wurde ausgewählt.", view=None)
        return callback

    async def _withdraw_offer_callback(self, interaction: discord.Interaction):
        offer_id = int(interaction.data["values"][0])
        pool = get_pool()
        await pool.execute("UPDATE substitute_offers SET status = 'withdrawn' WHERE id = $1 AND status = 'open'", offer_id)
        await refresh_substitute_panel(interaction.client, interaction.guild)
        await interaction.response.edit_message(content="🗑️ Angebot zurückgezogen.", view=None)

    async def _withdraw_request_callback(self, interaction: discord.Interaction):
        request_id = int(interaction.data["values"][0])
        pool = get_pool()
        await pool.execute("UPDATE substitute_requests SET status = 'withdrawn' WHERE id = $1 AND status = 'open'", request_id)
        await refresh_substitute_panel(interaction.client, interaction.guild)
        await interaction.response.edit_message(content="🗑️ Anfrage zurückgezogen.", view=None)


# ---------- Cog ----------

class SubstitutesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("sub:"):
            return
        action = custom_id.split(":", 1)[1]
        pool = get_pool()

        if action == "offer_start":
            await interaction.response.send_message(
                content="Welche Position(en) kannst du spielen, und wie viel Erfahrung bringst du mit?",
                view=PositionSelectView(ask_experience=True), ephemeral=True,
            )

        elif action == "request_start":
            team = await get_team_for_user(interaction.guild_id, interaction.user.id)
            if not team:
                await interaction.response.send_message(
                    view=error_embed("Du hast noch kein Team.", "Registriere zuerst dein Team im Team-Manager-Panel."), ephemeral=True
                )
                return
            await interaction.response.send_message(
                content="Für welche Position(en) sucht ihr eine Aushilfe?",
                view=PositionSelectView(ask_experience=False), ephemeral=True,
            )

        elif action == "apply_offer":
            offer_id = int(interaction.data["values"][0])
            offer = await pool.fetchrow("SELECT * FROM substitute_offers WHERE id = $1 AND status = 'open'", offer_id)
            if not offer:
                await interaction.response.send_message(view=error_embed("Dieses Angebot ist nicht mehr offen."), ephemeral=True)
                return
            team = await get_team_for_user(interaction.guild_id, interaction.user.id)
            if not team:
                await interaction.response.send_message(
                    view=error_embed("Du hast noch kein Team.", "Registriere zuerst dein Team im Team-Manager-Panel."), ephemeral=True
                )
                return
            existing = await pool.fetchrow("SELECT 1 FROM substitute_offer_candidates WHERE offer_id = $1 AND team_id = $2", offer_id, team["id"])
            if existing:
                await interaction.response.send_message(view=error_embed("Ihr habt euch dafür bereits beworben."), ephemeral=True)
                return
            await pool.execute(
                "INSERT INTO substitute_offer_candidates (offer_id, team_id, discord_id) VALUES ($1, $2, $3)",
                offer_id, team["id"], interaction.user.id,
            )
            await interaction.response.send_message(content=f"✅ Interesse an <@{offer['discord_id']}> hinterlegt.", ephemeral=True)
            await refresh_substitute_panel(interaction.client, interaction.guild)
            try:
                target = interaction.guild.get_member(offer["discord_id"]) or await interaction.client.fetch_user(offer["discord_id"])
                await target.send(f"🔄 **{team['name']}** hat Interesse an dir als Aushilfe! Wähle über 'Meine Einträge' im Aushilfen-Kanal aus.")
            except discord.HTTPException:
                pass

        elif action == "apply_request":
            request_id = int(interaction.data["values"][0])
            request = await pool.fetchrow("SELECT * FROM substitute_requests WHERE id = $1 AND status = 'open'", request_id)
            if not request:
                await interaction.response.send_message(view=error_embed("Diese Anfrage ist nicht mehr offen."), ephemeral=True)
                return
            existing = await pool.fetchrow(
                "SELECT 1 FROM substitute_request_candidates WHERE request_id = $1 AND discord_id = $2", request_id, interaction.user.id
            )
            if existing:
                await interaction.response.send_message(view=error_embed("Du hast dich dafür bereits beworben."), ephemeral=True)
                return
            await pool.execute(
                "INSERT INTO substitute_request_candidates (request_id, discord_id) VALUES ($1, $2)", request_id, interaction.user.id
            )
            await interaction.response.send_message(content="✅ Bewerbung eingetragen.", ephemeral=True)
            await refresh_substitute_panel(interaction.client, interaction.guild)
            team = await pool.fetchrow("SELECT * FROM teams WHERE id = $1", request["team_id"])
            managers = await get_team_managers(request["team_id"])
            for m in managers:
                try:
                    user = interaction.guild.get_member(m["discord_id"]) or await interaction.client.fetch_user(m["discord_id"])
                    await user.send(f"📋 <@{interaction.user.id}> möchte für **{team['name']}** aushelfen. Wähle über 'Meine Einträge' im Aushilfen-Kanal aus.")
                except discord.HTTPException:
                    pass

        elif action == "mine":
            view = await MyEntriesView.build(pool, interaction.guild_id, interaction.user.id)
            if not view:
                await interaction.response.send_message(content="Du hast aktuell kein offenes Angebot oder keine offene Anfrage.", ephemeral=True)
                return
            await interaction.response.send_message(content="Deine offenen Einträge:", view=view, ephemeral=True)

    @app_commands.command(name="aushilfen_setup", description="Richtet die Aushilfen-Börse in diesem Kanal ein (Admin)")
    async def aushilfen_setup(self, interaction: discord.Interaction):
        if not await is_tournament_admin(interaction.user):
            await interaction.response.send_message(view=error_embed("Nur Admins können die Aushilfen-Börse einrichten."), ephemeral=True)
            return
        pool = get_pool()
        await pool.execute(
            "INSERT INTO guild_settings (guild_id, substitute_channel_id, substitute_panel_message_id) VALUES ($1, $2, NULL) "
            "ON CONFLICT (guild_id) DO UPDATE SET substitute_channel_id = $2, substitute_panel_message_id = NULL",
            interaction.guild_id, interaction.channel_id,
        )
        await interaction.response.send_message(content="Aushilfen-Börse wird eingerichtet...", ephemeral=True)
        await refresh_substitute_panel(interaction.client, interaction.guild)


async def setup(bot: commands.Bot):
    await bot.add_cog(SubstitutesCog(bot))
