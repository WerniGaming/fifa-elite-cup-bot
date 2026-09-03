"""
Freundschaftsspiel-Cog: Vereinsmanager koennen im Freundschaftsspiel-Kanal ein
Gesuch mit MEHREREN Zeit-Slots posten (z.B. "Sa 18 Uhr", "Sa 20 Uhr", "So 15 Uhr").
Fuer jeden Slot koennen sich mehrere andere Teams bewerben ("Zusagen"); der
Ersteller waehlt anschliessend pro Slot aus, welches Team er nimmt - erst dann
gilt der Slot als vereinbart und beide Manager bekommen eine DM zur
Kontaktaufnahme. Nicht ausgewaehlte Bewerber fuer diesen Slot werden per DM
informiert, dass ein anderes Team den Zuschlag bekommen hat.

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

MAX_SLOTS = 4


# ---------- Datenzugriff ----------

async def fetch_full_request(pool, request_id: int) -> dict | None:
    req = await pool.fetchrow("SELECT fr.*, t.name AS team_name FROM friendly_requests fr JOIN teams t ON t.id = fr.team_id WHERE fr.id = $1", request_id)
    if not req:
        return None
    slots = await pool.fetch(
        """
        SELECT fs.*, mt.name AS matched_team_name,
               (SELECT COUNT(*) FROM friendly_candidates fc WHERE fc.slot_id = fs.id) AS candidate_count
        FROM friendly_slots fs
        LEFT JOIN teams mt ON mt.id = fs.matched_team_id
        WHERE fs.request_id = $1
        ORDER BY fs.id
        """,
        request_id,
    )
    return {**dict(req), "slots": [dict(s) for s in slots]}


def truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------- Kartenaufbau ----------

def build_request_container(req: dict) -> discord.ui.Container:
    lines = [f"### 🤝 {req['team_name']} sucht Freundschaftsspiele"]
    if req["note"]:
        lines.append(f"📝 {req['note']}")
    lines.append("")

    buttons = []
    any_open = False
    for slot in req["slots"]:
        if slot["status"] == "matched":
            lines.append(f"✅ **{slot['proposed_time']}** — vereinbart mit **{slot['matched_team_name']}**")
        else:
            any_open = True
            cand_txt = f" · {slot['candidate_count']} Bewerber" if slot["candidate_count"] else ""
            lines.append(f"🕓 **{slot['proposed_time']}** — offen{cand_txt}")
            buttons.append(
                discord.ui.Button(
                    label=truncate(f"Zusagen: {slot['proposed_time']}", 80), emoji="✅",
                    style=discord.ButtonStyle.success, custom_id=f"friendly:acceptslot:{slot['id']}",
                )
            )

    lines.append(f"\n-# Angefragt von <@{req['requested_by_discord_id']}> · Anfrage #{req['id']}")
    items: list = [discord.ui.TextDisplay("\n".join(lines))]

    if req["status"] == "open":
        rows = []
        for i in range(0, len(buttons), 5):
            rows.append(discord.ui.ActionRow(*buttons[i:i + 5]))
        items.extend(rows)
        manage_row = [discord.ui.Button(label="Bewerber verwalten", emoji="📋", style=discord.ButtonStyle.primary, custom_id=f"friendly:candidates:{req['id']}")]
        if any_open:
            manage_row.append(discord.ui.Button(label="Zurückziehen", emoji="🗑️", style=discord.ButtonStyle.secondary, custom_id=f"friendly:withdraw:{req['id']}"))
        items.append(discord.ui.ActionRow(*manage_row))
        accent = discord.Color.gold()
    elif req["status"] == "withdrawn":
        items = [discord.ui.TextDisplay("### 🗑️ Zurückgezogen\n" + "\n".join(lines[1:]))]
        accent = discord.Color.greyple()
    else:  # closed - alle Slots vergeben
        accent = discord.Color.green()

    return discord.ui.Container(*items, accent_color=accent)


async def refresh_request_message(bot: commands.Bot, request_id: int):
    pool = get_pool()
    req = await fetch_full_request(pool, request_id)
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


async def maybe_close_request(pool, request_id: int):
    open_count = await pool.fetchval(
        "SELECT COUNT(*) FROM friendly_slots WHERE request_id = $1 AND status = 'open'", request_id
    )
    if open_count == 0:
        await pool.execute("UPDATE friendly_requests SET status = 'closed' WHERE id = $1 AND status = 'open'", request_id)


# ---------- Modal: neue Anfrage ----------

class FriendlyRequestModal(discord.ui.Modal, title="Freundschaftsspiele suchen"):
    times_input = discord.ui.TextInput(
        label=f"Zeitpunkte (1 pro Zeile, max. {MAX_SLOTS})", style=discord.TextStyle.paragraph, max_length=300,
        placeholder="Sa. 18:00 Uhr\nSa. 20:00 Uhr\nSo. 15:00 Uhr",
    )
    note_input = discord.ui.TextInput(
        label="Notiz (optional)", style=discord.TextStyle.paragraph, required=False, max_length=300,
        placeholder="z.B. Liga, Best-of, gesuchtes Niveau...",
    )

    def __init__(self, team_id: int):
        super().__init__()
        self.team_id = team_id

    async def on_submit(self, interaction: discord.Interaction):
        times = [t.strip() for t in self.times_input.value.splitlines() if t.strip()][:MAX_SLOTS]
        if not times:
            await interaction.response.send_message(view=error_embed("Mindestens ein Zeitpunkt wird benötigt."), ephemeral=True)
            return

        pool = get_pool()
        row = await pool.fetchrow(
            "INSERT INTO friendly_requests (guild_id, team_id, requested_by_discord_id, note, channel_id) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id",
            interaction.guild_id, self.team_id, interaction.user.id, self.note_input.value or None, interaction.channel_id,
        )
        request_id = row["id"]
        for t in times:
            await pool.execute(
                "INSERT INTO friendly_slots (request_id, proposed_time) VALUES ($1, $2)", request_id, t
            )

        req = await fetch_full_request(pool, request_id)
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(build_request_container(req))
        await interaction.response.send_message(view=view)
        msg = await interaction.original_response()
        await pool.execute("UPDATE friendly_requests SET message_id = $1 WHERE id = $2", msg.id, request_id)


# ---------- Ephemerale Bewerber-Verwaltung (nur Ersteller) ----------

class CandidateChooseView(discord.ui.View):
    """Ein Select pro offenem Slot mit Bewerbern - Wahl bestaetigt sofort den Slot."""

    def __init__(self, request_id: int, slots_with_candidates: list[dict]):
        super().__init__(timeout=180)
        self.request_id = request_id
        for slot in slots_with_candidates:
            options = [
                discord.SelectOption(label=truncate(c["team_name"], 100), value=str(c["team_id"]))
                for c in slot["candidates"]
            ]
            select = discord.ui.Select(
                placeholder=truncate(f"{slot['proposed_time']} — Team wählen...", 150), options=options,
            )
            select.callback = self._make_callback(slot["id"])
            self.add_item(select)

    def _make_callback(self, slot_id: int):
        async def callback(interaction: discord.Interaction):
            team_id = int(interaction.data["values"][0])
            pool = get_pool()
            slot = await pool.fetchrow("SELECT * FROM friendly_slots WHERE id = $1", slot_id)
            if not slot or slot["status"] != "open":
                await interaction.response.edit_message(content="Dieser Slot ist nicht mehr offen.", view=None)
                return

            candidate = await pool.fetchrow(
                "SELECT * FROM friendly_candidates WHERE slot_id = $1 AND team_id = $2", slot_id, team_id
            )
            chosen_team = await pool.fetchrow("SELECT * FROM teams WHERE id = $1", team_id)
            request = await pool.fetchrow("SELECT * FROM friendly_requests WHERE id = $1", slot["request_id"])
            requester_team = await pool.fetchrow("SELECT * FROM teams WHERE id = $1", request["team_id"])

            await pool.execute(
                "UPDATE friendly_slots SET status = 'matched', matched_team_id = $1, matched_by_discord_id = $2 WHERE id = $3",
                team_id, candidate["discord_id"] if candidate else None, slot_id,
            )
            await maybe_close_request(pool, slot["request_id"])
            await refresh_request_message(interaction.client, slot["request_id"])

            other_candidates = await pool.fetch(
                "SELECT * FROM friendly_candidates WHERE slot_id = $1 AND team_id != $2", slot_id, team_id
            )
            dm_text = (
                f"🤝 **Freundschaftsspiel vereinbart!**\n"
                f"**{requester_team['name']}** 🆚 **{chosen_team['name']}**\n"
                f"🗓️ {slot['proposed_time']}\n\nSprecht die Details (Uhrzeit, Plattform, Format) am besten direkt hier ab."
            )
            for user_id, mention_other in (
                (request["requested_by_discord_id"], chosen_team["id"]),
                (candidate["discord_id"] if candidate else None, requester_team["id"]),
            ):
                if not user_id:
                    continue
                other_name = chosen_team["name"] if mention_other == chosen_team["id"] else requester_team["name"]
                try:
                    user = interaction.guild.get_member(user_id) or await interaction.client.fetch_user(user_id)
                    await user.send(dm_text + f"\nGegner: **{other_name}**")
                except discord.HTTPException:
                    pass
            for oc in other_candidates:
                try:
                    user = interaction.guild.get_member(oc["discord_id"]) or await interaction.client.fetch_user(oc["discord_id"])
                    await user.send(
                        f"😕 Für **{requester_team['name']}** um **{slot['proposed_time']}** wurde sich für ein anderes Team entschieden."
                    )
                except discord.HTTPException:
                    pass

            await interaction.response.edit_message(
                content=f"✅ Für **{slot['proposed_time']}** wurde **{chosen_team['name']}** bestätigt.", view=None
            )
        return callback


# ---------- Panel ----------

def build_friendly_panel() -> tuple[discord.ui.LayoutView, discord.File]:
    banner_file = discord.File(FRIENDLY_BANNER_PATH, filename="friendly_banner.jpg")
    intro = discord.ui.TextDisplay(
        "# 🤝 Freundschaftsspiele\n"
        "Sucht dein Team ein Testspiel? Poste ein Gesuch mit bis zu "
        f"{MAX_SLOTS} Wunschterminen gleichzeitig. Mehrere Teams können sich pro Termin "
        "bewerben - ihr wählt danach selbst aus, mit wem ihr spielt. Der Bot vermittelt "
        "euch anschließend per DM, damit ihr die Details klären könnt."
    )
    row = discord.ui.ActionRow(
        discord.ui.Button(label="Freundschaftsspiele suchen", emoji="🤝", style=discord.ButtonStyle.primary, custom_id="friendly:new"),
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
        pool = get_pool()

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

        entity_id = int(parts[2])

        if action == "acceptslot":
            slot = await pool.fetchrow("SELECT * FROM friendly_slots WHERE id = $1", entity_id)
            if not slot or slot["status"] != "open":
                await interaction.response.send_message(view=error_embed("Dieser Zeitslot ist nicht mehr offen."), ephemeral=True)
                return
            request = await pool.fetchrow("SELECT * FROM friendly_requests WHERE id = $1", slot["request_id"])
            accepting_team = await get_team_for_user(interaction.guild_id, interaction.user.id)
            if not accepting_team:
                await interaction.response.send_message(
                    view=error_embed("Du hast noch kein Team.", "Registriere zuerst dein Team im Team-Manager-Panel."),
                    ephemeral=True,
                )
                return
            if accepting_team["id"] == request["team_id"]:
                await interaction.response.send_message(view=error_embed("Du kannst nicht deiner eigenen Anfrage zusagen."), ephemeral=True)
                return
            existing = await pool.fetchrow(
                "SELECT 1 FROM friendly_candidates WHERE slot_id = $1 AND team_id = $2", entity_id, accepting_team["id"]
            )
            if existing:
                await interaction.response.send_message(view=error_embed("Ihr habt euch für diesen Termin bereits beworben."), ephemeral=True)
                return
            await pool.execute(
                "INSERT INTO friendly_candidates (slot_id, team_id, discord_id) VALUES ($1, $2, $3)",
                entity_id, accepting_team["id"], interaction.user.id,
            )
            await refresh_request_message(interaction.client, slot["request_id"])
            await interaction.response.send_message(
                content=f"✅ Bewerbung für **{slot['proposed_time']}** eingetragen. Der Ersteller wählt daraus ein Team aus.",
                ephemeral=True,
            )
            try:
                requester = interaction.guild.get_member(request["requested_by_discord_id"]) or await interaction.client.fetch_user(request["requested_by_discord_id"])
                await requester.send(
                    f"📋 **{accepting_team['name']}** möchte euer Freundschaftsspiel am **{slot['proposed_time']}** annehmen. "
                    f"Wähle über 'Bewerber verwalten' auf der Anfrage-Karte aus, mit wem ihr spielt."
                )
            except discord.HTTPException:
                pass

        elif action == "candidates":
            request = await fetch_full_request(pool, entity_id)
            if not request:
                await interaction.response.send_message(view=error_embed("Diese Anfrage existiert nicht mehr."), ephemeral=True)
                return
            is_owner = interaction.user.id == request["requested_by_discord_id"]
            if not is_owner and not await is_tournament_admin(interaction.user):
                await interaction.response.send_message(view=error_embed("Nur der Ersteller kann Bewerber verwalten."), ephemeral=True)
                return
            slots_with_candidates = []
            for slot in request["slots"]:
                if slot["status"] != "open":
                    continue
                cands = await pool.fetch(
                    "SELECT fc.*, t.name AS team_name FROM friendly_candidates fc JOIN teams t ON t.id = fc.team_id WHERE fc.slot_id = $1",
                    slot["id"],
                )
                if cands:
                    slots_with_candidates.append({**slot, "candidates": [dict(c) for c in cands]})
            if not slots_with_candidates:
                await interaction.response.send_message(content="Noch keine Bewerbungen für offene Termine.", ephemeral=True)
                return
            await interaction.response.send_message(
                content="Wählt pro Termin das Team aus, mit dem ihr spielen wollt:",
                view=CandidateChooseView(entity_id, slots_with_candidates),
                ephemeral=True,
            )

        elif action == "withdraw":
            request = await pool.fetchrow("SELECT * FROM friendly_requests WHERE id = $1", entity_id)
            if not request:
                await interaction.response.send_message(view=error_embed("Diese Anfrage existiert nicht mehr."), ephemeral=True)
                return
            is_owner = interaction.user.id == request["requested_by_discord_id"]
            if not is_owner and not await is_tournament_admin(interaction.user):
                await interaction.response.send_message(view=error_embed("Nur der Ersteller kann diese Anfrage zurückziehen."), ephemeral=True)
                return
            if request["status"] != "open":
                await interaction.response.send_message(view=error_embed("Diese Anfrage ist nicht mehr offen."), ephemeral=True)
                return
            await pool.execute("UPDATE friendly_requests SET status = 'withdrawn' WHERE id = $1", entity_id)
            req = await fetch_full_request(pool, entity_id)
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(build_request_container(req))
            await interaction.response.edit_message(view=view)

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
