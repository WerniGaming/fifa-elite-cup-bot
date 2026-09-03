"""
Freundschaftsspiel-Cog: EIN einziges, live aktualisiertes Uebersichts-Panel
(gleiches Prinzip wie der Kalender/die Stream-Uebersicht) statt einer
eigenen Nachricht pro Anfrage - sonst verschwindet das eigentliche Panel
mit den Buttons nach oben, sobald ein paar Anfragen gepostet wurden.

Bewerben laeuft ueber EIN Dropdown ("Fuer welchen Termin bewerben?") statt
eines eigenen Buttons pro Zeitslot - deutlich weniger Knopf-Chaos, vor
allem wenn mehrere Teams gleichzeitig mehrere Termine ausschreiben.

Der Ersteller (bzw. jeder Manager des anfragenden Teams) verwaltet seine
eigenen offenen Anfragen ueber "Meine Anfragen" - dort waehlt er per
Dropdown, welches Bewerber-Team er fuer welchen Termin nimmt, oder zieht
die ganze Anfrage zurueck. Erst bei Auswahl gilt der Slot als vereinbart,
beide Manager bekommen dann eine DM.
"""
from __future__ import annotations
import os

import discord
from discord import app_commands
from discord.ext import commands

from db import get_pool
from ui_helpers import error_embed
from cogs.team_manager import get_team_for_user

ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
FRIENDLY_BANNER_PATH = os.path.join(ASSETS_DIR, "friendly_banner.jpg")

MAX_SLOTS = 4


def truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------- Datenzugriff ----------

async def fetch_open_requests(pool, guild_id: int) -> list[dict]:
    requests = await pool.fetch(
        "SELECT fr.*, t.name AS team_name FROM friendly_requests fr JOIN teams t ON t.id = fr.team_id "
        "WHERE fr.guild_id = $1 AND fr.status = 'open' ORDER BY fr.created_at",
        guild_id,
    )
    result = []
    for req in requests:
        slots = await pool.fetch(
            """
            SELECT fs.*, mt.name AS matched_team_name,
                   (SELECT COUNT(*) FROM friendly_candidates fc WHERE fc.slot_id = fs.id) AS candidate_count
            FROM friendly_slots fs
            LEFT JOIN teams mt ON mt.id = fs.matched_team_id
            WHERE fs.request_id = $1
            ORDER BY fs.id
            """,
            req["id"],
        )
        result.append({**dict(req), "slots": [dict(s) for s in slots]})
    return result


async def fetch_open_requests_for_team(pool, team_id: int) -> list[dict]:
    all_reqs = await fetch_open_requests(pool, (await pool.fetchrow("SELECT guild_id FROM teams WHERE id = $1", team_id))["guild_id"])
    return [r for r in all_reqs if r["team_id"] == team_id]


async def maybe_close_request(pool, request_id: int):
    open_count = await pool.fetchval(
        "SELECT COUNT(*) FROM friendly_slots WHERE request_id = $1 AND status = 'open'", request_id
    )
    if open_count == 0:
        await pool.execute("UPDATE friendly_requests SET status = 'closed' WHERE id = $1 AND status = 'open'", request_id)


# ---------- Panel-Aufbau ----------

def build_panel_view(open_requests: list[dict]) -> tuple[discord.ui.LayoutView, discord.File]:
    banner_file = discord.File(FRIENDLY_BANNER_PATH, filename="friendly_banner.jpg")
    items: list = [
        discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://friendly_banner.jpg")),
        discord.ui.TextDisplay(
            "# 🤝 Freundschaftsspiele\n"
            "-# Vereinsmanager können hier Testspiele ausschreiben und sich für Termine anderer Teams bewerben."
        ),
        discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
    ]

    apply_options = []
    if not open_requests:
        items.append(discord.ui.TextDisplay("_Aktuell sucht kein Team ein Freundschaftsspiel - sei das erste!_"))
    else:
        for req in open_requests:
            lines = [f"### 🤝 {req['team_name']}"]
            if req["note"]:
                lines.append(f"📝 {req['note']}")
            for slot in req["slots"]:
                if slot["status"] == "matched":
                    lines.append(f"✅ **{slot['proposed_time']}** — vereinbart mit **{slot['matched_team_name']}**")
                else:
                    cand_txt = f" · {slot['candidate_count']} Bewerber" if slot["candidate_count"] else ""
                    lines.append(f"🕓 **{slot['proposed_time']}** — offen{cand_txt}")
                    apply_options.append(discord.SelectOption(
                        label=truncate(f"{req['team_name']} — {slot['proposed_time']}", 100),
                        value=str(slot["id"]),
                    ))
            items.append(discord.ui.TextDisplay("\n".join(lines)))
            items.append(discord.ui.Separator())
        items.pop()  # letzten Separator entfernen

    items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
    if apply_options:
        items.append(discord.ui.ActionRow(
            discord.ui.Select(
                placeholder="Für einen Termin bewerben...", custom_id="friendly:apply",
                options=apply_options[:25],
            )
        ))
    items.append(discord.ui.ActionRow(
        discord.ui.Button(label="Neue Anfrage", emoji="🤝", style=discord.ButtonStyle.primary, custom_id="friendly:new"),
        discord.ui.Button(label="Meine Anfragen", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="friendly:myrequests"),
    ))

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_color=discord.Color.gold()))
    return view, banner_file


