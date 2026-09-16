"""
Admin-Panel - Components V2, aehnlich dem Team-Manager-Panel.

Postet ein persistentes Panel (/admin_panel_setup, Admin-only) mit:
- "Turnier erstellen" -> oeffnet dasselbe Modal wie /tournament_create
- "Turniere verwalten" -> Auswahl-Menu mit allen Turnieren dieser Guild,
  danach ein Unterpanel mit allen Admin-Aktionen fuer das gewaehlte Turnier.
- "DM an alle Vereinsmanager" -> eigene Nachricht an alle Team-Manager server-weit.

Alle Antworten des Bots werden als Embeds gestaltet (statt reinem Text).
"""
from __future__ import annotations
import logging
import os
import discord
from discord import app_commands
from discord.ext import commands

import asyncpg

from db import get_pool
from ui_helpers import info_embed, success_embed, error_embed, warning_embed, WEBSITE_URL
from cogs.tournament_manager import (
    TournamentCreateModal,
    get_tournament,
    get_signup_counts,
    get_registered_teams,
    get_waitlisted_teams,
    get_all_teams_for_swap,
    get_unready_groups,
    get_unready_teams,
    start_group_phase,
    release_first_matchday,
    start_knockout_phase,
    reset_knockout_phase,
    reset_group_phase,
    all_groups_complete,
    refresh_panel,
    get_all_open_matches,
    get_all_completed_matches,
    get_group_standings,
    team_name_map,
    GroupMatchSelect,
    ScoreModal,
    swap_team_for_bye,
    swap_team_for_waitlisted,
    cleanup_tournament_channels,
    get_pool_team,
)
from cogs.stats_manager import post_bracket_stats, StatsChannelsConfigView
from cogs.moderation import (
    PlayerBanView, TeamBanView, TeamBanSearchModal, UnbanSelect,
    get_all_bans, get_all_team_bans, get_all_guild_teams,
)
from cogs.team_manager import (
    get_team_managers, EditFieldModal, LogoUploadModal, CoManagerView, is_valid_twitch_link, apply_team_nickname,
    dissolve_team_by_admin,
)
from permissions import is_tournament_admin, is_tournament_moderator, can_correct_results
from cogs.embed_builder import EmbedBuilderModal

log = logging.getLogger("fifa-elite-cup")


def status_label(t: dict) -> str:
    status_map = {"open": "Anmeldung offen", "closed": "Anmeldung geschlossen", "started": "Läuft", "finished": "Beendet"}
    phase_map = {"signup": "", "groups": " (Gruppenphase)", "knockout": " (KO-Phase)", "finished": ""}
    return status_map.get(t["status"], t["status"]) + phase_map.get(t.get("phase", "signup"), "")


