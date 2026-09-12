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
from discord.ext import commands, tasks

import io
import logging
import re
import os

import asyncpg
from PIL import Image

from db import get_pool
from ea_api import EAProClubsAPI
from ui_helpers import success_embed, error_embed, info_embed, warning_embed, WEBSITE_URL

log = logging.getLogger("fifa-elite-cup")

PLATFORM_DEFAULT = "common-gen5"
TWITCH_LINK_PATTERN = re.compile(r"^https://(?:www\.)?twitch\.tv/[A-Za-z0-9_]+/?$")


def is_valid_twitch_link(value: str) -> bool:
    return bool(TWITCH_LINK_PATTERN.match(value.strip()))


def _crop_to_square(file_bytes: bytes) -> bytes:
    """Schneidet ein Bild mittig auf ein 1:1-Seitenverhaeltnis zu (laengere Seite wird gekuerzt),
    damit Logos ueberall (Kreis-Avatare, Bot-Grafiken) sauber aussehen statt verzerrt/schief."""
    img = Image.open(io.BytesIO(file_bytes))
    img = img.convert("RGBA") if img.mode in ("P", "RGBA", "LA") else img.convert("RGB")
    w, h = img.size
    if w != h:
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


async def save_team_logo_attachment(guild: discord.Guild, team_id: int, attachment) -> tuple[bool, str]:
    """
    Speichert ein hochgeladenes Logo dauerhaft im Logo-Speicherkanal (siehe schema_phase30: die
    Discord-CDN-URL selbst laeuft nach ca. 24h ab, deshalb wird zusaetzlich Kanal+Nachrichten-ID
    gespeichert, damit refresh_all_team_logo_urls() die URL periodisch auffrischen kann).
    Gibt (erfolg, meldung) zurueck.
    """
    pool = get_pool()
    row = await pool.fetchrow("SELECT logo_storage_channel_id FROM guild_settings WHERE guild_id = $1", guild.id)
    storage_channel_id = row["logo_storage_channel_id"] if row else None
    if not storage_channel_id:
        return False, (
            "⚠️ Es ist noch kein Logo-Speicherkanal eingerichtet (Admin muss das im Admin-Panel unter "
            "'Stats-Kanäle einstellen' festlegen). Ohne diesen Kanal würde der Logo-Link nach kurzer Zeit "
            "ablaufen, deshalb wurde nichts gespeichert."
        )

    storage_channel = guild.get_channel(storage_channel_id)
    if storage_channel is None:
        try:
            storage_channel = await guild.fetch_channel(storage_channel_id)
        except discord.HTTPException:
            storage_channel = None
    if storage_channel is None:
        return False, "Logo-Speicherkanal nicht gefunden. Bitte Admin kontaktieren."

    try:
        file_bytes = await attachment.read()
        try:
            file_bytes = _crop_to_square(file_bytes)
            filename = "logo.png"
        except Exception:
            log.exception(f"Logo fuer Team {team_id} konnte nicht quadratisch zugeschnitten werden, speichere Original.")
            filename = attachment.filename
        permanent_msg = await storage_channel.send(
            content=f"Logo für Team-ID {team_id}",
            file=discord.File(io.BytesIO(file_bytes), filename=filename),
        )
        permanent_url = permanent_msg.attachments[0].url
    except Exception:
        log.exception(f"Fehler beim dauerhaften Speichern des Logos fuer Team {team_id}")
        return False, "Logo konnte nicht gespeichert werden. Bitte erneut versuchen."

    await pool.execute(
        "UPDATE teams SET logo_url = $1, logo_channel_id = $2, logo_message_id = $3 WHERE id = $4",
        permanent_url, storage_channel.id, permanent_msg.id, team_id,
    )
    return True, "Logo aktualisiert!"


async def fetch_member_safe(guild: discord.Guild, user_id: int) -> discord.Member | None:
    """guild.get_member() liefert nur etwas, wenn der Member-Cache bereits gefuellt ist -
    bei den Standalone-Skripten (Login ohne Gateway-Verbindung, siehe Session-Konventionen)
    ist der Cache immer leer, wodurch Rollen-/Nickname-Reset beim Team-Aufloesen bisher
    stillschweigend uebersprungen wurde (live per API bestaetigt: VM-/Co-Manager-Rollen
    blieben nach dem Aufloesen bestehen). Faellt deshalb auf einen echten API-Call zurueck."""
    member = guild.get_member(user_id)
    if member:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:
        return None


async def apply_team_nickname(member: discord.Member, team_name: str) -> bool:
    """Setzt den Server-Nickname auf 'Team | Username'. Gibt False zurueck, falls keine Berechtigung."""
    base_username = member.name
    new_nick = f"{team_name} | {base_username}"[:32]
    if member.display_name == new_nick:
        return True
    try:
        await member.edit(nick=new_nick)
        return True
    except discord.Forbidden:
        log.warning(f"Konnte Nickname von {member} nicht setzen (fehlende Berechtigung, z.B. hoehere Rolle oder Server-Owner).")
        return False
    except discord.HTTPException:
        log.exception(f"Fehler beim Setzen des Nicknames fuer {member}")
        return False


async def _toggle_configured_role(guild: discord.Guild, member: discord.Member, settings_column: str, grant: bool):
    """Vergibt/entzieht die in guild_settings.{settings_column} konfigurierte Rolle - macht nichts, falls keine gesetzt ist."""
    pool = get_pool()
    row = await pool.fetchrow(f"SELECT {settings_column} FROM guild_settings WHERE guild_id = $1", guild.id)
    role_id = row[settings_column] if row else None
    if not role_id:
        return
    role = guild.get_role(role_id)
    if not role:
        return
    try:
        if grant:
            await member.add_roles(role)
        else:
            await member.remove_roles(role)
    except discord.HTTPException:
        log.exception(f"Fehler beim {'Vergeben' if grant else 'Entziehen'} der Rolle {role_id} an {member}")


async def reset_team_nickname(member: discord.Member):
    """Setzt den Nickname zurueck (Discord zeigt dann wieder den normalen Usernamen), falls er noch das 'Team | ...'-Format hat."""
    if "|" not in (member.nick or ""):
        return
    try:
        await member.edit(nick=None)
    except discord.Forbidden:
        log.warning(f"Konnte Nickname von {member} nicht zuruecksetzen (fehlende Berechtigung).")
    except discord.HTTPException:
        log.exception(f"Fehler beim Zuruecksetzen des Nicknames fuer {member}")


