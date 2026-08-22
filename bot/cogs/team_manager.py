"""
Team Manager - Components V2 Panel.

Postet ein persistentes Panel (via /team_manager_setup, Admin-only) mit Buttons:
Team erstellen, bearbeiten, Co-Manager verwalten, Benachrichtigungen, Verlassen/Löschen.

Jede Guild bekommt beim Bot-Start dasselbe persistente View wieder registriert
(custom_ids bleiben stabil), damit die Buttons auch nach einem Neustart funktionieren.
"""
from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands

import io
import logging
import os

from db import get_pool
from ea_api import EAProClubsAPI

log = logging.getLogger("fifa-elite-cup")

PLATFORM_DEFAULT = "common-gen5"
BANNER_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "banner.jpg")


# ---------- Hilfsfunktionen ----------

async def get_team_for_user(guild_id: int, user_id: int):
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT t.* FROM teams t
        JOIN team_managers tm ON tm.team_id = t.id
        WHERE t.guild_id = $1 AND tm.discord_id = $2
        """,
        guild_id, user_id,
    )
    return row


async def get_role_for_user(team_id: int, user_id: int) -> str | None:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT role FROM team_managers WHERE team_id = $1 AND discord_id = $2",
        team_id, user_id,
    )
    return row["role"] if row else None


async def get_team_managers(team_id: int) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT discord_id, role FROM team_managers WHERE team_id = $1 ORDER BY role",
        team_id,
    )
    return [dict(r) for r in rows]


def team_info_text(team: dict, owner_id: int | None, comanager_ids: list[int], user_role: str | None) -> str:
    owner_mention = f"<@{owner_id}>" if owner_id else "_unbekannt_"
    comanager_text = ", ".join(f"<@{cid}>" for cid in comanager_ids) if comanager_ids else "_Keine_"
    notif_text = "An" if team["notifications_enabled"] else "Aus"
    lines = [
        f"## {team['name']}",
        "",
        f"**Deine Rolle:** {'Vereinsmanager' if user_role == 'owner' else 'Co-Manager'}",
        f"**Vereinsmanager:** {owner_mention}",
        f"**Co-Manager:** {comanager_text}",
        f"**Stream:** {team['stream_link'] or '_kein Link_'}",
        f"**Benachrichtigungen:** {notif_text}",
        "",
        "**Turnier-Statistiken:**",
        "Spiele: 0 (0S / 0U / 0N)",
        "Winrate: 0%",
        "Tore: 0:0 (+0)",
    ]
    return "\n".join(lines)


# ---------- Modals ----------

class CreateTeamModal(discord.ui.Modal, title="Team verknuepfen"):
    """
    Wie bei PadBot: nur EA-Club-Name + Stream-Link. Der Teamname wird
    automatisch aus der EA-API uebernommen. Logo wird separat ueber
    'Team bearbeiten' -> 'Logo hochladen' gesetzt (eigenes Popup).
    Klassische Deklarationsform (wie EAClubModal, die bekannt funktioniert).
    """
    ea_club_name = discord.ui.TextInput(label="EA FC 26 Pro Clubs Name", max_length=60)
    stream_link = discord.ui.TextInput(
        label="Stream-Link (Twitch/YouTube)", required=False, max_length=200
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        pool = get_pool()

        from cogs.moderation import get_active_ban, format_ban_reason
        ban = await get_active_ban(interaction.guild_id, interaction.user.id)
        if ban:
            await interaction.followup.send(f"🚫 {format_ban_reason(ban)}", ephemeral=True)
            return

        existing = await get_team_for_user(interaction.guild_id, interaction.user.id)
        if existing:
            await interaction.followup.send(
                f"Du bist bereits Manager/Co-Manager von **{existing['name']}**. "
                "Verlasse dieses Team erst, bevor du ein neues erstellst.",
                ephemeral=True,
            )
            return

        try:
            async with EAProClubsAPI() as api:
                results = await api.search_club(self.ea_club_name.value, PLATFORM_DEFAULT)
        except Exception as e:
            await interaction.followup.send(f"❌ EA-API-Fehler: `{e}`. Bitte später erneut versuchen.", ephemeral=True)
            return

        if not results:
            await interaction.followup.send(
                f"Kein EA-Club namens **{self.ea_club_name.value}** gefunden (Plattform PS5/XSX/PC). "
                "Prüfe die Schreibweise oder sag Bescheid, falls dein Club auf PS4/Xbox One/Switch spielt "
                "(wird aktuell noch nicht automatisch geprüft).",
                ephemeral=True,
            )
            return

        club = results[0]
        info = club.get("clubInfo", {})
        ea_club_id = str(info.get("clubId") or club.get("clubId") or "")
        ea_club_name = info.get("name") or club.get("clubName") or self.ea_club_name.value

        try:
            row = await pool.fetchrow(
                """
                INSERT INTO teams (guild_id, name, ea_club_id, ea_club_name, ea_platform, stream_link, owner_discord_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
                """,
                interaction.guild_id, ea_club_name, ea_club_id, ea_club_name,
                PLATFORM_DEFAULT, self.stream_link.value or None, interaction.user.id,
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ Konnte Team nicht anlegen (existiert der Name schon?): `{e}`", ephemeral=True
            )
            return

        team_id = row["id"]
        await pool.execute(
            "INSERT INTO team_managers (team_id, discord_id, role) VALUES ($1, $2, 'owner')",
            team_id, interaction.user.id,
        )

        await interaction.followup.send(
            f"✅ Team **{ea_club_name}** erstellt und verknüpft! "
            "Logo kannst du jetzt über 'Team bearbeiten' -> 'Logo hochladen' setzen.",
            ephemeral=True,
        )


class EditFieldModal(discord.ui.Modal):
    def __init__(self, team_id: int, field: str, label: str, current: str | None):
        super().__init__(title=f"{label} bearbeiten")
        self.team_id = team_id
        self.field = field
        self.value_input = discord.ui.TextInput(label=label, default=current or "", required=False, max_length=200)
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        pool = get_pool()
        await pool.execute(f"UPDATE teams SET {self.field} = $1 WHERE id = $2", self.value_input.value or None, self.team_id)
        await interaction.response.send_message("✅ Aktualisiert.", ephemeral=True)


# ---------- Ephemere Untermenüs ----------

class EAClubModal(discord.ui.Modal, title="EA Club verknüpfen"):
    ea_club_name = discord.ui.TextInput(label="EA FC 26 Pro Clubs Name", max_length=60)

    def __init__(self, team_id: int):
        super().__init__()
        self.team_id = team_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with EAProClubsAPI() as api:
                results = await api.search_club(self.ea_club_name.value, PLATFORM_DEFAULT)
        except Exception as e:
            await interaction.followup.send(f"❌ EA-API-Fehler: `{e}`", ephemeral=True)
            return
        if not results:
            await interaction.followup.send("Kein Club mit diesem Namen gefunden.", ephemeral=True)
            return
        club = results[0]
        info = club.get("clubInfo", {})
        ea_club_id = str(info.get("clubId") or club.get("clubId") or "")
        ea_club_name = info.get("name") or club.get("clubName") or self.ea_club_name.value

        pool = get_pool()
        await pool.execute(
            "UPDATE teams SET ea_club_id = $1, ea_club_name = $2 WHERE id = $3",
            ea_club_id, ea_club_name, self.team_id,
        )
        await interaction.followup.send(f"✅ Verknüpft mit **{ea_club_name}**.", ephemeral=True)


class LogoUploadModal(discord.ui.Modal, title="Logo hochladen"):
    def __init__(self, team_id: int):
        super().__init__()
        self.team_id = team_id
        self.file_upload = discord.ui.FileUpload(
            custom_id="logo_file",
            min_values=0,
            max_values=1,
            required=False,
        )
        self.add_item(
            discord.ui.Label(
                text="Team-Logo",
                description="PNG, JPG oder WEBP - direkt hochladen, kein Link noetig.",
                component=self.file_upload,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        values = getattr(self.file_upload, "values", None) or getattr(self.file_upload, "attachments", None) or []
        if not values:
            await interaction.response.send_message("Kein Logo hochgeladen.", ephemeral=True)
            return
        attachment = values[0]

        await interaction.response.defer(ephemeral=True, thinking=True)
        pool = get_pool()

        row = await pool.fetchrow(
            "SELECT logo_storage_channel_id FROM guild_settings WHERE guild_id = $1", interaction.guild_id
        )
        storage_channel_id = row["logo_storage_channel_id"] if row else None
        if not storage_channel_id:
            await interaction.followup.send(
                "⚠️ Es ist noch kein Logo-Speicherkanal eingerichtet (Admin muss das im Admin-Panel unter "
                "'Stats-Kanäle einstellen' festlegen). Ohne diesen Kanal würde der Logo-Link nach kurzer Zeit "
                "ablaufen, deshalb wurde nichts gespeichert.",
                ephemeral=True,
            )
            return

        storage_channel = interaction.guild.get_channel(storage_channel_id)
        if storage_channel is None:
            try:
                storage_channel = await interaction.guild.fetch_channel(storage_channel_id)
            except discord.HTTPException:
                storage_channel = None
        if storage_channel is None:
            await interaction.followup.send("⚠️ Logo-Speicherkanal nicht gefunden. Bitte Admin kontaktieren.", ephemeral=True)
            return

        try:
            file_bytes = await attachment.read()
            permanent_msg = await storage_channel.send(
                content=f"Logo für Team-ID {self.team_id}",
                file=discord.File(io.BytesIO(file_bytes), filename=attachment.filename),
            )
            permanent_url = permanent_msg.attachments[0].url
        except Exception:
            log.exception(f"Fehler beim dauerhaften Speichern des Logos fuer Team {self.team_id}")
            await interaction.followup.send("⚠️ Logo konnte nicht gespeichert werden. Bitte erneut versuchen.", ephemeral=True)
            return

        await pool.execute("UPDATE teams SET logo_url = $1 WHERE id = $2", permanent_url, self.team_id)
        await interaction.followup.send("✅ Logo aktualisiert!", ephemeral=True)


class NotificationsView(discord.ui.View):
    def __init__(self, team: dict):
        super().__init__(timeout=120)
        self.team = team
        select = discord.ui.Select(
            placeholder="Turnier-Benachrichtigungen",
            options=[
                discord.SelectOption(
                    label="An - DM bei neuen Turnieren",
                    value="on",
                    default=team["notifications_enabled"],
                ),
                discord.SelectOption(
                    label="Aus - keine Benachrichtigungen",
                    value="off",
                    default=not team["notifications_enabled"],
                ),
            ],
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        select = interaction.data["values"][0]
        new_state = select == "on"
        pool = get_pool()
        await pool.execute("UPDATE teams SET notifications_enabled = $1 WHERE id = $2", new_state, self.team["id"])
        state_text = "aktiviert" if new_state else "deaktiviert"
        await interaction.response.send_message(f"Benachrichtigungen {state_text}.", ephemeral=True)


class CoManagerView(discord.ui.View):
    def __init__(self, team: dict):
        super().__init__(timeout=120)
        self.team = team

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Co-Manager hinzufügen")
    async def add_comanager(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0]
        pool = get_pool()
        try:
            await pool.execute(
                "INSERT INTO team_managers (team_id, discord_id, role) VALUES ($1, $2, 'co_manager')",
                self.team["id"], user.id,
            )
        except Exception:
            await interaction.response.send_message(f"{user.mention} ist bereits Manager dieses Teams.", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {user.mention} ist jetzt Co-Manager von **{self.team['name']}**.", ephemeral=True)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Co-Manager entfernen")
    async def remove_comanager(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0]
        pool = get_pool()
        role = await get_role_for_user(self.team["id"], user.id)
        if role == "owner":
            await interaction.response.send_message("Der Team-Owner kann hier nicht entfernt werden.", ephemeral=True)
            return
        await pool.execute("DELETE FROM team_managers WHERE team_id = $1 AND discord_id = $2", self.team["id"], user.id)
        await interaction.response.send_message(f"✅ {user.mention} wurde entfernt.", ephemeral=True)


class LeaveConfirmView(discord.ui.View):
    def __init__(self, team: dict, is_owner: bool):
        super().__init__(timeout=60)
        self.team = team
        self.is_owner = is_owner

    @discord.ui.button(label="Ja, bestätigen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        pool = get_pool()
        if self.is_owner:
            await pool.execute("DELETE FROM teams WHERE id = $1", self.team["id"])
            await interaction.response.edit_message(content=f"🗑️ Team **{self.team['name']}** wurde gelöscht.", view=None)
        else:
            await pool.execute(
                "DELETE FROM team_managers WHERE team_id = $1 AND discord_id = $2",
                self.team["id"], interaction.user.id,
            )
            await interaction.response.edit_message(content=f"👋 Du hast **{self.team['name']}** verlassen.", view=None)

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Abgebrochen.", view=None)


# ---------- Persistentes Hauptpanel (Components V2) ----------

class TeamManagerPanel(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        self.banner_file = discord.File(BANNER_PATH, filename="banner.jpg")
        text = (
            "# TEAM MANAGER\n"
            "Verwalte dein Team für den FIFA Elite Cup.\n"
            "\n"
            "-----\n"
            "\n"
            "**» TEAM VERKNÜPFEN**\n"
            "- Verbinde deinen EA FC Pro Club mit deinem Discord-Account\n"
            "- Der Club-Name wird automatisch aus der EA API übernommen\n"
            "- Optional: Twitch/YouTube Stream-Link hinterlegen\n"
            "\n"
            "-----\n"
            "\n"
            "**» FUNKTIONEN**\n"
            "\n"
            "**Stream-Link**\n"
            "- Ändere deinen hinterlegten Stream jederzeit\n"
            "\n"
            "**Logo**\n"
            "- Lade ein Team-Logo hoch (PNG, JPG, WEBP)\n"
            "\n"
            "**Co-Manager**\n"
            "- Füge Co-Manager hinzu, sie können Spiele für dein Team eintragen\n"
            "\n"
            "**Team-Info**\n"
            "- Zeigt Statistiken und Mitglieder\n"
            "\n"
            "-----\n"
            "\n"
            "**» WICHTIG**\n"
            "\n"
            "Bist du Spieler eines Teams? Dann musst du hier nichts tun!\n"
            "\n"
            "Dieser Bereich ist nur für Vereinsmanager, die ein Team für Turniere anmelden möchten.\n"
            "\n"
            "Wichtig: Der EA FC Pro Club Name muss exakt stimmen!\n"
            "FIFA Elite Cup"
        )
        container = discord.ui.Container(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media=self.banner_file),
            ),
            discord.ui.TextDisplay(text),
            discord.ui.ActionRow(
                discord.ui.Button(label="Team verknüpfen", style=discord.ButtonStyle.primary, custom_id="team:create"),
                discord.ui.Button(label="Mein Team", style=discord.ButtonStyle.secondary, custom_id="team:info"),
                discord.ui.Button(label="Logo", style=discord.ButtonStyle.secondary, custom_id="team:logo"),
            ),
            discord.ui.ActionRow(
                discord.ui.Button(label="Stream-Link", style=discord.ButtonStyle.secondary, custom_id="team:stream"),
                discord.ui.Button(label="Co-Manager", style=discord.ButtonStyle.secondary, custom_id="team:comanager"),
                discord.ui.Button(label="Team verlassen", style=discord.ButtonStyle.secondary, custom_id="team:leave"),
            ),
            discord.ui.ActionRow(
                discord.ui.Button(label="Benachrichtigungen", style=discord.ButtonStyle.secondary, custom_id="team:notifications"),
            ),
            accent_color=discord.Color.gold(),
        )
        self.add_item(container)


# ---------- Cog ----------

class TeamManagerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(TeamManagerPanel())

    @app_commands.command(name="team_manager_setup", description="Postet das Team-Manager-Panel in diesem Kanal (Admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def team_manager_setup(self, interaction: discord.Interaction):
        panel = TeamManagerPanel()
        await interaction.response.send_message(view=panel, files=[panel.banner_file])

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("team:"):
            return

        action = custom_id.split(":", 1)[1]

        if action == "create":
            await interaction.response.send_modal(CreateTeamModal())
            return

        team = await get_team_for_user(interaction.guild_id, interaction.user.id)
        if not team:
            await interaction.response.send_message(
                "Du hast noch kein Team. Klick auf '🆕 Team erstellen'.", ephemeral=True
            )
            return

        if action == "info":
            managers = await get_team_managers(team["id"])
            owner_id = next((m["discord_id"] for m in managers if m["role"] == "owner"), None)
            comanager_ids = [m["discord_id"] for m in managers if m["role"] == "co_manager"]
            role = await get_role_for_user(team["id"], interaction.user.id)
            await interaction.response.send_message(
                team_info_text(team, owner_id, comanager_ids, role), ephemeral=True
            )

        elif action == "logo":
            await interaction.response.send_modal(LogoUploadModal(team["id"]))

        elif action == "stream":
            await interaction.response.send_modal(
                EditFieldModal(team["id"], "stream_link", "Stream-Link", team["stream_link"])
            )

        elif action == "comanager":
            role = await get_role_for_user(team["id"], interaction.user.id)
            if role != "owner":
                await interaction.response.send_message("Nur der Vereinsmanager kann Co-Manager verwalten.", ephemeral=True)
                return
            await interaction.response.send_message("Co-Manager verwalten:", view=CoManagerView(team), ephemeral=True)

        elif action == "notifications":
            await interaction.response.send_message(
                "Turnier-Benachrichtigungen:", view=NotificationsView(team), ephemeral=True
            )

        elif action == "leave":
            role = await get_role_for_user(team["id"], interaction.user.id)
            is_owner = role == "owner"
            warning = (
                f"⚠️ Du bist **Owner** von **{team['name']}**. Bestätigen löscht das Team komplett "
                "inkl. aller Co-Manager-Einträge."
                if is_owner else
                f"Möchtest du **{team['name']}** wirklich verlassen?"
            )
            await interaction.response.send_message(warning, view=LeaveConfirmView(team, is_owner), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TeamManagerCog(bot))