async def refresh_friendly_panel(bot: commands.Bot, guild: discord.Guild):
    pool = get_pool()
    settings = await pool.fetchrow("SELECT friendly_channel_id, friendly_panel_message_id FROM guild_settings WHERE guild_id = $1", guild.id)
    if not settings or not settings["friendly_channel_id"]:
        return
    channel = guild.get_channel(settings["friendly_channel_id"])
    if channel is None:
        try:
            channel = await guild.fetch_channel(settings["friendly_channel_id"])
        except discord.HTTPException:
            return

    if settings["friendly_panel_message_id"]:
        try:
            old_msg = await channel.fetch_message(settings["friendly_panel_message_id"])
            await old_msg.delete()
        except discord.HTTPException:
            pass

    open_requests = await fetch_open_requests(pool, guild.id)
    view, banner_file = build_panel_view(open_requests)
    try:
        msg = await channel.send(view=view, files=[banner_file])
        await pool.execute("UPDATE guild_settings SET friendly_panel_message_id = $1 WHERE guild_id = $2", msg.id, guild.id)
    except discord.HTTPException:
        pass


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
            await pool.execute("INSERT INTO friendly_slots (request_id, proposed_time) VALUES ($1, $2)", request_id, t)

        await interaction.response.send_message(
            content=f"✅ Anfrage mit {len(times)} Termin(en) erstellt - das Panel wurde aktualisiert.", ephemeral=True
        )
        await refresh_friendly_panel(interaction.client, interaction.guild)


# ---------- "Meine Anfragen" (ephemeral) ----------

class MyRequestsView(discord.ui.View):
    """Pro offenem Slot mit Bewerbern ein Select zur Auswahl, plus ein Select zum Zurueckziehen."""

    def __init__(self, requests: list[dict]):
        super().__init__(timeout=180)
        self.requests = requests

    @classmethod
    async def build(cls, pool, requests: list[dict]) -> "MyRequestsView":
        self = cls(requests)
        for req in requests:
            for slot in req["slots"]:
                if slot["status"] != "open":
                    continue
                cands = await pool.fetch(
                    "SELECT fc.*, t.name AS team_name FROM friendly_candidates fc JOIN teams t ON t.id = fc.team_id WHERE fc.slot_id = $1",
                    slot["id"],
                )
                if not cands:
                    continue
                options = [discord.SelectOption(label=truncate(c["team_name"], 100), value=str(c["team_id"])) for c in cands]
                select = discord.ui.Select(
                    placeholder=truncate(f"{slot['proposed_time']} ({len(cands)} Bewerber) — Team wählen", 150),
                    options=options,
                )
                select.callback = self._make_choose_callback(slot["id"])
                self.add_item(select)

        if requests:
            withdraw_options = [
                discord.SelectOption(label=truncate(f"{r['team_name']} ({len(r['slots'])} Termin(e))", 100), value=str(r["id"]))
                for r in requests
            ]
            withdraw_select = discord.ui.Select(placeholder="Anfrage zurückziehen...", options=withdraw_options)
            withdraw_select.callback = self._withdraw_callback
            self.add_item(withdraw_select)
        return self

    def _make_choose_callback(self, slot_id: int):
        async def callback(interaction: discord.Interaction):
            team_id = int(interaction.data["values"][0])
            pool = get_pool()
            slot = await pool.fetchrow("SELECT * FROM friendly_slots WHERE id = $1", slot_id)
            if not slot or slot["status"] != "open":
                await interaction.response.edit_message(content="Dieser Termin ist nicht mehr offen.", view=None)
                return

            candidate = await pool.fetchrow("SELECT * FROM friendly_candidates WHERE slot_id = $1 AND team_id = $2", slot_id, team_id)
            chosen_team = await pool.fetchrow("SELECT * FROM teams WHERE id = $1", team_id)
            request = await pool.fetchrow("SELECT * FROM friendly_requests WHERE id = $1", slot["request_id"])
            requester_team = await pool.fetchrow("SELECT * FROM teams WHERE id = $1", request["team_id"])

            await pool.execute(
                "UPDATE friendly_slots SET status = 'matched', matched_team_id = $1, matched_by_discord_id = $2 WHERE id = $3",
                team_id, candidate["discord_id"] if candidate else None, slot_id,
            )
            await maybe_close_request(pool, slot["request_id"])
            await refresh_friendly_panel(interaction.client, interaction.guild)

            other_candidates = await pool.fetch(
                "SELECT * FROM friendly_candidates WHERE slot_id = $1 AND team_id != $2", slot_id, team_id
            )
            dm_text = (
                f"🤝 **Freundschaftsspiel vereinbart!**\n"
                f"**{requester_team['name']}** 🆚 **{chosen_team['name']}**\n"
                f"🗓️ {slot['proposed_time']}\n\nSprecht die Details (Uhrzeit, Plattform, Format) am besten direkt hier ab."
            )
            targets = [(request["requested_by_discord_id"], chosen_team["name"])]
            if candidate:
                targets.append((candidate["discord_id"], requester_team["name"]))
            for user_id, other_name in targets:
                try:
                    user = interaction.guild.get_member(user_id) or await interaction.client.fetch_user(user_id)
                    await user.send(dm_text + f"\nGegner: **{other_name}**")
                except discord.HTTPException:
                    pass
            for oc in other_candidates:
                try:
                    user = interaction.guild.get_member(oc["discord_id"]) or await interaction.client.fetch_user(oc["discord_id"])
                    await user.send(f"😕 Für **{requester_team['name']}** um **{slot['proposed_time']}** wurde sich für ein anderes Team entschieden.")
                except discord.HTTPException:
                    pass

            await interaction.response.edit_message(content=f"✅ Für **{slot['proposed_time']}** wurde **{chosen_team['name']}** bestätigt.", view=None)
        return callback

    async def _withdraw_callback(self, interaction: discord.Interaction):
        request_id = int(interaction.data["values"][0])
        pool = get_pool()
        await pool.execute("UPDATE friendly_requests SET status = 'withdrawn' WHERE id = $1 AND status = 'open'", request_id)
        await refresh_friendly_panel(interaction.client, interaction.guild)
        await interaction.response.edit_message(content="🗑️ Anfrage zurückgezogen.", view=None)