async def refresh_all_team_logo_urls(bot: commands.Bot):
    """
    Discord-CDN-Attachment-URLs sind nur ca. 24h gueltig (signierte ex=/is=/hm=-Parameter),
    auch wenn die Nachricht dauerhaft im Speicherkanal liegt - nur ein erneutes Abrufen der
    Nachricht liefert eine frische URL. Laeuft periodisch als Hintergrund-Task
    (siehe TeamManagerCog._refresh_logos_task) und einmalig direkt beim Bot-Start.
    """
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT id, logo_channel_id, logo_message_id FROM teams WHERE logo_channel_id IS NOT NULL AND logo_message_id IS NOT NULL"
    )
    refreshed, failed = 0, 0
    for row in rows:
        channel = bot.get_channel(row["logo_channel_id"])
        if channel is None:
            try:
                channel = await bot.fetch_channel(row["logo_channel_id"])
            except discord.HTTPException:
                failed += 1
                continue
        try:
            msg = await channel.fetch_message(row["logo_message_id"])
            fresh_url = msg.attachments[0].url
        except (discord.HTTPException, IndexError):
            failed += 1
            continue
        await pool.execute("UPDATE teams SET logo_url = $1 WHERE id = $2", fresh_url, row["id"])
        refreshed += 1
    if refreshed or failed:
        log.info(f"Team-Logo-URLs aufgefrischt: {refreshed} ok, {failed} fehlgeschlagen.")


STREAM_BANNER_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "stream_banner.jpg")


async def refresh_stream_list(bot: commands.Bot, guild: discord.Guild):
    """Baut die Stream-Link-Uebersicht neu auf (oder legt sie an) im konfigurierten Kanal."""
    pool = get_pool()
    settings = await pool.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", guild.id)
    if not settings or not settings["stream_list_channel_id"]:
        return

    channel = guild.get_channel(settings["stream_list_channel_id"])
    if channel is None:
        try:
            channel = await guild.fetch_channel(settings["stream_list_channel_id"])
        except discord.HTTPException:
            return

    teams = await pool.fetch(
        "SELECT name, stream_link FROM teams WHERE guild_id = $1 AND stream_link IS NOT NULL ORDER BY name",
        guild.id,
    )

    lines = []
    if teams:
        lines += [f"**{t['name']}** — {t['stream_link']}" for t in teams]
    else:
        lines.append("_Aktuell hat kein Team einen Stream-Link hinterlegt._")
    lines.append("")
    now_ts = int(discord.utils.utcnow().timestamp())
    lines.append(f"-# Zuletzt aktualisiert: <t:{now_ts}:R>")
    text = "\n".join(lines)

    banner_file = discord.File(STREAM_BANNER_PATH, filename="stream_banner.jpg")
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://stream_banner.jpg")),
        discord.ui.TextDisplay(text),
        accent_color=discord.Color.gold(),
    ))

    if settings["stream_list_message_id"]:
        try:
            msg = await channel.fetch_message(settings["stream_list_message_id"])
            await msg.edit(view=view, attachments=[banner_file])
            return
        except discord.HTTPException:
            pass

    try:
        msg = await channel.send(view=view, files=[banner_file])
        await pool.execute(
            "UPDATE guild_settings SET stream_list_message_id = $1 WHERE guild_id = $2", msg.id, guild.id
        )
    except discord.HTTPException:
        log.exception(f"Fehler beim Erstellen der Stream-Uebersicht in Guild {guild.id}")


TEAMS_PER_OVERVIEW_MESSAGE = 8  # Components-V2-Nachrichten haben ein Zeichenlimit, bei vielen Teams auf mehrere Nachrichten verteilen


async def get_team_tournament_history(pool, team_id: int) -> list[str]:
    """Kompakte Turnierhistorie eines Teams: Platzierung soweit bekannt, sonst 'teilgenommen'."""
    rows = await pool.fetch(
        """
        SELECT t.name, t.status, t.winner_champion_id, t.loser_champion_id,
               t.winner_bracket_third_id, t.loser_bracket_third_id
        FROM tournament_signups ts
        JOIN tournaments t ON t.id = ts.tournament_id
        WHERE ts.team_id = $1 AND ts.status IN ('registered', 'waitlist')
        ORDER BY t.created_at DESC
        LIMIT 10
        """,
        team_id,
    )
    lines = []
    for r in rows:
        if r["winner_champion_id"] == team_id:
            placement = "🥇 Sieger Winner-Bracket"
        elif r["loser_champion_id"] == team_id:
            placement = "🥇 Sieger Loser-Bracket"
        elif r["winner_bracket_third_id"] == team_id or r["loser_bracket_third_id"] == team_id:
            placement = "🥉 Platz 3"
        elif r["status"] == "finished":
            placement = "Teilgenommen"
        else:
            placement = "Läuft noch"
        lines.append(f"> {r['name']} — {placement}")
    return lines


def build_team_block(team: dict, owner_id: int | None, comanager_ids: list[int], history: list[str]) -> discord.ui.Item:
    """Ein Team als TextDisplay - mit kleinem Logo-Thumbnail rechts daneben, falls das
    Team eins hinterlegt hat (via Section+Thumbnail, kein eigener Upload noetig, da
    logo_url schon eine gehostete Discord-CDN-URL ist)."""
    lines = [
        f"### {team['name']}",
        f"**EA-Club:** {team.get('ea_club_name') or '-'}",
        f"**Stream:** {team.get('stream_link') or '_keiner hinterlegt_'}",
        f"**Vereinsmanager:** {f'<@{owner_id}>' if owner_id else '_unbekannt_'}",
        f"**Co-Manager:** {', '.join(f'<@{cid}>' for cid in comanager_ids) if comanager_ids else '-'}",
    ]
    if history:
        lines.append("**Turniere:**")
        lines += history
    text = discord.ui.TextDisplay("\n".join(lines))
    if team.get("logo_url"):
        return discord.ui.Section(text, accessory=discord.ui.Thumbnail(media=team["logo_url"]))
    return text


