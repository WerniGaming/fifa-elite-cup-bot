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
import discord
from discord import app_commands
from discord.ext import commands

from db import get_pool
from ui_helpers import info_embed, success_embed, error_embed, warning_embed
from cogs.tournament_manager import (
    TournamentCreateModal,
    get_tournament,
    get_signup_counts,
    get_registered_teams,
    get_waitlisted_teams,
    get_unconfirmed_teams,
    start_group_phase,
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
    PlayerBanView, TeamBanView, UnbanSelect,
    get_all_bans, get_all_team_bans, get_all_guild_teams,
)
from cogs.team_manager import get_team_managers
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
            await interaction.response.send_message(embed=error_embed("Turnier nicht gefunden."), ephemeral=True)
            return
        registered, waitlist = await get_signup_counts(tournament_id)
        embed = info_embed(f"{t['name']} (ID {t['id']})")
        embed.add_field(name="Status", value=status_label(t), inline=True)
        embed.add_field(name="Min/Max Teams", value=f"{t['min_teams']} / {t['max_teams']}", inline=True)
        embed.add_field(name="Angemeldet", value=f"{registered} | Warteliste: {waitlist}", inline=True)
        await interaction.response.send_message(embed=embed, view=TournamentAdminView(t), ephemeral=True)


class ActivityOverrideView(discord.ui.View):
    def __init__(self, tournament_id: int):
        super().__init__(timeout=120)
        self.tournament_id = tournament_id

    @discord.ui.button(label="Trotzdem starten", style=discord.ButtonStyle.danger)
    async def force_start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = await get_tournament(self.tournament_id)
        pool = get_pool()
        await pool.execute("UPDATE tournaments SET status = 'started' WHERE id = $1", self.tournament_id)
        await refresh_panel(interaction.client, self.tournament_id)
        await start_group_phase(interaction.client, interaction.guild, self.tournament_id, t)
        await interaction.followup.send(
            embed=success_embed(f"{t['name']} wurde ohne vollständigen Aktivitätscheck gestartet."), ephemeral=True
        )

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=None, embed=info_embed("Abgebrochen."), view=None)


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
            await interaction.response.send_message(embed=error_embed("Match nicht gefunden."), ephemeral=True)
            return
        await interaction.response.send_modal(
            ScoreModal(
                match_id, match["team1_id"], match["team2_id"],
                self.names.get(match["team1_id"], "?"), self.names.get(match["team2_id"], "?"),
                is_admin=True,
                default_score1=match["team1_score"], default_score2=match["team2_score"],
            )
        )


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
            await interaction.followup.send(embed=success_embed("KO-Phase wurde zurückgesetzt und neu erstellt."), ephemeral=True)
        except Exception:
            log.exception(f"Fehler beim Zuruecksetzen/Neuerstellen der KO-Phase fuer Turnier {self.tournament_id}")
            await interaction.followup.send(
                embed=error_embed(
                    "Fehler beim Zurücksetzen",
                    "Bitte im Bot-Log nachschauen (`sudo journalctl -u fifa-elite-cup-v2 -n 50 --no-pager`).",
                ),
                ephemeral=True,
            )

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=None, embed=info_embed("Abgebrochen."), view=None)


class RegroupConfirmView(discord.ui.View):
    def __init__(self, tournament_id: int, new_group_size: int):
        super().__init__(timeout=120)
        self.tournament_id = tournament_id
        self.new_group_size = new_group_size

    @discord.ui.button(label="Ja, neu aufteilen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await reset_group_phase(interaction.client, interaction.guild, self.tournament_id, self.new_group_size)
            t = await get_tournament(self.tournament_id)
            await refresh_panel(interaction.client, self.tournament_id)
            await start_group_phase(interaction.client, interaction.guild, self.tournament_id, t)
            await interaction.followup.send(
                embed=success_embed(
                    "Gruppenphase neu aufgeteilt",
                    f"Alle Gruppen wurden mit {self.new_group_size}er-Gruppen neu erstellt. "
                    "Bisherige Gruppenergebnisse wurden dabei gelöscht.",
                ),
                ephemeral=True,
            )
        except Exception:
            log.exception(f"Fehler beim Neu-Aufteilen der Gruppenphase fuer Turnier {self.tournament_id}")
            await interaction.followup.send(
                embed=error_embed(
                    "Fehler beim Neu-Aufteilen",
                    "Bitte im Bot-Log nachschauen (`sudo journalctl -u fifa-elite-cup-v2 -n 50 --no-pager`).",
                ),
                ephemeral=True,
            )

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=None, embed=info_embed("Abgebrochen."), view=None)