class TournamentSelect(discord.ui.View):
    def __init__(self, tournaments: list[dict]):
        super().__init__(timeout=120)
        select = discord.ui.Select(
            placeholder="Turnier auswählen...",
            options=[
                discord.SelectOption(label=f"#{t['id']} - {t['name']}"[:100], value=str(t["id"]))
                for t in tournaments[:25]
            ],
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        tournament_id = int(interaction.data["values"][0])
        t = await get_tournament(tournament_id)
        if not t:
            await interaction.response.send_message(view=error_embed("Turnier nicht gefunden."), ephemeral=True)
            return
        registered, waitlist = await get_signup_counts(tournament_id)
        summary = (
            f"### {t['name']} (ID `{t['id']}`)\n"
            f"**Status:** {status_label(t)}\n"
            f"**Min/Max Teams:** `{t['min_teams']}` / `{t['max_teams']}`\n"
            f"**Angemeldet:** `{registered}` · Warteliste: `{waitlist}`"
        )
        await interaction.response.send_message(content=summary, view=TournamentAdminView(t), ephemeral=True)


class GroupReadinessOverrideView(discord.ui.View):
    """Notausgang, falls Spieltag 1 trotz fehlender 'Team ist da'-Bestaetigungen freigegeben werden soll."""

    def __init__(self, tournament_id: int):
        super().__init__(timeout=120)
        self.tournament_id = tournament_id

    @discord.ui.button(label="Trotzdem freigeben", style=discord.ButtonStyle.danger)
    async def force_release(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await release_first_matchday(interaction.client, interaction.guild, self.tournament_id)
        await interaction.followup.send(
            view=success_embed("Spieltag 1 wurde ohne vollständigen Team-Check-in freigegeben."), ephemeral=True
        )

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=None, view=info_embed("Abgebrochen."))


class MatchSearchModal(discord.ui.Modal, title="Match suchen"):
    """Bei vielen gleichzeitig offenen/abgeschlossenen Matches (z.B. 6er-Gruppen mit vielen
    Teams) zeigt ein Discord-Select maximal 25 Optionen - alles danach war bisher unsichtbar
    und nicht auswaehlbar. Sucht per Team-Name vor, genau wie die Team-Sperren-Suche."""
    query = discord.ui.TextInput(label="Team-Name (auch Teilstring reicht)", max_length=60)

    def __init__(self, tournament_id: int, mode: str):
        super().__init__()
        self.tournament_id = tournament_id
        self.mode = mode  # "open" oder "completed"

    async def on_submit(self, interaction: discord.Interaction):
        matches = await get_all_open_matches(self.tournament_id) if self.mode == "open" else await get_all_completed_matches(self.tournament_id)
        team_ids = [m["team1_id"] for m in matches] + [m["team2_id"] for m in matches]
        names = await team_name_map(team_ids)
        query_lower = self.query.value.lower()
        filtered = [
            m for m in matches
            if query_lower in (names.get(m["team1_id"], "") or "").lower()
            or query_lower in (names.get(m["team2_id"], "") or "").lower()
        ]
        if not filtered:
            await interaction.response.send_message(view=warning_embed(f'Kein Match mit "{self.query.value}" gefunden.'), ephemeral=True)
            return
        if self.mode == "open":
            await interaction.response.send_message(
                content=f"{len(filtered)} Treffer für \"{self.query.value}\" - welches Match?",
                view=GroupMatchSelect(filtered, names, is_admin=True),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                content=f"{len(filtered)} Treffer für \"{self.query.value}\" - welches Match korrigieren?",
                view=EditMatchSelectView(filtered, names),
                ephemeral=True,
            )


class MatchSearchPromptView(discord.ui.View):
    """Wird gezeigt, wenn es zu viele Matches fuer ein einzelnes Select gibt (>25) -
    Button oeffnet die Such-Modal statt direkt eine (unvollstaendige) Liste zu zeigen."""

    def __init__(self, tournament_id: int, mode: str, total_count: int):
        super().__init__(timeout=180)
        self.tournament_id = tournament_id
        self.mode = mode
        self.total_count = total_count

    @discord.ui.button(label="🔍 Match suchen", style=discord.ButtonStyle.primary)
    async def search(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MatchSearchModal(self.tournament_id, self.mode))


class EditMatchSelectView(discord.ui.View):
    """Auswahl eines bereits abgeschlossenen Matches zum nachtraeglichen Korrigieren."""

    def __init__(self, matches: list[dict], names: dict[int, str]):
        super().__init__(timeout=180)
        self.matches_by_id = {m["id"]: m for m in matches}
        self.names = names
        options = [
            discord.SelectOption(
                label=f"{names.get(m['team1_id'], '?')} {m['team1_score']}:{m['team2_score']} {names.get(m['team2_id'], '?')}"[:100],
                value=str(m["id"]),
            )
            for m in matches[:25]
        ]
        select = discord.ui.Select(placeholder="Match auswählen...", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        match_id = int(interaction.data["values"][0])
        match = self.matches_by_id.get(match_id)
        if not match:
            await interaction.response.send_message(view=error_embed("Match nicht gefunden."), ephemeral=True)
            return
        await interaction.response.send_modal(
            ScoreModal(
                match_id, match["team1_id"], match["team2_id"],
                self.names.get(match["team1_id"], "?"), self.names.get(match["team2_id"], "?"),
                is_admin=True,
                default_score1=match["team1_score"], default_score2=match["team2_score"],
            )
        )


class AdminRoleSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        select = discord.ui.RoleSelect(placeholder="Neue Admin-Rolle wählen...")
        select.callback = self.on_select
        self.add_item(select)

    @discord.ui.button(label="Entfernen (nur echte Admins)", style=discord.ButtonStyle.danger, row=1)
    async def remove_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        pool = get_pool()
        await pool.execute(
            "INSERT INTO guild_settings (guild_id, admin_role_id) VALUES ($1, NULL) "
            "ON CONFLICT (guild_id) DO UPDATE SET admin_role_id = NULL",
            interaction.guild_id,
        )
        await interaction.response.edit_message(
            view=success_embed("Admin-Rolle entfernt", "Nur noch echte Server-Administratoren haben Zugriff.")
        )

    async def on_select(self, interaction: discord.Interaction):
        role_id = int(interaction.data["values"][0])
        pool = get_pool()
        await pool.execute(
            "INSERT INTO guild_settings (guild_id, admin_role_id) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET admin_role_id = $2",
            interaction.guild_id, role_id,
        )
        await interaction.response.edit_message(
            view=success_embed("Admin-Rolle gesetzt", f"<@&{role_id}> kann jetzt zusätzlich zu echten Admins das Admin-Panel nutzen."),
        )


class GenericRoleSelectView(discord.ui.View):
    """Wie AdminRoleSelectView, aber wiederverwendbar fuer beliebige guild_settings-Rollenspalten."""

    def __init__(self, column: str, label: str):
        super().__init__(timeout=180)
        self.column = column
        self.label = label
        select = discord.ui.RoleSelect(placeholder=f"Neue {label} wählen...")
        select.callback = self.on_select
        self.add_item(select)

    @discord.ui.button(label="Entfernen", style=discord.ButtonStyle.danger, row=1)
    async def remove_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        pool = get_pool()
        await pool.execute(
            f"INSERT INTO guild_settings (guild_id, {self.column}) VALUES ($1, NULL) "
            f"ON CONFLICT (guild_id) DO UPDATE SET {self.column} = NULL",
            interaction.guild_id,
        )
        await interaction.response.edit_message(view=success_embed(f"{self.label} entfernt."))

    async def on_select(self, interaction: discord.Interaction):
        role_id = int(interaction.data["values"][0])
        pool = get_pool()
        await pool.execute(
            f"INSERT INTO guild_settings (guild_id, {self.column}) VALUES ($1, $2) "
            f"ON CONFLICT (guild_id) DO UPDATE SET {self.column} = $2",
            interaction.guild_id, role_id,
        )
        await interaction.response.edit_message(view=success_embed(f"{self.label} gesetzt", f"<@&{role_id}>"))


class TeamOverviewView(discord.ui.View):
    """Paginierte Uebersicht aller registrierten Teams der Guild (Name, EA-Club, Owner, Co-Manager)."""

    PAGE_SIZE = 10

    def __init__(self, lines: list[str], page: int = 0):
        super().__init__(timeout=180)
        self.lines = lines
        self.page = page
        self.max_page = max(0, (len(lines) - 1) // self.PAGE_SIZE)
        self.prev_button.disabled = page <= 0
        self.next_button.disabled = page >= self.max_page

    def content(self) -> str:
        start = self.page * self.PAGE_SIZE
        chunk = self.lines[start:start + self.PAGE_SIZE]
        return f"### Registrierte Teams (Seite {self.page + 1}/{self.max_page + 1})\n\n" + "\n\n".join(chunk)

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = TeamOverviewView(self.lines, self.page - 1)
        await interaction.response.edit_message(content=view.content(), view=view)

    @discord.ui.button(label="Weiter ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = TeamOverviewView(self.lines, self.page + 1)
        await interaction.response.edit_message(content=view.content(), view=view)


class AdminTournamentsMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(discord.ui.Button(label="Turnier erstellen", style=discord.ButtonStyle.primary, custom_id="admin:create"))
        self.add_item(discord.ui.Button(label="Turniere verwalten", style=discord.ButtonStyle.secondary, custom_id="admin:manage"))
        self.add_item(discord.ui.Button(label="Alle Teams anzeigen", style=discord.ButtonStyle.secondary, custom_id="admin:allteams"))


class AdminModerationMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(discord.ui.Button(label="Spieler sperren", style=discord.ButtonStyle.danger, custom_id="admin:banplayer"))
        self.add_item(discord.ui.Button(label="Team sperren", style=discord.ButtonStyle.danger, custom_id="admin:banteam"))
        self.add_item(discord.ui.Button(label="Sperren verwalten", style=discord.ButtonStyle.secondary, custom_id="admin:banlist"))
        self.add_item(discord.ui.Button(label="Ticket-System einstellen", style=discord.ButtonStyle.secondary, custom_id="admin:ticketconfig"))
        self.add_item(discord.ui.Button(label="Audit-Log", style=discord.ButtonStyle.secondary, custom_id="admin:auditlog"))


class AdminCommunicationMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(discord.ui.Button(label="Nachricht erstellen", style=discord.ButtonStyle.secondary, custom_id="admin:embed"))
        self.add_item(discord.ui.Button(label="DM an alle Vereinsmanager", style=discord.ButtonStyle.secondary, custom_id="admin:dmall"))


class AdminSystemMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(discord.ui.Button(label="Stats-Kanäle einstellen", style=discord.ButtonStyle.secondary, custom_id="admin:statschannels"))
        self.add_item(discord.ui.Button(label="Admin-Rolle festlegen", style=discord.ButtonStyle.secondary, custom_id="admin:setrole"))
        self.add_item(discord.ui.Button(label="Moderator-Rolle festlegen", style=discord.ButtonStyle.secondary, custom_id="admin:setmodrole"))
        self.add_item(discord.ui.Button(label="VM-Rolle festlegen", style=discord.ButtonStyle.secondary, custom_id="admin:setvmrole"))
        self.add_item(discord.ui.Button(label="Co-Manager-Rolle festlegen", style=discord.ButtonStyle.secondary, custom_id="admin:setcomanagerrole"))
        self.add_item(discord.ui.Button(label="Team-Nicknames aktualisieren", style=discord.ButtonStyle.secondary, custom_id="admin:syncnicknames"))
        self.add_item(discord.ui.Button(label="Stream-Liste aktualisieren", style=discord.ButtonStyle.secondary, custom_id="admin:refreshstreams"))
        self.add_item(discord.ui.Button(label="Team-Manager (Admin)", style=discord.ButtonStyle.secondary, custom_id="admin:teammanager"))
        self.add_item(discord.ui.Button(label="Live-Log-Kanal einstellen", style=discord.ButtonStyle.secondary, custom_id="admin:setauditchannel"))
        self.add_item(discord.ui.Button(label="Live-Ergebnis-Kanal einstellen", style=discord.ButtonStyle.secondary, custom_id="admin:setresultschannel"))
        self.add_item(discord.ui.Button(label="Spieler-Suche-Kanal einstellen", style=discord.ButtonStyle.secondary, custom_id="admin:setplayersearchchannel"))
        self.add_item(discord.ui.Button(label="Medien-Kanal einstellen", style=discord.ButtonStyle.secondary, custom_id="admin:setmediachannel"))


class AuditChannelSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        select = discord.ui.ChannelSelect(placeholder="Live-Log-Kanal wählen...", channel_types=[discord.ChannelType.text])
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        channel_id = int(interaction.data["values"][0])
        pool = get_pool()
        await pool.execute(
            "INSERT INTO guild_settings (guild_id, audit_log_channel_id) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET audit_log_channel_id = $2",
            interaction.guild_id, channel_id,
        )
        await interaction.response.edit_message(
            content=None, view=success_embed("Live-Log-Kanal gesetzt", f"<#{channel_id}>")
        )


class ResultsChannelSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        select = discord.ui.ChannelSelect(placeholder="Live-Ergebnis-Kanal wählen...", channel_types=[discord.ChannelType.text])
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        channel_id = int(interaction.data["values"][0])
        pool = get_pool()
        await pool.execute(
            "INSERT INTO guild_settings (guild_id, results_feed_channel_id) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET results_feed_channel_id = $2",
            interaction.guild_id, channel_id,
        )
        await interaction.response.edit_message(
            content=None, view=success_embed("Live-Ergebnis-Kanal gesetzt", f"<#{channel_id}>")
        )


class PlayerSearchChannelSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        select = discord.ui.ChannelSelect(placeholder="Spieler-Suche-Kanal wählen...", channel_types=[discord.ChannelType.text])
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        channel_id = int(interaction.data["values"][0])
        pool = get_pool()
        await pool.execute(
            "INSERT INTO guild_settings (guild_id, player_search_channel_id) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET player_search_channel_id = $2",
            interaction.guild_id, channel_id,
        )
        await interaction.response.edit_message(
            content=None,
            view=success_embed("Spieler-Suche-Kanal gesetzt", f"<#{channel_id}> — nur noch Vereinsmanager dürfen dort schreiben."),
        )


class MediaChannelSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        select = discord.ui.ChannelSelect(placeholder="Medien-Kanal wählen...", channel_types=[discord.ChannelType.text])
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        channel_id = int(interaction.data["values"][0])
        pool = get_pool()
        await pool.execute(
            "INSERT INTO guild_settings (guild_id, media_only_channel_id) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET media_only_channel_id = $2",
            interaction.guild_id, channel_id,
        )
        await interaction.response.edit_message(
            content=None,
            view=success_embed("Medien-Kanal gesetzt", f"<#{channel_id}> — reiner Text ist ab jetzt verboten, Bilder/Videos/Links bleiben erlaubt."),
        )


class TicketConfigView(discord.ui.View):
    """Konfiguration fuers Ticket-System: Kategorie, Log-Kanal, Support-Rolle - je ein Select."""

    def __init__(self):
        super().__init__(timeout=300)

        category_select = discord.ui.ChannelSelect(
            placeholder="Kategorie für neue Ticket-Kanäle wählen...", channel_types=[discord.ChannelType.category]
        )
        category_select.callback = self._make_channel_callback("ticket_category_id", "Ticket-Kategorie")
        self.add_item(category_select)

        log_select = discord.ui.ChannelSelect(
            placeholder="Log-Kanal für Transkripte wählen...", channel_types=[discord.ChannelType.text]
        )
        log_select.callback = self._make_channel_callback("ticket_log_channel_id", "Ticket-Log")
        self.add_item(log_select)

        role_select = discord.ui.RoleSelect(placeholder="Support-Rolle wählen (kann Tickets sehen/übernehmen/schließen)...")
        role_select.callback = self._role_callback
        self.add_item(role_select)

    def _make_channel_callback(self, field_name: str, label: str):
        async def callback(interaction: discord.Interaction):
            channel_id = int(interaction.data["values"][0])
            pool = get_pool()
            await pool.execute(
                f"INSERT INTO guild_settings (guild_id, {field_name}) VALUES ($1, $2) "
                f"ON CONFLICT (guild_id) DO UPDATE SET {field_name} = $2",
                interaction.guild_id, channel_id,
            )
            await interaction.response.send_message(view=success_embed(f"{label} gesetzt", f"<#{channel_id}>"), ephemeral=True)
        return callback

    async def _role_callback(self, interaction: discord.Interaction):
        role_id = int(interaction.data["values"][0])
        pool = get_pool()
        await pool.execute(
            "INSERT INTO guild_settings (guild_id, ticket_support_role_id) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET ticket_support_role_id = $2",
            interaction.guild_id, role_id,
        )
        await interaction.response.send_message(view=success_embed("Support-Rolle gesetzt", f"<@&{role_id}>"), ephemeral=True)


class ResetKoConfirmView(discord.ui.View):
    def __init__(self, tournament_id: int):
        super().__init__(timeout=120)
        self.tournament_id = tournament_id

    @discord.ui.button(label="Ja, zurücksetzen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await reset_knockout_phase(interaction.client, interaction.guild, self.tournament_id)
            t = await get_tournament(self.tournament_id)
            await start_knockout_phase(interaction.client, interaction.guild, self.tournament_id, t)
            from audit import log_action
            await log_action(interaction.guild_id, interaction.user, "tournament.bracket_created", "tournament", self.tournament_id, "KO-Phase zurückgesetzt & neu erstellt")
            await interaction.followup.send(view=success_embed("KO-Phase wurde zurückgesetzt und neu erstellt."), ephemeral=True)
        except Exception:
            log.exception(f"Fehler beim Zuruecksetzen/Neuerstellen der KO-Phase fuer Turnier {self.tournament_id}")
            await interaction.followup.send(
                view=error_embed(
                    "Fehler beim Zurücksetzen",
                    "Bitte im Bot-Log nachschauen (`sudo journalctl -u fifa-elite-cup-v2 -n 50 --no-pager`).",
                ),
                ephemeral=True,
            )

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=None, view=info_embed("Abgebrochen."))


class RegroupConfirmView(discord.ui.View):
    """Loescht die aktuelle Gruppenauslosung unwiderruflich und lost mit der gewaehlten
    Gruppengroesse neu aus - fuer den Fall, dass das Turnier-Format (4er/6er-Gruppen) noch
    waehrend laufender Gruppenphase geaendert werden muss."""

    def __init__(self, tournament_id: int, group_size_override: int | None):
        super().__init__(timeout=120)
        self.tournament_id = tournament_id
        self.group_size_override = group_size_override

    @discord.ui.button(label="Ja, neu auslosen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await reset_group_phase(interaction.client, interaction.guild, self.tournament_id, self.group_size_override)
            t = await get_tournament(self.tournament_id)
            await start_group_phase(interaction.client, interaction.guild, self.tournament_id, t)
            await release_first_matchday(interaction.client, interaction.guild, self.tournament_id)
            from audit import log_action
            await log_action(
                interaction.guild_id, interaction.user, "tournament.groups_redrawn",
                "tournament", self.tournament_id, "Gruppenphase zurückgesetzt & neu ausgelost",
            )
            await interaction.followup.send(
                view=success_embed(
                    "Gruppen wurden neu ausgelost",
                    "Alle bisherigen Gruppenergebnisse wurden dabei gelöscht. Spieltag 1 wurde direkt freigegeben.",
                ),
                ephemeral=True,
            )
        except Exception:
            log.exception(f"Fehler beim Neu-Auslosen der Gruppenphase fuer Turnier {self.tournament_id}")
            await interaction.followup.send(
                view=error_embed(
                    "Fehler beim Neu-Auslosen",
                    "Bitte im Bot-Log nachschauen (`sudo journalctl -u fifa-elite-cup-v2 -n 50 --no-pager`).",
                ),
                ephemeral=True,
            )

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=None, view=info_embed("Abgebrochen."))


class RegroupSizeView(discord.ui.View):
    """Auswahl der neuen Gruppengroesse, bevor die laufende Gruppenphase neu ausgelost wird."""

    def __init__(self, tournament_id: int):
        super().__init__(timeout=120)
        select = discord.ui.Select(
            placeholder="Neue Gruppengröße...",
            options=[
                discord.SelectOption(label="Nur 4er-Gruppen", value="4", description="Mehr, kleinere Gruppen - 3 Spieltage pro Gruppe."),
                discord.SelectOption(label="6er-Gruppen bevorzugt", value="6", description="Faellt automatisch auf 4er zurueck, wenn nicht durch 6 teilbar."),
            ],
        )
        select.callback = self.on_select
        self.tournament_id = tournament_id
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        value = int(interaction.data["values"][0])
        override = None if value == 4 else value
        label = "4er-Gruppen" if override is None else "6er-Gruppen bevorzugt"
        await interaction.response.send_message(
            content=(
                "⚠️ **Sicher?** Löscht alle bestehenden Gruppen-Kanäle, -Rollen und Gruppen-Ergebnisse "
                f"unwiderruflich und lost alle angemeldeten Teams neu aus ({label}). Die KO-Phase ist davon "
                "nicht betroffen (muss vorher separat über 'KO-Phase resetten' zurückgesetzt sein, falls sie schon lief)."
            ),
            view=RegroupConfirmView(self.tournament_id, override),
            ephemeral=True,
        )


class DonationConfigModal(discord.ui.Modal, title="Spendenturnier einrichten"):
    donation_info = discord.ui.TextInput(
        label="Zahlungsdetails (PayPal/IBAN/etc.)", style=discord.TextStyle.paragraph, required=True, max_length=1000
    )

    def __init__(self, tournament_id: int):
        super().__init__()
        self.tournament_id = tournament_id

    async def on_submit(self, interaction: discord.Interaction):
        pool = get_pool()
        await pool.execute(
            "UPDATE tournaments SET is_donation_tournament = true, donation_info = $1 WHERE id = $2",
            self.donation_info.value, self.tournament_id,
        )
        await interaction.response.send_message(
            view=success_embed(
                "Spendenturnier aktiviert",
                "Ab jetzt bekommt jedes neu angemeldete Team automatisch einen privaten Zahlungs-Kanal mit diesen Details.",
            ),
            ephemeral=True,
        )


class DonationDeactivateView(discord.ui.View):
    def __init__(self, tournament_id: int):
        super().__init__(timeout=120)
        self.tournament_id = tournament_id

    @discord.ui.button(label="Zahlungsdetails ändern", style=discord.ButtonStyle.secondary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DonationConfigModal(self.tournament_id))

    @discord.ui.button(label="Spendenturnier deaktivieren", style=discord.ButtonStyle.danger)
    async def deactivate(self, interaction: discord.Interaction, button: discord.ui.Button):
        pool = get_pool()
        await pool.execute("UPDATE tournaments SET is_donation_tournament = false WHERE id = $1", self.tournament_id)
        await interaction.response.edit_message(
            content="Spendenturnier-Funktion deaktiviert. Neue Anmeldungen lösen keinen Zahlungs-Kanal mehr aus.",
            view=None,
        )


class AdminTeamNameModal(discord.ui.Modal, title="Team-Namen ändern"):
    new_name = discord.ui.TextInput(label="Neuer Team-Name", max_length=60)

    def __init__(self, team_id: int, current_name: str):
        super().__init__()
        self.team_id = team_id
        self.new_name.default = current_name

    async def on_submit(self, interaction: discord.Interaction):
        pool = get_pool()
        try:
            await pool.execute("UPDATE teams SET name = $1 WHERE id = $2", self.new_name.value, self.team_id)
        except asyncpg.UniqueViolationError:
            await interaction.response.send_message(
                view=error_embed(f'Ein Team namens "{self.new_name.value}" existiert auf diesem Server bereits.'),
                ephemeral=True,
            )
            return
        managers = await get_team_managers(self.team_id)
        for m in managers:
            member = interaction.guild.get_member(m["discord_id"])
            if member:
                await apply_team_nickname(member, self.new_name.value)
        await interaction.response.send_message(
            view=success_embed("Team umbenannt", f"Alle laufenden Anzeigen (Panels, Live-Spielplan, etc.) ziehen den neuen Namen automatisch."),
            ephemeral=True,
        )


class AdminTeamEditView(discord.ui.View):
    def __init__(self, team: dict):
        super().__init__(timeout=300)
        self.team = team

    @discord.ui.button(label="Team-Namen ändern", style=discord.ButtonStyle.primary)
    async def edit_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdminTeamNameModal(self.team["id"], self.team["name"]))

    @discord.ui.button(label="EA-Club-Namen ändern", style=discord.ButtonStyle.secondary)
    async def edit_ea_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            EditFieldModal(self.team["id"], "ea_club_name", "EA-Club-Name", self.team.get("ea_club_name"))
        )

    @discord.ui.button(label="Stream-Link ändern", style=discord.ButtonStyle.secondary)
    async def edit_stream(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            EditFieldModal(self.team["id"], "stream_link", "Stream-Link", self.team.get("stream_link"))
        )

    @discord.ui.button(label="Logo ändern", style=discord.ButtonStyle.secondary)
    async def edit_logo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LogoUploadModal(self.team["id"]))

    @discord.ui.button(label="Co-Manager verwalten", style=discord.ButtonStyle.secondary, row=1)
    async def manage_comanagers(self, interaction: discord.Interaction, button: discord.ui.Button):
        managers = await get_team_managers(self.team["id"])
        lines = [f"<@{m['discord_id']}> ({m['role']})" for m in managers]
        await interaction.response.send_message(
            content=f"**Aktuelle Manager von {self.team['name']}:**\n" + "\n".join(lines),
            view=CoManagerView(self.team),
            ephemeral=True,
        )

    @discord.ui.button(label="Team auflösen", style=discord.ButtonStyle.danger, row=1)
    async def dissolve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DissolveTeamModal(self.team))