class TeamOverviewSearchModal(discord.ui.Modal, title="Team suchen"):
    query = discord.ui.TextInput(label="Team-Name (auch Teilstring reicht)", max_length=60)

    async def on_submit(self, interaction: discord.Interaction):
        pool = get_pool()
        rows = await pool.fetch(
            "SELECT * FROM teams WHERE guild_id = $1 AND dissolved_at IS NULL AND name ILIKE $2 ORDER BY name LIMIT 10",
            interaction.guild_id, f"%{self.query.value}%",
        )
        if not rows:
            await interaction.response.send_message(
                view=warning_embed(f'Kein Team gefunden, das zu "{self.query.value}" passt.'), ephemeral=True
            )
            return

        blocks: list[discord.ui.Item] = [discord.ui.TextDisplay(f'### 🔍 Treffer für "{self.query.value}"')]
        for team in rows:
            team = dict(team)
            managers = await get_team_managers(team["id"])
            owner_id = next((m["discord_id"] for m in managers if m["role"] == "owner"), None)
            comanager_ids = [m["discord_id"] for m in managers if m["role"] != "owner"]
            history = await get_team_tournament_history(pool, team["id"])
            blocks.append(discord.ui.Separator())
            blocks.append(build_team_block(team, owner_id, comanager_ids, history))

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(*blocks, accent_color=discord.Color.gold()))
        await interaction.response.send_message(view=view, ephemeral=True)


async def refresh_team_overview(bot: commands.Bot, guild: discord.Guild):
    """Baut die Vereins-Uebersicht komplett neu auf und postet sie frisch (statt zu editieren, da sich
    die Anzahl benoetigter Nachrichten je nach Teamzahl aendert)."""
    pool = get_pool()
    settings = await pool.fetchrow("SELECT * FROM team_overview_panel WHERE guild_id = $1", guild.id)
    if not settings:
        return

    channel = guild.get_channel(settings["channel_id"])
    if channel is None:
        try:
            channel = await guild.fetch_channel(settings["channel_id"])
        except discord.HTTPException:
            return

    for old_id in settings["message_ids"] or []:
        try:
            old_msg = await channel.fetch_message(old_id)
            await old_msg.delete()
        except discord.HTTPException:
            pass

    teams = await pool.fetch("SELECT * FROM teams WHERE guild_id = $1 AND dissolved_at IS NULL ORDER BY name", guild.id)
    now_ts = int(discord.utils.utcnow().timestamp())

    if not teams:
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# 📋 Vereins-Übersicht\n_Noch keine Teams registriert._\n\n-# Stand: <t:{now_ts}:R>"),
            accent_color=discord.Color.gold(),
        ))
        try:
            msg = await channel.send(view=view)
            await pool.execute(
                "UPDATE team_overview_panel SET message_ids = $1 WHERE guild_id = $2", [msg.id], guild.id
            )
        except discord.HTTPException:
            log.exception(f"Fehler beim Erstellen der leeren Vereins-Uebersicht in Guild {guild.id}")
        return

    chunks = [teams[i:i + TEAMS_PER_OVERVIEW_MESSAGE] for i in range(0, len(teams), TEAMS_PER_OVERVIEW_MESSAGE)]
    new_message_ids = []
    for idx, chunk in enumerate(chunks):
        blocks = []
        if idx == 0:
            blocks.append(discord.ui.TextDisplay(f"# 📋 Vereins-Übersicht\n{len(teams)} registrierte Teams"))
            blocks.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        for i, team in enumerate(chunk):
            managers = await get_team_managers(team["id"])
            owner_id = next((m["discord_id"] for m in managers if m["role"] == "owner"), None)
            comanager_ids = [m["discord_id"] for m in managers if m["role"] != "owner"]
            history = await get_team_tournament_history(pool, team["id"])
            blocks.append(build_team_block(dict(team), owner_id, comanager_ids, history))
            if i < len(chunk) - 1:
                blocks.append(discord.ui.Separator())
        if idx == len(chunks) - 1:
            blocks.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
            blocks.append(discord.ui.ActionRow(
                discord.ui.Button(label="🔍 Team suchen", style=discord.ButtonStyle.secondary, custom_id="team:overviewsearch"),
                discord.ui.Button(label="🌐 Alle Teams auf der Website", style=discord.ButtonStyle.link, url=f"{WEBSITE_URL}/teams"),
            ))
            blocks.append(discord.ui.TextDisplay(f"-# Stand: <t:{now_ts}:R>"))

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(*blocks, accent_color=discord.Color.gold()))
        try:
            msg = await channel.send(view=view)
            new_message_ids.append(msg.id)
        except discord.HTTPException:
            log.exception(f"Fehler beim Posten der Vereins-Uebersicht (Chunk {idx}) in Guild {guild.id}")

    await pool.execute(
        "UPDATE team_overview_panel SET message_ids = $1 WHERE guild_id = $2", new_message_ids, guild.id
    )


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


async def team_register_hint(guild_id: int) -> str:
    """Verlinkt direkt den Team-Manager-Panel-Kanal (statt ihn nur zu benennen) - Teams
    mussten den Kanal bisher selbst suchen, wenn eine 'du hast kein Team'-Meldung kam."""
    pool = get_pool()
    row = await pool.fetchrow("SELECT team_register_channel_id FROM guild_settings WHERE guild_id = $1", guild_id)
    if row and row["team_register_channel_id"]:
        return f"<#{row['team_register_channel_id']}>"
    return "im Team-Manager-Panel"