# ---------- Cog ----------

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
        action = custom_id.split(":", 1)[1]
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

        if action == "apply":
            slot_id = int(interaction.data["values"][0])
            slot = await pool.fetchrow("SELECT * FROM friendly_slots WHERE id = $1", slot_id)
            if not slot or slot["status"] != "open":
                await interaction.response.send_message(view=error_embed("Dieser Termin ist nicht mehr offen."), ephemeral=True)
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
                "SELECT 1 FROM friendly_candidates WHERE slot_id = $1 AND team_id = $2", slot_id, accepting_team["id"]
            )
            if existing:
                await interaction.response.send_message(view=error_embed("Ihr habt euch für diesen Termin bereits beworben."), ephemeral=True)
                return
            await pool.execute(
                "INSERT INTO friendly_candidates (slot_id, team_id, discord_id) VALUES ($1, $2, $3)",
                slot_id, accepting_team["id"], interaction.user.id,
            )
            await interaction.response.send_message(
                content=f"✅ Bewerbung für **{slot['proposed_time']}** eingetragen. Der Ersteller wählt daraus ein Team aus.",
                ephemeral=True,
            )
            await refresh_friendly_panel(interaction.client, interaction.guild)
            try:
                requester = interaction.guild.get_member(request["requested_by_discord_id"]) or await interaction.client.fetch_user(request["requested_by_discord_id"])
                await requester.send(
                    f"📋 **{accepting_team['name']}** möchte euer Freundschaftsspiel am **{slot['proposed_time']}** annehmen. "
                    f"Wähle über 'Meine Anfragen' im Freundschaftsspiel-Kanal aus, mit wem ihr spielt."
                )
            except discord.HTTPException:
                pass

        elif action == "myrequests":
            team = await get_team_for_user(interaction.guild_id, interaction.user.id)
            if not team:
                await interaction.response.send_message(view=error_embed("Du hast noch kein Team."), ephemeral=True)
                return
            requests = await fetch_open_requests_for_team(pool, team["id"])
            if not requests:
                await interaction.response.send_message(content="Ihr habt aktuell keine offene Anfrage.", ephemeral=True)
                return
            view = await MyRequestsView.build(pool, requests)
            await interaction.response.send_message(content="Eure offenen Anfragen:", view=view, ephemeral=True)

    @app_commands.command(name="friendly_setup", description="Richtet das Freundschaftsspiel-Panel in diesem Kanal ein (Admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def friendly_setup(self, interaction: discord.Interaction):
        pool = get_pool()
        await pool.execute(
            "INSERT INTO guild_settings (guild_id, friendly_channel_id, friendly_panel_message_id) VALUES ($1, $2, NULL) "
            "ON CONFLICT (guild_id) DO UPDATE SET friendly_channel_id = $2, friendly_panel_message_id = NULL",
            interaction.guild_id, interaction.channel_id,
        )
        await interaction.response.send_message(content="Freundschaftsspiel-Panel wird eingerichtet...", ephemeral=True)
        await refresh_friendly_panel(interaction.client, interaction.guild)


async def setup(bot: commands.Bot):
    await bot.add_cog(FriendliesCog(bot))