class RegroupSizeModal(discord.ui.Modal, title="Gruppenphase neu aufteilen"):
    group_size = discord.ui.TextInput(label="Neue Gruppengröße (Teams pro Gruppe)", max_length=2)

    def __init__(self, tournament_id: int):
        super().__init__()
        self.tournament_id = tournament_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            size = int(self.group_size.value)
        except ValueError:
            await interaction.response.send_message(embed=error_embed("Gruppengröße muss eine Zahl sein."), ephemeral=True)
            return
        if size < 2:
            await interaction.response.send_message(embed=error_embed("Gruppengröße muss mindestens 2 sein."), ephemeral=True)
            return

        await interaction.response.send_message(
            embed=warning_embed(
                "Sicher?",
                f"Löscht alle bestehenden Gruppen-Kanäle, -Rollen und Gruppen-Ergebnisse unwiderruflich und teilt "
                f"alle angemeldeten Teams neu in **{size}er-Gruppen** ein. Die KO-Phase ist davon nicht betroffen "
                "(muss vorher separat zurückgesetzt sein, falls sie schon lief).",
            ),
            view=RegroupConfirmView(self.tournament_id, size),
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
            embed=info_embed(f"{team_name} austauschen — wie?"),
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
        await swap_team_for_bye(self.tournament_id, self.team_id)
        await refresh_panel(interaction.client, self.tournament_id)
        await interaction.response.edit_message(
            content=None, embed=success_embed(f"{self.team_name} wurde entfernt, der Platz bleibt frei (Freilos)."), view=None
        )

    @discord.ui.button(label="Durch Warteliste ersetzen", style=discord.ButtonStyle.primary)
    async def to_waitlist_swap(self, interaction: discord.Interaction, button: discord.ui.Button):
        waitlist = await get_waitlisted_teams(self.tournament_id)
        if not waitlist:
            await interaction.response.edit_message(content=None, embed=warning_embed("Die Warteliste ist aktuell leer."), view=None)
            return
        await interaction.response.edit_message(
            content=None,
            embed=info_embed(f"Welches Warteliste-Team soll {self.team_name} ersetzen?"),
            view=SwapInSelectView(self.tournament_id, self.team_id, self.team_name, waitlist),
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
        team_id_in = int(interaction.data["values"][0])
        team_in_name = self.team_names.get(team_id_in, f"Team {team_id_in}")
        await swap_team_for_waitlisted(self.tournament_id, self.team_id_out, team_id_in)
        await refresh_panel(interaction.client, self.tournament_id)
        await interaction.response.edit_message(
            content=None, embed=success_embed(f"{self.team_out_name} wurde durch {team_in_name} ersetzt."), view=None
        )


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
            embed=success_embed("Turnier beendet", "Statistiken gepostet, Kanäle/Rollen aufgeräumt."), ephemeral=True
        )

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=None, embed=info_embed("Abgebrochen."), view=None)