async def get_team_for_user_in_group(group_id: int, user_id: int):
    """Wie get_team_for_user, aber auf eine Turnier-Gruppe eingeschraenkt - noetig, weil ein
    Co-Manager Co-Manager MEHRERER Teams sein kann (z.B. als Aushilfe). get_team_for_user
    liefert dann irgendeines seiner Teams zurueck (ohne ORDER BY), moeglicherweise eines,
    das gar nicht in dieser Gruppe spielt - dadurch schlug z.B. 'Team ist da' faelschlich
    mit 'Dein Team ist nicht in dieser Gruppe' fehl, obwohl sein tatsaechliches Team dort war."""
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT t.* FROM teams t
        JOIN team_managers tm ON tm.team_id = t.id
        JOIN tournament_group_teams tgt ON tgt.team_id = t.id
        WHERE tgt.group_id = $1 AND tm.discord_id = $2
        """,
        group_id, user_id,
    )
    return row


async def get_team_for_user_in_tournament(tournament_id: int, user_id: int):
    """Wie get_team_for_user_in_group, aber fuer die KO-Phase - schraenkt auf Teams ein,
    die tatsaechlich ein Match in diesem Turnier haben (aus demselben Grund: ein
    Co-Manager mehrerer Teams braucht das richtige Team fuer DIESES Turnier)."""
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT t.* FROM teams t
        JOIN team_managers tm ON tm.team_id = t.id
        WHERE tm.discord_id = $2 AND EXISTS (
            SELECT 1 FROM tournament_matches m
            WHERE m.tournament_id = $1 AND (m.team1_id = t.id OR m.team2_id = t.id)
        )
        """,
        tournament_id, user_id,
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
    EA-Club-Name + Stream-Link + optionales Logo in einem Rutsch. Der Teamname
    wird automatisch aus der EA-API uebernommen. Logo kann alternativ auch
    spaeter ueber 'Team bearbeiten' -> 'Logo hochladen' gesetzt/geaendert werden.
    """
    ea_club_name = discord.ui.TextInput(label="EA FC 26 Pro Clubs Name", max_length=60)
    stream_link = discord.ui.TextInput(
        label="Twitch-Link (z.B. https://www.twitch.tv/name)", required=False, max_length=200
    )

    def __init__(self):
        super().__init__()
        self.file_upload = discord.ui.FileUpload(
            custom_id="team_logo_file",
            min_values=0,
            max_values=1,
            required=False,
        )
        self.add_item(
            discord.ui.Label(
                text="Team-Logo (optional)",
                description="PNG, JPG oder WEBP - kannst du auch später über 'Logo hochladen' setzen.",
                component=self.file_upload,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        pool = get_pool()

        if self.stream_link.value and not is_valid_twitch_link(self.stream_link.value):
            await interaction.followup.send(
                view=error_embed(
                    "Das ist kein gültiger Twitch-Link.",
                    "Format: `https://twitch.tv/name` oder `https://www.twitch.tv/name`",
                ),
                ephemeral=True,
            )
            return

        from cogs.moderation import get_active_ban, format_ban_reason
        ban = await get_active_ban(interaction.guild_id, interaction.user.id)
        if ban:
            await interaction.followup.send(view=warning_embed(format_ban_reason(ban)), ephemeral=True)
            return

        existing = await get_team_for_user(interaction.guild_id, interaction.user.id)
        if existing:
            await interaction.followup.send(
                f"Du bist bereits Manager/Co-Manager von **{existing['name']}**. "
                "Verlasse dieses Team erst, bevor du ein neues erstellst.",
                ephemeral=True,
            )
            return

        # EA-Suche bei der Team-Erstellung komplett deaktiviert (nicht nur "best effort"):
        # EA ist auf FC27 umgestiegen, fast jede Suche liefert aktuell nichts und laesst die
        # Anfrage erst in den vollen 25s-API-Timeout laufen ("Bot denkt ewig nach"), bevor der
        # Fallback greift. Der eingegebene Name wird direkt uebernommen - die EA-Verknuepfung
        # kann jederzeit ueber 'Team bearbeiten' -> 'EA Club verknuepfen' nachgetragen werden,
        # sobald der Club in FC27 aktiv ist.
        ea_club_id = ""
        ea_club_name = self.ea_club_name.value

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
        except asyncpg.UniqueViolationError:
            await interaction.followup.send(
                view=error_embed(f"Ein Team namens \"{ea_club_name}\" existiert auf diesem Server bereits.", "Team-Namen müssen eindeutig sein."),
                ephemeral=True,
            )
            return
        except Exception:
            log.exception("Fehler beim Anlegen eines Teams")
            await interaction.followup.send(view=error_embed("Team konnte nicht angelegt werden."), ephemeral=True)
            return

        team_id = row["id"]
        await pool.execute(
            "INSERT INTO team_managers (team_id, discord_id, role) VALUES ($1, $2, 'owner')",
            team_id, interaction.user.id,
        )
        from audit import log_action
        await log_action(interaction.guild_id, interaction.user, "team.created", "team", team_id, ea_club_name)
        await apply_team_nickname(interaction.user, ea_club_name)
        await _toggle_configured_role(interaction.guild, interaction.user, "vm_role_id", grant=True)
        if self.stream_link.value:
            await refresh_stream_list(interaction.client, interaction.guild)

        logo_values = getattr(self.file_upload, "values", None) or getattr(self.file_upload, "attachments", None) or []
        logo_note = "Logo kannst du jederzeit über 'Team bearbeiten' -> 'Logo hochladen' ändern."
        if logo_values:
            success, message = await save_team_logo_attachment(interaction.guild, team_id, logo_values[0])
            logo_note = message if success else f"⚠️ Logo-Upload fehlgeschlagen: {message}"

        await refresh_team_overview(interaction.client, interaction.guild)

        await interaction.followup.send(
            f"✅ Team **{ea_club_name}** erstellt und verknüpft!\n{logo_note}",
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
        if self.field == "stream_link" and self.value_input.value and not is_valid_twitch_link(self.value_input.value):
            await interaction.response.send_message(
                view=error_embed(
                    "Das ist kein gültiger Twitch-Link.",
                    "Format: `https://twitch.tv/name` oder `https://www.twitch.tv/name`",
                ),
                ephemeral=True,
            )
            return
        pool = get_pool()
        await pool.execute(f"UPDATE teams SET {self.field} = $1 WHERE id = $2", self.value_input.value or None, self.team_id)
        await interaction.response.send_message(view=success_embed("Aktualisiert."), ephemeral=True)
        if self.field == "stream_link":
            await refresh_stream_list(interaction.client, interaction.guild)
        # Vereins-Uebersicht zeigt auch EA-Club-Name/Stream etc. an - bisher wurde sie nur
        # beim Stream-Link aktualisiert, wodurch z.B. ein geaenderter EA-Club-Name dort stehen
        # blieb, obwohl die DB laengst den neuen Wert hatte (live gemeldet: "Calcio Strada").
        await refresh_team_overview(interaction.client, interaction.guild)


class TeamRenameModal(discord.ui.Modal, title="Team umbenennen"):
    def __init__(self, team_id: int, current_name: str):
        super().__init__()
        self.team_id = team_id
        self.name_input = discord.ui.TextInput(label="Neuer Team-Name", default=current_name, max_length=60)
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_name = self.name_input.value.strip()
        if not new_name:
            await interaction.response.send_message(view=error_embed("Der Team-Name darf nicht leer sein."), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        pool = get_pool()
        try:
            # Team-Name und EA-Club-Name sollen immer identisch sein - Umbenennen setzt
            # deshalb beide zusammen, statt nur den Discord-Anzeigenamen zu aendern.
            await pool.execute("UPDATE teams SET name = $1, ea_club_name = $1 WHERE id = $2", new_name, self.team_id)
        except asyncpg.UniqueViolationError:
            await interaction.followup.send(
                view=error_embed(f'Ein Team namens "{new_name}" existiert auf diesem Server bereits.'), ephemeral=True
            )
            return

        from audit import log_action
        await log_action(interaction.guild_id, interaction.user, "team.renamed", "team", self.team_id, new_name)

        # Nicknames aller Manager auf den neuen Namen umstellen (bestehendes Format "Team | User")
        managers = await get_team_managers(self.team_id)
        for m in managers:
            member = await fetch_member_safe(interaction.guild, m["discord_id"])
            if member:
                await apply_team_nickname(member, new_name)

        await refresh_team_overview(interaction.client, interaction.guild)
        await interaction.followup.send(view=success_embed(f'Team umbenannt in "{new_name}".'), ephemeral=True)


# ---------- Ephemere Untermenüs ----------

class EAClubModal(discord.ui.Modal, title="EA Club verknüpfen"):
    ea_club_name = discord.ui.TextInput(label="EA FC 26 Pro Clubs Name", max_length=60)

    def __init__(self, team_id: int):
        super().__init__()
        self.team_id = team_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        # Best effort, wie bei der Team-Erstellung: EA laesst gerade wegen des FC27-Umstiegs
        # viele Clubs nicht finden - blockiert das Verknuepfen nicht mehr, uebernimmt notfalls
        # einfach den eingegebenen Namen (EA-Club-ID bleibt leer, laesst sich spaeter erneut
        # versuchen, sobald der Club in FC27 aktiv ist).
        ea_club_id = ""
        ea_club_name = self.ea_club_name.value
        note = ""
        try:
            async with EAProClubsAPI() as api:
                results = await api.search_club(self.ea_club_name.value, PLATFORM_DEFAULT)
            if results:
                club = results[0]
                info = club.get("clubInfo", {})
                ea_club_id = str(info.get("clubId") or club.get("clubId") or "")
                ea_club_name = info.get("name") or club.get("clubName") or self.ea_club_name.value
            else:
                note = " ⚠️ EA hat den Club nicht gefunden (FC27-Umstieg) - Name wurde trotzdem übernommen, EA-Verknüpfung bitte später erneut versuchen."
        except Exception:
            note = " ⚠️ EA-API gerade nicht erreichbar - Name wurde trotzdem übernommen, EA-Verknüpfung bitte später erneut versuchen."

        pool = get_pool()
        await pool.execute(
            "UPDATE teams SET ea_club_id = $1, ea_club_name = $2 WHERE id = $3",
            ea_club_id, ea_club_name, self.team_id,
        )
        await interaction.followup.send(view=success_embed(f"Verknüpft mit {ea_club_name}{note}"), ephemeral=True)


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
            await interaction.response.send_message(view=error_embed("Kein Logo hochgeladen."), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        success, message = await save_team_logo_attachment(interaction.guild, self.team_id, values[0])
        if success:
            await interaction.followup.send(view=success_embed(message), ephemeral=True)
        else:
            await interaction.followup.send(view=error_embed(message), ephemeral=True)


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
        await interaction.response.send_message(view=success_embed(f"Benachrichtigungen {state_text}."), ephemeral=True)


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
            await interaction.response.send_message(view=warning_embed(f"{user.mention} ist bereits Manager dieses Teams."), ephemeral=True)
            return
        # ERST antworten, DANN die (mehreren, teils langsamen) Discord-API-Aufrufe
        # (Nickname, Rolle, Panel-Neuaufbau) - sonst laeuft das 3-Sekunden-Interaktionsfenster
        # ab, bevor ueberhaupt geantwortet wird ("Bot reagiert nicht").
        await interaction.response.defer(ephemeral=True, thinking=True)
        member = await fetch_member_safe(interaction.guild, user.id)
        if member:
            await apply_team_nickname(member, self.team["name"])
            await _toggle_configured_role(interaction.guild, member, "co_manager_role_id", grant=True)
            from cogs.tournament_manager import grant_live_tournament_access
            await grant_live_tournament_access(interaction.guild, self.team["id"], member)
        await refresh_team_overview(interaction.client, interaction.guild)
        await interaction.followup.send(view=success_embed(f"{user.mention} ist jetzt Co-Manager von {self.team['name']}"), ephemeral=True)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Co-Manager entfernen")
    async def remove_comanager(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0]
        pool = get_pool()
        role = await get_role_for_user(self.team["id"], user.id)
        if role == "owner":
            await interaction.response.send_message(view=warning_embed("Der Team-Owner kann hier nicht entfernt werden."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await pool.execute("DELETE FROM team_managers WHERE team_id = $1 AND discord_id = $2", self.team["id"], user.id)
        member = await fetch_member_safe(interaction.guild, user.id)
        if member:
            await _toggle_configured_role(interaction.guild, member, "co_manager_role_id", grant=False)
            await reset_team_nickname(member)
        await refresh_team_overview(interaction.client, interaction.guild)
        await interaction.followup.send(view=success_embed(f"{user.mention} wurde entfernt."), ephemeral=True)


class LeaveConfirmView(discord.ui.View):
    def __init__(self, team: dict, is_owner: bool):
        super().__init__(timeout=60)
        self.team = team
        self.is_owner = is_owner

    @discord.ui.button(label="Ja, bestätigen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        pool = get_pool()
        if self.is_owner:
            # Team wird komplett geloescht - Rolle/Nickname bei ALLEN Managern (Owner + Co-Manager)
            # zuruecksetzen, nicht nur beim Owner der gerade klickt.
            managers = await get_team_managers(self.team["id"])

            from cogs.moderation import withdraw_team_from_open_tournaments
            await withdraw_team_from_open_tournaments(interaction.client, self.team["id"], self.team["name"])

            # Team NICHT hart loeschen - schlaegt bei bereits gespielten Matches mit einem
            # Fremdschluessel-Fehler fehl (tournament_matches referenziert team1_id/team2_id).
            # Das war bisher der Grund fuer "Interaktion fehlgeschlagen"/keine Bot-Antwort:
            # die Exception flog hier, BEVOR ueberhaupt geantwortet wurde. Stattdessen wie bei
            # der Admin-Aufloesung als aufgeloest markieren statt hart zu loeschen.
            await pool.execute("UPDATE teams SET dissolved_at = now() WHERE id = $1", self.team["id"])
            await pool.execute("DELETE FROM team_managers WHERE team_id = $1", self.team["id"])
            from audit import log_action
            await log_action(interaction.guild_id, interaction.user, "team.deleted", "team", self.team["id"], self.team["name"])
            for m in managers:
                member = await fetch_member_safe(interaction.guild, m["discord_id"])
                if not member:
                    continue
                role_column = "vm_role_id" if m["role"] == "owner" else "co_manager_role_id"
                await _toggle_configured_role(interaction.guild, member, role_column, grant=False)
                await reset_team_nickname(member)
            await interaction.response.edit_message(content=f"🗑️ Team **{self.team['name']}** wurde gelöscht.", view=None)
            if self.team.get("stream_link"):
                await refresh_stream_list(interaction.client, interaction.guild)
            await refresh_team_overview(interaction.client, interaction.guild)
        else:
            await pool.execute(
                "DELETE FROM team_managers WHERE team_id = $1 AND discord_id = $2",
                self.team["id"], interaction.user.id,
            )
            await _toggle_configured_role(interaction.guild, interaction.user, "co_manager_role_id", grant=False)
            await reset_team_nickname(interaction.user)
            await refresh_team_overview(interaction.client, interaction.guild)
            await interaction.response.edit_message(content=f"👋 Du hast **{self.team['name']}** verlassen.", view=None)

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Abgebrochen.", view=None)


async def dissolve_team_by_admin(bot: commands.Bot, guild: discord.Guild, team: dict, actor: discord.abc.User, reason: str | None = None):
    """Loest ein Team administrativ auf (Admin-Panel) - wie das Selbst-Loeschen durch den
    Owner (Team-Manager-Panel -> Verlassen/Loeschen), aber vom Admin ausgeloest, zieht das
    Team zusaetzlich aus allen noch offenen Turnier-Anmeldungen zurueck und benachrichtigt
    alle Manager per DM."""
    pool = get_pool()
    managers = await get_team_managers(team["id"])

    from cogs.moderation import withdraw_team_from_open_tournaments
    await withdraw_team_from_open_tournaments(bot, team["id"], team["name"])

    # Team NICHT hart loeschen - schlaegt bei bereits gespielten Matches mit einem
    # Fremdschluessel-Fehler fehl (tournament_matches referenziert team1_id/team2_id)
    # und wuerde die Spielhistorie/Statistiken zerstoeren. Stattdessen als aufgeloest
    # markieren und alle Manager-Zuordnungen entfernen - macht das Team fuer alle
    # praktischen Zwecke (Turnier-Anmeldung, Team-Manager-Panel) inaktiv, behaelt
    # aber Name/Logo/Historie fuer Turnierstatistiken und Hall of Fame.
    await pool.execute("UPDATE teams SET dissolved_at = now() WHERE id = $1", team["id"])
    await pool.execute("DELETE FROM team_managers WHERE team_id = $1", team["id"])
    from audit import log_action
    detail = f"durch Admin aufgelöst" + (f" - Grund: {reason}" if reason else "")
    await log_action(guild.id, actor, "team.deleted", "team", team["id"], f"{team['name']} ({detail})")

    dm_text = f"🚫 Dein Team **{team['name']}** wurde von der Turnierleitung aufgelöst."
    if reason:
        dm_text += f"\n**Grund:** {reason}"

    for m in managers:
        member = await fetch_member_safe(guild, m["discord_id"])
        if member:
            role_column = "vm_role_id" if m["role"] == "owner" else "co_manager_role_id"
            await _toggle_configured_role(guild, member, role_column, grant=False)
            await reset_team_nickname(member)
        try:
            user = member or await bot.fetch_user(m["discord_id"])
            await user.send(dm_text)
        except discord.HTTPException:
            pass

    if team.get("stream_link"):
        await refresh_stream_list(bot, guild)
    await refresh_team_overview(bot, guild)


# ---------- Persistentes Hauptpanel (Components V2) ----------

class TeamManagerPanel(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        self.banner_file = discord.File(BANNER_PATH, filename="banner.jpg")
        intro = discord.ui.TextDisplay(
            "# 🧢 Team Manager\n"
            "Zentrale Anlaufstelle für alles rund um deinen Verein im FIFA Elite Cup."
        )
        link_block = discord.ui.TextDisplay(
            "### 🔗 Team gründen\n"
            "> koppelt deinen EA FC Pro Club mit deinem Discord-Account\n"
            "> der Club-Name wird direkt von der EA API übernommen\n"
            "> optional: Twitch- oder YouTube-Link direkt mit anlegen"
        )
        features_block = discord.ui.TextDisplay(
            "### ⚙️ Was du hier sonst noch einstellen kannst\n"
            "**Stream-Link** — jederzeit änderbar\n"
            "**Logo** — PNG, JPG oder WEBP hochladen\n"
            "**Co-Manager** — dürfen ebenfalls Ergebnisse für dein Team eintragen\n"
            "**Mein Team** — zeigt Kader, Statistiken und aktuelle Einstellungen"
        )
        note_block = discord.ui.TextDisplay(
            "### ℹ️ Hinweis\n"
            "Spielst du nur mit, ohne selbst Team-Verantwortung zu haben? Dann brauchst du hier "
            "nichts zu tun — dieser Bereich ist ausschließlich für Vereinsmanager.\n"
            "Achte beim Verknüpfen darauf, dass der EA FC Pro Club Name exakt übereinstimmt.\n"
            "-# FIFA Elite Cup"
        )
        container = discord.ui.Container(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media="attachment://banner.jpg"),
            ),
            intro,
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
            link_block,
            discord.ui.Separator(),
            features_block,
            discord.ui.Separator(),
            note_block,
            discord.ui.ActionRow(
                discord.ui.Button(label="Team gründen", style=discord.ButtonStyle.primary, custom_id="team:create"),
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
                discord.ui.Button(label="Umbenennen", style=discord.ButtonStyle.secondary, custom_id="team:rename"),
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
        self._refresh_logos_task.start()

    def cog_unload(self):
        self._refresh_logos_task.cancel()

    @tasks.loop(hours=6)
    async def _refresh_logos_task(self):
        try:
            await refresh_all_team_logo_urls(self.bot)
        except Exception:
            log.exception("Fehler beim periodischen Auffrischen der Team-Logo-URLs")

    @_refresh_logos_task.before_loop
    async def _before_refresh_logos(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="team_manager_setup", description="Postet das Team-Manager-Panel in diesem Kanal (Admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def team_manager_setup(self, interaction: discord.Interaction):
        panel = TeamManagerPanel()
        await interaction.response.send_message(view=success_embed("Team-Manager-Panel wird gepostet..."), ephemeral=True)
        await interaction.channel.send(view=panel, files=[panel.banner_file])
        pool = get_pool()
        await pool.execute(
            "INSERT INTO guild_settings (guild_id, team_register_channel_id) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET team_register_channel_id = $2",
            interaction.guild_id, interaction.channel_id,
        )

    @app_commands.command(name="team_overview_setup", description="Legt diesen Kanal als Live-Vereins-Übersicht fest (Admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def team_overview_setup(self, interaction: discord.Interaction):
        pool = get_pool()
        await pool.execute(
            "INSERT INTO team_overview_panel (guild_id, channel_id, message_ids) VALUES ($1, $2, '{}') "
            "ON CONFLICT (guild_id) DO UPDATE SET channel_id = $2, message_ids = '{}'",
            interaction.guild_id, interaction.channel_id,
        )
        await interaction.response.send_message(view=success_embed("Vereins-Übersicht wird eingerichtet..."), ephemeral=True)
        await refresh_team_overview(interaction.client, interaction.guild)

    @app_commands.command(name="club_stats", description="Zeigt EA-Club-Statistiken eines Teams: letzte Friendlys, Liga, Kader")
    @app_commands.describe(team="Name des Teams (auf diesem Server)")
    async def club_stats(self, interaction: discord.Interaction, team: str):
        await interaction.response.defer(thinking=True)
        pool = get_pool()
        row = await pool.fetchrow(
            "SELECT * FROM teams WHERE guild_id = $1 AND LOWER(name) = LOWER($2)", interaction.guild_id, team
        )
        if not row:
            await interaction.followup.send(view=error_embed(f'Kein Team namens "{team}" gefunden.'))
            return
        if not row["ea_club_id"]:
            await interaction.followup.send(view=error_embed(f'{row["name"]} hat keinen verknüpften EA-Club.'))
            return

        club_id = row["ea_club_id"]
        platform = row["ea_platform"] or PLATFORM_DEFAULT

        club_info, seasonal, matches, members = {}, {}, [], []
        async with EAProClubsAPI() as api:
            try:
                club_info = await api.get_club_info(club_id, platform)
            except Exception:
                log.warning(f"get_club_info fehlgeschlagen fuer Team {row['id']}", exc_info=True)
            try:
                seasonal = await api.get_seasonal_stats(club_id, platform)
            except Exception:
                log.warning(f"get_seasonal_stats fehlgeschlagen fuer Team {row['id']}", exc_info=True)
            try:
                matches = await api.get_matches(club_id, platform, "friendlyMatch", max_results=5)
            except Exception:
                log.warning(f"get_matches fehlgeschlagen fuer Team {row['id']}", exc_info=True)
            try:
                members = await api.get_members(club_id, platform)
            except Exception:
                log.warning(f"get_members fehlgeschlagen fuer Team {row['id']}", exc_info=True)

        cup_stats = await pool.fetchrow(
            """
            SELECT
              (SELECT COUNT(*) FROM tournaments WHERE winner_champion_id = $1) AS cup_titles,
              (SELECT COUNT(*) FROM tournaments WHERE loser_champion_id = $1) AS loser_bracket_titles,
              (SELECT COUNT(*) FROM tournaments WHERE winner_bracket_third_id = $1) AS third_places,
              COUNT(*) FILTER (WHERE tm.winner_id = $1) AS wins,
              COUNT(*) FILTER (WHERE tm.winner_id IS NULL AND tm.status = 'completed') AS draws,
              COUNT(*) FILTER (WHERE tm.status = 'completed' AND tm.winner_id IS NOT NULL AND tm.winner_id != $1) AS losses,
              COALESCE(SUM(CASE WHEN tm.team1_id = $1 THEN tm.team1_score WHEN tm.team2_id = $1 THEN tm.team2_score ELSE 0 END), 0) AS goals_for,
              COALESCE(SUM(CASE WHEN tm.team1_id = $1 THEN tm.team2_score WHEN tm.team2_id = $1 THEN tm.team1_score ELSE 0 END), 0) AS goals_against
            FROM tournament_matches tm
            WHERE (tm.team1_id = $1 OR tm.team2_id = $1) AND tm.status = 'completed'
            """,
            row["id"],
        )

        if not club_info and not matches and not members:
            await interaction.followup.send(view=error_embed("EA-API gerade nicht erreichbar. Später erneut versuchen."))
            return

        items = [discord.ui.TextDisplay(f"# 📊 {row['name']}\nEA-Club: **{row['ea_club_name'] or '?'}**")]

        division = seasonal.get("bestDivision") or seasonal.get("currentDivision") or seasonal.get("division")
        league_points = seasonal.get("leaguePoints") or seasonal.get("skillRating")
        cup_has_data = cup_stats and (cup_stats["wins"] or cup_stats["draws"] or cup_stats["losses"])

        # Freundschaftsspiele zuerst auswerten (wird unten fuer Text UND als Karten-Fallback gebraucht,
        # falls das Team noch keine Cup-Historie hat - sonst waere die Karte bei neuen Teams komplett leer).
        player_totals: dict[str, dict] = {}
        friendly_lines = []
        friendly_wins = friendly_draws = friendly_losses = 0
        friendly_goals_for = friendly_goals_against = 0
        for m in matches[:5]:
            clubs = m.get("clubs", {})
            club_ids = list(clubs.keys())
            if len(club_ids) < 2:
                continue
            opponent_id = club_ids[0] if club_ids[1] == str(club_id) else club_ids[1]
            my_goals = clubs.get(str(club_id), {}).get("goals", "?")
            opp_goals = clubs.get(opponent_id, {}).get("goals", "?")
            opp_name = clubs.get(opponent_id, {}).get("details", {}).get("name", "Unbekannt")
            friendly_lines.append(f"`{my_goals}:{opp_goals}` vs. {opp_name}")
            try:
                mg, og = int(my_goals), int(opp_goals)
                friendly_goals_for += mg
                friendly_goals_against += og
                if mg > og:
                    friendly_wins += 1
                elif mg < og:
                    friendly_losses += 1
                else:
                    friendly_draws += 1
            except (TypeError, ValueError):
                pass

            club_players = m.get("players", {}).get(str(club_id))
            if club_players:
                for player_id, p in club_players.items():
                    name = p.get("playername") or p.get("proName") or f"Player {player_id}"
                    if player_id not in player_totals:
                        player_totals[player_id] = {"name": name, "matches": 0, "goals": 0, "assists": 0, "rating_sum": 0.0}
                    entry = player_totals[player_id]
                    entry["matches"] += 1
                    entry["goals"] += int(p.get("goals") or 0)
                    entry["assists"] += int(p.get("assists") or 0)
                    try:
                        entry["rating_sum"] += float(p.get("rating") or 0)
                    except (TypeError, ValueError):
                        pass

        from graphics import render_club_stats_card
        division_text = f"Division {division}" + (f" · {league_points} Punkte" if league_points else "") if division else None
        medals = []
        if cup_has_data:
            if cup_stats["cup_titles"]:
                medals.append(f"🥇×{cup_stats['cup_titles']}")
            if cup_stats["loser_bracket_titles"]:
                medals.append(f"🥈×{cup_stats['loser_bracket_titles']}")
            if cup_stats["third_places"]:
                medals.append(f"🥉×{cup_stats['third_places']}")
        if cup_has_data:
            gd = cup_stats["goals_for"] - cup_stats["goals_against"]
            record_text = f"Cup: {cup_stats['wins']}S {cup_stats['draws']}U {cup_stats['losses']}N"
            goals_text = f"Tore {cup_stats['goals_for']}:{cup_stats['goals_against']} (Diff {gd:+d})"
        elif friendly_wins or friendly_draws or friendly_losses:
            fgd = friendly_goals_for - friendly_goals_against
            record_text = f"Friendlys: {friendly_wins}S {friendly_draws}U {friendly_losses}N"
            goals_text = f"Tore {friendly_goals_for}:{friendly_goals_against} (Diff {fgd:+d})"
        else:
            record_text = None
            goals_text = None
        buf = await render_club_stats_card(
            row["name"], row["ea_club_name"], row.get("logo_url"), division_text, medals, record_text, goals_text
        )
        stats_card_file = discord.File(buf, filename="stats_card.png")
        items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://stats_card.png")))

        if division or league_points or cup_has_data:
            items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large))
            block = ["### 🏆 Titel & Liga"]
            if division:
                block.append(f"Division `{division}`" + (f" · `{league_points}` Punkte" if league_points else ""))
            if cup_has_data:
                medals = []
                if cup_stats["cup_titles"]:
                    medals.append(f"🥇 `{cup_stats['cup_titles']}×` Turniersieger")
                if cup_stats["loser_bracket_titles"]:
                    medals.append(f"🥈 `{cup_stats['loser_bracket_titles']}×` Loser-Bracket-Sieger")
                if cup_stats["third_places"]:
                    medals.append(f"🥉 `{cup_stats['third_places']}×` Dritter")
                block += medals
                goal_diff = cup_stats["goals_for"] - cup_stats["goals_against"]
                block.append(f"Bilanz: `{cup_stats['wins']}S {cup_stats['draws']}U {cup_stats['losses']}N`")
                block.append(f"Tore: `{cup_stats['goals_for']}:{cup_stats['goals_against']}` (Diff. `{goal_diff:+d}`)")
            items.append(discord.ui.TextDisplay("\n".join(block)))

        if friendly_lines:
            items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large))
            items.append(discord.ui.TextDisplay("### ⚽ Letzte Friendlys\n" + "\n".join(friendly_lines)))

        if player_totals:
            medals = ["🥇", "🥈", "🥉"]
            sorted_players = sorted(player_totals.values(), key=lambda p: p["goals"], reverse=True)
            block = [
                "### 🎮 Spieler dieser Friendlys",
                "-# Nur Spieler, die in den letzten 5 Freundschaftsspielen mitgespielt haben",
                "",
            ]
            for i, p in enumerate(sorted_players):
                prefix = medals[i] if i < 3 else f"`{i + 1}.`"
                block.append(f"{prefix} **{p['name']}** — `{p['goals']}` ⚽ `{p['assists']}` 🅰️ · Ø `{p['rating_sum'] / p['matches']:.1f}`")
            items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
            items.append(discord.ui.TextDisplay("\n".join(block)))

        if members:
            sorted_members = sorted(members, key=lambda p: float(p.get("proOverallRating", 0) or 0), reverse=True)
            items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large))
            names = [p.get("name", "?") for p in sorted_members]
            block = [f"### 👥 Kader (`{len(names)}`)", ""]
            chunk_lines = []
            char_count = 0
            for name in names:
                chunk_lines.append(f"`{name}`")
                char_count += len(name) + 3
                if char_count > 3500:
                    break
            block.append(" · ".join(chunk_lines))
            items.append(discord.ui.TextDisplay("\n".join(block)))

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(*items, accent_color=discord.Color.gold()))
        await interaction.followup.send(view=view, files=[stats_card_file])

    @club_stats.autocomplete("team")
    async def club_stats_autocomplete(self, interaction: discord.Interaction, current: str):
        pool = get_pool()
        rows = await pool.fetch(
            "SELECT name FROM teams WHERE guild_id = $1 AND name ILIKE $2 ORDER BY name LIMIT 25",
            interaction.guild_id, f"%{current}%",
        )
        return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]

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

        if action == "overviewsearch":
            await interaction.response.send_modal(TeamOverviewSearchModal())
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
                await interaction.response.send_message(view=error_embed("Nur der Vereinsmanager kann Co-Manager verwalten."), ephemeral=True)
                return
            await interaction.response.send_message(content="Co-Manager verwalten:", view=CoManagerView(team), ephemeral=True)

        elif action == "rename":
            role = await get_role_for_user(team["id"], interaction.user.id)
            if role != "owner":
                await interaction.response.send_message(view=error_embed("Nur der Vereinsmanager kann das Team umbenennen."), ephemeral=True)
                return
            await interaction.response.send_modal(TeamRenameModal(team["id"], team["name"]))

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