class DissolveTeamModal(discord.ui.Modal):
    reason = discord.ui.TextInput(label="Grund (optional, wird per DM mitgeteilt)", required=False, max_length=200)

    def __init__(self, team: dict):
        super().__init__(title=f"Team auflösen: {team['name']}"[:45])
        self.team = team

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await dissolve_team_by_admin(interaction.client, interaction.guild, self.team, interaction.user, self.reason.value or None)
        await interaction.followup.send(
            view=success_embed(f"Team {self.team['name']} wurde aufgelöst", "Alle Manager wurden per DM benachrichtigt."),
            ephemeral=True,
        )


class AdminTeamSelectView(discord.ui.View):
    def __init__(self, teams: list[dict]):
        super().__init__(timeout=180)
        self.teams_by_id = {t["id"]: t for t in teams}
        options = [discord.SelectOption(label=t["name"][:100], value=str(t["id"])) for t in teams[:25]]
        select = discord.ui.Select(placeholder="Team auswählen...", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        team_id = int(interaction.data["values"][0])
        team = self.teams_by_id.get(team_id)
        if not team:
            await interaction.response.send_message(view=error_embed("Team nicht gefunden."), ephemeral=True)
            return
        await interaction.response.send_message(
            content=f"**{team['name']}** bearbeiten:",
            view=AdminTeamEditView(team),
            ephemeral=True,
        )

    @discord.ui.button(label="🔍 Team suchen (falls nicht gelistet)", style=discord.ButtonStyle.secondary, row=1)
    async def search(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdminTeamSearchModal())


class AdminTeamSearchModal(discord.ui.Modal, title="Team suchen"):
    query = discord.ui.TextInput(label="Team-Name (auch Teilstring reicht)", max_length=60)

    async def on_submit(self, interaction: discord.Interaction):
        pool = get_pool()
        rows = await pool.fetch(
            "SELECT * FROM teams WHERE guild_id = $1 AND name ILIKE $2 ORDER BY name LIMIT 25",
            interaction.guild_id, f"%{self.query.value}%",
        )
        if not rows:
            await interaction.response.send_message(view=warning_embed(f'Kein Team gefunden, das zu "{self.query.value}" passt.'), ephemeral=True)
            return
        teams = [dict(r) for r in rows]
        await interaction.response.send_message(
            content=f'Treffer für "{self.query.value}" — welches Team bearbeiten?',
            view=AdminTeamSelectView(teams),
            ephemeral=True,
        )


class SwapOutSelectView(discord.ui.View):
    """Auswahl, welches registrierte Team ausgetauscht werden soll."""

    def __init__(self, tournament_id: int, teams: list[dict]):
        super().__init__(timeout=180)
        self.tournament_id = tournament_id
        self.team_names = {t["id"]: t["name"] for t in teams}
        options = [discord.SelectOption(label=t["name"][:100], value=str(t["id"])) for t in teams[:25]]
        select = discord.ui.Select(placeholder="Team auswählen (wird entfernt)...", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        team_id = int(interaction.data["values"][0])
        team_name = self.team_names.get(team_id, f"Team {team_id}")
        await interaction.response.send_message(
            content=f"**{team_name}** austauschen — wie?",
            view=SwapActionChoiceView(self.tournament_id, team_id, team_name),
            ephemeral=True,
        )


class SwapActionChoiceView(discord.ui.View):
    def __init__(self, tournament_id: int, team_id: int, team_name: str):
        super().__init__(timeout=180)
        self.tournament_id = tournament_id
        self.team_id = team_id
        self.team_name = team_name

    @discord.ui.button(label="Entfernen (Freilos)", style=discord.ButtonStyle.danger)
    async def to_bye(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        from cogs.tournament_manager import get_team_group, remove_team_from_group_as_bye, refresh_group_panel, refresh_live_schedule
        group = await get_team_group(self.tournament_id, self.team_id)
        if group:
            # Gruppenphase laeuft schon - Team sauber als Freilos raus, ohne Forfeit-Siege
            # zu verteilen (im Unterschied zu 'Team verlaesst Turnier').
            await remove_team_from_group_as_bye(interaction.client, interaction.guild, self.tournament_id, group["id"], self.team_id)
            await refresh_group_panel(interaction.client, group["id"])
            await refresh_live_schedule(interaction.client, interaction.guild, self.tournament_id)
        else:
            await swap_team_for_bye(self.tournament_id, self.team_id)
            await refresh_panel(interaction.client, self.tournament_id)
        await interaction.edit_original_response(
            content=None, view=success_embed(f"{self.team_name} wurde entfernt, der Platz bleibt frei (Freilos).")
        )

    @discord.ui.button(label="Durch anderes Team ersetzen", style=discord.ButtonStyle.primary)
    async def to_waitlist_swap(self, interaction: discord.Interaction, button: discord.ui.Button):
        candidates = await get_all_teams_for_swap(interaction.guild_id, self.tournament_id, self.team_id)
        if not candidates:
            await interaction.response.edit_message(content=None, view=warning_embed("Kein anderes Team auf diesem Server verfügbar."))
            return
        await interaction.response.edit_message(
            content=f"Welches Team soll **{self.team_name}** ersetzen? (Suche möglich, falls nicht gelistet)",
            view=SwapInSelectView(self.tournament_id, self.team_id, self.team_name, candidates),
        )


class WithdrawTeamSelectView(discord.ui.View):
    def __init__(self, tournament_id: int, teams: list[dict]):
        super().__init__(timeout=180)
        self.tournament_id = tournament_id
        self.team_names = {t["id"]: t["name"] for t in teams}
        options = [discord.SelectOption(label=t["name"][:100], value=str(t["id"])) for t in teams[:25]]
        select = discord.ui.Select(placeholder="Team auswählen, das das Turnier verlässt...", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        team_id = int(interaction.data["values"][0])
        team_name = self.team_names.get(team_id, f"Team {team_id}")
        await interaction.response.edit_message(
            content=f"🚫 **Sicher?** Alle noch offenen Spiele von **{team_name}** werden sofort automatisch 1:0 für den jeweiligen Gegner gewertet. Das lässt sich nicht rückgängig machen.",
            view=WithdrawConfirmView(self.tournament_id, team_id, team_name),
        )


class WithdrawConfirmView(discord.ui.View):
    def __init__(self, tournament_id: int, team_id: int, team_name: str):
        super().__init__(timeout=120)
        self.tournament_id = tournament_id
        self.team_id = team_id
        self.team_name = team_name

    @discord.ui.button(label="Ja, Def-Wins vergeben", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        from cogs.tournament_manager import withdraw_team_with_forfeits
        count = await withdraw_team_with_forfeits(interaction.client, interaction.guild, self.tournament_id, self.team_id)
        await interaction.followup.send(
            view=success_embed(f"{self.team_name} hat das Turnier verlassen", f"{count} Spiel(e) automatisch 1:0 für den Gegner gewertet."),
            ephemeral=True,
        )

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Abgebrochen.", view=None)


class SwapInSearchModal(discord.ui.Modal, title="Team suchen"):
    query = discord.ui.TextInput(label="Team-Name (auch Teilstring reicht)", max_length=60)

    def __init__(self, tournament_id: int, team_id_out: int, team_out_name: str):
        super().__init__()
        self.tournament_id = tournament_id
        self.team_id_out = team_id_out
        self.team_out_name = team_out_name

    async def on_submit(self, interaction: discord.Interaction):
        pool = get_pool()
        rows = await pool.fetch(
            """
            SELECT t.id, t.name FROM teams t
            WHERE t.guild_id = $1 AND t.id != $2 AND t.name ILIKE $3
              AND NOT EXISTS (
                SELECT 1 FROM tournament_signups ts
                WHERE ts.tournament_id = $4 AND ts.team_id = t.id AND ts.status = 'registered'
              )
            ORDER BY t.name LIMIT 25
            """,
            interaction.guild_id, self.team_id_out, f"%{self.query.value}%", self.tournament_id,
        )
        if not rows:
            await interaction.response.send_message(view=warning_embed(f'Kein Team gefunden, das zu "{self.query.value}" passt.'), ephemeral=True)
            return
        candidates = [dict(r) for r in rows]
        await interaction.response.send_message(
            content=f"Treffer für \"{self.query.value}\" — welches Team soll **{self.team_out_name}** ersetzen?",
            view=SwapInSelectView(self.tournament_id, self.team_id_out, self.team_out_name, candidates),
            ephemeral=True,
        )


class SwapInSelectView(discord.ui.View):
    def __init__(self, tournament_id: int, team_id_out: int, team_out_name: str, waitlist: list[dict]):
        super().__init__(timeout=180)
        self.tournament_id = tournament_id
        self.team_id_out = team_id_out
        self.team_out_name = team_out_name
        self.team_names = {t["id"]: t["name"] for t in waitlist}
        options = [discord.SelectOption(label=t["name"][:100], value=str(t["id"])) for t in waitlist[:25]]
        select = discord.ui.Select(placeholder="Nachrückendes Team auswählen...", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        team_id_in = int(interaction.data["values"][0])
        team_in_name = self.team_names.get(team_id_in, f"Team {team_id_in}")
        from cogs.tournament_manager import get_team_group, replace_team_in_group, refresh_group_panel, refresh_live_schedule
        group = await get_team_group(self.tournament_id, self.team_id_out)
        if group:
            # Gruppenphase laeuft schon - neues Team uebernimmt den Restspielplan direkt.
            await replace_team_in_group(interaction.client, interaction.guild, self.tournament_id, group["id"], self.team_id_out, team_id_in)
            await refresh_group_panel(interaction.client, group["id"])
            await refresh_live_schedule(interaction.client, interaction.guild, self.tournament_id)
        else:
            await swap_team_for_waitlisted(self.tournament_id, self.team_id_out, team_id_in)
            await refresh_panel(interaction.client, self.tournament_id)
        await interaction.edit_original_response(
            content=None, view=success_embed(f"{self.team_out_name} wurde durch {team_in_name} ersetzt.")
        )

    @discord.ui.button(label="🔍 Team suchen (falls nicht gelistet)", style=discord.ButtonStyle.secondary, row=1)
    async def search(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SwapInSearchModal(self.tournament_id, self.team_id_out, self.team_out_name))


class EndTournamentConfirmView(discord.ui.View):
    def __init__(self, tournament_id: int):
        super().__init__(timeout=120)
        self.tournament_id = tournament_id

    @discord.ui.button(label="Ja, beenden", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        pool = get_pool()
        brackets = await pool.fetch(
            "SELECT bracket FROM tournament_bracket_meta WHERE tournament_id = $1", self.tournament_id
        )
        for b in brackets:
            await post_bracket_stats(interaction.client, interaction.guild, self.tournament_id, b["bracket"])

        await cleanup_tournament_channels(interaction.client, interaction.guild, self.tournament_id)
        await pool.execute("UPDATE tournaments SET status = 'finished' WHERE id = $1", self.tournament_id)
        await interaction.followup.send(
            view=success_embed("Turnier beendet", "Statistiken gepostet, Kanäle/Rollen aufgeräumt."), ephemeral=True
        )

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=None, view=info_embed("Abgebrochen."))


class TournamentFormatView(discord.ui.View):
    """Selbstbedienung fuers Turnier-Format: Gruppengroesse + Winner-/Loser-Bracket-Modus.
    Nur vor der Gruppenauslosung nutzbar (aendert sonst die Spielregeln mitten im Turnier)."""

    def __init__(self, tournament_id: int, t: dict):
        super().__init__(timeout=300)
        self.tournament_id = tournament_id

        current_group_size = t.get("group_size_override")
        group_size_select = discord.ui.Select(
            placeholder="Gruppengröße...",
            options=[
                discord.SelectOption(
                    label="Nur 4er-Gruppen (Standard)", value="4",
                    description="Mehr, kleinere Gruppen - 3 Spieltage pro Gruppe.",
                    default=not current_group_size,
                ),
                discord.SelectOption(
                    label="6er-Gruppen bevorzugt", value="6",
                    description="Faellt automatisch auf 4er zurueck, wenn die Teamzahl nicht durch 6 teilbar ist.",
                    default=current_group_size == 6,
                ),
            ],
        )
        group_size_select.callback = self.on_group_size
        self.add_item(group_size_select)

        current_single = bool(t.get("single_bracket_mode"))
        bracket_select = discord.ui.Select(
            placeholder="Bracket-Modus...",
            options=[
                discord.SelectOption(
                    label="Winner + Loser Bracket (Standard)", value="both",
                    description="Jedes Team kommt nach der Gruppenphase in eines von beiden Brackets weiter.",
                    default=not current_single,
                ),
                discord.SelectOption(
                    label="Nur Winner Bracket", value="single",
                    description="Kein Loser-Bracket - nur die besten qualifizieren sich, der Rest ist nach den Gruppen fertig.",
                    default=current_single,
                ),
            ],
        )
        bracket_select.callback = self.on_bracket_mode
        self.add_item(bracket_select)

    async def on_group_size(self, interaction: discord.Interaction):
        value = int(interaction.data["values"][0])
        override = None if value == 4 else value
        pool = get_pool()
        await pool.execute("UPDATE tournaments SET group_size_override = $1 WHERE id = $2", override, self.tournament_id)
        await refresh_panel(interaction.client, self.tournament_id)
        label = "Nur 4er-Gruppen" if override is None else "6er-Gruppen bevorzugt"
        await interaction.response.send_message(view=success_embed(f"Gruppengröße: {label}"), ephemeral=True)

    async def on_bracket_mode(self, interaction: discord.Interaction):
        single = interaction.data["values"][0] == "single"
        pool = get_pool()
        await pool.execute("UPDATE tournaments SET single_bracket_mode = $1 WHERE id = $2", single, self.tournament_id)
        await refresh_panel(interaction.client, self.tournament_id)
        label = "Nur Winner Bracket" if single else "Winner + Loser Bracket"
        await interaction.response.send_message(view=success_embed(f"Bracket-Modus: {label}"), ephemeral=True)


class FillWithByeView(discord.ui.View):
    """Zeigt die 4er- und 6er-Gruppen-Option fuer 'Jetzt mit Freilos auffuellen' - jeweils mit
    Angabe, wie viele Freilose das braucht, damit klar ist, welche Option weniger 'verschenkte'
    Plaetze hat (kleinere Gruppengroesse braucht i.d.R. weniger Freilose)."""

    def __init__(self, tournament_id: int, total: int, options: list[dict]):
        super().__init__(timeout=180)
        self.tournament_id = tournament_id
        self.total = total
        for opt in options:
            btn = discord.ui.Button(
                label=f"{opt['group_size']}er-Gruppen ({opt['bracket_size']} Plätze, {opt['byes']} Freilos)",
                style=discord.ButtonStyle.primary,
            )
            btn.callback = self._make_callback(opt["bracket_size"], opt["group_size"])
            self.add_item(btn)

    def _make_callback(self, bracket_size: int, group_size: int):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            from cogs.tournament_manager import fill_with_bye_and_start, get_tournament, start_group_phase
            await fill_with_bye_and_start(self.tournament_id, bracket_size, group_size)
            pool = get_pool()
            await pool.execute("UPDATE tournaments SET status = 'started' WHERE id = $1", self.tournament_id)
            t = await get_tournament(self.tournament_id)
            await refresh_panel(interaction.client, self.tournament_id)
            await start_group_phase(interaction.client, interaction.guild, self.tournament_id, t)
            await interaction.followup.send(
                view=success_embed(
                    f"{t['name']} — gestartet mit {bracket_size} Plätzen ({group_size}er-Gruppen)!",
                    f"Alle {self.total} Anmeldungen (inkl. Warteliste) sind dabei, {bracket_size - self.total} Freilos-Plätze aufgefüllt. "
                    "Gruppenkanäle wurden angelegt.",
                ),
                ephemeral=True,
            )
        return callback


class TournamentAdminView(discord.ui.View):
    def __init__(self, t: dict):
        super().__init__(timeout=180)
        self.t = t

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Die meisten Buttons hier bleiben vollen Admins (inkl. Head Moderator ueber
        admin_role_ids) vorbehalten. 'Ergebnis eintragen' und 'Ergebnis korrigieren' sind
        bewusst gelockert - Ergebnisse melden duerfen alle Moderator-Raenge, korrigieren
        duerfen Moderator + Head Moderator (nicht Trial Moderator)."""
        custom_id = interaction.data.get("custom_id", "") if interaction.data else ""
        if custom_id == "ta:report_result":
            allowed = await is_tournament_moderator(interaction.user)
        elif custom_id == "ta:correct_result":
            allowed = await can_correct_results(interaction.user)
        else:
            allowed = await is_tournament_admin(interaction.user)
        if not allowed:
            await interaction.response.send_message(view=error_embed("Dafür fehlt dir die Berechtigung."), ephemeral=True)
        return allowed

    @discord.ui.button(label="Anmeldung schließen", style=discord.ButtonStyle.secondary)
    async def close_signup(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        from cogs.tournament_manager import close_tournament_signup
        await close_tournament_signup(interaction.client, self.t["id"])
        await interaction.followup.send(
            view=success_embed("Anmeldung geschlossen", "Teams wurden per DM informiert."),
            ephemeral=True,
        )

    @discord.ui.button(label="Anmeldung öffnen", style=discord.ButtonStyle.secondary)
    async def reopen_signup(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = await get_tournament(self.t["id"])
        if t.get("phase") != "signup":
            await interaction.response.send_message(
                view=error_embed(
                    "Nicht möglich",
                    "Die Anmeldung kann nur wieder geöffnet werden, solange das Turnier noch nicht in der Gruppenphase ist.",
                ),
                ephemeral=True,
            )
            return
        pool = get_pool()
        await pool.execute("UPDATE tournaments SET status = 'open' WHERE id = $1", self.t["id"])
        await refresh_panel(interaction.client, self.t["id"])
        await interaction.response.send_message(view=success_embed("Anmeldung wieder geöffnet."), ephemeral=True)

    @discord.ui.button(label="⚙️ Turnier-Format", style=discord.ButtonStyle.secondary)
    async def edit_format(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = await get_tournament(self.t["id"])
        if t.get("phase") != "signup":
            await interaction.response.send_message(
                view=error_embed(
                    "Nicht möglich",
                    "Das Turnier-Format (Gruppengröße, Winner/Loser-Bracket) kann nur vor der Gruppenauslosung geändert werden.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            content="Wie soll dieses Turnier ablaufen?",
            view=TournamentFormatView(self.t["id"], t),
            ephemeral=True,
        )

    @discord.ui.button(label="🔄 Gruppen neu auslosen", style=discord.ButtonStyle.danger)
    async def regroup(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Fuer den Fall, dass die Gruppengroesse noch geaendert werden muss, obwohl die
        Auslosung schon gelaufen ist (Turnier-Format-Button geht dann nicht mehr, siehe
        edit_format oben) - loescht die aktuelle Auslosung und macht sie mit neuer
        Gruppengroesse neu."""
        t = await get_tournament(self.t["id"])
        if t.get("phase") != "groups":
            await interaction.response.send_message(
                view=error_embed(
                    "Nicht möglich",
                    "Neu-Auslosen ist nur möglich, solange sich das Turnier in der Gruppenphase befindet "
                    "(läuft schon die KO-Phase, erst mit 'KO-Phase resetten' zurücksetzen).",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            content="Mit welcher Gruppengröße soll neu ausgelost werden?",
            view=RegroupSizeView(self.t["id"]),
            ephemeral=True,
        )

    @discord.ui.button(label="🎟️ Mit Freilos auffüllen", style=discord.ButtonStyle.secondary)
    async def fill_with_bye(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Fuer '1-2 Teams fehlen noch bis zur naechsten Turnierstufe' - nimmt ALLE aktuellen
        Anmeldungen (inkl. Warteliste) sofort mit, statt auf weitere echte Anmeldungen zu warten,
        und fuellt die Luecke zur naechsten durch 4 bzw. 6 teilbaren Gruppengroesse mit Freilosen."""
        t = await get_tournament(self.t["id"])
        if t.get("phase") != "signup":
            await interaction.response.send_message(
                view=error_embed("Nicht möglich", "Geht nur, solange die Anmeldung noch läuft."), ephemeral=True
            )
            return
        registered, waitlist = await get_signup_counts(self.t["id"])
        total = registered + waitlist
        if total < 2:
            await interaction.response.send_message(view=error_embed("Zu wenige Anmeldungen."), ephemeral=True)
            return
        from cogs.tournament_manager import compute_fill_with_bye_options
        options = compute_fill_with_bye_options(total)
        await interaction.response.send_message(
            content=f"Aktuell **{total}** Anmeldungen (davon {waitlist} auf der Warteliste). Womit auffüllen und sofort starten?",
            view=FillWithByeView(self.t["id"], total, options),
            ephemeral=True,
        )

    @discord.ui.button(label="Gruppenphase starten", style=discord.ButtonStyle.success)
    async def start_tournament(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = await get_tournament(self.t["id"])
        if t["status"] in ("started",) or t.get("phase") not in ("signup",):
            await interaction.followup.send(view=error_embed("Dieses Turnier läuft bereits."), ephemeral=True)
            return
        registered, _ = await get_signup_counts(self.t["id"])
        if registered < 2:
            await interaction.followup.send(
                view=error_embed(
                    "Zu wenige Teams",
                    f"({registered}) angemeldet. Es werden mindestens 2 Teams benötigt "
                    "(fehlende Plätze bis zur Turnierstufe werden automatisch als Freilose aufgefüllt).",
                ),
                ephemeral=True,
            )
            return

        await self._do_start(interaction)

    async def _do_start(self, interaction: discord.Interaction):
        t = await get_tournament(self.t["id"])
        pool = get_pool()
        await pool.execute("UPDATE tournaments SET status = 'started' WHERE id = $1", self.t["id"])
        await refresh_panel(interaction.client, self.t["id"])
        await start_group_phase(interaction.client, interaction.guild, self.t["id"], t)
        await interaction.followup.send(
            view=success_embed(
                f"{t['name']} — Gruppenauslosung abgeschlossen!",
                "Gruppenkanäle wurden angelegt. Klick auf 'Spieltag 1 freigeben', sobald der Spielplan starten soll.",
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Spieltag 1 freigeben", style=discord.ButtonStyle.success)
    async def release_first_matchday_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = await get_tournament(self.t["id"])
        if t.get("phase") != "groups":
            await interaction.followup.send(
                view=error_embed("Die Gruppenphase muss erst gestartet sein (Auslosung), bevor ein Spieltag freigegeben werden kann."),
                ephemeral=True,
            )
            return
        pool = get_pool()
        already_released = await pool.fetchval(
            "SELECT COUNT(*) FROM tournament_groups WHERE tournament_id = $1 AND released_round > 0",
            self.t["id"],
        )
        if already_released:
            await interaction.followup.send(view=warning_embed("Spieltag 1 wurde bereits freigegeben."), ephemeral=True)
            return

        unready = await get_unready_groups(self.t["id"])
        if unready:
            group_list = ", ".join(f"Gruppe {g['group_number']} ({g['unready_count']} fehlt/fehlen)" for g in unready)
            await interaction.followup.send(
                content=(
                    f"🚫 **Noch nicht alle Teams bereit:** {group_list}\n\n"
                    "Jedes Team muss erst im jeweiligen Gruppen-Panel-Kanal auf '✅ Team ist da' klicken. "
                    "Du kannst trotzdem freigeben, falls nötig:"
                ),
                view=GroupReadinessOverrideView(self.t["id"]),
                ephemeral=True,
            )
            return

        await release_first_matchday(interaction.client, interaction.guild, self.t["id"])
        await interaction.followup.send(view=success_embed("Spieltag 1 freigegeben!"), ephemeral=True)

    @discord.ui.button(label="🔔 Unbestätigte erinnern", style=discord.ButtonStyle.secondary)
    async def remind_unready_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Schickt allen Managern von Teams, die 'Team ist da' noch nicht bestaetigt haben,
        eine DM-Erinnerung - fuer den Fall, dass Teams das vor Spieltag-Freigabe verpennen."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = await get_tournament(self.t["id"])
        if t.get("phase") != "groups":
            await interaction.followup.send(
                view=error_embed("Geht nur, solange die Gruppenphase läuft (nach der Auslosung)."), ephemeral=True
            )
            return
        unready = await get_unready_teams(self.t["id"])
        if not unready:
            await interaction.followup.send(view=success_embed("Alle Teams sind bereits bestätigt! ✅"), ephemeral=True)
            return
        sent = 0
        for row in unready:
            for m in await get_team_managers(row["team_id"]):
                try:
                    user = await interaction.client.fetch_user(m["discord_id"])
                    await user.send(
                        f"📢 **Erinnerung:** Euer Team **{row['team_name']}** hat für **{t['name']}** "
                        f"(Gruppe {row['group_number']}) noch nicht 'Team ist da' bestätigt! "
                        "Bitte klickt im Gruppenpanel auf ✅ **Team ist da**, damit es rund läuft."
                    )
                    sent += 1
                except discord.HTTPException:
                    pass
        team_list = ", ".join(r["team_name"] for r in unready)
        await interaction.followup.send(
            view=success_embed(f"{sent} DM(s) verschickt.", f"Noch unbestätigt: {team_list}"), ephemeral=True
        )

    @discord.ui.button(label="Live-Spielplan posten", style=discord.ButtonStyle.secondary)
    async def post_live_schedule_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = await get_tournament(self.t["id"])
        if t.get("phase") not in ("groups", "knockout", "finished"):
            await interaction.followup.send(
                view=error_embed("Für dieses Turnier gibt es noch keine Gruppen (Auslosung erst durchführen)."), ephemeral=True
            )
            return
        from cogs.tournament_manager import refresh_live_schedule
        await refresh_live_schedule(interaction.client, interaction.guild, self.t["id"])
        await interaction.followup.send(view=success_embed("Live-Spielplan gepostet/aktualisiert."), ephemeral=True)

    @discord.ui.button(label="Panel-Kanäle einrichten", style=discord.ButtonStyle.secondary)
    async def create_panel_channels_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        pool = get_pool()
        groups = await pool.fetch(
            "SELECT * FROM tournament_groups WHERE tournament_id = $1 AND panel_channel_id IS NULL", self.t["id"]
        )
        if not groups:
            await interaction.followup.send(view=info_embed("Alle Gruppen haben bereits einen eigenen Panel-Kanal."), ephemeral=True)
            return
        t = await get_tournament(self.t["id"])
        category = interaction.guild.get_channel(t.get("group_category_id")) if t.get("group_category_id") else None
        from cogs.tournament_manager import create_group_panel_channel
        created = 0
        for g in groups:
            try:
                await create_group_panel_channel(interaction.guild, category, dict(g))
                created += 1
            except Exception:
                log.exception(f"Fehler beim nachtraeglichen Erstellen des Panel-Kanals fuer Gruppe {g['id']}")
        await interaction.followup.send(
            view=success_embed(
                f"{created} Panel-Kanal/Kanäle erstellt",
                "Alte Panel-Nachrichten in den normalen Gruppenkanälen kannst du jetzt manuell löschen, falls gewünscht (nicht zwingend nötig).",
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="KO-Phase starten", style=discord.ButtonStyle.success)
    async def start_ko_phase(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = await get_tournament(self.t["id"])
        pool = get_pool()

        existing_brackets = await pool.fetchval(
            "SELECT COUNT(*) FROM tournament_bracket_meta WHERE tournament_id = $1", self.t["id"]
        )
        if existing_brackets > 0:
            await interaction.followup.send(
                view=error_embed("KO-Phase bereits gestartet", "Bracket-Kanäle existieren schon."), ephemeral=True
            )
            return

        if t.get("phase") not in ("groups", "knockout"):
            await interaction.followup.send(
                view=error_embed("Nicht möglich", "Die KO-Phase kann erst gestartet werden, wenn die Gruppenphase läuft."),
                ephemeral=True,
            )
            return
        if not await all_groups_complete(self.t["id"]):
            await interaction.followup.send(
                view=warning_embed(
                    "Noch nicht alle Spiele abgeschlossen",
                    "Erst wenn alle Ergebnisse eingetragen sind, kann die KO-Phase gestartet werden "
                    "(das passiert normalerweise automatisch mit dem letzten Ergebnis).",
                ),
                ephemeral=True,
            )
            return

        if t.get("phase") == "knockout":
            await pool.execute("UPDATE tournaments SET phase = 'groups' WHERE id = $1", self.t["id"])
            t = await get_tournament(self.t["id"])

        await start_knockout_phase(interaction.client, interaction.guild, self.t["id"], t)
        from audit import log_action
        await log_action(interaction.guild_id, interaction.user, "tournament.bracket_created", "tournament", self.t["id"], "KO-Phase gestartet")
        await interaction.followup.send(view=success_embed("KO-Phase gestartet!"), ephemeral=True)

    @discord.ui.button(label="KO-Phase resetten", style=discord.ButtonStyle.danger)
    async def reset_ko_phase(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = await get_tournament(self.t["id"])
        if t.get("phase") not in ("knockout",):
            await interaction.response.send_message(view=error_embed("Es gibt aktuell keine KO-Phase zum Zurücksetzen."), ephemeral=True)
            return
        await interaction.response.send_message(
            content=(
                "⚠️ **Sicher?** Löscht alle Winner-/Loser-Bracket-Kanäle, -Rollen und -Matches unwiderruflich und "
                "setzt das Turnier zurück auf die Gruppenphase (Gruppen bleiben unangetastet)."
            ),
            view=ResetKoConfirmView(self.t["id"]),
            ephemeral=True,
        )

    @discord.ui.button(label="Spielplan-Grafiken posten", style=discord.ButtonStyle.secondary)
    async def post_schedule_graphics(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        pool = get_pool()
        groups = await pool.fetch("SELECT * FROM tournament_groups WHERE tournament_id = $1 ORDER BY group_number", self.t["id"])
        if not groups:
            await interaction.followup.send(view=error_embed("Keine Gruppen gefunden (Gruppenphase noch nicht gestartet?)."), ephemeral=True)
            return

        from cogs.tournament_manager import build_group_schedule_file

        posted, failed = 0, 0
        for group in groups:
            channel = interaction.guild.get_channel(group["channel_id"])
            if channel is None:
                try:
                    channel = await interaction.guild.fetch_channel(group["channel_id"])
                except discord.HTTPException:
                    failed += 1
                    continue
            try:
                schedule_file = await build_group_schedule_file(dict(group))
                await channel.send(file=schedule_file)
                posted += 1
            except Exception:
                logging.getLogger("fifa-elite-cup").exception(f"Fehler beim nachtraeglichen Posten der Spielplan-Grafik fuer Gruppe {group['id']}")
                failed += 1

        await interaction.followup.send(
            view=success_embed("Spielplan-Grafiken gepostet", f"Erfolgreich: {posted} | Fehlgeschlagen: {failed}"), ephemeral=True
        )

    @discord.ui.button(label="Spendenturnier einrichten", style=discord.ButtonStyle.secondary)
    async def setup_donation(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = await get_tournament(self.t["id"])
        if t.get("is_donation_tournament"):
            await interaction.response.send_message(
                content=f"**Bereits als Spendenturnier aktiv.**\nAktuelle Zahlungsdetails:\n{t.get('donation_info') or '-'}",
                view=DonationDeactivateView(self.t["id"]),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(DonationConfigModal(self.t["id"]))

    @discord.ui.button(label="Panel aktualisieren", style=discord.ButtonStyle.secondary)
    async def refresh_panel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await refresh_panel(interaction.client, self.t["id"])
        await interaction.followup.send(view=success_embed("Anmelde-Panel neu aufgebaut."), ephemeral=True)

    @discord.ui.button(label="Team verlässt Turnier (Def-Win)", style=discord.ButtonStyle.danger)
    async def withdraw_with_forfeit(self, interaction: discord.Interaction, button: discord.ui.Button):
        pool = get_pool()
        rows = await pool.fetch(
            """
            SELECT DISTINCT t.id, t.name FROM teams t
            JOIN tournament_matches tm ON (tm.team1_id = t.id OR tm.team2_id = t.id)
            WHERE tm.tournament_id = $1
            ORDER BY t.name
            """,
            self.t["id"],
        )
        if not rows:
            await interaction.response.send_message(view=error_embed("Keine Teams mit Spielen in diesem Turnier gefunden."), ephemeral=True)
            return
        teams = [dict(r) for r in rows]
        await interaction.response.send_message(
            content="Welches Team verlässt das Turnier? Alle noch offenen Spiele werden automatisch 1:0 für den Gegner gewertet.",
            view=WithdrawTeamSelectView(self.t["id"], teams),
            ephemeral=True,
        )

    @discord.ui.button(label="Team tauschen", style=discord.ButtonStyle.secondary)
    async def swap_team(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = await get_tournament(self.t["id"])
        if t.get("phase") not in ("signup", "groups"):
            await interaction.response.send_message(
                view=error_embed(
                    "Nicht möglich",
                    "Team-Tausch ist ab der K.-o.-Phase nicht mehr möglich - dafür bitte "
                    "'Team verlässt Turnier' nutzen (wertet offene Spiele als Forfeit-Niederlage).",
                ),
                ephemeral=True,
            )
            return
        registered = await get_registered_teams(self.t["id"])
        if not registered:
            await interaction.response.send_message(view=error_embed("Keine registrierten Teams vorhanden."), ephemeral=True)
            return
        await interaction.response.send_message(
            content="Welches Team soll ausgetauscht werden?",
            view=SwapOutSelectView(self.t["id"], registered),
            ephemeral=True,
        )

    @discord.ui.button(label="Teams anzeigen", style=discord.ButtonStyle.secondary)
    async def show_teams(self, interaction: discord.Interaction, button: discord.ui.Button):
        registered = await get_registered_teams(self.t["id"])
        waitlist = await get_waitlisted_teams(self.t["id"])
        blocks = [f"### Teams\n**Angemeldet ({len(registered)}):**\n" + ("\n".join(f"- {r['name']}" for r in registered) or "- (keine)")]
        if waitlist:
            blocks.append(f"**Warteliste ({len(waitlist)}):**\n" + "\n".join(f"- {r['name']}" for r in waitlist))
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(discord.ui.TextDisplay("\n\n".join(blocks)), accent_color=discord.Color.gold()))
        await interaction.response.send_message(view=view, ephemeral=True)

    @discord.ui.button(label="Gruppen-Status", style=discord.ButtonStyle.secondary)
    async def show_group_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = await get_tournament(self.t["id"])
        if t.get("phase") not in ("groups", "knockout", "finished"):
            await interaction.response.send_message(view=error_embed("Für dieses Turnier wurde noch keine Gruppenphase gestartet."), ephemeral=True)
            return

        standings = await get_group_standings(self.t["id"])
        pool = get_pool()
        blocks = [f"### {t['name']} - Gruppenphase"]
        for g in standings:
            team_ids = [s["team_id"] for s in g["standings"]]
            names = await team_name_map(team_ids)
            table_lines = [f"**Gruppe {g['group_number']}**"]
            table_lines += [f"{names.get(s['team_id'], '?')}: `{s['wins']}` Siege" for s in g["standings"]]

            matches = await pool.fetch(
                "SELECT * FROM tournament_matches WHERE group_id = $1 ORDER BY round, match_number", g["group_id"]
            )
            open_matches = [m for m in matches if m["status"] != "completed"]
            if open_matches:
                m_names = await team_name_map([m["team1_id"] for m in open_matches] + [m["team2_id"] for m in open_matches])
                table_lines.append("Offen:")
                table_lines += [
                    f"ST{m['round']}: {m_names.get(m['team1_id'],'?')} vs {m_names.get(m['team2_id'],'?')}" for m in open_matches
                ]
            blocks.append("\n".join(table_lines))

        view = discord.ui.LayoutView(timeout=None)
        items = [discord.ui.TextDisplay(blocks[0])]
        for block in blocks[1:]:
            items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
            items.append(discord.ui.TextDisplay(block))
        view.add_item(discord.ui.Container(*items, accent_color=discord.Color.gold()))
        await interaction.response.send_message(view=view, ephemeral=True)

    @discord.ui.button(label="Ergebnis eintragen", style=discord.ButtonStyle.primary, custom_id="ta:report_result")
    async def report_result(self, interaction: discord.Interaction, button: discord.ui.Button):
        matches = await get_all_open_matches(self.t["id"])
        if not matches:
            await interaction.response.send_message(view=error_embed("Keine offenen Matches gefunden."), ephemeral=True)
            return
        if len(matches) > 25:
            await interaction.response.send_message(
                content=f"{len(matches)} offene Matches - zu viele für eine Liste (Discord-Limit: 25). Bitte suchen:",
                view=MatchSearchPromptView(self.t["id"], "open", len(matches)),
                ephemeral=True,
            )
            return
        team_ids = [m["team1_id"] for m in matches] + [m["team2_id"] for m in matches]
        names = await team_name_map(team_ids)
        await interaction.response.send_message(
            content="Welches Match? (Admin-Eintragung wird sofort final gespeichert, ohne Bestätigung)",
            view=GroupMatchSelect(matches, names, is_admin=True),
            ephemeral=True,
        )

    @discord.ui.button(label="Ergebnis korrigieren", style=discord.ButtonStyle.secondary, custom_id="ta:correct_result")
    async def correct_result(self, interaction: discord.Interaction, button: discord.ui.Button):
        matches = await get_all_completed_matches(self.t["id"])
        if not matches:
            await interaction.response.send_message(view=error_embed("Keine abgeschlossenen Matches gefunden."), ephemeral=True)
            return
        if len(matches) > 25:
            await interaction.response.send_message(
                content=f"{len(matches)} abgeschlossene Matches - zu viele für eine Liste (Discord-Limit: 25). Bitte suchen:",
                view=MatchSearchPromptView(self.t["id"], "completed", len(matches)),
                ephemeral=True,
            )
            return
        team_ids = [m["team1_id"] for m in matches] + [m["team2_id"] for m in matches]
        names = await team_name_map(team_ids)
        await interaction.response.send_message(
            content=(
                "Welches Match korrigieren? (Läuft die KO-Phase schon, wird eine spätere Korrektur an einem "
                "Gruppenspiel NICHT rückwirkend im Bracket nachgezogen - dann bitte auch das Bracket manuell prüfen)"
            ),
            view=EditMatchSelectView(matches, names),
            ephemeral=True,
        )

    @discord.ui.button(label="🏁 Turnier beenden", style=discord.ButtonStyle.danger)
    async def end_tournament(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = await get_tournament(self.t["id"])
        if t.get("phase") != "knockout":
            await interaction.response.send_message(
                view=error_embed("Nicht möglich", "Das Turnier muss erst in der KO-Phase sein, bevor es beendet werden kann."),
                ephemeral=True,
            )
            return

        pool = get_pool()
        bracket_count = await pool.fetchval(
            "SELECT COUNT(*) FROM tournament_bracket_meta WHERE tournament_id = $1", self.t["id"]
        )
        missing = []
        if not t.get("winner_champion_id"):
            missing.append("Winner Bracket")
        if bracket_count >= 2 and not t.get("loser_champion_id"):
            missing.append("Loser Bracket")
        if missing:
            await interaction.response.send_message(
                view=warning_embed(
                    "Noch nicht fertig",
                    f"{', '.join(missing)} hat noch keinen Sieger. Turnier kann erst beendet werden, "
                    "wenn beide Brackets abgeschlossen sind.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            content=(
                "🚫 **Sicher?** Das postet die Abschluss-Statistiken (Winner-/Loser-Top3, Awards, Top-11) in die "
                "konfigurierten Kanäle und löscht danach **alle** für dieses Turnier erstellten Kanäle und Rollen "
                "(Gruppen + Winner-/Loser-Bracket) unwiderruflich."
            ),
            view=EndTournamentConfirmView(self.t["id"]),
            ephemeral=True,
        )

    @discord.ui.button(label="Turnier löschen", style=discord.ButtonStyle.danger)
    async def delete_tournament(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            content="🚫 **Sicher?** Das löscht das Turnier inkl. aller Anmeldungen und Matches unwiderruflich.",
            view=ConfirmDeleteView(self.t["id"]),
            ephemeral=True,
        )


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, tournament_id: int):
        super().__init__(timeout=60)
        self.tournament_id = tournament_id

    @discord.ui.button(label="Ja, löschen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        pool = get_pool()
        await pool.execute("DELETE FROM tournaments WHERE id = $1", self.tournament_id)
        from audit import log_action
        await log_action(interaction.guild_id, interaction.user, "tournament.deleted", "tournament", self.tournament_id)
        await interaction.response.edit_message(content=None, view=success_embed("Turnier gelöscht."))

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=None, view=info_embed("Abgebrochen."))


class DMBroadcastModal(discord.ui.Modal, title="DM an alle Vereinsmanager"):
    dm_title = discord.ui.TextInput(label="Titel", max_length=100)
    dm_message = discord.ui.TextInput(label="Nachricht", style=discord.TextStyle.paragraph, max_length=1800)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        pool = get_pool()
        teams = await pool.fetch("SELECT id FROM teams WHERE guild_id = $1", interaction.guild_id)

        recipient_ids: set[int] = set()
        for team in teams:
            managers = await get_team_managers(team["id"])
            for m in managers:
                recipient_ids.add(m["discord_id"])

        if not recipient_ids:
            await interaction.followup.send(view=error_embed("Keine Team-Manager gefunden."), ephemeral=True)
            return

        text = (
            f"### {self.dm_title.value}\n"
            f"{self.dm_message.value}\n\n"
            f"-# {interaction.guild.name} - Nachricht von der Admin-Leitung"
        )
        dm_view = discord.ui.LayoutView(timeout=None)
        dm_view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_color=discord.Color.gold()))

        sent, failed = 0, 0
        for discord_id in recipient_ids:
            try:
                user = await interaction.client.fetch_user(discord_id)
                await user.send(view=dm_view)
                sent += 1
            except discord.HTTPException:
                failed += 1
            except Exception:
                log.exception(f"Unerwarteter Fehler beim Senden der DM an {discord_id}")
                failed += 1

        await interaction.followup.send(
            view=success_embed("DM-Broadcast abgeschlossen", f"Zugestellt: {sent} | Fehlgeschlagen: {failed}"),
            ephemeral=True,
        )


ADMIN_BANNER_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "admin_banner.jpg")


class AdminPanel(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        intro = discord.ui.TextDisplay(
            "# 🛠️ Admin Panel\n"
            "Steuerzentrale für den FIFA Elite Cup — wähle unten eine Kategorie."
        )
        categories = discord.ui.TextDisplay(
            "**🏆 Turniere** — anlegen, verwalten, komplette Team-Liste einsehen\n"
            "**🔨 Moderation** — Spieler/Teams sperren, Ticket-System konfigurieren\n"
            "**📣 Kommunikation** — eigene Ankündigungen posten, DM an alle Vereinsmanager\n"
            "**⚙️ System** — Stats-Kanäle, Rollen (Admin/Moderator/VM/Co-Manager), Nicknames, Stream-Liste\n"
            "**🗓️ Kalender** — Termine für Cups/Ligen/Sonstiges anlegen, Kalender-Kanal einstellen"
        )
        media_items = []
        if os.path.exists(ADMIN_BANNER_PATH):
            self.banner_file = discord.File(ADMIN_BANNER_PATH, filename="admin_banner.jpg")
            media_items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://admin_banner.jpg")))
        container = discord.ui.Container(
            *media_items,
            intro,
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
            categories,
            discord.ui.ActionRow(
                discord.ui.Button(label="Turniere", style=discord.ButtonStyle.primary, custom_id="admincat:tournaments"),
                discord.ui.Button(label="Moderation", style=discord.ButtonStyle.danger, custom_id="admincat:moderation"),
                discord.ui.Button(label="Kommunikation", style=discord.ButtonStyle.secondary, custom_id="admincat:communication"),
                discord.ui.Button(label="System", style=discord.ButtonStyle.secondary, custom_id="admincat:system"),
                discord.ui.Button(label="Kalender", style=discord.ButtonStyle.secondary, custom_id="admincat:calendar"),
            ),
            discord.ui.ActionRow(
                discord.ui.Button(label="🌐 Admin-Dashboard (Website)", style=discord.ButtonStyle.link, url=f"{WEBSITE_URL}/dashboard"),
            ),
            accent_color=discord.Color.gold(),
        )
        self.add_item(container)


class AdminPanelCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(AdminPanel())

    @app_commands.command(name="admin_panel_setup", description="Postet das Admin-Panel in diesem Kanal (Admin)")
    async def admin_panel_setup(self, interaction: discord.Interaction):
        if not await is_tournament_admin(interaction.user):
            await interaction.response.send_message(view=error_embed("Nur Admins können das Admin-Panel posten."), ephemeral=True)
            return
        await interaction.response.send_message(view=success_embed("Admin-Panel wird gepostet..."), ephemeral=True)
        panel = AdminPanel()
        if hasattr(panel, "banner_file"):
            await interaction.channel.send(view=panel, files=[panel.banner_file])
        else:
            await interaction.channel.send(view=panel)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if not (custom_id.startswith("admin:") or custom_id.startswith("admincat:")):
            return

        if not await is_tournament_admin(interaction.user):
            await interaction.response.send_message(view=error_embed("Nur Admins können das Admin-Panel nutzen."), ephemeral=True)
            return

        if custom_id.startswith("admincat:"):
            category = custom_id.split(":", 1)[1]
            from cogs.calendar import CalendarMenu
            menus = {
                "tournaments": AdminTournamentsMenu(),
                "moderation": AdminModerationMenu(),
                "communication": AdminCommunicationMenu(),
                "system": AdminSystemMenu(),
                "calendar": CalendarMenu(),
            }
            menu = menus.get(category)
            if menu is None:
                return
            await interaction.response.send_message(content="Wähle eine Aktion:", view=menu, ephemeral=True)
            return

        action = custom_id.split(":", 1)[1]

        if action == "create":
            await interaction.response.send_modal(TournamentCreateModal())

        elif action == "manage":
            pool = get_pool()
            rows = await pool.fetch(
                "SELECT id, name FROM tournaments WHERE guild_id = $1 ORDER BY created_at DESC", interaction.guild_id
            )
            if not rows:
                await interaction.response.send_message(view=error_embed("Noch keine Turniere vorhanden."), ephemeral=True)
                return
            tournaments = [dict(r) for r in rows]
            await interaction.response.send_message(
                content="Welches Turnier verwalten?", view=TournamentSelect(tournaments), ephemeral=True
            )

        elif action == "statschannels":
            await interaction.response.send_message(
                content="Wähle für jeden Bereich den passenden Kanal aus:",
                view=StatsChannelsConfigView(interaction.guild_id, StatsChannelsConfigView.ALL_FIELDS[:5]),
                ephemeral=True,
            )
            await interaction.followup.send(
                content="Und noch zwei:",
                view=StatsChannelsConfigView(interaction.guild_id, StatsChannelsConfigView.ALL_FIELDS[5:]),
                ephemeral=True,
            )

        elif action == "banplayer":
            await interaction.response.send_message(
                content="Wähle den zu sperrenden Spieler aus:", view=PlayerBanView(), ephemeral=True
            )

        elif action == "banteam":
            await interaction.response.send_modal(TeamBanSearchModal())

        elif action == "banlist":
            user_bans = await get_all_bans(interaction.guild_id)
            team_bans = await get_all_team_bans(interaction.guild_id)
            if not user_bans and not team_bans:
                await interaction.response.send_message(view=info_embed("Aktuell ist niemand/kein Team gesperrt."), ephemeral=True)
                return
            blocks = []
            if user_bans:
                lines = ["**Spieler:**"]
                for b in user_bans:
                    until = b["expires_at"].strftime("%d.%m.%Y %H:%M") if b["expires_at"] else "dauerhaft"
                    lines.append(f"<@{b['discord_id']}> - bis {until} - Grund: {b['reason'] or 'keiner'}")
                blocks.append("\n".join(lines))
            if team_bans:
                lines = ["**Teams:**"]
                for b in team_bans:
                    until = b["expires_at"].strftime("%d.%m.%Y %H:%M") if b["expires_at"] else "dauerhaft"
                    lines.append(f"**{b['team_name']}** - bis {until} - Grund: {b['reason'] or 'keiner'}")
                blocks.append("\n".join(lines))
            banner_path = os.path.join(os.path.dirname(__file__), "..", "assets", "sperren_banner.jpg")
            await interaction.response.send_message(
                content="\n\n".join(blocks), file=discord.File(banner_path, filename="sperren_banner.jpg"),
                view=UnbanSelect(user_bans, team_bans), ephemeral=True,
            )

        elif action == "auditlog":
            from cogs.moderation import AuditLogView
            pager = AuditLogView(interaction.guild_id)
            log_view = await pager.render()
            await interaction.response.send_message(view=log_view, ephemeral=True)

        elif action == "embed":
            await interaction.response.send_modal(EmbedBuilderModal())

        elif action == "dmall":
            await interaction.response.send_modal(DMBroadcastModal())

        elif action == "setauditchannel":
            pool = get_pool()
            row = await pool.fetchrow("SELECT audit_log_channel_id FROM guild_settings WHERE guild_id = $1", interaction.guild_id)
            current = f"<#{row['audit_log_channel_id']}>" if row and row["audit_log_channel_id"] else "keiner gesetzt"
            await interaction.response.send_message(
                content=f"**Live-Log-Kanal einstellen**\nAktuell: {current}\nJede protokollierte Aktion (Team-/Turnier-Verwaltung, Bans, Kalender, An-/Abmeldungen, Ergebnisse) wird sofort hier gepostet.",
                view=AuditChannelSelectView(),
                ephemeral=True,
            )

        elif action == "setresultschannel":
            pool = get_pool()
            row = await pool.fetchrow("SELECT results_feed_channel_id FROM guild_settings WHERE guild_id = $1", interaction.guild_id)
            current = f"<#{row['results_feed_channel_id']}>" if row and row["results_feed_channel_id"] else "keiner gesetzt"
            await interaction.response.send_message(
                content=f"**Live-Ergebnis-Kanal einstellen**\nAktuell: {current}\nJedes fertig gespielte Match wird sofort hier gepostet.",
                view=ResultsChannelSelectView(),
                ephemeral=True,
            )

        elif action == "setmediachannel":
            pool = get_pool()
            row = await pool.fetchrow("SELECT media_only_channel_id FROM guild_settings WHERE guild_id = $1", interaction.guild_id)
            current = f"<#{row['media_only_channel_id']}>" if row and row["media_only_channel_id"] else "keiner gesetzt"
            await interaction.response.send_message(
                content=f"**Medien-Kanal einstellen**\nAktuell: {current}\nIn diesem Kanal sind nur Bilder/Videos erlaubt - jede Nachricht mit Text wird automatisch gelöscht.",
                view=MediaChannelSelectView(),
                ephemeral=True,
            )

        elif action == "setplayersearchchannel":
            pool = get_pool()
            row = await pool.fetchrow("SELECT player_search_channel_id FROM guild_settings WHERE guild_id = $1", interaction.guild_id)
            current = f"<#{row['player_search_channel_id']}>" if row and row["player_search_channel_id"] else "keiner gesetzt"
            await interaction.response.send_message(
                content=f"**Spieler-Suche-Kanal einstellen**\nAktuell: {current}\nIn diesem Kanal dürfen nur Vereinsmanager schreiben - alle anderen Nachrichten werden automatisch gelöscht.",
                view=PlayerSearchChannelSelectView(),
                ephemeral=True,
            )

        elif action == "setrole":
            pool = get_pool()
            row = await pool.fetchrow("SELECT admin_role_id FROM guild_settings WHERE guild_id = $1", interaction.guild_id)
            current = f"<@&{row['admin_role_id']}>" if row and row["admin_role_id"] else "keine gesetzt"
            await interaction.response.send_message(
                content=f"**Admin-Rolle festlegen**\nAktuell: {current}\nWähle eine neue Rolle, oder 'Entfernen' um zurückzusetzen (dann zählen nur noch echte Server-Admins).",
                view=AdminRoleSelectView(),
                ephemeral=True,
            )

        elif action in ("setmodrole", "setvmrole", "setcomanagerrole"):
            column, label = {
                "setmodrole": ("mod_role_id", "Moderator-Rolle (darf Ergebnisse für alle Turniere verwalten)"),
                "setvmrole": ("vm_role_id", "VM-Rolle (automatisch bei Team-Erstellung)"),
                "setcomanagerrole": ("co_manager_role_id", "Co-Manager-Rolle (automatisch bei Co-Manager-Hinzufügen)"),
            }[action]
            pool = get_pool()
            row = await pool.fetchrow(f"SELECT {column} FROM guild_settings WHERE guild_id = $1", interaction.guild_id)
            current = f"<@&{row[column]}>" if row and row[column] else "keine gesetzt"
            await interaction.response.send_message(
                content=f"**{label} festlegen**\nAktuell: {current}",
                view=GenericRoleSelectView(column, label),
                ephemeral=True,
            )

        elif action == "allteams":
            pool = get_pool()
            rows = await pool.fetch("SELECT * FROM teams WHERE guild_id = $1 ORDER BY name", interaction.guild_id)
            if not rows:
                await interaction.response.send_message(view=error_embed("Noch keine Teams auf diesem Server."), ephemeral=True)
                return
            lines = []
            for r in rows:
                managers = await pool.fetch(
                    "SELECT discord_id, role FROM team_managers WHERE team_id = $1 ORDER BY role", r["id"]
                )
                owner = next((m for m in managers if m["role"] == "owner"), None)
                comanagers = [m for m in managers if m["role"] != "owner"]
                owner_text = f"<@{owner['discord_id']}>" if owner else "_kein Owner_"
                comanager_text = ", ".join(f"<@{m['discord_id']}>" for m in comanagers) or "-"
                lines.append(
                    f"**{r['name']}**\nEA-Club: {r['ea_club_name'] or '-'} · Owner: {owner_text} · Co-Manager: {comanager_text}"
                )
            view = TeamOverviewView(lines)
            await interaction.response.send_message(content=view.content(), view=view, ephemeral=True)

        elif action == "ticketconfig":
            await interaction.response.send_message(
                content=(
                    "**Ticket-System einstellen**\n"
                    "- Kategorie: wo neue Ticket-Kanäle angelegt werden\n"
                    "- Log-Kanal: wo Transkripte beim Schließen landen\n"
                    "- Support-Rolle: wer Tickets sehen/übernehmen/schließen darf (zusätzlich zu Admins)"
                ),
                view=TicketConfigView(),
                ephemeral=True,
            )

        elif action == "syncnicknames":
            await interaction.response.defer(ephemeral=True, thinking=True)
            from cogs.team_manager import apply_team_nickname
            pool = get_pool()
            teams = await pool.fetch("SELECT * FROM teams WHERE guild_id = $1", interaction.guild_id)
            updated, failed = 0, 0
            for team in teams:
                managers = await pool.fetch("SELECT discord_id FROM team_managers WHERE team_id = $1", team["id"])
                for m in managers:
                    member = interaction.guild.get_member(m["discord_id"])
                    if member is None:
                        try:
                            member = await interaction.guild.fetch_member(m["discord_id"])
                        except discord.HTTPException:
                            failed += 1
                            continue
                    success = await apply_team_nickname(member, team["name"])
                    if success:
                        updated += 1
                    else:
                        failed += 1
            await interaction.followup.send(
                view=success_embed("Nicknames aktualisiert", f"Erfolgreich: {updated} | Fehlgeschlagen (fehlende Rechte): {failed}"),
                ephemeral=True,
            )

        elif action == "refreshstreams":
            await interaction.response.defer(ephemeral=True, thinking=True)
            from cogs.team_manager import refresh_stream_list
            await refresh_stream_list(interaction.client, interaction.guild)
            await interaction.followup.send(view=success_embed("Stream-Liste aktualisiert."), ephemeral=True)

        elif action == "teammanager":
            pool = get_pool()
            teams = await pool.fetch(
                "SELECT * FROM teams WHERE guild_id = $1 AND dissolved_at IS NULL ORDER BY name", interaction.guild_id
            )
            if not teams:
                await interaction.response.send_message(view=error_embed("Noch keine Teams auf diesem Server."), ephemeral=True)
                return
            teams = [dict(t) for t in teams]
            await interaction.response.send_message(
                content="Welches Team bearbeiten?", view=AdminTeamSelectView(teams), ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminPanelCog(bot))