class TournamentAdminView(discord.ui.View):
    def __init__(self, t: dict):
        super().__init__(timeout=180)
        self.t = t

    @discord.ui.button(label="Anmeldung schließen", style=discord.ButtonStyle.secondary)
    async def close_signup(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        pool = get_pool()
        await pool.execute("UPDATE tournaments SET status = 'closed' WHERE id = $1", self.t["id"])
        await refresh_panel(interaction.client, self.t["id"])

        t = await get_tournament(self.t["id"])
        registered = await get_registered_teams(self.t["id"])
        for team in registered:
            managers = await get_team_managers(team["id"])
            for m in managers:
                try:
                    user = await interaction.client.fetch_user(m["discord_id"])
                    dm_embed = warning_embed(
                        f"Anmeldung für {t['name']} geschlossen!",
                        f"Bitte bestätigt jetzt mit **{team['name']}** im Turnier-Panel, dass ihr aktiv seid "
                        "(Button 'Team ist da'), sonst kann die Gruppenphase nicht starten.",
                    )
                    await user.send(embed=dm_embed)
                except discord.HTTPException:
                    pass

        await interaction.followup.send(
            embed=success_embed("Anmeldung geschlossen", "Teams wurden per DM zum Aktivitätscheck aufgefordert."),
            ephemeral=True,
        )

    @discord.ui.button(label="Anmeldung öffnen", style=discord.ButtonStyle.secondary)
    async def reopen_signup(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = await get_tournament(self.t["id"])
        if t.get("phase") != "signup":
            await interaction.response.send_message(
                embed=error_embed(
                    "Nicht möglich",
                    "Die Anmeldung kann nur wieder geöffnet werden, solange das Turnier noch nicht in der Gruppenphase ist.",
                ),
                ephemeral=True,
            )
            return
        pool = get_pool()
        await pool.execute("UPDATE tournaments SET status = 'open' WHERE id = $1", self.t["id"])
        await refresh_panel(interaction.client, self.t["id"])
        await interaction.response.send_message(embed=success_embed("Anmeldung wieder geöffnet."), ephemeral=True)

    @discord.ui.button(label="Gruppenphase starten", style=discord.ButtonStyle.success)
    async def start_tournament(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = await get_tournament(self.t["id"])
        if t["status"] in ("started",) or t.get("phase") not in ("signup",):
            await interaction.followup.send(embed=error_embed("Dieses Turnier läuft bereits."), ephemeral=True)
            return
        registered, _ = await get_signup_counts(self.t["id"])
        if registered < 2:
            await interaction.followup.send(
                embed=error_embed(
                    "Zu wenige Teams",
                    f"({registered}) angemeldet. Es werden mindestens 2 Teams benötigt "
                    "(fehlende Plätze bis zur Turnierstufe werden automatisch als Freilose aufgefüllt).",
                ),
                ephemeral=True,
            )
            return

        unconfirmed = await get_unconfirmed_teams(self.t["id"])
        if unconfirmed:
            names = ", ".join(u["name"] for u in unconfirmed)
            await interaction.followup.send(
                embed=warning_embed(
                    f"{len(unconfirmed)} Team(s) noch nicht aktiv gemeldet",
                    f"{names}\n\nNormalerweise sollten erst alle Teams bestätigen ('✅ Team ist da' im Panel). "
                    "Du kannst trotzdem starten, falls nötig:",
                ),
                view=ActivityOverrideView(self.t["id"]),
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
            embed=success_embed(f"{t['name']} gestartet!", "Gruppenkanäle wurden angelegt."), ephemeral=True
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
                embed=error_embed("KO-Phase bereits gestartet", "Bracket-Kanäle existieren schon."), ephemeral=True
            )
            return

        if t.get("phase") not in ("groups", "knockout"):
            await interaction.followup.send(
                embed=error_embed("Nicht möglich", "Die KO-Phase kann erst gestartet werden, wenn die Gruppenphase läuft."),
                ephemeral=True,
            )
            return
        if not await all_groups_complete(self.t["id"]):
            await interaction.followup.send(
                embed=warning_embed(
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
        await interaction.followup.send(embed=success_embed("KO-Phase gestartet!"), ephemeral=True)

    @discord.ui.button(label="KO-Phase resetten", style=discord.ButtonStyle.danger)
    async def reset_ko_phase(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = await get_tournament(self.t["id"])
        if t.get("phase") not in ("knockout",):
            await interaction.response.send_message(embed=error_embed("Es gibt aktuell keine KO-Phase zum Zurücksetzen."), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=warning_embed(
                "Sicher?",
                "Löscht alle Winner-/Loser-Bracket-Kanäle, -Rollen und -Matches unwiderruflich und setzt das "
                "Turnier zurück auf die Gruppenphase (Gruppen bleiben unangetastet).",
            ),
            view=ResetKoConfirmView(self.t["id"]),
            ephemeral=True,
        )

    @discord.ui.button(label="Gruppenphase neu aufteilen", style=discord.ButtonStyle.danger)
    async def regroup(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = await get_tournament(self.t["id"])
        if t.get("phase") != "groups":
            await interaction.response.send_message(
                embed=error_embed(
                    "Nicht möglich",
                    "Neu-Aufteilen ist nur möglich, solange sich das Turnier in der Gruppenphase befindet "
                    "(läuft schon die KO-Phase, erst mit 'KO-Phase resetten' zurücksetzen).",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(RegroupSizeModal(self.t["id"]))

    @discord.ui.button(label="Spielplan-Grafiken posten", style=discord.ButtonStyle.secondary)
    async def post_schedule_graphics(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        pool = get_pool()
        groups = await pool.fetch("SELECT * FROM tournament_groups WHERE tournament_id = $1 ORDER BY group_number", self.t["id"])
        if not groups:
            await interaction.followup.send(embed=error_embed("Keine Gruppen gefunden (Gruppenphase noch nicht gestartet?)."), ephemeral=True)
            return

        from graphics import generate_group_schedule_images

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
                all_group_matches = await pool.fetch(
                    "SELECT * FROM tournament_matches WHERE group_id = $1 ORDER BY round, match_number", group["id"]
                )
                all_team_ids = {m["team1_id"] for m in all_group_matches if m["team1_id"]} | {
                    m["team2_id"] for m in all_group_matches if m["team2_id"]
                }
                team_rows = {tid: await get_pool_team(tid) for tid in all_team_ids}

                max_matchday = max((m["round"] for m in all_group_matches), default=0)
                matchdays_data: list[list[dict]] = [[] for _ in range(max_matchday)]
                for m in all_group_matches:
                    if m["team1_id"] is None or m["team2_id"] is None:
                        continue
                    idx = m["round"] - 1
                    t1, t2 = team_rows[m["team1_id"]], team_rows[m["team2_id"]]
                    matchdays_data[idx].append({
                        "team1_name": t1["name"], "team2_name": t2["name"],
                        "team1_logo_url": t1.get("logo_url"), "team2_logo_url": t2.get("logo_url"),
                    })

                image_bufs = await generate_group_schedule_images(matchdays_data)
                for i, buf in enumerate(image_bufs, start=1):
                    suffix = f"_teil{i}" if len(image_bufs) > 1 else ""
                    await channel.send(file=discord.File(buf, filename=f"spielplan_gruppe_{group['group_number']}{suffix}.png"))
                posted += 1
            except Exception:
                logging.getLogger("fifa-elite-cup").exception(f"Fehler beim nachtraeglichen Posten der Spielplan-Grafik fuer Gruppe {group['id']}")
                failed += 1

        await interaction.followup.send(
            embed=success_embed("Spielplan-Grafiken gepostet", f"Erfolgreich: {posted} | Fehlgeschlagen: {failed}"), ephemeral=True
        )

    @discord.ui.button(label="Team tauschen", style=discord.ButtonStyle.secondary)
    async def swap_team(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = await get_tournament(self.t["id"])
        if t.get("phase") != "signup":
            await interaction.response.send_message(
                embed=error_embed(
                    "Nicht möglich",
                    "Team-Tausch ist nur möglich, solange sich das Turnier noch in der Anmeldephase befindet "
                    "(vor Gruppenphasen-Start).",
                ),
                ephemeral=True,
            )
            return
        registered = await get_registered_teams(self.t["id"])
        if not registered:
            await interaction.response.send_message(embed=error_embed("Keine registrierten Teams vorhanden."), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=info_embed("Welches Team soll ausgetauscht werden?"),
            view=SwapOutSelectView(self.t["id"], registered),
            ephemeral=True,
        )

    @discord.ui.button(label="Teams anzeigen", style=discord.ButtonStyle.secondary)
    async def show_teams(self, interaction: discord.Interaction, button: discord.ui.Button):
        registered = await get_registered_teams(self.t["id"])
        waitlist = await get_waitlisted_teams(self.t["id"])
        embed = info_embed("Teams")
        embed.add_field(
            name=f"Angemeldet ({len(registered)})",
            value="\n".join(f"- {r['name']}" for r in registered) or "- (keine)",
            inline=False,
        )
        if waitlist:
            embed.add_field(name=f"Warteliste ({len(waitlist)})", value="\n".join(f"- {r['name']}" for r in waitlist), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Gruppen-Status", style=discord.ButtonStyle.secondary)
    async def show_group_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = await get_tournament(self.t["id"])
        if t.get("phase") not in ("groups", "knockout", "finished"):
            await interaction.response.send_message(embed=error_embed("Für dieses Turnier wurde noch keine Gruppenphase gestartet."), ephemeral=True)
            return

        standings = await get_group_standings(self.t["id"])
        pool = get_pool()
        embed = info_embed(f"{t['name']} - Gruppenphase")
        for g in standings:
            team_ids = [s["team_id"] for s in g["standings"]]
            names = await team_name_map(team_ids)
            table_lines = [f"{names.get(s['team_id'], '?')}: {s['wins']} Siege" for s in g["standings"]]

            matches = await pool.fetch(
                "SELECT * FROM tournament_matches WHERE group_id = $1 ORDER BY round, match_number", g["group_id"]
            )
            open_matches = [m for m in matches if m["status"] != "completed"]
            if open_matches:
                m_names = await team_name_map([m["team1_id"] for m in open_matches] + [m["team2_id"] for m in open_matches])
                table_lines.append("")
                table_lines.append("Offen:")
                table_lines += [
                    f"ST{m['round']}: {m_names.get(m['team1_id'],'?')} vs {m_names.get(m['team2_id'],'?')}" for m in open_matches
                ]
            embed.add_field(name=f"Gruppe {g['group_number']}", value="\n".join(table_lines) or "-", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Ergebnis eintragen", style=discord.ButtonStyle.primary)
    async def report_result(self, interaction: discord.Interaction, button: discord.ui.Button):
        matches = await get_all_open_matches(self.t["id"])
        if not matches:
            await interaction.response.send_message(embed=error_embed("Keine offenen Matches gefunden."), ephemeral=True)
            return
        team_ids = [m["team1_id"] for m in matches] + [m["team2_id"] for m in matches]
        names = await team_name_map(team_ids)
        await interaction.response.send_message(
            embed=info_embed("Welches Match?", "Admin-Eintragung wird sofort final gespeichert, ohne Bestätigung."),
            view=GroupMatchSelect(matches, names, is_admin=True),
            ephemeral=True,
        )

    @discord.ui.button(label="Ergebnis korrigieren", style=discord.ButtonStyle.secondary)
    async def correct_result(self, interaction: discord.Interaction, button: discord.ui.Button):
        matches = await get_all_completed_matches(self.t["id"])
        if not matches:
            await interaction.response.send_message(embed=error_embed("Keine abgeschlossenen Matches gefunden."), ephemeral=True)
            return
        team_ids = [m["team1_id"] for m in matches] + [m["team2_id"] for m in matches]
        names = await team_name_map(team_ids)
        await interaction.response.send_message(
            embed=info_embed(
                "Welches Match korrigieren?",
                "Läuft die KO-Phase schon, wird eine spätere Korrektur an einem Gruppenspiel NICHT rückwirkend "
                "im Bracket nachgezogen - dann bitte auch das Bracket manuell prüfen.",
            ),
            view=EditMatchSelectView(matches, names),
            ephemeral=True,
        )

    @discord.ui.button(label="🏁 Turnier beenden", style=discord.ButtonStyle.danger)
    async def end_tournament(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = await get_tournament(self.t["id"])
        if t.get("phase") != "knockout":
            await interaction.response.send_message(
                embed=error_embed("Nicht möglich", "Das Turnier muss erst in der KO-Phase sein, bevor es beendet werden kann."),
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
                embed=warning_embed(
                    "Noch nicht fertig",
                    f"{', '.join(missing)} hat noch keinen Sieger. Turnier kann erst beendet werden, "
                    "wenn beide Brackets abgeschlossen sind.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=warning_embed(
                "Sicher?",
                "Das postet die Abschluss-Statistiken (Winner-/Loser-Top3, Awards, Top-11) in die konfigurierten "
                "Kanäle und löscht danach **alle** für dieses Turnier erstellten Kanäle und Rollen "
                "(Gruppen + Winner-/Loser-Bracket) unwiderruflich.",
            ),
            view=EndTournamentConfirmView(self.t["id"]),
            ephemeral=True,
        )

    @discord.ui.button(label="Turnier löschen", style=discord.ButtonStyle.danger)
    async def delete_tournament(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=warning_embed("Sicher?", "Das löscht das Turnier inkl. aller Anmeldungen und Matches unwiderruflich."),
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
        await interaction.response.edit_message(content=None, embed=success_embed("Turnier gelöscht."), view=None)

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=None, embed=info_embed("Abgebrochen."), view=None)


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
            await interaction.followup.send(embed=error_embed("Keine Team-Manager gefunden."), ephemeral=True)
            return

        dm_embed = info_embed(self.dm_title.value, self.dm_message.value)
        dm_embed.set_footer(text=f"{interaction.guild.name} - Nachricht von der Admin-Leitung")

        sent, failed = 0, 0
        for discord_id in recipient_ids:
            try:
                user = await interaction.client.fetch_user(discord_id)
                await user.send(embed=dm_embed)
                sent += 1
            except discord.HTTPException:
                failed += 1

        await interaction.followup.send(
            embed=success_embed("DM-Broadcast abgeschlossen", f"Zugestellt: {sent} | Fehlgeschlagen: {failed}"),
            ephemeral=True,
        )


class AdminPanel(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        text = (
            "# ADMIN PANEL\n"
            "Zentrale Turnierverwaltung für den FIFA Elite Cup.\n"
            "\n"
            "-----\n"
            "\n"
            "**» TURNIER ERSTELLEN**\n"
            "- Name, Mindest-/Maximalteams festlegen (Empfehlung: min. 8)\n"
            "- Bracket-Größe wird automatisch anhand der Anmeldungen berechnet\n"
            "\n"
            "**» TURNIERE VERWALTEN**\n"
            "- Anmeldung schließen/öffnen\n"
            "- Gruppenphase starten (legt automatisch Kanäle + Rollen an)\n"
            "- Teams/Warteliste einsehen, Team tauschen\n"
            "- Turnier löschen\n"
            "\n"
            "**» STATS-KANÄLE EINSTELLEN**\n"
            "- Winner-Top3, Loser-Top3, Awards, Top-11 und Sperren-Log Kanäle festlegen\n"
            "\n"
            "**» SPERREN**\n"
            "- Spieler oder Teams sperren/entsperren (können dann nicht mehr an Turnieren teilnehmen)\n"
            "\n"
            "**» NACHRICHTEN**\n"
            "- Eigene Nachrichten bauen und in einen Kanal posten\n"
            "- DM an alle Vereinsmanager server-weit senden"
        )
        container = discord.ui.Container(
            discord.ui.TextDisplay(text),
            discord.ui.ActionRow(
                discord.ui.Button(label="Turnier erstellen", style=discord.ButtonStyle.primary, custom_id="admin:create"),
                discord.ui.Button(label="Turniere verwalten", style=discord.ButtonStyle.secondary, custom_id="admin:manage"),
                discord.ui.Button(label="Stats-Kanäle einstellen", style=discord.ButtonStyle.secondary, custom_id="admin:statschannels"),
            ),
            discord.ui.ActionRow(
                discord.ui.Button(label="Spieler sperren", style=discord.ButtonStyle.danger, custom_id="admin:banplayer"),
                discord.ui.Button(label="Team sperren", style=discord.ButtonStyle.danger, custom_id="admin:banteam"),
                discord.ui.Button(label="Sperren verwalten", style=discord.ButtonStyle.secondary, custom_id="admin:banlist"),
            ),
            discord.ui.ActionRow(
                discord.ui.Button(label="Nachricht erstellen", style=discord.ButtonStyle.secondary, custom_id="admin:embed"),
                discord.ui.Button(label="DM an alle Vereinsmanager", style=discord.ButtonStyle.secondary, custom_id="admin:dmall"),
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
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_panel_setup(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=AdminPanel())

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("admin:"):
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(embed=error_embed("Nur Admins können das Admin-Panel nutzen."), ephemeral=True)
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
                await interaction.response.send_message(embed=error_embed("Noch keine Turniere vorhanden."), ephemeral=True)
                return
            tournaments = [dict(r) for r in rows]
            await interaction.response.send_message(
                embed=info_embed("Welches Turnier verwalten?"), view=TournamentSelect(tournaments), ephemeral=True
            )

        elif action == "statschannels":
            await interaction.response.send_message(
                embed=info_embed("Wähle für jeden Bereich den passenden Kanal aus:"),
                view=StatsChannelsConfigView(interaction.guild_id, StatsChannelsConfigView.ALL_FIELDS[:5]),
                ephemeral=True,
            )
            await interaction.followup.send(
                embed=info_embed("Und noch zwei:"),
                view=StatsChannelsConfigView(interaction.guild_id, StatsChannelsConfigView.ALL_FIELDS[5:]),
                ephemeral=True,
            )

        elif action == "banplayer":
            await interaction.response.send_message(
                embed=info_embed("Wähle den zu sperrenden Spieler aus:"), view=PlayerBanView(), ephemeral=True
            )

        elif action == "banteam":
            teams = await get_all_guild_teams(interaction.guild_id)
            if not teams:
                await interaction.response.send_message(embed=error_embed("Noch keine Teams auf diesem Server."), ephemeral=True)
                return
            await interaction.response.send_message(
                embed=info_embed("Wähle das zu sperrende Team aus:"), view=TeamBanView(teams), ephemeral=True
            )

        elif action == "banlist":
            user_bans = await get_all_bans(interaction.guild_id)
            team_bans = await get_all_team_bans(interaction.guild_id)
            if not user_bans and not team_bans:
                await interaction.response.send_message(embed=info_embed("Aktuell ist niemand/kein Team gesperrt."), ephemeral=True)
                return
            embed = info_embed("Gesperrte Spieler & Teams")
            if user_bans:
                lines = []
                for b in user_bans:
                    until = b["expires_at"].strftime("%d.%m.%Y %H:%M") if b["expires_at"] else "dauerhaft"
                    lines.append(f"<@{b['discord_id']}> - bis {until} - Grund: {b['reason'] or 'keiner'}")
                embed.add_field(name="Spieler", value="\n".join(lines), inline=False)
            if team_bans:
                lines = []
                for b in team_bans:
                    until = b["expires_at"].strftime("%d.%m.%Y %H:%M") if b["expires_at"] else "dauerhaft"
                    lines.append(f"**{b['team_name']}** - bis {until} - Grund: {b['reason'] or 'keiner'}")
                embed.add_field(name="Teams", value="\n".join(lines), inline=False)
            await interaction.response.send_message(embed=embed, view=UnbanSelect(user_bans, team_bans), ephemeral=True)

        elif action == "embed":
            await interaction.response.send_modal(EmbedBuilderModal())

        elif action == "dmall":
            await interaction.response.send_modal(DMBroadcastModal())


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminPanelCog(bot))
