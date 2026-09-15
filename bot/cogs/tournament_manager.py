"""
Turnier-Cog: Erstellung (Admin), Anmeldung/Abmeldung mit Warteliste,
automatische Bracket-Berechnung im Hintergrund, Turnierstart mit
Runde-1-Paarungen, Team-/Turnierübersicht.
"""
from __future__ import annotations
import asyncio
import io
import logging
import math
import os
import random
import zlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import discord
from discord import app_commands
from discord.ext import commands, tasks

from db import get_pool
from ui_helpers import success_embed, error_embed, info_embed, warning_embed, WEBSITE_URL
from permissions import is_tournament_admin, is_tournament_moderator
from typing import Literal
from cogs.team_manager import (
    get_team_for_user, get_team_for_user_in_group, get_team_for_user_in_tournament,
    get_role_for_user, get_team_managers, is_valid_twitch_link, team_register_hint,
    apply_team_nickname, reset_team_nickname,
)

BERLIN_TZ = ZoneInfo("Europe/Berlin")
TOURNAMENT_BANNER_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "tournament_banner.jpg")
WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def fmt_date_de(dt: datetime) -> str:
    return f"{WEEKDAYS_DE[dt.weekday()]}, {dt.strftime('%d.%m.%Y')}"


def fmt_time_de(dt: datetime) -> str:
    return dt.strftime("%H:%M") + " Uhr"


def fmt_relative_days_de(target: datetime) -> str:
    now = datetime.now(target.tzinfo)
    delta_days = (target.date() - now.date()).days
    if delta_days <= 0:
        return "heute"
    if delta_days == 1:
        return "in 1 Tag"
    return f"in {delta_days} Tagen"
from ea_api import EAProClubsAPI

log = logging.getLogger("fifa-elite-cup")

ALLOWED_BRACKET_SIZES = [8, 12, 16, 20, 24, 32, 36, 40, 48, 64, 68, 72, 80, 96, 128]
# Jede dieser Zahlen laesst sich als Summe zweier 2er-Potenzen (je >= 4) schreiben, z.B.
# 24 = 16 + 8, 40 = 32 + 8. Winner- und Loser-Bracket muessen NICHT mehr gleich gross sein
# (siehe _split_bracket_sizes) - dadurch reicht es, wenn JEDES Bracket fuer sich eine
# 2er-Potenz ist, nicht mehr die Team-Gesamtzahl selbst. Das ergibt deutlich mehr moegliche
# Turnierstufen als nur 8/16/32/64/128, ohne dass jemals eine Qualifikationsrunde noetig wird.


def group_size_for(bracket_size: int, preferred: int | None = None) -> int:
    """Standardmaessig Vierergruppen. `preferred` (z.B. 6, siehe tournaments.group_size_override)
    wird nur genutzt, wenn die aktuelle Turnierstufe glatt dadurch teilbar ist - sonst faellt
    es automatisch auf 4er-Gruppen zurueck. So bleiben ALLE Turnierstufen nutzbar (kleine
    Sprünge), statt nur die durch 6 teilbaren - vorher fuehrte ein erzwungenes "nur 6er" zu
    riesigen Luecken zwischen den Stufen und einer ellenlangen Warteliste."""
    if preferred and bracket_size % preferred == 0:
        return preferred
    return 4


def _split_bracket_sizes(total: int) -> tuple[int, int]:
    """Teilt `total` qualifizierte Teams auf Winner-/Loser-Bracket auf, so dass BEIDE
    Teilgroessen eine 2er-Potenz sind - moeglichst ausgewogen, bei Gleichstand gewinnt die
    groessere Aufteilung fuers Winner-Bracket (naeher am alten 50/50-Verhalten bei reinen
    2er-Potenz-Gesamtzahlen, wo das exakt 50/50 bleibt). Existiert ausnahmsweise keine exakte
    Zerlegung (z.B. weil waehrend des Turniers Teams ausgetreten sind und die Teamzahl dadurch
    von der geplanten Turnierstufe abweicht), faellt es auf die groesstmoegliche 2er-Potenz
    fuers Winner-Bracket zurueck - das Loser-Bracket kann dann ausnahmsweise doch eine
    Qualifikationsrunde brauchen (macht create_bracket() automatisch)."""
    def is_pow2(n: int) -> bool:
        return n > 0 and (n & (n - 1)) == 0

    best = None
    w = 1
    while w < total:
        l = total - w
        if is_pow2(l):
            balance = abs(w - l)
            if best is None or balance < best[0] or (balance == best[0] and w > best[1]):
                best = (balance, w, l)
        w *= 2
    if best:
        return best[1], best[2]

    w = 1
    while w * 2 <= total:
        w *= 2
    return w, total - w


# ---------- Hilfsfunktionen ----------

def compute_bracket_size(total_signups: int, min_teams: int, max_teams: int, group_size_override: int | None = None) -> int:
    """
    Die 'aktive Stufe' ist die groesste Turniergroesse, fuer die bereits GENUG
    Anmeldungen (registriert + Warteliste zusammen) vorliegen, um sie komplett
    zu fuellen (in sauberen 4er- oder 6er-Gruppen). Ein einzelnes Team ueber
    der aktuellen Stufe wandert also erst auf die Warteliste, statt die Stufe
    sofort hochzuschalten - die naechste Stufe wird erst 'aktiv', wenn sie
    wirklich voll waere.
    """
    # group_size_override wird hier NICHT mehr zum Filtern der Stufen verwendet - jede Stufe
    # in ALLOWED_BRACKET_SIZES ist durch 4 teilbar und damit immer nutzbar. group_size_for()
    # entscheidet pro Stufe selbst, ob die bevorzugte Gruppengroesse (z.B. 6er) passt oder
    # automatisch auf 4er zurueckgefallen wird.
    candidates = sorted(s for s in ALLOWED_BRACKET_SIZES if min_teams <= s <= max_teams)
    if not candidates:
        return max_teams
    active = candidates[0]
    for size in candidates:
        if total_signups >= size:
            active = size
        else:
            break
    return active


def bracket_size_progression_text(min_teams: int, max_teams: int, total_signups: int, group_size_override: int | None = None) -> str:
    """Zeigt konkret, ab wie vielen Anmeldungen das Turnier auf welche Groesse waechst -
    damit Teams verstehen, warum ihre Anmeldung das Turnier ggf. noch vergroessert, statt
    nur den vagen Hinweis 'die Groesse waechst automatisch' zu lesen."""
    # group_size_override wird hier NICHT mehr zum Filtern der Stufen verwendet - jede Stufe
    # in ALLOWED_BRACKET_SIZES ist durch 4 teilbar und damit immer nutzbar. group_size_for()
    # entscheidet pro Stufe selbst, ob die bevorzugte Gruppengroesse (z.B. 6er) passt oder
    # automatisch auf 4er zurueckgefallen wird.
    candidates = sorted(s for s in ALLOWED_BRACKET_SIZES if min_teams <= s <= max_teams)
    if not candidates:
        return ""
    active = compute_bracket_size(total_signups, min_teams, max_teams, group_size_override)
    lines = ["### 📈 Wie die Turniergröße wächst"]
    for size in candidates:
        gsize = group_size_for(size, group_size_override)
        num_groups = size // gsize
        marker = "👉" if size == active else "  "
        status = " ← **aktuell**" if size == active else ""
        lines.append(f"{marker} `ab {size} Teams` — {num_groups} Gruppen à {gsize} Teams{status}")
    next_size = next((s for s in candidates if s > active), None)
    if next_size:
        missing = next_size - total_signups
        if missing > 0:
            lines.append(f"\n-# Noch **{missing}** Anmeldung{'en' if missing != 1 else ''} bis zur nächsten Stufe ({next_size} Teams).")
    else:
        lines.append(f"\n-# Maximalgröße erreicht ({max_teams} Teams).")
    return "\n".join(lines)


async def get_tournament(tournament_id: int) -> dict | None:
    pool = get_pool()
    row = await pool.fetchrow("SELECT * FROM tournaments WHERE id = $1", tournament_id)
    return dict(row) if row else None


async def get_unready_groups(tournament_id: int) -> list[dict]:
    """Gruppen dieses Turniers, in denen noch nicht jedes Team 'Team ist da' bestaetigt hat."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT tg.id AS group_id, tg.group_number,
               COUNT(*) FILTER (WHERE NOT tgt.confirmed_ready) AS unready_count
        FROM tournament_group_teams tgt
        JOIN tournament_groups tg ON tg.id = tgt.group_id
        WHERE tg.tournament_id = $1
        GROUP BY tg.id, tg.group_number
        HAVING COUNT(*) FILTER (WHERE NOT tgt.confirmed_ready) > 0
        ORDER BY tg.group_number
        """,
        tournament_id,
    )
    return [dict(r) for r in rows]


async def get_unready_teams(tournament_id: int) -> list[dict]:
    """Einzelne Teams dieses Turniers, die noch nicht 'Team ist da' bestaetigt haben."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT tgt.team_id, te.name AS team_name, tg.group_number
        FROM tournament_group_teams tgt
        JOIN tournament_groups tg ON tg.id = tgt.group_id
        JOIN teams te ON te.id = tgt.team_id
        WHERE tg.tournament_id = $1 AND NOT tgt.confirmed_ready
        ORDER BY tg.group_number
        """,
        tournament_id,
    )
    return [dict(r) for r in rows]


async def get_signup_counts(tournament_id: int) -> tuple[int, int]:
    pool = get_pool()
    registered = await pool.fetchval(
        "SELECT COUNT(*) FROM tournament_signups WHERE tournament_id = $1 AND status = 'registered'",
        tournament_id,
    )
    waitlist = await pool.fetchval(
        "SELECT COUNT(*) FROM tournament_signups WHERE tournament_id = $1 AND status = 'waitlist'",
        tournament_id,
    )
    return registered, waitlist


async def reconcile_signups(tournament_id: int) -> int:
    """
    Berechnet die aktuell aktive Bracket-Stufe neu und teilt ALLE Anmeldungen
    (nach Anmeldezeitpunkt sortiert) entsprechend in 'registered' / 'waitlist'
    auf. Wird nach jeder An-/Abmeldung aufgerufen. Gibt die aktuelle
    Bracket-Groesse zurueck.
    """
    pool = get_pool()
    t = await get_tournament(tournament_id)
    rows = await pool.fetch(
        """
        SELECT id FROM tournament_signups
        WHERE tournament_id = $1 AND status != 'withdrawn'
        ORDER BY signup_time ASC
        """,
        tournament_id,
    )
    total = len(rows)
    bracket_size = compute_bracket_size(total, t["min_teams"], t["max_teams"], t.get("group_size_override"))

    for i, row in enumerate(rows, start=1):
        new_status = "registered" if i <= bracket_size else "waitlist"
        await pool.execute(
            "UPDATE tournament_signups SET status = $1 WHERE id = $2 AND status != $1", new_status, row["id"]
        )
    return bracket_size


def compute_fill_with_bye_options(total_signups: int) -> list[dict]:
    """Fuer 'Jetzt mit Freilos auffuellen': zeigt, wie viele Byes bei 4er- bzw. 6er-Gruppen
    noetig waeren, um ALLE aktuellen Anmeldungen (inkl. Warteliste) sofort mitzunehmen, statt
    auf die naechste feste Turnierstufe zu warten. Kein Bezug zu ALLOWED_BRACKET_SIZES - das
    ist bewusst eine Turnierstufe ausserhalb der Norm, extra fuer diesen Fall."""
    options = []
    for group_size in (4, 6):
        padded = math.ceil(max(total_signups, MIN_BRACKET_SIZE) / group_size) * group_size
        options.append({"group_size": group_size, "bracket_size": padded, "byes": padded - total_signups})
    return options


async def fill_with_bye_and_start(tournament_id: int, bracket_size: int, group_size: int):
    """Setzt die Turnierstufe manuell auf `bracket_size` (siehe compute_fill_with_bye_options)
    und nimmt ALLE Warteliste-Teams sofort mit auf - fuer den Fall '1-2 Teams fehlen noch bis
    zur naechsten Stufe, wir wollen aber jetzt schon mit Freilos starten' statt laenger auf
    weitere echte Anmeldungen zu warten."""
    pool = get_pool()
    await pool.execute(
        "UPDATE tournament_signups SET status = 'registered' WHERE tournament_id = $1 AND status = 'waitlist'",
        tournament_id,
    )
    await pool.execute(
        "UPDATE tournaments SET custom_bracket_size = $1, group_size_override = $2 WHERE id = $3",
        bracket_size, group_size, tournament_id,
    )


async def swap_team_for_bye(tournament_id: int, team_id: int):
    """Entfernt ein registriertes Team ohne Nachruecken (Freilos)."""
    pool = get_pool()
    await pool.execute(
        "UPDATE tournament_signups SET status = 'withdrawn' WHERE tournament_id = $1 AND team_id = $2",
        tournament_id, team_id,
    )


async def swap_team_for_waitlisted(tournament_id: int, team_id_out: int, team_id_in: int):
    """
    Ersetzt ein registriertes Team durch ein beliebiges anderes Team auf dem
    Server - unabhaengig davon, ob das eintauschende Team bereits fuer dieses
    Turnier angemeldet/auf der Warteliste war oder ueberhaupt noch nie.
    """
    pool = get_pool()
    await pool.execute(
        "UPDATE tournament_signups SET status = 'withdrawn' WHERE tournament_id = $1 AND team_id = $2",
        tournament_id, team_id_out,
    )
    await pool.execute(
        """
        INSERT INTO tournament_signups (tournament_id, team_id, status)
        VALUES ($1, $2, 'registered')
        ON CONFLICT (tournament_id, team_id) DO UPDATE SET status = 'registered'
        """,
        tournament_id, team_id_in,
    )


async def get_team_group(tournament_id: int, team_id: int) -> dict | None:
    """Gibt die Gruppe zurueck, in der ein Team gerade spielt - nur relevant, wenn die
    Gruppenphase schon laeuft (fuer 'Team ersetzen'/'Freilos setzen' NACH Anmeldeschluss)."""
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT tg.* FROM tournament_groups tg
        JOIN tournament_group_teams tgt ON tgt.group_id = tg.id
        WHERE tg.tournament_id = $1 AND tgt.team_id = $2
        """,
        tournament_id, team_id,
    )
    return dict(row) if row else None


async def _revoke_group_access(guild: discord.Guild, group_id: int, team_id: int):
    """Entzieht einem Team, das eine Gruppe waehrend der laufenden Phase verlaesst, die
    Gruppen-Rolle (=Kanalzugriff) und setzt den Team-Nickname zurueck."""
    pool = get_pool()
    group = await pool.fetchrow("SELECT role_id FROM tournament_groups WHERE id = $1", group_id)
    role = guild.get_role(group["role_id"]) if group and group["role_id"] else None
    for m in await get_team_managers(team_id):
        member = guild.get_member(m["discord_id"])
        if member is None:
            try:
                member = await guild.fetch_member(m["discord_id"])
            except discord.HTTPException:
                continue
        if role:
            try:
                await member.remove_roles(role)
            except discord.HTTPException:
                pass
        await reset_team_nickname(member)


async def _grant_group_access(guild: discord.Guild, group_id: int, team_id: int, team_name: str):
    """Gibt einem neu in eine laufende Gruppe eintretenden Team die Gruppen-Rolle
    (=Kanalzugriff) und setzt den Team-Nickname - fehlte bisher komplett bei
    replace_team_in_group(), Ersatzteams sahen die Gruppenkanaele dadurch gar nicht."""
    pool = get_pool()
    group = await pool.fetchrow("SELECT role_id FROM tournament_groups WHERE id = $1", group_id)
    role = guild.get_role(group["role_id"]) if group and group["role_id"] else None
    for m in await get_team_managers(team_id):
        member = guild.get_member(m["discord_id"])
        if member is None:
            try:
                member = await guild.fetch_member(m["discord_id"])
            except discord.HTTPException:
                continue
        if role:
            try:
                await member.add_roles(role)
            except discord.HTTPException:
                pass
        await apply_team_nickname(member, team_name)


async def remove_team_from_group_as_bye(bot: commands.Bot, guild: discord.Guild, tournament_id: int, group_id: int, team_id: int):
    """Entfernt ein Team WAEHREND der laufenden Gruppenphase sauber als Freilos - im
    Unterschied zu withdraw_team_with_forfeits() werden KEINE Forfeit-Siege verteilt.
    Bereits gespielte Ergebnisse bleiben unveraendert stehen (echte Historie), nur noch
    offene (pending) Spiele gegen dieses Team werden ersatzlos gestrichen - die Gegner haben
    an dem Spieltag dann schlicht kein Spiel, statt einen gewerteten Freilos-Sieg zu bekommen."""
    pool = get_pool()
    team_row = await get_pool_team(team_id)
    await pool.execute(
        "UPDATE tournament_signups SET status = 'withdrawn' WHERE tournament_id = $1 AND team_id = $2",
        tournament_id, team_id,
    )
    await pool.execute(
        "DELETE FROM tournament_matches WHERE group_id = $1 AND status = 'pending' AND (team1_id = $2 OR team2_id = $2)",
        group_id, team_id,
    )
    await pool.execute("DELETE FROM tournament_group_teams WHERE group_id = $1 AND team_id = $2", group_id, team_id)
    await _revoke_group_access(guild, group_id, team_id)

    group = await pool.fetchrow("SELECT channel_id FROM tournament_groups WHERE id = $1", group_id)
    if group and group["channel_id"]:
        channel = guild.get_channel(group["channel_id"])
        if channel:
            try:
                await channel.send(f"ℹ️ **{team_row['name']}** ist aus dieser Gruppe ausgetreten (Freilos).")
            except discord.HTTPException:
                pass


async def replace_team_in_group(bot: commands.Bot, guild: discord.Guild, tournament_id: int, group_id: int, team_id_out: int, team_id_in: int):
    """Ersetzt ein Team WAEHREND der laufenden Gruppenphase durch ein anderes - das neue Team
    uebernimmt alle noch OFFENEN Spiele (Restspielplan), bereits gespielte Ergebnisse bleiben
    unter dem alten Team-Namen stehen (Historie bleibt korrekt, kein rueckwirkendes Umschreiben).
    Gibt dem neuen Team auch die Gruppen-Rolle (Kanalzugriff) - fehlte vorher komplett, das
    eingetauschte Team konnte die Gruppenkanaele gar nicht sehen."""
    pool = get_pool()
    team_in_row = await get_pool_team(team_id_in)
    await pool.execute(
        "UPDATE tournament_signups SET status = 'withdrawn' WHERE tournament_id = $1 AND team_id = $2",
        tournament_id, team_id_out,
    )
    await pool.execute(
        "INSERT INTO tournament_signups (tournament_id, team_id, status) VALUES ($1, $2, 'registered') "
        "ON CONFLICT (tournament_id, team_id) DO UPDATE SET status = 'registered'",
        tournament_id, team_id_in,
    )
    await pool.execute(
        "UPDATE tournament_group_teams SET team_id = $1 WHERE group_id = $2 AND team_id = $3",
        team_id_in, group_id, team_id_out,
    )
    await pool.execute(
        "UPDATE tournament_matches SET team1_id = $1 WHERE group_id = $2 AND status = 'pending' AND team1_id = $3",
        team_id_in, group_id, team_id_out,
    )
    await pool.execute(
        "UPDATE tournament_matches SET team2_id = $1 WHERE group_id = $2 AND status = 'pending' AND team2_id = $3",
        team_id_in, group_id, team_id_out,
    )

    await _revoke_group_access(guild, group_id, team_id_out)
    await _grant_group_access(guild, group_id, team_id_in, team_in_row["name"])

    group = await pool.fetchrow("SELECT channel_id FROM tournament_groups WHERE id = $1", group_id)
    if group and group["channel_id"]:
        channel = guild.get_channel(group["channel_id"])
        if channel:
            mentions = " ".join(f"<@{m['discord_id']}>" for m in await get_team_managers(team_id_in))
            try:
                await channel.send(f"📢 **{team_in_row['name']}** {mentions} ist neu in dieser Gruppe - willkommen!")
            except discord.HTTPException:
                pass


async def get_all_teams_for_swap(guild_id: int, tournament_id: int, exclude_team_id: int) -> list[dict]:
    """Alle Teams auf dem Server, die aktuell NICHT bei diesem Turnier registriert sind (fuers Eintauschen)."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT t.id, t.name FROM teams t
        WHERE t.guild_id = $1 AND t.id != $2
          AND NOT EXISTS (
            SELECT 1 FROM tournament_signups ts
            WHERE ts.tournament_id = $3 AND ts.team_id = t.id AND ts.status = 'registered'
          )
        ORDER BY t.name
        """,
        guild_id, exclude_team_id, tournament_id,
    )
    return [dict(r) for r in rows]


async def get_team_signup(tournament_id: int, team_id: int) -> dict | None:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM tournament_signups WHERE tournament_id = $1 AND team_id = $2",
        tournament_id, team_id,
    )
    return dict(row) if row else None


async def get_registered_teams(tournament_id: int) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT te.id, te.name, te.owner_discord_id, ts.signup_time, ts.confirmed_active
        FROM tournament_signups ts
        JOIN teams te ON te.id = ts.team_id
        WHERE ts.tournament_id = $1 AND ts.status = 'registered'
        ORDER BY ts.signup_time ASC
        """,
        tournament_id,
    )
    return [dict(r) for r in rows]


async def get_waitlisted_teams(tournament_id: int) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT te.id, te.name, te.owner_discord_id, ts.signup_time
        FROM tournament_signups ts
        JOIN teams te ON te.id = ts.team_id
        WHERE ts.tournament_id = $1 AND ts.status = 'waitlist'
        ORDER BY ts.signup_time ASC
        """,
        tournament_id,
    )
    return [dict(r) for r in rows]


async def generate_round1_bracket(tournament_id: int, t: dict) -> tuple[int, list[dict]]:
    """Erstellt die Runde-1-Paarungen. Gibt (bracket_size, matches) zurueck."""
    pool = get_pool()
    registered = await get_registered_teams(tournament_id)
    team_ids = [r["id"] for r in registered]
    bracket_size = compute_bracket_size(len(team_ids), t["min_teams"], t["max_teams"])

    random.shuffle(team_ids)
    while len(team_ids) < bracket_size:
        team_ids.append(None)  # Freilos

    match_num = 1
    matches = []
    for i in range(0, len(team_ids), 2):
        team1 = team_ids[i]
        team2 = team_ids[i + 1] if i + 1 < len(team_ids) else None
        winner = None
        status = "pending"
        if team1 and not team2:
            winner, status = team1, "completed"
        elif team2 and not team1:
            winner, status = team2, "completed"

        row = await pool.fetchrow(
            """
            INSERT INTO tournament_matches (tournament_id, round, match_number, team1_id, team2_id, winner_id, status)
            VALUES ($1, 1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            tournament_id, match_num, team1, team2, winner, status,
        )
        matches.append({
            "id": row["id"], "match_number": match_num,
            "team1_id": team1, "team2_id": team2, "winner_id": winner, "status": status,
        })
        match_num += 1

    return bracket_size, matches


async def advance_tournament(tournament_id: int, current_round: int, bracket: str = "winner") -> tuple | None:
    """
    Prueft ob die aktuelle Runde (innerhalb eines Brackets: 'winner' oder 'loser')
    komplett ist. Falls ja: generiert die naechste Runde (oder markiert das
    Bracket als beendet, wenn nur noch 1 Team uebrig ist).
    Laeuft in einer Schleife weiter, falls neue Runden durch Freilose direkt
    komplett waeren. Gibt None zurueck, wenn noch auf Ergebnisse gewartet wird,
    sonst ("finished", winner_id, round) oder ("next_round", round, matches).
    """
    pool = get_pool()
    round_num = current_round

    while True:
        matches = await pool.fetch(
            """
            SELECT * FROM tournament_matches
            WHERE tournament_id = $1 AND phase = 'knockout' AND bracket = $2 AND round = $3
                  AND is_third_place_match = false
            ORDER BY match_number
            """,
            tournament_id, bracket, round_num,
        )
        if not matches or not all(m["status"] == "completed" for m in matches):
            return None

        winners = [m["winner_id"] for m in matches]

        if round_num == 1:
            meta = await pool.fetchrow(
                "SELECT direct_entrants FROM tournament_bracket_meta WHERE tournament_id = $1 AND bracket = $2",
                tournament_id, bracket,
            )
            if meta and meta["direct_entrants"]:
                winners += list(meta["direct_entrants"])
                # direct_entrants bewusst NICHT loeschen (round_num==1-Gate oben verhindert
                # Doppelzaehlung in spaeteren Runden) - bracket_round_labels() braucht diesen
                # Wert dauerhaft, um die Qualifikationsrunde auch nach ihrem Abschluss noch
                # korrekt zu erkennen und alle folgenden Rundennamen richtig zu berechnen.

        if len(winners) == 1:
            return ("finished", winners[0], round_num)

        # Halbfinale abgeschlossen (genau 2 Gewinner ziehen in die naechste Runde ein,
        # die dann das Finale ist) -> zusaetzlich ein Spiel um Platz 3 zwischen den
        # beiden Halbfinal-Verlierern anlegen - gilt fuer BEIDE Brackets (Winner + Loser),
        # da beide als eigene KO-Phase bis Finale + Spiel um Platz 3 laufen sollen.
        third_place_match = None
        if len(winners) == 2 and len(matches) == 2:
            losers = []
            for m in matches:
                if m["team1_id"] and m["team2_id"]:  # nur echte Spiele, keine Freilose
                    loser = m["team2_id"] if m["winner_id"] == m["team1_id"] else m["team1_id"]
                    losers.append(loser)
            if len(losers) == 2:
                row = await pool.fetchrow(
                    """
                    INSERT INTO tournament_matches (tournament_id, round, match_number, team1_id, team2_id, status, phase, bracket, is_third_place_match)
                    VALUES ($1, $2, 999, $3, $4, 'pending', 'knockout', $5, true)
                    ON CONFLICT (tournament_id, phase, bracket, round, match_number) DO NOTHING
                    RETURNING id
                    """,
                    tournament_id, round_num + 1, losers[0], losers[1], bracket,
                )
                if row:
                    third_place_match = {
                        "id": row["id"], "match_number": 999,
                        "team1_id": losers[0], "team2_id": losers[1], "winner_id": None, "status": "pending",
                        "is_third_place_match": True,
                    }

        next_round = round_num + 1
        match_num = 1
        next_matches = []
        for i in range(0, len(winners), 2):
            team1 = winners[i]
            team2 = winners[i + 1] if i + 1 < len(winners) else None
            winner = None
            status = "pending"
            if team1 and not team2:
                winner, status = team1, "completed"
            elif team2 and not team1:
                winner, status = team2, "completed"

            row = await pool.fetchrow(
                """
                INSERT INTO tournament_matches (tournament_id, round, match_number, team1_id, team2_id, winner_id, status, phase, bracket)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'knockout', $8)
                RETURNING id
                """,
                tournament_id, next_round, match_num, team1, team2, winner, status, bracket,
            )
            next_matches.append({
                "id": row["id"], "match_number": match_num,
                "team1_id": team1, "team2_id": team2, "winner_id": winner, "status": status,
            })
            match_num += 1

        round_num = next_round
        if third_place_match:
            next_matches.append(third_place_match)
        # Schleife prueft die neue Runde erneut - falls sie zufaellig nur aus
        # Freilosen besteht, geht's direkt weiter zur uebernaechsten Runde.
        if not all(m["status"] == "completed" for m in next_matches):
            return ("next_round", next_round, next_matches)


async def get_current_round(tournament_id: int) -> int | None:
    pool = get_pool()
    return await pool.fetchval(
        "SELECT MAX(round) FROM tournament_matches WHERE tournament_id = $1", tournament_id
    )


async def get_round_matches(tournament_id: int, round_num: int) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT * FROM tournament_matches WHERE tournament_id = $1 AND round = $2 ORDER BY match_number",
        tournament_id, round_num,
    )
    return [dict(r) for r in rows]


async def withdraw_team_with_forfeits(bot: commands.Bot, guild: discord.Guild, tournament_id: int, team_id: int) -> int:
    """
    Team verlaesst mitten im Turnier. Team wird als 'withdrawn' markiert, damit es bei
    einem spaeteren KO-Phase-Start NICHT mehr fuer Winner-/Loser-Bracket qualifiziert
    wird, egal wie seine (eingefrorene) Tabellenposition aussieht. Gibt die Anzahl der
    betroffenen Spiele zurueck.

    Gruppenphase: ALLE Spiele dieses Teams werden 1:0-Niederlage fuer den Gegner
    gewertet - auch bereits gespielte/gewonnene, nicht nur noch offene. Grund: die
    Gruppentabelle ist eine reine Aggregation ohne Verzweigung, ein rueckwirkender
    Wertungsverlust verfaelscht dort nichts weiter Nachgelagertes und verhindert, dass
    ein spaeter aussteigendes Team seinen fruehen Sieg gegen ein anderes Team "behaelt",
    waehrend es selbst nicht mehr zur Verantwortung gezogen werden kann.

    KO-Phase: NUR noch offene (nicht gespielte) Spiele werden 1:0 fuer den Gegner
    gewertet - der Gegner rueckt dadurch ganz normal ueber advance_tournament() in
    die naechste Runde nach. Bereits gespielte KO-Spiele werden NICHT rueckwirkend
    veraendert, weil davon abhaengige Folgerunden (naechste Matches) schon anhand des
    tatsaechlichen Ergebnisses erzeugt wurden - ein nachtraeglicher Sieger-Tausch
    wuerde den Turnierbaum strukturell zerreissen (falsche Team-Paarungen in bereits
    bestehenden Folgerunden). Ein bereits ausgeschiedenes Team braucht ohnehin keine
    Wertung mehr, ein bereits weitergekommenes Team verliert nur sein noch offenes
    naechstes Spiel reell gegen den jeweiligen Gegner.
    """
    pool = get_pool()
    await pool.execute(
        "UPDATE tournament_signups SET status = 'withdrawn' WHERE tournament_id = $1 AND team_id = $2",
        tournament_id, team_id,
    )

    group_matches = await pool.fetch(
        """
        SELECT * FROM tournament_matches
        WHERE tournament_id = $1 AND phase = 'group'
          AND (team1_id = $2 OR team2_id = $2)
        """,
        tournament_id, team_id,
    )
    ko_matches = await pool.fetch(
        """
        SELECT * FROM tournament_matches
        WHERE tournament_id = $1 AND phase = 'knockout' AND status != 'completed'
          AND (team1_id = $2 OR team2_id = $2)
        """,
        tournament_id, team_id,
    )

    count = 0
    for m in list(group_matches) + list(ko_matches):
        opponent_id = m["team2_id"] if m["team1_id"] == team_id else m["team1_id"]
        if opponent_id is None:
            continue  # Freilos gegen Freilos - nichts zu werten
        score1 = 0 if m["team1_id"] == team_id else 1
        score2 = 1 if m["team1_id"] == team_id else 0
        await finalize_match_result(bot, guild, m["id"], score1, score2)
        count += 1
    return count


async def get_match(match_id: int) -> dict | None:
    pool = get_pool()
    row = await pool.fetchrow("SELECT * FROM tournament_matches WHERE id = $1", match_id)
    return dict(row) if row else None


async def get_open_matches_for_team(group_id: int, team_id: int) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT tm.* FROM tournament_matches tm
        JOIN tournament_groups tg ON tg.id = tm.group_id
        WHERE tm.group_id = $1 AND tm.status = 'pending' AND tm.round <= tg.released_round
              AND (tm.team1_id = $2 OR tm.team2_id = $2)
        ORDER BY tm.round DESC, tm.match_number
        """,
        group_id, team_id,
    )
    return [dict(r) for r in rows]


async def get_open_matches_in_group(group_id: int) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT tm.* FROM tournament_matches tm
        JOIN tournament_groups tg ON tg.id = tm.group_id
        WHERE tm.group_id = $1 AND tm.status = 'pending' AND tm.round <= tg.released_round
        ORDER BY tm.round DESC, tm.match_number
        """,
        group_id,
    )
    return [dict(r) for r in rows]


async def get_open_matches_for_team_bracket(tournament_id: int, bracket: str, team_id: int) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT * FROM tournament_matches
        WHERE tournament_id = $1 AND phase = 'knockout' AND bracket = $2 AND status = 'pending'
              AND (team1_id = $3 OR team2_id = $3)
        ORDER BY round DESC, match_number
        """,
        tournament_id, bracket, team_id,
    )
    return [dict(r) for r in rows]


async def get_open_matches_in_bracket(tournament_id: int, bracket: str) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT * FROM tournament_matches
        WHERE tournament_id = $1 AND phase = 'knockout' AND bracket = $2 AND status = 'pending'
              AND team1_id IS NOT NULL AND team2_id IS NOT NULL
        ORDER BY round DESC, match_number
        """,
        tournament_id, bracket,
    )
    return [dict(r) for r in rows]


async def get_all_open_matches(tournament_id: int) -> list[dict]:
    """Alle offenen Matches eines Turniers (Gruppen- UND KO-Phase), fuer Admin-Auswahl - ignoriert Spieltag-Freigabe.
    KO-Phase-Spiele zuerst, damit sie nicht durch das 25er-Auswahllisten-Limit von alten Gruppenspielen verdraengt werden."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT * FROM tournament_matches
        WHERE tournament_id = $1 AND status = 'pending' AND team1_id IS NOT NULL AND team2_id IS NOT NULL
        ORDER BY (phase = 'knockout') DESC, round, match_number
        """,
        tournament_id,
    )
    return [dict(r) for r in rows]


async def get_all_completed_matches(tournament_id: int) -> list[dict]:
    """Alle bereits abgeschlossenen Matches, zum nachtraeglichen Korrigieren durch einen Admin.
    KO-Phase-Spiele zuerst, damit sie nicht durch das 25er-Auswahllisten-Limit von alten Gruppenspielen verdraengt werden."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT * FROM tournament_matches
        WHERE tournament_id = $1 AND status = 'completed' AND team1_id IS NOT NULL AND team2_id IS NOT NULL
        ORDER BY (phase = 'knockout') DESC, round, match_number
        """,
        tournament_id,
    )
    return [dict(r) for r in rows]


async def try_fetch_ea_result(team1: dict, team2: dict) -> tuple[int, int] | None:
    """
    Versucht, das Ergebnis eines Freundschaftsspiels zwischen zwei Teams direkt
    aus der EA-API zu ziehen (letzte 20 Freundschaftsspiele von Team 1, gesucht
    wird ein Match gegen den EA-Club von Team 2). Gibt (tore_team1, tore_team2)
    zurueck oder None, falls kein passendes Match gefunden wurde.
    """
    if not team1.get("ea_club_id") or not team2.get("ea_club_id"):
        return None
    try:
        async with EAProClubsAPI() as api:
            matches = await api.get_matches(
                team1["ea_club_id"], team1.get("ea_platform") or "common-gen5",
                match_type="friendlyMatch", max_results=20,
            )
    except Exception:
        return None

    for m in matches:
        clubs = m.get("clubs", {})
        c1 = clubs.get(str(team1["ea_club_id"]))
        c2 = clubs.get(str(team2["ea_club_id"]))
        if c1 and c2:
            try:
                return int(c1.get("goals", 0)), int(c2.get("goals", 0))
            except (TypeError, ValueError):
                continue
    return None


async def get_bracket_podium_places(tournament_id: int, champion_id: int, bracket: str) -> dict[int, dict]:
    """Ermittelt Erster/Zweiter/Dritter eines Brackets (soweit ermittelbar). Gibt {platz: team_row} zurueck."""
    pool = get_pool()
    t = await get_tournament(tournament_id)

    final_match = await pool.fetchrow(
        """
        SELECT * FROM tournament_matches
        WHERE tournament_id = $1 AND phase = 'knockout' AND bracket = $2 AND (team1_id = $3 OR team2_id = $3)
        ORDER BY round DESC LIMIT 1
        """,
        tournament_id, bracket, champion_id,
    )
    runner_up_id, final_round = None, None
    if final_match:
        runner_up_id = final_match["team2_id"] if final_match["team1_id"] == champion_id else final_match["team1_id"]
        final_round = final_match["round"]

    third_place_column = "winner_bracket_third_id" if bracket == "winner" else "loser_bracket_third_id"
    third_place_id = t.get(third_place_column)
    if third_place_id is None and final_round and final_round > 1:
        semi_matches = await pool.fetch(
            """
            SELECT * FROM tournament_matches
            WHERE tournament_id = $1 AND phase = 'knockout' AND bracket = $2 AND round = $3
                  AND status = 'completed' AND is_third_place_match = false
            """,
            tournament_id, bracket, final_round - 1,
        )
        losers = [
            (m["team2_id"] if m["winner_id"] == m["team1_id"] else m["team1_id"])
            for m in semi_matches if m["winner_id"] and m["team1_id"] and m["team2_id"]
        ]
        if losers:
            third_place_id = losers[0]

    ids = [i for i in (champion_id, runner_up_id, third_place_id) if i]
    team_rows = {tid: await get_pool_team(tid) for tid in ids}

    places = {1: team_rows[champion_id]}
    if runner_up_id:
        places[2] = team_rows[runner_up_id]
    if third_place_id:
        places[3] = team_rows[third_place_id]
    return places


async def build_bracket_finish_file(tournament_id: int, champion_id: int, bracket: str) -> discord.File:
    """Podium-Grafik fuer den Bracket-Abschluss: Erster/Zweiter/Dritter (soweit ermittelbar)."""
    t = await get_tournament(tournament_id)
    bracket_label = "Winner Bracket" if bracket == "winner" else "Loser Bracket"
    places = await get_bracket_podium_places(tournament_id, champion_id, bracket)
    image_places = {rank: (row["name"], row.get("logo_url")) for rank, row in places.items()}

    from graphics import render_podium_image
    buf = await render_podium_image(f"{bracket_label} Champion", t["name"], image_places)
    return discord.File(buf, filename="podium.png")


async def build_bracket_finish_text(tournament_id: int, champion_id: int, bracket: str) -> str:
    """Vollstaendige Platzierung als lesbarer Text (fuer die Components-V2-Nachricht neben dem Podium-Bild)."""
    places = await get_bracket_podium_places(tournament_id, champion_id, bracket)
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"{medals[rank]} **{row['name']}**" for rank, row in sorted(places.items())]
    return "\n".join(lines)


async def post_live_result(bot: commands.Bot, guild: discord.Guild, match: dict, score1: int, score2: int, winner_id: int | None):
    """Postet jedes fertig gespielte Match sofort in den konfigurierten Live-Ergebnis-Kanal (falls eingerichtet) -
    Pendant zum Ticker-Band auf der Website."""
    pool = get_pool()
    row = await pool.fetchrow("SELECT results_feed_channel_id FROM guild_settings WHERE guild_id = $1", guild.id)
    channel_id = row["results_feed_channel_id"] if row else None
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except discord.HTTPException:
            return

    names = await team_name_map([match["team1_id"], match["team2_id"]])
    t1, t2 = names.get(match["team1_id"], "?"), names.get(match["team2_id"], "?")
    if winner_id == match["team1_id"]:
        t1 = f"**{t1}**"
    elif winner_id == match["team2_id"]:
        t2 = f"**{t2}**"
    try:
        await channel.send(f"⚽ {t1} `{score1}:{score2}` {t2} — <{WEBSITE_URL}/stats>", allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass


async def finalize_match_result(bot: commands.Bot, guild: discord.Guild, match_id: int, score1: int, score2: int):
    """Setzt das Ergebnis final, bestimmt den Sieger und schaltet die Phase ggf. weiter."""
    pool = get_pool()
    match = await get_match(match_id)
    winner_id = None
    if score1 > score2:
        winner_id = match["team1_id"]
    elif score2 > score1:
        winner_id = match["team2_id"]

    await pool.execute(
        """
        UPDATE tournament_matches
        SET team1_score = $1, team2_score = $2, winner_id = $3, status = 'completed', pending_confirmation = false
        WHERE id = $4
        """,
        score1, score2, winner_id, match_id,
    )
    await post_live_result(bot, guild, match, score1, score2, winner_id)

    if match["team1_id"] and match["team2_id"]:
        from cogs.stats_manager import capture_match_player_stats
        asyncio.create_task(capture_match_player_stats(match_id, match["team1_id"], match["team2_id"], score1, score2))

    t = await get_tournament(match["tournament_id"])

    if match.get("is_third_place_match"):
        column = "winner_bracket_third_id" if match["bracket"] == "winner" else "loser_bracket_third_id"
        await pool.execute(
            f"UPDATE tournaments SET {column} = $1 WHERE id = $2",
            winner_id, match["tournament_id"],
        )
        names = await team_name_map([match["team1_id"], match["team2_id"]])
        bracket_meta = await pool.fetchrow(
            "SELECT * FROM tournament_bracket_meta WHERE tournament_id = $1 AND bracket = $2",
            match["tournament_id"], match["bracket"],
        )
        if bracket_meta:
            channel = guild.get_channel(bracket_meta["channel_id"])
            if channel is None:
                try:
                    channel = await guild.fetch_channel(bracket_meta["channel_id"])
                except discord.HTTPException:
                    channel = None
            if channel:
                await channel.send(f"🥉 **Spiel um Platz 3:** {names.get(winner_id, '?')} wird Dritter!")
        await refresh_bracket_panel(bot, match["tournament_id"], match["bracket"])
        return None

    if match["phase"] == "group":
        await refresh_group_panel(bot, match["group_id"])
        if await all_groups_complete(match["tournament_id"]):
            result = await start_knockout_phase(bot, guild, match["tournament_id"], t)
            await refresh_live_schedule(bot, guild, match["tournament_id"])
            return result
        await check_and_release_next_matchday(bot, guild, match["group_id"], match["round"])
        await refresh_live_schedule(bot, guild, match["tournament_id"])
        return None

    bracket = match["bracket"] or "winner"
    await refresh_bracket_panel(bot, match["tournament_id"], bracket)
    result = await advance_tournament(match["tournament_id"], match["round"], bracket)
    if result is None:
        await refresh_live_schedule(bot, guild, match["tournament_id"])
        return None

    pool2 = get_pool()
    bracket_meta = await pool2.fetchrow(
        "SELECT * FROM tournament_bracket_meta WHERE tournament_id = $1 AND bracket = $2",
        match["tournament_id"], bracket,
    )
    bracket_channel = None
    if bracket_meta:
        bracket_channel = guild.get_channel(bracket_meta["channel_id"])
        if bracket_channel is None:
            try:
                bracket_channel = await guild.fetch_channel(bracket_meta["channel_id"])
            except discord.HTTPException:
                bracket_channel = None

    if result[0] == "finished":
        _, champion_id, _ = result
        podium_file = await build_bracket_finish_file(match["tournament_id"], champion_id, bracket)
        if bracket_channel:
            podium_text = await build_bracket_finish_text(match["tournament_id"], champion_id, bracket)
            bracket_label = "Winner Bracket" if bracket == "winner" else "Loser Bracket"
            t_row = await get_tournament(match["tournament_id"])
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(discord.ui.Container(
                discord.ui.TextDisplay(f"# 🏆 {bracket_label} Champion\n{t_row['name']}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(podium_text),
                discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://podium.png")),
                accent_color=discord.Color.gold(),
            ))
            await bracket_channel.send(view=view, files=[podium_file])

        champion_field = "winner_champion_id" if bracket == "winner" else "loser_champion_id"
        await pool2.execute(f"UPDATE tournaments SET {champion_field} = $1 WHERE id = $2", champion_id, match["tournament_id"])

        row = await pool2.fetchrow(
            "SELECT winner_champion_id, loser_champion_id FROM tournaments WHERE id = $1", match["tournament_id"]
        )
        bracket_count = await pool2.fetchval(
            "SELECT COUNT(*) FROM tournament_bracket_meta WHERE tournament_id = $1", match["tournament_id"]
        )
        both_done = (
            row["winner_champion_id"] is not None
            and (bracket_count < 2 or row["loser_champion_id"] is not None)
        )
        if both_done:
            await pool2.execute("UPDATE tournaments SET status = 'finished' WHERE id = $1", match["tournament_id"])
        await refresh_bracket_panel(bot, match["tournament_id"], bracket)

    elif result[0] == "next_round":
        _, next_round, next_matches = result
        round_label = round_name(len([m for m in next_matches if not m.get("is_third_place_match")]) or len(next_matches))
        if bracket_channel:
            await bracket_channel.send("➡️ Vorrunde abgeschlossen, weiter geht's:")
            await release_ko_round(bot, bracket_channel, next_matches, round_label)
            await bracket_channel.send(view=build_bracket_actions_view(match["tournament_id"], bracket))
        if bracket_meta and bracket_meta["panel_channel_id"]:
            panel_channel = guild.get_channel(bracket_meta["panel_channel_id"])
            if panel_channel is None:
                try:
                    panel_channel = await guild.fetch_channel(bracket_meta["panel_channel_id"])
                except discord.HTTPException:
                    panel_channel = None
            await _purge_transient_action_messages(panel_channel)
        await refresh_bracket_panel(bot, match["tournament_id"], bracket)

    await refresh_live_schedule(bot, guild, match["tournament_id"])
    return result


class ScoreModal(discord.ui.Modal):
    def __init__(
        self, match_id: int, team1_id: int, team2_id: int, team1_name: str, team2_name: str, is_admin: bool,
        default_score1: int | None = None, default_score2: int | None = None,
    ):
        super().__init__(title="Ergebnis eintragen" if default_score1 is None else "Ergebnis korrigieren")
        self.match_id = match_id
        self.team1_id = team1_id
        self.team2_id = team2_id
        self.is_admin = is_admin
        self.score1_input = discord.ui.TextInput(
            label=f"Tore {team1_name}"[:45], max_length=2,
            default=str(default_score1) if default_score1 is not None else None,
        )
        self.score2_input = discord.ui.TextInput(
            label=f"Tore {team2_name}"[:45], max_length=2,
            default=str(default_score2) if default_score2 is not None else None,
        )
        self.add_item(self.score1_input)
        self.add_item(self.score2_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            s1 = int(self.score1_input.value)
            s2 = int(self.score2_input.value)
        except ValueError:
            await interaction.response.send_message(view=error_embed("Bitte gültige Zahlen eingeben."), ephemeral=True)
            return

        if self.is_admin:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await finalize_match_result(interaction.client, interaction.guild, self.match_id, s1, s2)
                from audit import log_action
                await log_action(interaction.guild_id, interaction.user, "match.result_confirmed", "match", self.match_id, f"{s1}:{s2} (Admin)")
                await interaction.followup.send(view=success_embed(f"Admin-Ergebnis gespeichert: {s1}:{s2}"), ephemeral=True)
            except Exception:
                log.exception(f"Fehler beim Verarbeiten des Admin-Ergebnisses für Match {self.match_id}")
                await interaction.followup.send(
                    view=error_embed(
                        f"Ergebnis {s1}:{s2} wurde gespeichert, aber danach ist ein Fehler aufgetreten",
                        "(z.B. beim Starten der KO-Phase oder Aktualisieren des Live-Spielplans). Bitte im Log nachschauen.",
                    ),
                    ephemeral=True,
                )
            return

        # Sofort antworten (defer), BEVOR die (potenziell langsame) Spielplan-Grafik erzeugt wird -
        # sonst laeuft das 3-Sekunden-Interaktionsfenster ab, bevor ueberhaupt geantwortet wurde
        # ("Unknown interaction"), live beobachtet bei mehreren Ergebnis-Meldungen.
        await interaction.response.defer()

        role1 = await get_role_for_user(self.team1_id, interaction.user.id)
        reporter_team_id = self.team1_id if role1 else self.team2_id
        opponent_team_id = self.team2_id if reporter_team_id == self.team1_id else self.team1_id

        pool = get_pool()
        await pool.execute(
            """
            UPDATE tournament_matches
            SET team1_score = $1, team2_score = $2, reported_by_team_id = $3, pending_confirmation = true
            WHERE id = $4
            """,
            s1, s2, reporter_team_id, self.match_id,
        )
        from audit import log_action
        await log_action(interaction.guild_id, interaction.user, "match.result_reported", "match", self.match_id, f"{s1}:{s2}")
        match = await get_match(self.match_id)
        names = await team_name_map([match["team1_id"], match["team2_id"]])
        opponent_managers = await get_team_managers(opponent_team_id)
        mentions = " ".join(f"<@{m['discord_id']}>" for m in opponent_managers) or "(kein Manager gefunden)"

        text = (
            (f"{mentions}\n" if mentions else "")
            + f"### {names.get(match['team1_id'])} {s1}:{s2} {names.get(match['team2_id'])}\n"
            "Ergebnis gemeldet - bitte bestätigen oder ablehnen."
        )
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text),
                discord.ui.ActionRow(
                    discord.ui.Button(label="Bestätigen", style=discord.ButtonStyle.success, custom_id=f"matchconfirm:{self.match_id}:yes"),
                    discord.ui.Button(label="Ablehnen", style=discord.ButtonStyle.danger, custom_id=f"matchconfirm:{self.match_id}:no"),
                ),
                accent_color=discord.Color.gold(),
            )
        )

        image_bytes = None
        image_filename = "spielplan.png"
        if match["phase"] == "group" and match.get("group_id"):
            try:
                group = await get_pool().fetchrow("SELECT * FROM tournament_groups WHERE id = $1", match["group_id"])
                image_file = await build_group_schedule_file(dict(group))
                image_bytes = image_file.fp.read()
                image_filename = image_file.filename
            except Exception:
                log.exception(f"Fehler beim Erstellen der Spielplan-Grafik fuer Bestaetigungs-Embed (Match {self.match_id})")

        # In BEIDE Kanaele posten (Gruppen-/Bracket-Kanal + Panel-Kanal) - analog zur
        # Groessenvideo-Anforderung, da die Buttons nur im Panel-Kanal sitzen, viele Manager
        # aber eher den normalen Kanal im Blick haben.
        channel_ids: set[int] = set()
        if match["phase"] == "group" and match.get("group_id"):
            group_row = await get_pool().fetchrow("SELECT channel_id, panel_channel_id FROM tournament_groups WHERE id = $1", match["group_id"])
            if group_row:
                channel_ids = {group_row["channel_id"], group_row["panel_channel_id"]}
        elif match["phase"] == "knockout":
            bracket_row = await get_pool().fetchrow(
                "SELECT channel_id, panel_channel_id FROM tournament_bracket_meta WHERE tournament_id = $1 AND bracket = $2",
                match["tournament_id"], match["bracket"],
            )
            if bracket_row:
                channel_ids = {bracket_row["channel_id"], bracket_row["panel_channel_id"]}
        if not channel_ids:
            channel_ids = {interaction.channel_id}

        sent_once = False
        for channel_id in channel_ids:
            if not channel_id:
                continue
            target_channel = interaction.guild.get_channel(channel_id)
            if target_channel is None:
                try:
                    target_channel = await interaction.guild.fetch_channel(channel_id)
                except discord.HTTPException:
                    continue
            files = [discord.File(io.BytesIO(image_bytes), filename=image_filename)] if image_bytes else []
            try:
                await target_channel.send(view=view, files=files)
                sent_once = True
            except discord.HTTPException:
                log.exception(f"Fehler beim Posten der Ergebnis-Bestaetigung in Kanal {channel_id} (Match {self.match_id})")

        if not sent_once:
            await interaction.followup.send(view=view)


class ConfirmMatchView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id
        confirm_btn = discord.ui.Button(label="Bestätigen", style=discord.ButtonStyle.success, custom_id=f"matchconfirm:{match_id}:yes")
        reject_btn = discord.ui.Button(label="Ablehnen", style=discord.ButtonStyle.danger, custom_id=f"matchconfirm:{match_id}:no")
        self.add_item(confirm_btn)
        self.add_item(reject_btn)


async def resolve_match_score_entry(interaction: discord.Interaction, match_id: int, is_admin: bool):
    """
    Gemeinsame Logik: prueft schnell (max 2s) die EA-API, uebernimmt bei Treffer
    direkt, sonst oeffnet sich das Tore-Eingabe-Popup. Wird sowohl direkt (1
    offenes Match) als auch nach Dropdown-Auswahl (mehrere Matches) genutzt.
    """
    match = await get_match(match_id)
    team1 = await get_pool_team(match["team1_id"])
    team2 = await get_pool_team(match["team2_id"])

    try:
        ea_result = await asyncio.wait_for(try_fetch_ea_result(team1, team2), timeout=2.0)
    except asyncio.TimeoutError:
        ea_result = None

    if ea_result:
        s1, s2 = ea_result
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await finalize_match_result(interaction.client, interaction.guild, match_id, s1, s2)
            await interaction.followup.send(
                view=success_embed(
                    "Ergebnis automatisch aus der EA-API übernommen",
                    f"**{team1['name']} {s1}:{s2} {team2['name']}**",
                ),
            )
        except Exception:
            log.exception(f"Fehler beim Verarbeiten des EA-Ergebnisses für Match {match_id}")
            await interaction.followup.send(
                view=error_embed(f"Ergebnis {s1}:{s2} wurde gespeichert, aber danach ist ein Fehler aufgetreten", "Bitte im Log nachschauen."),
                ephemeral=True,
            )
        return

    await interaction.response.send_modal(
        ScoreModal(match_id, match["team1_id"], match["team2_id"], team1["name"], team2["name"], is_admin)
    )


class GroupMatchSelect(discord.ui.View):
    def __init__(self, matches: list[dict], names: dict[int, str], is_admin: bool):
        super().__init__(timeout=120)
        self.is_admin = is_admin
        options = [
            discord.SelectOption(
                label=f"{names.get(m['team1_id'], '?')} vs {names.get(m['team2_id'], '?')}"[:100],
                value=str(m["id"]),
            )
            for m in matches[:25]
        ]
        select = discord.ui.Select(placeholder="Match auswählen...", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        match_id = int(interaction.data["values"][0])
        await resolve_match_score_entry(interaction, match_id, self.is_admin)


async def get_pool_team(team_id: int) -> dict:
    pool = get_pool()
    row = await pool.fetchrow("SELECT * FROM teams WHERE id = $1", team_id)
    return dict(row)


def build_bracket_actions_view(tournament_id: int, bracket: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(
        discord.ui.TextDisplay(
            "**Spielaktionen**\n"
            "- Gespielt: prüft automatisch bei EA, ob ihr gegen den richtigen Gegner gespielt habt, "
            "und trägt das Ergebnis direkt ein\n"
            "- Ergebnis eintragen: falls die Auto-Erkennung nichts findet, hier manuell eintragen "
            "(der Gegner muss bestätigen)\n"
            "- Größenvideo anfordern: pingt den Gegner-Manager eures aktuellen Matches"
        ),
        discord.ui.ActionRow(
            discord.ui.Button(label="Gespielt", style=discord.ButtonStyle.success, custom_id=f"bracketaction:{tournament_id}:{bracket}:played"),
            discord.ui.Button(label="Ergebnis eintragen", style=discord.ButtonStyle.primary, custom_id=f"bracketaction:{tournament_id}:{bracket}:report"),
            discord.ui.Button(label="Größenvideo anfordern", style=discord.ButtonStyle.secondary, custom_id=f"bracketaction:{tournament_id}:{bracket}:sizevideo"),
        ),
        accent_color=discord.Color.gold(),
    )
    view.add_item(container)
    return view


async def _team_group_record(pool, group_id: int, team_id: int) -> dict:
    """Sieg/Unentschieden/Niederlage + Tore eines Teams in einer Gruppe, mit normaler
    Fussball-Punktewertung (3 Punkte Sieg, 1 Punkt Unentschieden, 0 Punkte Niederlage) -
    vorher zaehlte hier NUR winner_id = team_id ("Siege"), Unentschieden gingen komplett
    unter, es gab ueberhaupt keine Punktewertung."""
    row = await pool.fetchrow(
        """
        SELECT
          COUNT(*) AS played,
          COUNT(*) FILTER (WHERE winner_id = $2) AS wins,
          COUNT(*) FILTER (
            WHERE winner_id IS NULL AND team1_id IS NOT NULL AND team2_id IS NOT NULL
          ) AS draws,
          COALESCE(SUM(CASE WHEN team1_id = $2 THEN team1_score WHEN team2_id = $2 THEN team2_score ELSE 0 END), 0) AS goals_for,
          COALESCE(SUM(CASE WHEN team1_id = $2 THEN team2_score WHEN team2_id = $2 THEN team1_score ELSE 0 END), 0) AS goals_against
        FROM tournament_matches
        WHERE group_id = $1 AND status = 'completed' AND (team1_id = $2 OR team2_id = $2)
        """,
        group_id, team_id,
    )
    wins, draws = row["wins"], row["draws"]
    losses = row["played"] - wins - draws
    goals_for, goals_against = row["goals_for"] or 0, row["goals_against"] or 0
    return {
        "team_id": team_id, "wins": wins, "draws": draws, "losses": losses,
        "points": wins * 3 + draws,
        "goals_for": goals_for, "goals_against": goals_against,
        "goal_diff": goals_for - goals_against,
    }


async def build_group_standings_text(group_id: int) -> str:
    pool = get_pool()
    team_rows = await pool.fetch("SELECT team_id FROM tournament_group_teams WHERE group_id = $1", group_id)
    standings = [await _team_group_record(pool, group_id, tr["team_id"]) for tr in team_rows]
    standings = await _sort_group_standings(pool, group_id, standings)

    names = await team_name_map([s["team_id"] for s in standings])
    lines = ["**Tabelle**", ""]
    for i, s in enumerate(standings, start=1):
        lines.append(
            f"`{i}.` {names.get(s['team_id'], '?')} — `{s['points']}` Punkte ({s['wins']}S/{s['draws']}U) · "
            f"Tore `{s['goals_for']}:{s['goals_against']}` (`{s['goal_diff']:+d}`)"
        )
    return "\n".join(lines)


async def grant_live_tournament_access(guild: discord.Guild, team_id: int, member: discord.Member):
    """
    Gibt einem neuen Team-Manager (Owner oder Co-Manager) sofortigen Zugriff auf
    ALLE aktuell laufenden Turnier-Kanaele (Gruppen- + Bracket-Rollen), in denen
    das Team gerade mitspielt - wichtig, wenn ein Co-Manager WAEHREND eines
    laufenden Turniers hinzugefuegt wird.
    """
    pool = get_pool()

    group_rows = await pool.fetch(
        """
        SELECT tg.role_id FROM tournament_group_teams tgt
        JOIN tournament_groups tg ON tg.id = tgt.group_id
        JOIN tournaments t ON t.id = tg.tournament_id
        WHERE tgt.team_id = $1 AND t.phase = 'groups'
        """,
        team_id,
    )
    bracket_rows = await pool.fetch(
        """
        SELECT DISTINCT tbm.role_id FROM tournament_matches tm
        JOIN tournament_bracket_meta tbm ON tbm.tournament_id = tm.tournament_id AND tbm.bracket = tm.bracket
        JOIN tournaments t ON t.id = tm.tournament_id
        WHERE (tm.team1_id = $1 OR tm.team2_id = $1) AND tm.phase = 'knockout' AND t.phase = 'knockout'
        """,
        team_id,
    )

    for row in list(group_rows) + list(bracket_rows):
        role = guild.get_role(row["role_id"])
        if role:
            try:
                await member.add_roles(role)
            except discord.HTTPException:
                pass


async def build_group_panel(group_id: int) -> discord.ui.LayoutView:
    """
    Landet im eigenen Panel-Kanal (nur Bot darf dort schreiben). Zeigt Tabelle +
    Spielplan-Grafik + (nur SOLANGE Spieltag 1 noch nicht freigegeben ist) den
    Aktivitaets-Check "Team ist da" mit Button - rein informativ, blockiert nichts.
    Sobald Spieltag 1 freigegeben ist, ist der Check durch (die Gruppe spielt jetzt),
    das Panel zeigt den Abschnitt danach nicht mehr - sonst haengt ein toter Button
    dauerhaft im Panel rum, den niemand mehr braucht.
    Die Spielplan-Grafik wird per MediaGallery eingebettet (view.schedule_file
    muss vom Aufrufer zusaetzlich in files= mitgegeben werden).
    """
    pool = get_pool()
    group = await pool.fetchrow("SELECT * FROM tournament_groups WHERE id = $1", group_id)
    view = discord.ui.LayoutView(timeout=None)
    schedule_file = await build_group_schedule_file(dict(group))
    view.schedule_file = schedule_file
    media = discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://spielplan.png"))

    standings_text = await build_group_standings_text(group_id)

    items = [
        discord.ui.TextDisplay(standings_text),
        media,
    ]

    if group["released_round"] == 0:
        team_rows = await pool.fetch(
            "SELECT tgt.team_id, tgt.confirmed_ready, te.name FROM tournament_group_teams tgt "
            "JOIN teams te ON te.id = tgt.team_id WHERE tgt.group_id = $1 ORDER BY te.name",
            group_id,
        )
        confirmed = [r for r in team_rows if r["confirmed_ready"]]
        ready_lines = [f"### ✅ Team ist da ({len(confirmed)}/{len(team_rows)})"]
        for r in team_rows:
            ready_lines.append(f"{'✅' if r['confirmed_ready'] else '🔴'} {r['name']}")
        items += [
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
            discord.ui.TextDisplay("\n".join(ready_lines)),
            discord.ui.ActionRow(
                discord.ui.Button(label="✅ Team ist da", style=discord.ButtonStyle.success, custom_id=f"groupaction:{group_id}:ready"),
            ),
        ]

    container = discord.ui.Container(
        *items,
        accent_color=discord.Color.gold(),
    )
    view.add_item(container)
    return view


def build_group_actions_view(group_id: int) -> discord.ui.LayoutView:
    """Die Spielaktions-Buttons - bleiben im normalen Gruppenkanal, wo Manager schreiben duerfen."""
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(
        discord.ui.TextDisplay(
            "**Spielaktionen**\n"
            "- Gespielt: prüft automatisch bei EA, ob ihr gegen den richtigen Gegner gespielt habt, "
            "und trägt das Ergebnis direkt ein\n"
            "- Ergebnis eintragen: falls die Auto-Erkennung nichts findet, hier manuell eintragen "
            "(der Gegner muss bestätigen)\n"
            "- Größenvideo anfordern: pingt den Gegner-Manager eures aktuellen Matches"
        ),
        discord.ui.ActionRow(
            discord.ui.Button(label="Gespielt", style=discord.ButtonStyle.success, custom_id=f"groupaction:{group_id}:played"),
            discord.ui.Button(label="Ergebnis eintragen", style=discord.ButtonStyle.primary, custom_id=f"groupaction:{group_id}:report"),
            discord.ui.Button(label="Größenvideo anfordern", style=discord.ButtonStyle.secondary, custom_id=f"groupaction:{group_id}:sizevideo"),
        ),
        accent_color=discord.Color.gold(),
    )
    view.add_item(container)
    return view


async def build_group_schedule_matchdays(group_id: int) -> list[list[dict]]:
    """Alle Matches einer Gruppe, nach Spieltag gruppiert, inkl. aktuellem Ergebnis (fuer die Spielplan-Grafik).
    Bei einer ungeraden Team-Anzahl (Freilos) wird pro Spieltag zusaetzlich ein Freilos-Eintrag
    angehaengt (status='bye') - vorher wurde das Freilos beim Spielplan-Erzeugen komplett
    weggelassen und tauchte dadurch nirgends auf (weder Grafik noch Freigabe-Text)."""
    pool = get_pool()
    all_group_matches = await pool.fetch(
        "SELECT * FROM tournament_matches WHERE group_id = $1 ORDER BY round, match_number", group_id
    )
    roster_rows = await pool.fetch("SELECT team_id FROM tournament_group_teams WHERE group_id = $1", group_id)
    roster = {r["team_id"] for r in roster_rows}
    team_rows = {tid: await get_pool_team(tid) for tid in roster}
    max_matchday = max((m["round"] for m in all_group_matches), default=0)
    matchdays_data: list[list[dict]] = [[] for _ in range(max_matchday)]
    playing_per_round: list[set[int]] = [set() for _ in range(max_matchday)]
    for m in all_group_matches:
        if m["team1_id"] is None or m["team2_id"] is None:
            continue
        idx = m["round"] - 1
        t1, t2 = team_rows[m["team1_id"]], team_rows[m["team2_id"]]
        matchdays_data[idx].append({
            "team1_name": t1["name"], "team2_name": t2["name"],
            "team1_logo_url": t1.get("logo_url"), "team2_logo_url": t2.get("logo_url"),
            "team1_score": m["team1_score"], "team2_score": m["team2_score"], "status": m["status"],
        })
        playing_per_round[idx] |= {m["team1_id"], m["team2_id"]}

    for idx, playing in enumerate(playing_per_round):
        for bye_team_id in roster - playing:
            bye_team = team_rows[bye_team_id]
            matchdays_data[idx].append({
                "team1_name": bye_team["name"], "team2_name": None,
                "team1_logo_url": bye_team.get("logo_url"), "team2_logo_url": None,
                "team1_score": None, "team2_score": None, "status": "bye",
            })
    return matchdays_data


async def build_group_schedule_file(group: dict) -> discord.File:
    from graphics import render_schedule_image
    matchdays = await build_group_schedule_matchdays(group["id"])
    sections = [(f"Spieltag {i}", md) for i, md in enumerate(matchdays, start=1)]
    buf = await render_schedule_image(f"Spielplan — Gruppe {group['group_number']}", sections)
    return discord.File(buf, filename="spielplan.png")


async def build_bracket_schedule_matches(tournament_id: int, bracket: str) -> list[tuple[str, list[dict]]]:
    """Alle Matches eines Brackets, nach Runde gruppiert mit deutschem Rundennamen, inkl. Ergebnis
    und Team-IDs (fuer die Baum-Grafik, die Verbindungen anhand echter Teams statt Listenposition
    zieht - wichtig bei gestaffelten Bracket-Groessen, wo Teams erst spaeter einsteigen)."""
    pool = get_pool()
    matches = await pool.fetch(
        """
        SELECT * FROM tournament_matches WHERE tournament_id = $1 AND phase = 'knockout' AND bracket = $2
        ORDER BY round, match_number
        """,
        tournament_id, bracket,
    )
    if not matches:
        return []
    team_ids = {m["team1_id"] for m in matches if m["team1_id"]} | {m["team2_id"] for m in matches if m["team2_id"]}
    team_rows = {tid: await get_pool_team(tid) for tid in team_ids}

    def to_dict(m) -> dict:
        t1 = team_rows.get(m["team1_id"]) or {"name": "Freilos", "logo_url": None}
        t2 = team_rows.get(m["team2_id"]) or {"name": "Freilos", "logo_url": None}
        return {
            "team1_id": m["team1_id"], "team2_id": m["team2_id"],
            "team1_name": t1["name"], "team2_name": t2["name"],
            "team1_logo_url": t1.get("logo_url"), "team2_logo_url": t2.get("logo_url"),
            "team1_score": m["team1_score"], "team2_score": m["team2_score"], "status": m["status"],
        }

    # Erst nach Runde gruppieren, Spiel-um-Platz-3 getrennt halten - Rundennamen werden danach
    # anhand des Abstands zum Finale vergeben (nicht anhand der Match-Anzahl pro Runde: bei
    # gestaffelten Turniergroessen mit spaeter nachrueckenden Teams kann eine fruehe Runde
    # zufaellig genauso viele Matches haben wie eine spaetere, das wuerde sonst falsch beschriftet).
    raw_rounds: list[list[dict]] = []
    third_place: list[dict] = []
    current_round = None
    current_matches: list[dict] = []
    for m in matches:
        if m["is_third_place_match"]:
            third_place.append(to_dict(m))
            continue
        if m["round"] != current_round:
            if current_matches:
                raw_rounds.append(current_matches)
            current_round = m["round"]
            current_matches = []
        current_matches.append(to_dict(m))
    if current_matches:
        raw_rounds.append(current_matches)

    meta = await pool.fetchrow(
        "SELECT direct_entrants FROM tournament_bracket_meta WHERE tournament_id = $1 AND bracket = $2",
        tournament_id, bracket,
    )
    direct_entrants_count = len(meta["direct_entrants"]) if meta and meta["direct_entrants"] else None
    labels = bracket_round_labels([len(r) for r in raw_rounds], direct_entrants_count)
    sections = list(zip(labels, raw_rounds))
    if third_place:
        sections.append(("Spiel um Platz 3", third_place))
    return sections


async def build_bracket_schedule_file(tournament_id: int, bracket: str) -> discord.File | None:
    from graphics import render_bracket_tree_image
    sections = await build_bracket_schedule_matches(tournament_id, bracket)
    if not sections:
        return None
    label = "Winner Bracket" if bracket == "winner" else "Loser Bracket"
    # Spiel um Platz 3 gehoert nicht in den Hauptbaum (spielt zwischen den Halbfinal-Verlierern,
    # nicht dem Finalgewinner) - wuerde die Baum-Verbindungslinien verfaelschen. Wird stattdessen
    # separat unten drangehaengt, damit es trotzdem sichtbar in der Grafik steht.
    tree_sections = [s for s in sections if s[0] != "Spiel um Platz 3"]
    third_place_section = next((s for s in sections if s[0] == "Spiel um Platz 3"), None)
    third_place_match = third_place_section[1][0] if third_place_section and third_place_section[1] else None
    buf = await render_bracket_tree_image(label, tree_sections, third_place=third_place_match)
    return discord.File(buf, filename="bracket.png")


async def apply_staff_overwrites(guild: discord.Guild, overwrites: dict) -> dict:
    """Fuegt allen konfigurierten Cup-Staff-Rollen (Trial Moderator, Moderator, Head Moderator, ...)
    automatisch Sichtbarkeit fuer diesen Kanal hinzu, ohne dass man sie manuell pro Kanal
    ergaenzen muss - gilt fuer alle Cup-Kanaele (Gruppen, Panels, Bracket-Kanaele)."""
    pool = get_pool()
    row = await pool.fetchrow("SELECT cup_staff_role_ids FROM guild_settings WHERE guild_id = $1", guild.id)
    role_ids = row["cup_staff_role_ids"] if row and row["cup_staff_role_ids"] else []
    for rid in role_ids:
        role = guild.get_role(rid)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
    return overwrites


async def create_group_panel_channel(guild: discord.Guild, category: discord.CategoryChannel, group: dict) -> discord.TextChannel:
    """
    Legt einen eigenen 'nur Panel'-Kanal fuer eine Gruppe an (z.B. 'gruppe-1-panel'),
    sichtbar fuer dieselbe Gruppen-Rolle wie der normale Gruppenkanal, aber nur der
    Bot darf dort schreiben - bleibt dadurch dauerhaft sauber. Postet das aktuelle
    Panel (NUR Tabelle) dort und aktualisiert panel_channel_id + panel_message_id in
    der DB. Die Spielaktions-Buttons werden zusaetzlich (erneut) im NORMALEN
    Gruppenkanal gepostet, da Manager dort schreiben/interagieren.
    """
    pool = get_pool()
    role = guild.get_role(group["role_id"])
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False), guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
    if role:
        overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)
    overwrites = await apply_staff_overwrites(guild, overwrites)

    panel_channel = await guild.create_text_channel(
        f"gruppe-{group['group_number']}-panel", category=category, overwrites=overwrites
    )
    panel = await build_group_panel(group["id"])
    msg = await panel_channel.send(view=panel, files=[panel.schedule_file])
    await pool.execute(
        "UPDATE tournament_groups SET panel_channel_id = $1, panel_message_id = $2 WHERE id = $3",
        panel_channel.id, msg.id, group["id"],
    )

    # Aktions-Buttons in BEIDE Kanaele (Hauptkanal + Panel-Kanal) - analog zu Groessenvideo/
    # Ergebnis-Bestaetigung, die ebenfalls in beiden Kanaelen landen.
    for target_channel in {panel_channel, guild.get_channel(group.get("channel_id")) if group.get("channel_id") else None}:
        if target_channel is None:
            continue
        try:
            await target_channel.send(view=build_group_actions_view(group["id"]))
        except discord.HTTPException:
            log.exception(f"Fehler beim Posten der Aktions-Buttons in Kanal {target_channel.id} (Gruppe {group['id']})")

    return panel_channel


async def refresh_group_panel(bot: commands.Bot, group_id: int):
    pool = get_pool()
    group = await pool.fetchrow("SELECT * FROM tournament_groups WHERE id = $1", group_id)
    if not group or not group["panel_message_id"]:
        return
    target_channel_id = group["panel_channel_id"] or group["channel_id"]
    channel = bot.get_channel(target_channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(target_channel_id)
        except discord.HTTPException:
            return
    try:
        msg = await channel.fetch_message(group["panel_message_id"])
    except discord.HTTPException:
        return
    panel = await build_group_panel(group_id)
    await msg.edit(view=panel, attachments=[panel.schedule_file])


async def get_live_schedule_channel(bot: commands.Bot, guild: discord.Guild):
    pool = get_pool()
    row = await pool.fetchrow("SELECT live_schedule_channel_id FROM guild_settings WHERE guild_id = $1", guild.id)
    if not row or not row["live_schedule_channel_id"]:
        return None
    channel = guild.get_channel(row["live_schedule_channel_id"])
    if channel is None:
        try:
            channel = await guild.fetch_channel(row["live_schedule_channel_id"])
        except discord.HTTPException:
            return None
    return channel


async def build_live_schedule_view(tournament_id: int) -> discord.ui.LayoutView:
    t = await get_tournament(tournament_id)
    pool = get_pool()
    medals = ["🥇", "🥈", "🥉"]

    header = f"# 📅 LIVE-SPIELPLAN\n## {t['name']}"

    items: list = [discord.ui.TextDisplay(header)]
    schedule_files: list[discord.File] = []

    standings = await get_group_standings(tournament_id)
    if standings:
        items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large))
        for g in standings:
            team_ids = [s["team_id"] for s in g["standings"]]
            names = await team_name_map(team_ids)
            matches = await pool.fetch(
                "SELECT * FROM tournament_matches WHERE group_id = $1 ORDER BY round, match_number", g["group_id"]
            )
            done = sum(1 for m in matches if m["status"] == "completed")
            total_matches = len(matches)
            filled = int((done / total_matches) * 10) if total_matches else 0
            bar = "🟨" * filled + "⬛" * (10 - filled)

            block = [f"### 🏟️ Gruppe {g['group_number']}", f"{bar} `{done}/{total_matches}`", ""]
            for i, s in enumerate(g["standings"]):
                prefix = medals[i] if i < 3 else f"`{i + 1}.`"
                block.append(
                    f"{prefix} **{names.get(s['team_id'], '?')}** — `{s['points']}` Punkte ({s['wins']}S/{s['draws']}U) · "
                    f"Tore `{s['goals_for']}:{s['goals_against']}` (`{s['goal_diff']:+d}`)"
                )

            # Nur noch eine Kurzfassung der offenen Spiele (naechster Spieltag), nicht mehr
            # ALLE offenen Spiele einzeln auflisten - bei 6er-Gruppen (15 Spiele statt 6 pro
            # Gruppe) sprengte das bei mehreren Gruppen zusammen erneut Discords 4000-Zeichen-
            # Limit fuer Components V2 und liess das Posten des kompletten Live-Spielplans
            # (und damit auch alle nachfolgenden Ergebnis-Verarbeitungen) crashen. Die volle
            # Liste steht ohnehin schon im jeweiligen Gruppenkanal/-Panel.
            open_matches = [m for m in matches if m["status"] != "completed"]
            if open_matches:
                next_round = min(m["round"] for m in open_matches)
                open_next_round = [m for m in open_matches if m["round"] == next_round]
                m_names = await team_name_map(
                    [m["team1_id"] for m in open_next_round] + [m["team2_id"] for m in open_next_round]
                )
                block.append("")
                block.append(f"**Nächster Spieltag ({next_round}):**")
                for m in open_next_round:
                    block.append(f"🔴 {m_names.get(m['team1_id'], '?')} 🆚 {m_names.get(m['team2_id'], '?')}")
                remaining = len(open_matches) - len(open_next_round)
                if remaining > 0:
                    block.append(f"-# + {remaining} weitere offene Spiele in dieser Gruppe")

            # Bewusst KEINE volle "Ergebnisse:"-Liste mehr hier - die wuchs unbegrenzt mit dem
            # Turnierfortschritt (bei vielen Gruppen/Spielen ueberschritt der gesamte Nachrichtentext
            # irgendwann Discords 4000-Zeichen-Limit fuer Components V2 und liess JEDE
            # Ergebnis-Verarbeitung crashen, live beobachtet). Ergebnisse stehen ohnehin schon in
            # der Spielplan-Grafik direkt darunter und in den Gruppenkanaelen/-Panels.
            items.append(discord.ui.TextDisplay("\n".join(block)))

            # Zusaetzlich zur Text-Zusammenfassung auch die Spielplan-Grafik einbetten (gleiche
            # Optik wie im Gruppen-Panel-Kanal) - bisher gab's waehrend der Gruppenphase im
            # Live-Spielplan-Kanal nur Text, keine Grafiken.
            group_row = await pool.fetchrow("SELECT * FROM tournament_groups WHERE id = $1", g["group_id"])
            if group_row:
                schedule_filename = f"group_{g['group_number']}_schedule.png"
                schedule_file = await build_group_schedule_file(dict(group_row))
                schedule_file.filename = schedule_filename
                schedule_files.append(schedule_file)
                items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(media=f"attachment://{schedule_filename}")))

            items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))

    bracket_files: list[discord.File] = list(schedule_files)
    for bracket, label, icon in (("winner", "Winner Bracket", "🏆"), ("loser", "Loser Bracket", "🥊")):
        sections = await build_bracket_schedule_matches(tournament_id, bracket)
        if not sections:
            continue
        tree_sections = [s for s in sections if s[0] != "Spiel um Platz 3"]

        from graphics import render_bracket_tree_image
        buf = await render_bracket_tree_image(label, tree_sections)
        filename = f"bracket_{bracket}.png"
        file = discord.File(buf, filename=filename)
        bracket_files.append(file)

        items.append(discord.ui.TextDisplay(f"### {icon} {label}"))
        items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(media=f"attachment://{filename}")))
        items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))

    now_ts = int(discord.utils.utcnow().timestamp())
    items.append(discord.ui.TextDisplay(f"-# Zuletzt aktualisiert: <t:{now_ts}:R>"))

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_color=discord.Color.gold()))
    view.bracket_files = bracket_files
    return view


async def refresh_live_schedule(bot: commands.Bot, guild: discord.Guild, tournament_id: int):
    channel = await get_live_schedule_channel(bot, guild)
    if not channel:
        return
    pool = get_pool()
    t = await get_tournament(tournament_id)
    view = await build_live_schedule_view(tournament_id)

    files = getattr(view, "bracket_files", [])

    if t.get("live_schedule_message_id"):
        try:
            msg = await channel.fetch_message(t["live_schedule_message_id"])
            if files:
                await msg.edit(view=view, attachments=files)
            else:
                await msg.edit(view=view, attachments=[])
            return
        except discord.HTTPException:
            pass

    msg = await channel.send(view=view, files=files)
    await pool.execute("UPDATE tournaments SET live_schedule_message_id = $1 WHERE id = $2", msg.id, tournament_id)


MIN_BRACKET_SIZE = 8
MATCHDAY_REMINDER_SECONDS = 300  # 5 Minuten


def generate_group_schedule(team_ids: list[int]) -> list[list[tuple[int, int]]]:
    """
    Kreisverfahren (Round-Robin): teilt 'jeder gegen jeden' in echte Spieltage auf.
    Jeder Spieltag: jedes Team spielt maximal einmal. Bei ungerader Teamzahl gibt
    es pro Spieltag ein Freilos (kein Match fuer das Team an diesem Spieltag).
    """
    teams = list(team_ids)
    if len(teams) % 2 == 1:
        teams.append(None)
    n = len(teams)
    if n < 2:
        return []
    num_rounds = n - 1
    half = n // 2
    schedule: list[list[tuple[int, int]]] = []
    arr = teams[:]
    for _ in range(num_rounds):
        pairs = []
        for i in range(half):
            t1, t2 = arr[i], arr[n - 1 - i]
            if t1 is not None and t2 is not None:
                pairs.append((t1, t2))
        schedule.append(pairs)
        arr = [arr[0]] + [arr[-1]] + arr[1:-1]
    return schedule


def _reminder_view(text: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_color=discord.Color.orange()))
    return view


async def send_matchday_reminder(channel: discord.abc.Messageable, matchday: int):
    await asyncio.sleep(MATCHDAY_REMINDER_SECONDS)
    try:
        await channel.send(view=_reminder_view(f"⏰ Die 5 Minuten sind um — **Spieltag {matchday}** muss jetzt laufen."))
    except discord.HTTPException:
        pass


async def send_ko_round_reminder(channel: discord.abc.Messageable, round_label: str):
    await asyncio.sleep(MATCHDAY_REMINDER_SECONDS)
    try:
        await channel.send(view=_reminder_view(f"⏰ Die 5 Minuten sind um — **{round_label}** muss jetzt laufen."))
    except discord.HTTPException:
        pass


def _walk_components(components):
    """Traversiert eine Components-V2-Baumstruktur (Container/ActionRow/etc.) rekursiv und
    liefert jedes einzelne Element (Button, TextDisplay, ...) - Discord.py verschachtelt
    diese in .children, aber die Tiefe variiert je nach Komponente."""
    for comp in components:
        yield comp
        children = getattr(comp, "children", None)
        if children:
            yield from _walk_components(children)


_TRANSIENT_MARKERS = (
    "Größenvideo wurde vom Gegner gefordert",
    "Die 5 Minuten sind um",
    "muss jetzt laufen",
)


async def _purge_transient_action_messages(channel: discord.abc.Messageable | None):
    """Loescht liegen gebliebene Ergebnis-Bestaetigungs-/Groessenvideo-/Zeit-abgelaufen-Karten
    aus einem Kanal - wird vor jeder neuen Spieltag-/Runden-Freigabe aufgerufen, damit sich
    das nicht ueber die ganze Turnierdauer im Kanal ansammelt. Rein optisches Aufraeumen,
    ruehrt keine Datenbank-Daten an - ein noch offenes, unbeantwortetes Bestaetigungs-Match
    bleibt in der DB weiterhin unbestaetigt, nur die Discord-Karte dazu verschwindet."""
    if channel is None:
        return
    try:
        async for msg in channel.history(limit=100):
            if not msg.author.bot or not msg.components:
                continue
            elements = list(_walk_components(msg.components))
            is_transient = any(
                str(getattr(el, "custom_id", "")).startswith("matchconfirm:") for el in elements
            )
            if not is_transient:
                text = " ".join(getattr(el, "content", "") or "" for el in elements)
                is_transient = any(marker in text for marker in _TRANSIENT_MARKERS)
            if is_transient:
                try:
                    await msg.delete()
                except discord.HTTPException:
                    pass
    except discord.HTTPException:
        pass


async def release_ko_round(bot: commands.Bot, channel: discord.abc.Messageable, matches: list[dict], round_label: str):
    """
    Postet die Paarungen einer KO-Runde mit EA-Club-Namen, Manager-Erwaehnungen und
    5-Minuten-Timer - analog zu release_matchday() in der Gruppenphase.
    """
    await _purge_transient_action_messages(channel)
    team_ids = [m["team1_id"] for m in matches] + [m["team2_id"] for m in matches]
    real_team_ids = set(tid for tid in team_ids if tid)
    names = await team_name_map(list(real_team_ids))

    ea_names: dict[int, str] = {}
    manager_mentions: dict[int, str] = {}
    for tid in real_team_ids:
        team_row = await get_pool_team(tid)
        ea_names[tid] = team_row.get("ea_club_name") or names.get(tid, "?")
        managers = await get_team_managers(tid)
        manager_mentions[tid] = " ".join(f"<@{m['discord_id']}>" for m in managers) or ""

    pairing_lines = []
    for m in matches:
        if m["team1_id"] is None or m["team2_id"] is None:
            real_team_id = m["team1_id"] or m["team2_id"]
            pairing_lines.append(
                f"> **{names.get(real_team_id, '?')}** {manager_mentions.get(real_team_id, '')} steht ohne Gegner da — "
                "Freilos, kein Spiel nötig."
            )
        else:
            # Heimteam-Konvention: das zuerst genannte Team (team1) laedt ein - passend zur
            # Regel "Heimteam laedt ein" im Cup-Regelwerk.
            pairing_lines.append(
                f"> 🏠 **{names.get(m['team1_id'], '?')}** {manager_mentions.get(m['team1_id'], '')} lädt ein → "
                f"**{names.get(m['team2_id'], '?')}** {manager_mentions.get(m['team2_id'], '')} "
                f"— EA-Club-Namen: `{ea_names.get(m['team1_id'], '?')}` vs. `{ea_names.get(m['team2_id'], '?')}`"
            )

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(f"# 📢 {round_label} ist freigegeben\nSo wird gespielt:"),
        discord.ui.Separator(),
        discord.ui.TextDisplay("\n".join(pairing_lines)),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "-# 🏠 = Heimteam lädt ein (unter dem oben genannten EA-Club-Namen) - ihr habt **5 Minuten** Zeit."
        ),
        accent_color=discord.Color.gold(),
    ))
    await channel.send(view=view)
    asyncio.create_task(send_ko_round_reminder(channel, round_label))



async def release_matchday(bot: commands.Bot, guild: discord.Guild, group_id: int, matchday: int):
    """Gibt einen Spieltag frei: postet Paarungen im Gruppenkanal, DMt alle Manager, startet 5-Min-Reminder.

    Race-sicher: der Freigabe-"Anspruch" (released_round hochsetzen) passiert atomar GANZ AM
    ANFANG, nicht erst nach dem Posten. Live beobachtet: wenn mehrere Ergebnisse eines
    Spieltags fast gleichzeitig bestaetigt werden (z.B. mehrere EA-Auto-Erkennungen kurz
    hintereinander), rief check_and_release_next_matchday() mehrfach parallel auf, bevor
    einer der Aufrufe released_round tatsaechlich aktualisiert hatte - die "schon freigegeben"-
    Pruefung kam dadurch mehrfach zu spaet, die Freigabe-Nachricht wurde 2-5x gepostet."""
    pool = get_pool()
    claimed = await pool.fetchval(
        "UPDATE tournament_groups SET released_round = $2 WHERE id = $1 AND released_round < $2 RETURNING id",
        group_id, matchday,
    )
    if claimed is None:
        return  # Schon freigegeben oder ein anderer, fast gleichzeitiger Aufruf macht es bereits

    group = await pool.fetchrow("SELECT * FROM tournament_groups WHERE id = $1", group_id)
    if not group:
        return

    matches = await pool.fetch(
        "SELECT * FROM tournament_matches WHERE group_id = $1 AND round = $2 ORDER BY match_number", group_id, matchday
    )
    # Freilos-Team fuer diesen Spieltag ermitteln: wer aus dem Gruppen-Kader an diesem
    # Spieltag in KEINEM Match auftaucht. Es wird bewusst kein eigener Match-Datensatz mit
    # team_id=NULL angelegt (wuerde die Spieltag-Fortschritts-Logik verkomplizieren), daher
    # laesst sich das Freilos nicht aus `matches` selbst ablesen - vorher wurde es dadurch bei
    # der Freigabe komplett uebersehen (kein Hinweis, keine DM).
    roster_rows = await pool.fetch("SELECT team_id FROM tournament_group_teams WHERE group_id = $1", group_id)
    playing_this_round = {tid for m in matches for tid in (m["team1_id"], m["team2_id"]) if tid is not None}
    bye_this_round = {r["team_id"] for r in roster_rows} - playing_this_round

    team_ids = [m["team1_id"] for m in matches] + [m["team2_id"] for m in matches] + list(bye_this_round)
    names = await team_name_map(team_ids)

    ea_names: dict[int, str] = {}
    for tid in set(i for i in team_ids if i):
        team_row = await get_pool_team(tid)
        ea_names[tid] = team_row.get("ea_club_name") or names.get(tid, "?")

    manager_mentions: dict[int, str] = {}
    for tid in set(i for i in team_ids if i):
        managers = await get_team_managers(tid)
        manager_mentions[tid] = " ".join(f"<@{m['discord_id']}>" for m in managers) or ""

    pairing_lines = []
    for m in matches:
        if m["team1_id"] is None or m["team2_id"] is None:
            real_team_id = m["team1_id"] or m["team2_id"]
            pairing_lines.append(
                f"> **{names.get(real_team_id, '?')}** {manager_mentions.get(real_team_id, '')} steht ohne Gegner da — "
                "Freilos, kein Spiel nötig."
            )
        else:
            # Heimteam-Konvention: das zuerst genannte Team (team1) laedt ein - passend zur
            # Regel "Heimteam laedt ein" im Cup-Regelwerk.
            pairing_lines.append(
                f"> 🏠 **{names.get(m['team1_id'], '?')}** {manager_mentions.get(m['team1_id'], '')} lädt ein → "
                f"**{names.get(m['team2_id'], '?')}** {manager_mentions.get(m['team2_id'], '')} "
                f"— EA-Club-Namen: `{ea_names.get(m['team1_id'], '?')}` vs. `{ea_names.get(m['team2_id'], '?')}`"
            )
    for bye_team_id in bye_this_round:
        pairing_lines.append(f"> 💤 **{names.get(bye_team_id, '?')}** hat diesen Spieltag **Freilos** - kein Spiel nötig.")

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(f"# 📢 Spieltag {matchday} ist freigegeben\nSo wird gespielt:"),
        discord.ui.Separator(),
        discord.ui.TextDisplay("\n".join(pairing_lines)),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "-# 🏠 = Heimteam lädt ein (unter dem oben genannten EA-Club-Namen) - ihr habt **5 Minuten** Zeit."
        ),
        accent_color=discord.Color.gold(),
    ))

    channel = guild.get_channel(group["channel_id"])
    if channel is None:
        try:
            channel = await guild.fetch_channel(group["channel_id"])
        except discord.HTTPException:
            channel = None

    panel_channel = guild.get_channel(group["panel_channel_id"]) if group["panel_channel_id"] else None
    if panel_channel is None and group["panel_channel_id"]:
        try:
            panel_channel = await guild.fetch_channel(group["panel_channel_id"])
        except discord.HTTPException:
            panel_channel = None
    await _purge_transient_action_messages(channel)
    await _purge_transient_action_messages(panel_channel)

    if channel:
        await channel.send(view=view)
        if matchday == 1:
            # Aktivitaets-Check ("Team ist da") ist mit der ersten Freigabe erledigt -
            # Panel aktualisieren, damit der Button/Status dort verschwindet.
            await refresh_group_panel(bot, group_id)
        asyncio.create_task(send_matchday_reminder(channel, matchday))

        try:
            schedule_file = await build_group_schedule_file(dict(group))
            schedule_view = discord.ui.LayoutView(timeout=None)
            schedule_view.add_item(discord.ui.Container(
                discord.ui.TextDisplay(f"### 📋 Aktueller Spielplan — Gruppe {group['group_number']}"),
                discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://spielplan.png")),
                accent_color=discord.Color.gold(),
            ))
            await channel.send(view=schedule_view, files=[schedule_file])
        except Exception:
            log.exception(f"Fehler beim Erstellen der Spielplan-Grafik fuer Gruppe {group_id}")

    bye_team_ids = bye_this_round
    home_team_ids = {m["team1_id"] for m in matches if m["team1_id"] is not None and m["team2_id"] is not None}
    involved_team_ids = {tid for tid in team_ids if tid is not None}
    t = await get_tournament(group["tournament_id"])
    for tid in involved_team_ids:
        managers = await get_team_managers(tid)
        for m in managers:
            try:
                user = await bot.fetch_user(m["discord_id"])
                if tid in bye_team_ids:
                    await user.send(
                        f"📢 **Spieltag {matchday}** in Gruppe {group['group_number']} ({t['name']}): "
                        "ihr habt diesen Spieltag **Freilos** - kein Spiel nötig, gilt automatisch als erledigt."
                    )
                elif tid in home_team_ids:
                    await user.send(
                        f"📢 **Spieltag {matchday}** in Gruppe {group['group_number']} ({t['name']}) wurde freigegeben! "
                        "🏠 Ihr seid **Heimteam** - ladet euren Gegner jetzt ins Spiel ein, ihr habt 5 Minuten Zeit."
                    )
                else:
                    await user.send(
                        f"📢 **Spieltag {matchday}** in Gruppe {group['group_number']} ({t['name']}) wurde freigegeben! "
                        "Ihr seid **Auswärtsteam** - wartet auf die Einladung eures Gegners (Heimteam), ihr habt 5 Minuten Zeit."
                    )
            except discord.HTTPException:
                pass


async def check_and_release_next_matchday(bot: commands.Bot, guild: discord.Guild, group_id: int, completed_round: int):
    pool = get_pool()
    remaining = await pool.fetchval(
        "SELECT COUNT(*) FROM tournament_matches WHERE group_id = $1 AND round = $2 AND status != 'completed'",
        group_id, completed_round,
    )
    if remaining > 0:
        return
    next_round = completed_round + 1

    group = await pool.fetchrow("SELECT released_round FROM tournament_groups WHERE id = $1", group_id)
    if group and group["released_round"] >= next_round:
        return  # Naechster Spieltag ist schon freigegeben - keine erneute Freigabe (z.B. bei Korrektur eines alten Ergebnisses)

    exists = await pool.fetchval(
        "SELECT COUNT(*) FROM tournament_matches WHERE group_id = $1 AND round = $2", group_id, next_round
    )
    if exists > 0:
        await release_matchday(bot, guild, group_id, next_round)


async def start_group_phase(bot: commands.Bot, guild: discord.Guild, tournament_id: int, t: dict):
    """
    Teilt die angemeldeten Teams in Gruppen ein (Auslosung), legt pro Gruppe
    eine Rolle + einen Textkanal an (nur fuer Team-Owner/Co-Manager der Gruppe
    sichtbar) und erstellt den Spielplan in echten Spieltagen (Kreisverfahren).
    Gibt Spieltag 1 NICHT automatisch frei - das passiert erst separat ueber
    release_first_matchday(), sobald der Admin bereit ist (z.B. zum offiziellen
    Turnierstart).
    """
    pool = get_pool()
    registered = await get_registered_teams(tournament_id)
    team_ids = [r["id"] for r in registered]

    # custom_bracket_size umgeht die feste Stufenliste (siehe fill_with_bye_and_promote_waitlist) -
    # fuer den Fall, dass ein Admin bewusst "jetzt mit Freilos starten" gewaehlt hat, statt auf
    # eine weitere echte Anmeldung zu warten.
    bracket_size = t.get("custom_bracket_size") or compute_bracket_size(
        len(team_ids), MIN_BRACKET_SIZE, t["max_teams"], t.get("group_size_override")
    )
    random.shuffle(team_ids)
    while len(team_ids) < bracket_size:
        team_ids.append(None)  # Freilos - fehlende Teams bis zur Turnierstufe auffuellen

    group_size = group_size_for(bracket_size, t.get("group_size_override"))
    num_groups = max(1, bracket_size // group_size)
    groups: list[list[int | None]] = [[] for _ in range(num_groups)]
    for i, tid in enumerate(team_ids):
        groups[i % num_groups].append(tid)

    category_overwrites = await apply_staff_overwrites(guild, {})
    category = await guild.create_category(f"{t['name']} Gruppenphase"[:100], overwrites=category_overwrites)
    await pool.execute("UPDATE tournaments SET group_category_id = $1 WHERE id = $2", category.id, tournament_id)
    match_number = 1
    new_group_ids = []

    for idx, group_team_ids in enumerate(groups, start=1):
        real_team_ids = [tid for tid in group_team_ids if tid is not None]
        if not real_team_ids:
            continue

        role = await guild.create_role(name=f"{t['name'][:60]} Gruppe {idx}")
        for tid in real_team_ids:
            managers = await get_team_managers(tid)
            for m in managers:
                member = guild.get_member(m["discord_id"])
                if member is None:
                    try:
                        member = await guild.fetch_member(m["discord_id"])
                    except discord.HTTPException:
                        continue
                try:
                    await member.add_roles(role)
                except discord.HTTPException:
                    pass

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        overwrites = await apply_staff_overwrites(guild, overwrites)
        channel = await guild.create_text_channel(f"gruppe-{idx}", category=category, overwrites=overwrites)

        row = await pool.fetchrow(
            "INSERT INTO tournament_groups (tournament_id, group_number, role_id, channel_id) VALUES ($1, $2, $3, $4) RETURNING id",
            tournament_id, idx, role.id, channel.id,
        )
        group_id = row["id"]
        new_group_ids.append(group_id)

        for tid in real_team_ids:
            await pool.execute(
                "INSERT INTO tournament_group_teams (group_id, team_id) VALUES ($1, $2)", group_id, tid
            )

        names = await team_name_map(real_team_ids)
        bye_note = f" (+ {len(group_team_ids) - len(real_team_ids)} Freilos)" if len(group_team_ids) > len(real_team_ids) else ""
        intro_text = f"# 🎉 Gruppenauslosung — Gruppe {idx}\nTeams: {', '.join(names.values())}{bye_note}\n\n-# Der Spielplan wird in Kürze freigegeben."
        await channel.send(intro_text)

        group_row = {"id": group_id, "group_number": idx, "role_id": role.id, "channel_id": channel.id}
        await create_group_panel_channel(guild, category, group_row)

        schedule = generate_group_schedule(group_team_ids)
        for matchday_idx, pairs in enumerate(schedule, start=1):
            for (team1, team2) in pairs:
                await pool.execute(
                    """
                    INSERT INTO tournament_matches (tournament_id, round, match_number, team1_id, team2_id, status, phase, group_id)
                    VALUES ($1, $2, $3, $4, $5, 'pending', 'group', $6)
                    """,
                    tournament_id, matchday_idx, match_number, team1, team2, group_id,
                )
                match_number += 1

    await pool.execute("UPDATE tournaments SET phase = 'groups', status = 'started' WHERE id = $1", tournament_id)
    await refresh_live_schedule(bot, guild, tournament_id)


async def release_first_matchday(bot: commands.Bot, guild: discord.Guild, tournament_id: int):
    """Gibt Spieltag 1 fuer alle Gruppen dieses Turniers frei. Separat vom Auslosen aufgerufen."""
    pool = get_pool()
    group_ids = await pool.fetch("SELECT id FROM tournament_groups WHERE tournament_id = $1", tournament_id)
    for row in group_ids:
        await release_matchday(bot, guild, row["id"], 1)
    await refresh_live_schedule(bot, guild, tournament_id)


async def _sort_group_standings(pool, group_id: int, standings: list[dict]) -> list[dict]:
    """Sortiert eine Gruppentabelle: 1. Punkte, 2. Torverhaeltnis, 3. geschossene Tore, 4. direkter
    Vergleich (Ergebnis des Spiels zwischen genau den beiden Teams, falls sie sich begegnet sind),
    5. Team-ID als letzter, rein deterministischer Fallback (nur um ueberhaupt eine reproduzierbare
    Reihenfolge zu haben, kein echtes Fairness-Kriterium). Vorher endete die Sortierung bei
    Tordifferenz/Toren - bei komplettem Gleichstand haette dann die zufaellige DB-Ruckgabe-
    Reihenfolge entschieden, WER von zwei gleich guten Drittplatzierten in die KO-Phase kommt."""
    import functools

    h2h_rows = await pool.fetch(
        """
        SELECT team1_id, team2_id, winner_id FROM tournament_matches
        WHERE group_id = $1 AND status = 'completed' AND team1_id IS NOT NULL AND team2_id IS NOT NULL
        """,
        group_id,
    )
    h2h = {}
    for m in h2h_rows:
        h2h[(m["team1_id"], m["team2_id"])] = m["winner_id"]
        h2h[(m["team2_id"], m["team1_id"])] = m["winner_id"]

    def compare(a: dict, b: dict) -> int:
        for key in ("points", "goal_diff", "goals_for"):
            if a[key] != b[key]:
                return -1 if a[key] > b[key] else 1
        pair = (a["team_id"], b["team_id"])
        if pair in h2h:
            winner = h2h[pair]
            if winner == a["team_id"]:
                return -1
            if winner == b["team_id"]:
                return 1
        return -1 if a["team_id"] < b["team_id"] else 1

    standings.sort(key=functools.cmp_to_key(compare))
    return standings


async def get_group_standings(tournament_id: int) -> list[dict]:
    """Gibt pro Gruppe eine Liste mit Team-Namen, Siegen und Torverhaeltnis zurueck, sortiert."""
    pool = get_pool()
    groups = await pool.fetch(
        "SELECT * FROM tournament_groups WHERE tournament_id = $1 ORDER BY group_number", tournament_id
    )
    result = []
    for g in groups:
        team_rows = await pool.fetch(
            "SELECT team_id FROM tournament_group_teams WHERE group_id = $1", g["id"]
        )
        standings = [await _team_group_record(pool, g["id"], tr["team_id"]) for tr in team_rows]
        standings = await _sort_group_standings(pool, g["id"], standings)
        result.append({"group_number": g["group_number"], "group_id": g["id"], "standings": standings})
    return result


async def all_groups_complete(tournament_id: int) -> bool:
    pool = get_pool()
    matches = await pool.fetch(
        "SELECT status FROM tournament_matches WHERE tournament_id = $1 AND phase = 'group'", tournament_id
    )
    if not matches:
        return False
    return all(m["status"] == "completed" for m in matches)


async def build_bracket_panel_view(tournament_id: int, bracket: str) -> discord.ui.LayoutView:
    """Zeigt den aktuellen Stand eines Brackets: laufende Runde, alle Paarungen mit Status/Ergebnis."""
    pool = get_pool()
    label = "🏆 Winner Bracket" if bracket == "winner" else "🥊 Loser Bracket"
    matches = await pool.fetch(
        """
        SELECT * FROM tournament_matches WHERE tournament_id = $1 AND phase = 'knockout' AND bracket = $2
        ORDER BY round, match_number
        """,
        tournament_id, bracket,
    )
    view = discord.ui.LayoutView(timeout=None)
    if not matches:
        view.schedule_file = None
        view.add_item(discord.ui.Container(discord.ui.TextDisplay(f"### {label}\n_Noch keine Paarungen._"), accent_color=discord.Color.gold()))
        return view

    ids = [m["team1_id"] for m in matches] + [m["team2_id"] for m in matches]
    names = await team_name_map(ids)
    block = [f"### {label}", ""]

    # Rundennamen anhand einer FESTEN Gesamtrundenzahl vergeben, nicht anhand dessen, wie viele
    # Runden bisher in der DB angelegt sind - sonst waere die jeweils neueste Runde immer
    # faelschlich "Finale" (siehe bracket_round_labels-Docstring).
    meta = await pool.fetchrow(
        "SELECT direct_entrants FROM tournament_bracket_meta WHERE tournament_id = $1 AND bracket = $2",
        tournament_id, bracket,
    )
    direct_entrants_count = len(meta["direct_entrants"]) if meta and meta["direct_entrants"] else None
    distinct_rounds = sorted({m["round"] for m in matches if not m.get("is_third_place_match")})
    matches_per_round = [
        len([mm for mm in matches if mm["round"] == r and not mm.get("is_third_place_match")])
        for r in distinct_rounds
    ]
    round_labels = dict(zip(distinct_rounds, bracket_round_labels(matches_per_round, direct_entrants_count)))

    current_round = None
    for m in matches:
        if m["round"] != current_round:
            current_round = m["round"]
            normal_matches_in_round = [mm for mm in matches if mm["round"] == current_round and not mm.get("is_third_place_match")]
            third_place_in_round = any(mm.get("is_third_place_match") for mm in matches if mm["round"] == current_round)
            if normal_matches_in_round:
                block.append(f"**{round_labels[current_round]}**")
            if third_place_in_round:
                block.append("**Spiel um Platz 3**")
        t1 = names.get(m["team1_id"], "Freilos") if m["team1_id"] else "Freilos"
        t2 = names.get(m["team2_id"], "Freilos") if m["team2_id"] else "Freilos"
        if m["status"] == "completed" and m["team1_score"] is not None:
            block.append(f"🟢 {t1} `{m['team1_score']}:{m['team2_score']}` {t2}")
        else:
            block.append(f"⏳ {t1} 🆚 {t2}")

    items = [discord.ui.TextDisplay("\n".join(block))]
    schedule_file = await build_bracket_schedule_file(tournament_id, bracket)
    view.schedule_file = schedule_file
    if schedule_file:
        items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://bracket.png")))
    view.add_item(discord.ui.Container(*items, accent_color=discord.Color.gold()))
    return view


async def create_bracket_panel_channel(
    guild: discord.Guild, tournament_id: int, bracket: str, role: discord.Role,
    category: discord.CategoryChannel | None = None,
) -> discord.TextChannel:
    """Legt den 'nur Panel'-Kanal fuer ein Bracket an (z.B. 'winner-bracket-panel'), nur Bot darf dort schreiben."""
    pool = get_pool()
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False), guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)
    overwrites = await apply_staff_overwrites(guild, overwrites)

    panel_channel = await guild.create_text_channel(f"{bracket}-bracket-panel"[:100], category=category, overwrites=overwrites)
    panel = await build_bracket_panel_view(tournament_id, bracket)
    msg = await panel_channel.send(view=panel, files=[panel.schedule_file] if panel.schedule_file else [])
    await pool.execute(
        "UPDATE tournament_bracket_meta SET panel_channel_id = $1, panel_message_id = $2 WHERE tournament_id = $3 AND bracket = $4",
        panel_channel.id, msg.id, tournament_id, bracket,
    )
    # Spielaktionen-Buttons auch im Panel-Kanal, analog zum Gruppen-Panel - manche Manager
    # nutzen nur den Panel-Kanal, nicht den eigentlichen Bracket-Kanal.
    await panel_channel.send(view=build_bracket_actions_view(tournament_id, bracket))
    return panel_channel


async def refresh_bracket_panel(bot: commands.Bot, tournament_id: int, bracket: str):
    pool = get_pool()
    meta = await pool.fetchrow(
        "SELECT * FROM tournament_bracket_meta WHERE tournament_id = $1 AND bracket = $2", tournament_id, bracket
    )
    if not meta or not meta["panel_channel_id"] or not meta["panel_message_id"]:
        return
    channel = bot.get_channel(meta["panel_channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(meta["panel_channel_id"])
        except discord.HTTPException:
            return
    try:
        msg = await channel.fetch_message(meta["panel_message_id"])
    except discord.HTTPException:
        return
    panel = await build_bracket_panel_view(tournament_id, bracket)
    await msg.edit(view=panel, attachments=[panel.schedule_file] if panel.schedule_file else [])


async def create_bracket(
    bot: commands.Bot, guild: discord.Guild, tournament_id: int, t: dict, bracket: str, team_ids: list[int],
    category: discord.CategoryChannel | None = None,
) -> list[dict]:
    """Erstellt Rolle+Kanal fuer ein einzelnes Bracket (winner/loser) und die Runde-1-Paarungen.

    Gegen doppelte Ausfuehrung abgesichert (Postgres Advisory-Lock ueber tournament_id+bracket):
    live beobachtet, dass ein automatischer Trigger (letztes Gruppenspiel bestaetigt) UND ein
    kurz danach folgender manueller 'KO-Phase starten'-Klick parallel liefen - beide kamen am
    existing_meta-Check vorbei, weil der automatische Lauf zu dem Zeitpunkt Rolle/Kanal zwar
    schon anlegte, den bracket_meta-Datenbankeintrag aber erst GANZ AM ENDE schreibt. Ergebnis:
    zwei 'winner-bracket'-Kanaele/-Rollen, UniqueViolation beim Anlegen der Runde-1-Spiele.
    Der Advisory-Lock serialisiert das: der zweite Aufruf wartet, bis der erste fertig ist,
    sieht dann existing_meta und bricht sauber ab, statt ein zweites Mal alles anzulegen."""
    if not team_ids:
        return []
    pool = get_pool()
    lock_key = zlib.crc32(f"bracket:{tournament_id}:{bracket}".encode()) & 0x7FFFFFFF

    conn = await pool.acquire()
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", lock_key)
        try:
            existing_meta = await conn.fetchrow(
                "SELECT * FROM tournament_bracket_meta WHERE tournament_id = $1 AND bracket = $2", tournament_id, bracket
            )
            if existing_meta:
                existing_matches = await conn.fetch(
                    """
                    SELECT * FROM tournament_matches
                    WHERE tournament_id = $1 AND phase = 'knockout' AND bracket = $2 AND round = 1
                    ORDER BY match_number
                    """,
                    tournament_id, bracket,
                )
                return [dict(m) for m in existing_matches]
            return await _create_bracket_locked(bot, guild, tournament_id, t, bracket, team_ids, category)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", lock_key)
    finally:
        await pool.release(conn)


async def _create_bracket_locked(
    bot: commands.Bot, guild: discord.Guild, tournament_id: int, t: dict, bracket: str, team_ids: list[int],
    category: discord.CategoryChannel | None,
) -> list[dict]:
    """Der eigentliche Erstellungs-Code fuer create_bracket() - laeuft nur, nachdem der
    Aufrufer den Advisory-Lock geholt und existing_meta erneut geprueft hat."""
    pool = get_pool()
    # team_ids kommt bereits nach Seed sortiert an (bestes Team zuerst) - siehe
    # start_knockout_phase / _seed_key. NICHT mehr mischen, sonst geht das Seeding verloren.
    team_ids = list(team_ids)

    label = "Winner-Bracket" if bracket == "winner" else "Loser-Bracket"
    try:
        role = await asyncio.wait_for(guild.create_role(name=f"{t['name'][:45]} {label}"), timeout=15)
    except asyncio.TimeoutError:
        log.error(f"Timeout beim Erstellen der Rolle fuer Bracket '{bracket}' (Turnier {tournament_id})")
        raise

    async def assign_roles():
        for tid in team_ids:
            managers = await get_team_managers(tid)
            for m in managers:
                member = guild.get_member(m["discord_id"])
                if member is None:
                    try:
                        member = await guild.fetch_member(m["discord_id"])
                    except discord.HTTPException:
                        continue
                try:
                    await member.add_roles(role)
                except discord.HTTPException:
                    log.exception(f"Konnte Bracket-Rolle nicht an {member} vergeben (Turnier {tournament_id}, Bracket {bracket})")

    # BEWUSST kein wait_for/Timeout mehr drumherum: das brach die Schleife bei vielen Teams
    # (Discord-Ratelimits) live mittendrin ab - alle Teams NACH dem Timeout bekamen die Rolle
    # nie. Laeuft jetzt als Hintergrund-Task zuende, egal wie lange es dauert; blockiert dabei
    # nicht das Anlegen von Kanal/Spielen weiter unten.
    asyncio.create_task(assign_roles())

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    overwrites = await apply_staff_overwrites(guild, overwrites)
    try:
        channel = await asyncio.wait_for(
            guild.create_text_channel(
                "winner-bracket" if bracket == "winner" else "looser-bracket", category=category, overwrites=overwrites
            ), timeout=15
        )
    except asyncio.TimeoutError:
        log.error(f"Timeout beim Erstellen des Kanals fuer Bracket '{bracket}' (Turnier {tournament_id})")
        raise

    await pool.execute(
        "INSERT INTO tournament_bracket_meta (tournament_id, bracket, role_id, channel_id) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (tournament_id, bracket) DO UPDATE SET role_id = $3, channel_id = $4",
        tournament_id, bracket, role.id, channel.id,
    )

    # Bracket-Groesse auf die naechstkleinere Zweierpotenz bringen, ohne Freilose zu
    # verschwenden: nur die ueberzaehligen Teams spielen eine echte Qualifikationsrunde
    # gegeneinander, alle anderen ("direct_entrants") steigen erst danach ein.
    n = len(team_ids)
    lower_pow2 = 1
    while lower_pow2 * 2 <= n:
        lower_pow2 *= 2
    excess = n - lower_pow2

    match_num = 1
    matches = []
    is_prelim = excess > 0

    if is_prelim:
        # Beste Seeds steigen direkt in die Hauptrunde ein, die schlechtesten 2*excess
        # Seeds spielen die Qualifikationsrunde - seed-gepaart (bester vs. schlechtester
        # der Quali-Teilnehmer), damit sich keine zwei starken Teams unnoetig frueh treffen.
        direct_entrants = team_ids[: n - 2 * excess]
        prelim_participants = team_ids[n - 2 * excess:]
        pairs = [
            (prelim_participants[i], prelim_participants[len(prelim_participants) - 1 - i])
            for i in range(len(prelim_participants) // 2)
        ]
        for team1, team2 in pairs:
            row = await pool.fetchrow(
                """
                INSERT INTO tournament_matches (tournament_id, round, match_number, team1_id, team2_id, status, phase, bracket)
                VALUES ($1, 1, $2, $3, $4, 'pending', 'knockout', $5)
                RETURNING id
                """,
                tournament_id, match_num, team1, team2, bracket,
            )
            matches.append({
                "id": row["id"], "match_number": match_num,
                "team1_id": team1, "team2_id": team2, "winner_id": None, "status": "pending",
            })
            match_num += 1
        await pool.execute(
            "UPDATE tournament_bracket_meta SET direct_entrants = $1 WHERE tournament_id = $2 AND bracket = $3",
            direct_entrants, tournament_id, bracket,
        )
        round_label = "Qualifikationsrunde"
    else:
        # Kein Ueberschuss (Teamzahl schon Zweierpotenz) - trotzdem seed-gepaart
        # (bester vs. schlechtester Seed), nicht einfach Reihenfolge-nach-nebeneinander.
        pairs = [(team_ids[i], team_ids[len(team_ids) - 1 - i]) for i in range(len(team_ids) // 2)]
        for team1, team2 in pairs:
            row = await pool.fetchrow(
                """
                INSERT INTO tournament_matches (tournament_id, round, match_number, team1_id, team2_id, status, phase, bracket)
                VALUES ($1, 1, $2, $3, $4, 'pending', 'knockout', $5)
                RETURNING id
                """,
                tournament_id, match_num, team1, team2, bracket,
            )
            matches.append({
                "id": row["id"], "match_number": match_num,
                "team1_id": team1, "team2_id": team2, "winner_id": None, "status": "pending",
            })
            match_num += 1
        round_label = round_name(len(matches))

    names = await team_name_map(team_ids)
    intro_text = f"# {label} - {t['name']}\nTeams: {', '.join(names.values())}"
    if is_prelim:
        intro_text += f"\n\n_Direkt qualifiziert für die nächste Runde: {', '.join(names.get(tid, '?') for tid in direct_entrants)}_"
    await channel.send(intro_text)
    await release_ko_round(bot, channel, matches, round_label)
    await channel.send(view=build_bracket_actions_view(tournament_id, bracket))

    try:
        await create_bracket_panel_channel(guild, tournament_id, bracket, role, category)
    except Exception:
        log.exception(f"Fehler beim Erstellen des Panel-Kanals fuer Bracket '{bracket}' (Turnier {tournament_id})")

    return matches


async def cleanup_tournament_channels(bot: commands.Bot, guild: discord.Guild, tournament_id: int):
    """Loescht alle fuer dieses Turnier automatisch erstellten Kanaele/Rollen (Gruppen + Brackets + Kategorie)."""
    pool = get_pool()

    groups = await pool.fetch("SELECT * FROM tournament_groups WHERE tournament_id = $1", tournament_id)
    for g in groups:
        if g["channel_id"]:
            ch = guild.get_channel(g["channel_id"])
            if ch:
                try:
                    await ch.delete(reason="Turnier beendet")
                except discord.HTTPException:
                    pass
        if g["panel_channel_id"]:
            ch = guild.get_channel(g["panel_channel_id"])
            if ch:
                try:
                    await ch.delete(reason="Turnier beendet")
                except discord.HTTPException:
                    pass
        if g["role_id"]:
            role = guild.get_role(g["role_id"])
            if role:
                try:
                    await role.delete(reason="Turnier beendet")
                except discord.HTTPException:
                    pass

    brackets = await pool.fetch("SELECT * FROM tournament_bracket_meta WHERE tournament_id = $1", tournament_id)
    for b in brackets:
        if b["channel_id"]:
            ch = guild.get_channel(b["channel_id"])
            if ch:
                try:
                    await ch.delete(reason="Turnier beendet")
                except discord.HTTPException:
                    pass
        if b["panel_channel_id"]:
            ch = guild.get_channel(b["panel_channel_id"])
            if ch:
                try:
                    await ch.delete(reason="Turnier beendet")
                except discord.HTTPException:
                    pass
        if b["role_id"]:
            role = guild.get_role(b["role_id"])
            if role:
                try:
                    await role.delete(reason="Turnier beendet")
                except discord.HTTPException:
                    pass

    t = await get_tournament(tournament_id)
    if t and t.get("group_category_id"):
        category = guild.get_channel(t["group_category_id"])
        if category:
            try:
                await category.delete(reason="Turnier beendet")
            except discord.HTTPException:
                pass
    if t and t.get("bracket_category_id"):
        category = guild.get_channel(t["bracket_category_id"])
        if category:
            try:
                await category.delete(reason="Turnier beendet")
            except discord.HTTPException:
                pass


async def reset_knockout_phase(bot: commands.Bot, guild: discord.Guild, tournament_id: int):
    """
    Loescht die komplette KO-Phase (Bracket-Kanaele/-Rollen + alle KO-Matches) und
    setzt das Turnier zurueck auf 'groups', damit die KO-Phase sauber neu erstellt
    werden kann. Ruehrt die Gruppenphase-Daten NICHT an.
    """
    pool = get_pool()

    brackets = await pool.fetch("SELECT * FROM tournament_bracket_meta WHERE tournament_id = $1", tournament_id)
    for b in brackets:
        if b["channel_id"]:
            ch = guild.get_channel(b["channel_id"])
            if ch:
                try:
                    await ch.delete(reason="KO-Phase zurueckgesetzt")
                except discord.HTTPException:
                    pass
        if b["panel_channel_id"]:
            ch = guild.get_channel(b["panel_channel_id"])
            if ch:
                try:
                    await ch.delete(reason="KO-Phase zurueckgesetzt")
                except discord.HTTPException:
                    pass
        if b["role_id"]:
            role = guild.get_role(b["role_id"])
            if role:
                try:
                    await role.delete(reason="KO-Phase zurueckgesetzt")
                except discord.HTTPException:
                    pass

    t = await get_tournament(tournament_id)
    if t and t.get("bracket_category_id"):
        category = guild.get_channel(t["bracket_category_id"])
        if category:
            try:
                await category.delete(reason="KO-Phase zurueckgesetzt")
            except discord.HTTPException:
                pass

    await pool.execute("DELETE FROM tournament_matches WHERE tournament_id = $1 AND phase = 'knockout'", tournament_id)
    await pool.execute("DELETE FROM tournament_bracket_meta WHERE tournament_id = $1", tournament_id)
    await pool.execute(
        "UPDATE tournaments SET phase = 'groups', winner_champion_id = NULL, loser_champion_id = NULL, bracket_category_id = NULL WHERE id = $1",
        tournament_id,
    )


async def start_knockout_phase(bot: commands.Bot, guild: discord.Guild, tournament_id: int, t: dict):
    """
    Ermittelt Top-N (Winner) und die naechsten N (Loser) pro Gruppe und startet beide Brackets.
    Atomar abgesichert: Falls mehrere Match-Ergebnisse quasi gleichzeitig ankommen (z.B. mehrere
    EA-Auto-Erkennungen kurz hintereinander), koennten mehrere Aufrufe gleichzeitig denken "Gruppenphase
    fertig, ich starte die KO-Phase" - der UPDATE mit WHERE phase='groups' laesst aber garantiert nur
    EINEN davon durch, alle anderen brechen sofort ab.
    """
    pool = get_pool()
    claimed = await pool.fetchval(
        "UPDATE tournaments SET phase = 'knockout' WHERE id = $1 AND phase = 'groups' RETURNING id",
        tournament_id,
    )
    if claimed is None:
        return  # Ein anderer Aufruf hat die KO-Phase bereits gestartet

    withdrawn_rows = await pool.fetch(
        "SELECT team_id FROM tournament_signups WHERE tournament_id = $1 AND status = 'withdrawn'", tournament_id
    )
    withdrawn_team_ids = {r["team_id"] for r in withdrawn_rows}

    standings = await get_group_standings(tournament_id)

    # Alle qualifizierten Teams gruppenuebergreifend in EINE Rangliste: tier = Platzierung
    # INNERHALB der eigenen Gruppe (0=Erster, 1=Zweiter, ...), bei Gleichstand Siege/
    # Tordifferenz/Tore. Alle Gruppenersten stehen so vor allen Gruppenzweiten usw. - der
    # eigene Gruppensieg garantiert IMMER einen Platz weit vorne. Winner-/Loser-Bracket
    # werden NICHT mehr strikt 50/50 pro Gruppe aufgeteilt (frueher: fixe Top-Haelfte pro
    # Gruppe), sondern anhand dieser Gesamtrangliste in zwei 2er-Potenz-grosse Bloecke
    # geschnitten (siehe _split_bracket_sizes) - dadurch sind viel mehr Turniergroessen
    # moeglich, ohne dass je eine Qualifikationsrunde noetig wird.
    all_seeds: list[dict] = []
    for g in standings:
        eligible = [s for s in g["standings"] if s["team_id"] not in withdrawn_team_ids]
        for tier, s in enumerate(eligible):
            all_seeds.append({**s, "tier": tier})

    def _seed_key(s):
        # Teams aus VERSCHIEDENEN Gruppen koennen sich nicht direkt begegnet sein - direkter
        # Vergleich faellt hier also flach, team_id ist als letzter Fallback wenigstens
        # deterministisch (reproduzierbar) statt von der zufaelligen DB-Reihenfolge abzuhaengen.
        return (s["tier"], -s["points"], -s["goal_diff"], -s["goals_for"], s["team_id"])

    all_seeds.sort(key=_seed_key)

    if t.get("single_bracket_mode"):
        per_group = t.get("single_bracket_advance_per_group")
        if per_group:
            # Feste Anzahl PRO GRUPPE (z.B. "nur 1. und 2.") statt der generischen
            # Zweierpotenz-Haelfte - fuer Faelle wie viele ausgefallene Teams/Freilose,
            # wo die Gruppen unterschiedlich gross sind und trotzdem ein klarer, fairer
            # Schnitt pro Gruppe gewollt ist. create_bracket() kommt auch mit einer
            # Nicht-Zweierpotenz-Teamzahl klar (Qualifikationsrunde fuer den Ueberschuss).
            winner_teams_seeds = [s for s in all_seeds if s["tier"] < per_group]
            winner_size = len(winner_teams_seeds)
        else:
            # Nur ein einziges KO-Bracket (kein Loser-Bracket) - die Top-N (groesste 2er-Potenz
            # <= HALBE Gesamtzahl, analog zur "oberen Haelfte" beim normalen Winner-Bracket-Split)
            # ziehen in eine normale Einzel-KO-Phase ein, der Rest ist nach der Gruppenphase fertig
            # (Endplatzierung anhand der Gruppentabelle). Wichtig: <= total//2, NICHT <= total -
            # sonst wuerden bei z.B. 36 Teams satte 32 davon durchgewunken statt nur die besten
            # Haelfte gefiltert (Gruppenphase haette dann kaum noch eine Aussiebe-Wirkung).
            winner_size = 1
            while winner_size * 2 <= len(all_seeds) // 2:
                winner_size *= 2
        loser_size = 0
    else:
        winner_size, loser_size = _split_bracket_sizes(len(all_seeds))
    winner_teams = [s["team_id"] for s in all_seeds[:winner_size]]
    loser_teams = [s["team_id"] for s in all_seeds[winner_size:winner_size + loser_size]]

    category_overwrites = await apply_staff_overwrites(guild, {})
    category = await guild.create_category(f"{t['name']} KO-Phase"[:100], overwrites=category_overwrites)
    try:
        await category.edit(position=len(guild.categories) + 10)  # ganz nach unten
    except discord.HTTPException:
        pass
    await pool.execute("UPDATE tournaments SET bracket_category_id = $1 WHERE id = $2", category.id, tournament_id)

    await create_bracket(bot, guild, tournament_id, t, "winner", winner_teams, category)
    if loser_teams:
        await create_bracket(bot, guild, tournament_id, t, "loser", loser_teams, category)


async def team_name_map(team_ids: list[int]) -> dict[int, str]:
    ids = [i for i in team_ids if i is not None]
    if not ids:
        return {}
    pool = get_pool()
    rows = await pool.fetch("SELECT id, name FROM teams WHERE id = ANY($1::int[])", ids)
    return {r["id"]: r["name"] for r in rows}


def round_name(num_matches: int) -> str:
    """Gibt den deutschen Turnier-Rundennamen anhand der Anzahl Matches in dieser Runde zurueck."""
    mapping = {
        1: "Finale",
        2: "Halbfinale",
        4: "Viertelfinale",
        8: "Achtelfinale",
        16: "Sechzehntelfinale",
        32: "Zweiunddreißigstelfinale",
    }
    return mapping.get(num_matches, f"Runde ({num_matches * 2} Teams)")


def bracket_round_labels(matches_per_round: list[int], direct_entrants_count: int | None) -> list[str]:
    """Rundennamen fuer ALLE bisher in der DB angelegten Runden eines Brackets, anhand einer
    FESTEN Gesamtrundenzahl (aus der Runde-1-Groesse + evtl. Qualifikationsrunde) - nicht anhand
    dessen, wie viele Runden bisher angelegt wurden. Eine neue Runde wird erst erzeugt, wenn die
    vorherige komplett abgeschlossen ist - wuerde man die Anzahl bisher bekannter Runden als
    Gesamtzahl nehmen, waere die jeweils neueste Runde immer faelschlich "Finale".
    direct_entrants_count ist None, wenn dieses Bracket keine Qualifikationsrunde hat."""
    if not matches_per_round:
        return []
    round1_count = matches_per_round[0]
    has_prelim = direct_entrants_count is not None
    if has_prelim:
        # Nach der Quali-Runde (round1_count Sieger) + direct_entrants ist die Teamzahl eine
        # saubere 2er-Potenz - daraus ergibt sich die feste Anzahl "echter" Runden danach.
        normal_rounds_count = round(math.log2(direct_entrants_count + round1_count))
    else:
        normal_rounds_count = round(math.log2(round1_count * 2))

    labels = []
    for i, count in enumerate(matches_per_round):
        if i == 0 and has_prelim:
            labels.append("Qualifikationsrunde")
        else:
            pos_from_start = (i - 1) if has_prelim else i
            offset = normal_rounds_count - 1 - pos_from_start
            labels.append(round_name(2 ** max(offset, 0)))
    return labels


def format_bracket_text(matches: list[dict], names: dict[int, str], round_num: int) -> str:
    lines = [f"## {round_name(len(matches))}"]
    for m in matches:
        t1 = names.get(m["team1_id"], "Freilos") if m["team1_id"] else "Freilos"
        t2 = names.get(m["team2_id"], "Freilos") if m["team2_id"] else "Freilos"
        if m["status"] == "completed" and m["winner_id"]:
            winner_name = names.get(m["winner_id"], "?")
            lines.append(f"**Match {m['match_number']}:** {t1} vs {t2} -> **{winner_name}** (Freilos)")
        else:
            lines.append(f"**Match {m['match_number']}:** {t1} vs {t2}")
    return "\n".join(lines)


def _discord_ts(dt: datetime, style: str = "t") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"


def estimate_schedule(t: dict, registered_count: int) -> dict:
    """Berechnet einen groben Zeitplan basierend auf Turnierstart + Spielrhythmus. Nur eine Schaetzung."""
    start = t.get("start_time")
    if not start:
        return {}
    start = start.astimezone(BERLIN_TZ)  # Postgres liefert TIMESTAMPTZ als UTC zurueck - zurueck auf Berlin-Zeit umrechnen
    rhythmus = t.get("minutes_per_round") or 20

    anmeldeschluss = start - timedelta(hours=2)
    checkin_start = anmeldeschluss
    checkin_end = anmeldeschluss + timedelta(minutes=30)
    gruppenauslosung = start - timedelta(minutes=30)

    effective_count = max(registered_count, t["min_teams"])
    bracket_size = compute_bracket_size(effective_count, MIN_BRACKET_SIZE, t["max_teams"], t.get("group_size_override"))
    group_size = group_size_for(bracket_size, t.get("group_size_override"))
    num_groups = max(1, bracket_size // group_size)
    teams_per_group = max(2, bracket_size // num_groups)
    matchdays = teams_per_group - 1 if teams_per_group % 2 == 0 else teams_per_group
    group_end = start + timedelta(minutes=matchdays * rhythmus + 15)

    winner_n = teams_per_group // 2
    ko_count = max(2, num_groups * winner_n)
    ko_rounds = max(1, math.ceil(math.log2(ko_count)))
    ko_end = group_end + timedelta(minutes=15 + ko_rounds * rhythmus + 10)

    return {
        "anmeldeschluss": anmeldeschluss,
        "checkin_start": checkin_start,
        "checkin_end": checkin_end,
        "gruppenauslosung": gruppenauslosung,
        "turnierstart": start,
        "bracket_size": bracket_size,
        "matchdays": matchdays,
        "group_end": group_end,
        "ko_rounds": ko_rounds,
        "ko_end": ko_end,
    }


class TournamentPanel(discord.ui.LayoutView):
    def __init__(self, t: dict, registered_teams: list[dict], waitlist_teams: list[dict]):
        super().__init__(timeout=None)
        registered = len(registered_teams)
        total_signups = registered + len(waitlist_teams)
        schedule = estimate_schedule(t, total_signups)
        rhythmus = t.get("minutes_per_round") or 20
        bracket_size = schedule.get("bracket_size", t["min_teams"])
        group_size = group_size_for(bracket_size, t.get("group_size_override"))
        num_groups = max(1, bracket_size // group_size)
        bracket_mode_text = "Nur Winner Bracket" if t.get("single_bracket_mode") else "Winner + Loser Bracket"

        # Block: Kopf - Name, Größe, Eckdaten
        header_lines = [f"# 🏆 {t['name']}", f"`{bracket_size} Teams` · {num_groups} Gruppen à {group_size} Teams · {bracket_mode_text}"]
        header_lines.append("")
        header_lines.append(f"📅 **Start:** {fmt_date_de(schedule['turnierstart']) if schedule else '_noch nicht festgelegt_'}")
        header_lines.append(f"⏱️ **Spielrhythmus:** {rhythmus} Minuten pro Runde")
        if t.get("stream_link"):
            header_lines.append(f"🔴 **Stream:** {t['stream_link']}")
        header_block = discord.ui.TextDisplay("\n".join(header_lines))

        # Block: Zeitplan
        schedule_block = None
        if schedule:
            schedule_lines = [
                "### 🗓️ Zeitplan",
                f"> **Anmeldeschluss:** {fmt_time_de(schedule['anmeldeschluss'])} ({fmt_relative_days_de(schedule['anmeldeschluss'])})",
                f"> **Check-in:** {fmt_time_de(schedule['checkin_start'])} – {fmt_time_de(schedule['checkin_end'])}",
                f"> **Gruppenauslosung:** {fmt_time_de(schedule['gruppenauslosung'])}",
                f"> **Anpfiff:** {fmt_time_de(schedule['turnierstart'])}",
                "",
                f"**Gruppenphase** _(geschätzt {schedule['matchdays']} Spieltage à {rhythmus} Min, Ende ca. {fmt_time_de(schedule['group_end'])})_",
                f"**KO-Phase** _(geschätzt {schedule['ko_rounds']} Runden{', Winner + Loser parallel' if not t.get('single_bracket_mode') else ' - nur Winner Bracket'})_",
                "",
                f"🏁 **Voraussichtliches Ende:** {fmt_time_de(schedule['ko_end'])}",
                f"-# Schätzung für {bracket_size}er Turnier — kann sich noch verschieben",
            ]
            schedule_block = discord.ui.TextDisplay("\n".join(schedule_lines))

        # Block: Mannschaftsliste + Warteliste
        # Der Aktivitaets-Check ("Team ist da") gehoert bewusst NUR in die Gruppen-Panels
        # (nach der Gruppenauslosung), nicht schon hier im Anmelde-Panel - zwei parallele
        # "Team ist da"-Checks an unterschiedlichen Stellen verwirrten Teams nur.
        paid_team_ids = t.get("_paid_team_ids", set()) if t.get("is_donation_tournament") else set()

        team_lines = ["### 📋 Gemeldete Teams"]
        if t.get("is_donation_tournament"):
            team_lines.append(f"💰 **Bezahlt:** {len(paid_team_ids)}/{registered} Teams")
        team_lines.append("")
        for i in range(1, bracket_size + 1):
            if i <= registered:
                team = registered_teams[i - 1]
                paid_mark = " 💰" if team["id"] in paid_team_ids else ""
                team_lines.append(f"`{i}.` **{team['name']}** (<@{team['owner_discord_id']}>){paid_mark}")
            else:
                team_lines.append(f"`{i}.` –")

        if waitlist_teams:
            team_lines += ["", f"**⏳ Warteliste ({len(waitlist_teams)}):**"]
            for i, team in enumerate(waitlist_teams, start=1):
                team_lines.append(f"`{i}.` {team['name']} (<@{team['owner_discord_id']}>)")
        team_block = discord.ui.TextDisplay("\n".join(team_lines))

        # Block: konkrete Wachstumsstufen (VOR der Teamliste, damit klar ist, warum sich die
        # Groesse noch aendern kann, bevor man die aktuelle Teamliste anschaut - Buttons landen
        # dadurch automatisch weiter unten in der Nachricht, nicht gleich am Anfang).
        progression_text = bracket_size_progression_text(t["min_teams"], t["max_teams"], total_signups, t.get("group_size_override"))
        progression_block = discord.ui.TextDisplay(progression_text) if progression_text else None

        closed = t["status"] != "open"
        items = []
        if os.path.exists(TOURNAMENT_BANNER_PATH):
            self.banner_file = discord.File(TOURNAMENT_BANNER_PATH, filename="tournament_banner.jpg")
            items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://tournament_banner.jpg")))
        items.append(header_block)
        if schedule_block:
            items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large))
            items.append(schedule_block)
        if progression_block:
            items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large))
            items.append(progression_block)
        items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large))
        items.append(team_block)
        items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large))

        items.append(
            discord.ui.ActionRow(
                discord.ui.Button(
                    label="Anmelden", style=discord.ButtonStyle.success,
                    custom_id=f"tourney:{t['id']}:register", disabled=closed,
                ),
                discord.ui.Button(
                    label="Abmelden", style=discord.ButtonStyle.danger,
                    custom_id=f"tourney:{t['id']}:unregister", disabled=closed,
                ),
                discord.ui.Button(
                    label="Stream-Link ändern", style=discord.ButtonStyle.secondary,
                    custom_id=f"tourney:{t['id']}:streamlink",
                ),
            )
        )
        items.append(
            discord.ui.ActionRow(
                discord.ui.Button(
                    label="🌐 Auf der Website ansehen", style=discord.ButtonStyle.link,
                    url=f"{WEBSITE_URL}/turniere/{t['id']}",
                ),
            )
        )
        container = discord.ui.Container(*items, accent_color=discord.Color.gold())
        self.add_item(container)


async def get_paid_team_ids(tournament_id: int) -> set[int]:
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT team_id FROM tickets WHERE tournament_id = $1 AND payment_status = 'confirmed'", tournament_id
    )
    return {r["team_id"] for r in rows}


async def build_tournament_panel(t: dict) -> TournamentPanel:
    registered_teams = await get_registered_teams(t["id"])
    waitlist_teams = await get_waitlisted_teams(t["id"])
    if t.get("is_donation_tournament"):
        t = dict(t)
        t["_paid_team_ids"] = await get_paid_team_ids(t["id"])
    return TournamentPanel(t, registered_teams, waitlist_teams)


async def refresh_panel(bot: commands.Bot, tournament_id: int):
    t = await get_tournament(tournament_id)
    if not t or not t["channel_id"] or not t["message_id"]:
        return
    channel = bot.get_channel(t["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(t["channel_id"])
        except discord.HTTPException:
            return
    try:
        msg = await channel.fetch_message(t["message_id"])
    except discord.HTTPException:
        return
    panel = await build_tournament_panel(t)
    if hasattr(panel, "banner_file"):
        await msg.edit(view=panel, attachments=[panel.banner_file])
    else:
        await msg.edit(view=panel)


async def close_tournament_signup(bot: commands.Bot, tournament_id: int) -> None:
    """Schliesst die Anmeldung (manuell per Admin-Button oder automatisch 2h vor Start) und
    informiert alle fest angemeldeten Teams per DM. Gemeinsame Logik, damit beide Wege
    (Button in admin_panel.py, automatischer Task hier unten) nicht auseinanderlaufen."""
    pool = get_pool()
    await pool.execute("UPDATE tournaments SET status = 'closed' WHERE id = $1", tournament_id)
    await refresh_panel(bot, tournament_id)

    t = await get_tournament(tournament_id)
    registered = await get_registered_teams(tournament_id)
    for team in registered:
        for m in await get_team_managers(team["id"]):
            try:
                user = await bot.fetch_user(m["discord_id"])
                await user.send(
                    embed=warning_embed(
                        f"Anmeldung für {t['name']} geschlossen!",
                        f"**{team['name']}** ist jetzt fest angemeldet. Sobald die Gruppen ausgelost sind, "
                        "meldet euch dort im Gruppen-Panel über den Button 'Team ist da' als bereit.",
                    )
                )
            except discord.HTTPException:
                pass


async def handle_external_signup_change(bot: commands.Bot, tournament_id: int, team_id: int):
    """Reagiert auf eine An-/Abmeldung, die ueber die Website (statt Discord) passiert ist -
    aktualisiert das Discord-Panel und legt bei Spendenturnieren bei Bedarf den
    Zahlungs-Ticket-Kanal an (dieselben Funktionen wie beim Discord-Signup-Button,
    kein doppelter Code fuer Discord-spezifische Nebenwirkungen)."""
    await reconcile_signups(tournament_id)
    await refresh_panel(bot, tournament_id)

    t = await get_tournament(tournament_id)
    signup = await get_team_signup(tournament_id, team_id)
    if not t or not signup or signup["status"] != "registered" or not t.get("is_donation_tournament"):
        return

    pool = get_pool()
    already_has_ticket = await pool.fetchval(
        "SELECT 1 FROM tickets WHERE tournament_id = $1 AND team_id = $2", tournament_id, team_id
    )
    if already_has_ticket:
        return

    guild = bot.get_guild(t["guild_id"])
    if guild is None:
        try:
            guild = await bot.fetch_guild(t["guild_id"])
        except discord.HTTPException:
            return

    team_row = await pool.fetchrow("SELECT * FROM teams WHERE id = $1", team_id)
    if not team_row:
        return
    team_row = dict(team_row)

    from cogs.tickets import create_payment_ticket
    await create_payment_ticket(bot, guild, t, team_row)


# ---------- Modal: Turnier erstellen ----------

class TournamentCreateModal(discord.ui.Modal, title="Turnier erstellen"):
    name = discord.ui.TextInput(label="Turniername", max_length=80)
    min_teams = discord.ui.TextInput(label="Mindestanzahl Teams (Empfehlung: 8)", default="8", max_length=3)
    max_teams = discord.ui.TextInput(label="Maximale Teams (z.B. 32)", default="32", max_length=3)
    datum = discord.ui.TextInput(label="Turnierstart (TT.MM.JJJJ HH:MM)", placeholder="18.08.2026 20:15", max_length=20)
    spielrhythmus = discord.ui.TextInput(label="Spielrhythmus (Minuten pro Runde)", default="20", max_length=3)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            min_t = int(self.min_teams.value)
            max_t = int(self.max_teams.value)
        except ValueError:
            await interaction.followup.send(view=error_embed("Min./Max. Teams müssen Zahlen sein."), ephemeral=True)
            return

        if min_t < 2 or max_t < min_t:
            await interaction.followup.send(view=error_embed("Ungültige Werte", "Min. muss >= 2 sein und Max. >= Min."), ephemeral=True)
            return

        try:
            rhythmus = int(self.spielrhythmus.value)
        except ValueError:
            await interaction.followup.send(view=error_embed("Spielrhythmus muss eine Zahl (Minuten) sein."), ephemeral=True)
            return

        try:
            naive_dt = datetime.strptime(self.datum.value.strip(), "%d.%m.%Y %H:%M")
            start_time = naive_dt.replace(tzinfo=BERLIN_TZ)
        except ValueError:
            await interaction.followup.send(
                view=error_embed("Ungültiges Datum", "Format muss sein: `TT.MM.JJJJ HH:MM`, z.B. `18.08.2026 20:15`"), ephemeral=True
            )
            return

        pool = get_pool()
        row = await pool.fetchrow(
            """
            INSERT INTO tournaments (guild_id, name, min_teams, max_teams, created_by, start_time, minutes_per_round)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id
            """,
            interaction.guild_id, self.name.value, min_t, max_t, interaction.user.id, start_time, rhythmus,
        )
        tournament_id = row["id"]
        t = await get_tournament(tournament_id)

        from audit import log_action
        await log_action(interaction.guild_id, interaction.user, "tournament.created", "tournament", tournament_id, self.name.value)

        try:
            from cogs.calendar import create_event_for_tournament, refresh_calendar
            await create_event_for_tournament(interaction.guild_id, tournament_id, self.name.value, start_time, interaction.user.id)
            await refresh_calendar(interaction.client, interaction.guild)
        except Exception:
            log.exception(f"Fehler beim automatischen Anlegen des Kalender-Eintrags fuer Turnier {tournament_id}")

        await interaction.followup.send(
            view=success_embed(f"Turnier {self.name.value} erstellt", f"ID `{tournament_id}`"),
            ephemeral=True,
        )
        await interaction.followup.send(
            "Wähle jetzt den Kanal für das Anmelde-Panel:",
            view=ChannelPickerView(tournament_id, t),
            ephemeral=True,
        )
        from cogs.admin_panel import TournamentFormatView
        await interaction.followup.send(
            "Optional: Turnier-Format anpassen (Standard: 4er-Gruppen, Winner + Loser Bracket).",
            view=TournamentFormatView(tournament_id, t),
            ephemeral=True,
        )


class ChannelPickerView(discord.ui.View):
    def __init__(self, tournament_id: int, t: dict):
        super().__init__(timeout=120)
        self.tournament_id = tournament_id
        self.t = t
        select = discord.ui.ChannelSelect(
            placeholder="Kanal für das Anmelde-Panel wählen...",
            channel_types=[discord.ChannelType.text],
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        channel_id = int(interaction.data["values"][0])
        channel = interaction.guild.get_channel(channel_id)
        if channel is None:
            await interaction.response.send_message(view=error_embed("Kanal nicht gefunden."), ephemeral=True)
            return

        pool = get_pool()
        panel = await build_tournament_panel(self.t)
        if hasattr(panel, "banner_file"):
            msg = await channel.send(view=panel, files=[panel.banner_file])
        else:
            msg = await channel.send(view=panel)
        await pool.execute(
            "UPDATE tournaments SET channel_id = $1, message_id = $2 WHERE id = $3",
            channel.id, msg.id, self.tournament_id,
        )
        await interaction.response.edit_message(content=f"✅ Anmelde-Panel gepostet in {channel.mention}.", view=None)
        await notify_teams_new_tournament(interaction.client, interaction.guild_id, self.t["name"], channel)


async def notify_teams_new_tournament(bot: commands.Bot, guild_id: int, tournament_name: str, channel: discord.abc.GuildChannel):
    """Schickt allen Team-Managern mit aktivierten Benachrichtigungen eine DM ueber das neue Turnier."""
    pool = get_pool()
    teams = await pool.fetch(
        "SELECT id, name FROM teams WHERE guild_id = $1 AND notifications_enabled = true", guild_id
    )
    for team in teams:
        managers = await get_team_managers(team["id"])
        for m in managers:
            try:
                user = await bot.fetch_user(m["discord_id"])
                view = discord.ui.LayoutView(timeout=None)
                view.add_item(discord.ui.Container(
                    discord.ui.TextDisplay(
                        f"# 📢 Neues Turnier: {tournament_name}\n"
                        f"Meld dein Team **{team['name']}** jetzt an, bevor die Plätze weg sind:\n"
                        f"{channel.mention}"
                    ),
                    accent_color=discord.Color.gold(),
                ))
                await user.send(view=view)
            except discord.HTTPException:
                pass


# ---------- Cog ----------

class TournamentStreamLinkModal(discord.ui.Modal, title="Stream-Link ändern"):
    stream_link = discord.ui.TextInput(
        label="Stream-Link (Twitch/YouTube)", required=False, max_length=300,
        placeholder="https://... (leer lassen zum Entfernen)",
    )

    def __init__(self, tournament_id: int, current: str | None):
        super().__init__()
        self.tournament_id = tournament_id
        self.stream_link.default = current or ""

    async def on_submit(self, interaction: discord.Interaction):
        if self.stream_link.value and not is_valid_twitch_link(self.stream_link.value):
            await interaction.response.send_message(
                view=error_embed(
                    "Das ist kein gültiger Twitch-Link.",
                    "Format: `https://twitch.tv/name` oder `https://www.twitch.tv/name`",
                ),
                ephemeral=True,
            )
            return
        pool = get_pool()
        await pool.execute("UPDATE tournaments SET stream_link = $1 WHERE id = $2", self.stream_link.value or None, self.tournament_id)
        await refresh_panel(interaction.client, self.tournament_id)
        if self.stream_link.value:
            await interaction.response.send_message(view=success_embed("Stream-Link aktualisiert."), ephemeral=True)
        else:
            await interaction.response.send_message(view=info_embed("Stream-Link entfernt."), ephemeral=True)


SIGNUP_AUTO_CLOSE_BEFORE_START = timedelta(hours=2)


class TournamentCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self._auto_close_signup_task.start()

    def cog_unload(self):
        self._auto_close_signup_task.cancel()

    @tasks.loop(minutes=5)
    async def _auto_close_signup_task(self):
        """Schliesst automatisch 2h vor Turnierstart die Anmeldung (fuer jedes Turnier,
        nicht nur manuell auf Knopfdruck). Admins koennen danach jederzeit ueber den
        Panel-Button wieder oeffnen/schliessen, Teams tauschen oder mit Freilos auffuellen -
        signup_auto_closed sorgt nur dafuer, dass der Task ein manuelles Wieder-Oeffnen
        nicht sofort wieder zumacht."""
        pool = get_pool()
        due = await pool.fetch(
            """
            SELECT id FROM tournaments
            WHERE status = 'open' AND NOT signup_auto_closed
              AND start_time IS NOT NULL AND start_time <= now() + $1
            """,
            SIGNUP_AUTO_CLOSE_BEFORE_START,
        )
        for row in due:
            await pool.execute("UPDATE tournaments SET signup_auto_closed = true WHERE id = $1", row["id"])
            try:
                await close_tournament_signup(self.bot, row["id"])
            except Exception:
                log.exception(f"Fehler beim automatischen Schliessen der Anmeldung fuer Turnier {row['id']}")

    @_auto_close_signup_task.before_loop
    async def _before_auto_close_signup_task(self):
        await self.bot.wait_until_ready()

    async def handle_group_action(self, interaction: discord.Interaction, custom_id: str):
        _, gid_str, action = custom_id.split(":", 2)
        group_id = int(gid_str)
        pool = get_pool()
        group = await pool.fetchrow("SELECT * FROM tournament_groups WHERE id = $1", group_id)
        if not group:
            await interaction.response.send_message(view=error_embed("Gruppe nicht gefunden."), ephemeral=True)
            return

        team = await get_team_for_user_in_group(group_id, interaction.user.id)
        is_admin = await is_tournament_moderator(interaction.user)

        if action == "played":
            if not team:
                await interaction.response.send_message(view=error_embed("Du hast kein Team."), ephemeral=True)
                return
            open_matches = await get_open_matches_for_team(group_id, team["id"])
            if not open_matches:
                await interaction.response.send_message(view=warning_embed("Du hast gerade kein offenes Spiel in dieser Gruppe."), ephemeral=True)
                return

            match = open_matches[0]
            opponent_team_id = match["team2_id"] if match["team1_id"] == team["id"] else match["team1_id"]
            team_row = await get_pool_team(team["id"])
            opponent_row = await get_pool_team(opponent_team_id)

            await interaction.response.defer(ephemeral=True, thinking=True)
            ea_result = await try_fetch_ea_result(team_row, opponent_row)
            if not ea_result:
                await interaction.followup.send(
                    view=warning_embed(
                        "Kein passendes EA-Match gefunden",
                        "Bitte trage das Ergebnis über 'Ergebnis eintragen' manuell ein.",
                    ),
                    ephemeral=True,
                )
                return

            s1, s2 = ea_result
            if team["id"] != match["team1_id"]:
                s1, s2 = s2, s1
            await finalize_match_result(self.bot, interaction.guild, match["id"], s1, s2)
            names = await team_name_map([match["team1_id"], match["team2_id"]])
            await interaction.followup.send(
                view=success_embed(
                    "EA-Match gefunden, Ergebnis automatisch übernommen",
                    f"**{names.get(match['team1_id'])} {s1}:{s2} {names.get(match['team2_id'])}**",
                )
            )

        elif action == "ready":
            if not team:
                await interaction.response.send_message(view=error_embed("Du hast kein Team."), ephemeral=True)
                return
            row = await pool.fetchrow(
                "SELECT 1 FROM tournament_group_teams WHERE group_id = $1 AND team_id = $2", group_id, team["id"]
            )
            if not row:
                await interaction.response.send_message(view=error_embed("Dein Team ist nicht in dieser Gruppe."), ephemeral=True)
                return
            await pool.execute(
                "UPDATE tournament_group_teams SET confirmed_ready = true WHERE group_id = $1 AND team_id = $2",
                group_id, team["id"],
            )
            # ERST antworten, DANN das (Grafik-lastige, potenziell langsame) Panel aktualisieren -
            # sonst laeuft das 3-Sekunden-Interaktionsfenster ab, bevor geantwortet wird
            # ("Unknown interaction"), live beobachtet bei mehreren gleichzeitigen Klicks.
            await interaction.response.send_message(view=success_embed(f"{team['name']} ist bereit! ✅"), ephemeral=True)
            await refresh_group_panel(self.bot, group_id)

        elif action == "report":
            if is_admin:
                matches = await get_open_matches_in_group(group_id)
            elif team:
                matches = await get_open_matches_for_team(group_id, team["id"])
            else:
                await interaction.response.send_message(view=error_embed("Du hast kein Team in dieser Gruppe."), ephemeral=True)
                return

            if not matches:
                await interaction.response.send_message(view=info_embed("Keine offenen Matches gefunden."), ephemeral=True)
                return

            if not is_admin and len(matches) == 1:
                await resolve_match_score_entry(interaction, matches[0]["id"], is_admin)
                return

            team_ids = [m["team1_id"] for m in matches] + [m["team2_id"] for m in matches]
            names = await team_name_map(team_ids)
            await interaction.response.send_message(
                content="Welches Match?", view=GroupMatchSelect(matches, names, is_admin), ephemeral=True
            )

        elif action == "sizevideo":
            if not team:
                await interaction.response.send_message(view=error_embed("Du hast kein Team."), ephemeral=True)
                return
            open_matches = await get_open_matches_for_team(group_id, team["id"])
            if not open_matches:
                await interaction.response.send_message(view=warning_embed("Du hast gerade kein offenes Match in dieser Gruppe."), ephemeral=True)
                return
            match = open_matches[0]
            opponent_team_id = match["team2_id"] if match["team1_id"] == team["id"] else match["team1_id"]
            opponent_managers = await get_team_managers(opponent_team_id)
            mentions = " ".join(f"<@{m['discord_id']}>" for m in opponent_managers) or "(kein Manager gefunden)"
            text = f"{mentions}\n### 📹 Größenvideo wurde vom Gegner gefordert."
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_color=discord.Color.gold()))

            # In BEIDE Kanaele posten (Gruppenkanal + Panel-Kanal) - die Buttons sitzen nur im
            # Panel-Kanal, aber die meisten Manager schauen eher in den normalen Gruppenkanal.
            await interaction.response.send_message(view=success_embed("Größenvideo angefordert."), ephemeral=True)
            for channel_id in {group["channel_id"], group["panel_channel_id"]}:
                if not channel_id:
                    continue
                target_channel = interaction.guild.get_channel(channel_id)
                if target_channel is None:
                    try:
                        target_channel = await interaction.guild.fetch_channel(channel_id)
                    except discord.HTTPException:
                        continue
                try:
                    await target_channel.send(view=view)
                except discord.HTTPException:
                    pass

            for m in opponent_managers:
                try:
                    user = await self.bot.fetch_user(m["discord_id"])
                    await user.send(
                        view=info_embed(
                            "📹 Größenvideo angefordert",
                            f"**{team['name']}** hat ein Größenvideo von deinem Team in **{interaction.guild.name}** "
                            "gefordert. Schau im Gruppenkanal vorbei.",
                        )
                    )
                except discord.HTTPException:
                    pass

    async def handle_bracket_action(self, interaction: discord.Interaction, custom_id: str):
        _, tid_str, bracket, action = custom_id.split(":", 3)
        tournament_id = int(tid_str)
        t = await get_tournament(tournament_id)
        if not t:
            await interaction.response.send_message(view=error_embed("Turnier nicht gefunden."), ephemeral=True)
            return

        team = await get_team_for_user_in_tournament(tournament_id, interaction.user.id)
        is_admin = await is_tournament_moderator(interaction.user)

        if action == "played":
            if not team:
                await interaction.response.send_message(view=error_embed("Du hast kein Team."), ephemeral=True)
                return
            open_matches = await get_open_matches_for_team_bracket(tournament_id, bracket, team["id"])
            if not open_matches:
                await interaction.response.send_message(view=warning_embed("Du hast gerade kein offenes Spiel in diesem Bracket."), ephemeral=True)
                return

            match = open_matches[0]
            opponent_team_id = match["team2_id"] if match["team1_id"] == team["id"] else match["team1_id"]
            team_row = await get_pool_team(team["id"])
            opponent_row = await get_pool_team(opponent_team_id)

            await interaction.response.defer(ephemeral=True, thinking=True)
            ea_result = await try_fetch_ea_result(team_row, opponent_row)
            if not ea_result:
                await interaction.followup.send(
                    view=warning_embed(
                        "Kein passendes EA-Match gefunden",
                        "Bitte trage das Ergebnis über 'Ergebnis eintragen' manuell ein.",
                    ),
                    ephemeral=True,
                )
                return

            s1, s2 = ea_result
            if team["id"] != match["team1_id"]:
                s1, s2 = s2, s1
            await finalize_match_result(self.bot, interaction.guild, match["id"], s1, s2)
            names = await team_name_map([match["team1_id"], match["team2_id"]])
            await interaction.followup.send(
                view=success_embed(
                    "EA-Match gefunden, Ergebnis automatisch übernommen",
                    f"**{names.get(match['team1_id'])} {s1}:{s2} {names.get(match['team2_id'])}**",
                )
            )

        elif action == "report":
            if is_admin:
                matches = await get_open_matches_in_bracket(tournament_id, bracket)
            elif team:
                matches = await get_open_matches_for_team_bracket(tournament_id, bracket, team["id"])
            else:
                await interaction.response.send_message(view=error_embed("Du hast kein Team in diesem Bracket."), ephemeral=True)
                return

            if not matches:
                await interaction.response.send_message(view=info_embed("Keine offenen Matches gefunden."), ephemeral=True)
                return

            if not is_admin and len(matches) == 1:
                await resolve_match_score_entry(interaction, matches[0]["id"], is_admin)
                return

            team_ids = [m["team1_id"] for m in matches] + [m["team2_id"] for m in matches]
            names = await team_name_map(team_ids)
            await interaction.response.send_message(
                content="Welches Match?", view=GroupMatchSelect(matches, names, is_admin), ephemeral=True
            )

        elif action == "sizevideo":
            if not team:
                await interaction.response.send_message(view=error_embed("Du hast kein Team."), ephemeral=True)
                return
            open_matches = await get_open_matches_for_team_bracket(tournament_id, bracket, team["id"])
            if not open_matches:
                await interaction.response.send_message(view=warning_embed("Du hast gerade kein offenes Match in diesem Bracket."), ephemeral=True)
                return
            match = open_matches[0]
            opponent_team_id = match["team2_id"] if match["team1_id"] == team["id"] else match["team1_id"]
            opponent_managers = await get_team_managers(opponent_team_id)
            mentions = " ".join(f"<@{m['discord_id']}>" for m in opponent_managers) or "(kein Manager gefunden)"
            text = f"{mentions}\n### 📹 Größenvideo wurde vom Gegner gefordert."
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_color=discord.Color.gold()))

            # In BEIDE Kanaele posten (Bracket-Kanal + Panel-Kanal)
            await interaction.response.send_message(view=success_embed("Größenvideo angefordert."), ephemeral=True)
            bracket_meta = await get_pool().fetchrow(
                "SELECT channel_id, panel_channel_id FROM tournament_bracket_meta WHERE tournament_id = $1 AND bracket = $2",
                tournament_id, bracket,
            )
            channel_ids = {bracket_meta["channel_id"], bracket_meta["panel_channel_id"]} if bracket_meta else set()
            for channel_id in channel_ids:
                if not channel_id:
                    continue
                target_channel = interaction.guild.get_channel(channel_id)
                if target_channel is None:
                    try:
                        target_channel = await interaction.guild.fetch_channel(channel_id)
                    except discord.HTTPException:
                        continue
                try:
                    await target_channel.send(view=view)
                except discord.HTTPException:
                    pass

            for m in opponent_managers:
                try:
                    user = await self.bot.fetch_user(m["discord_id"])
                    await user.send(
                        view=info_embed(
                            "📹 Größenvideo angefordert",
                            f"**{team['name']}** hat ein Größenvideo von deinem Team in **{interaction.guild.name}** "
                            "gefordert. Schau im Bracket-Kanal vorbei.",
                        )
                    )
                except discord.HTTPException:
                    pass

    async def handle_match_confirm(self, interaction: discord.Interaction, custom_id: str):
        _, mid_str, decision = custom_id.split(":", 2)
        match_id = int(mid_str)
        match = await get_match(match_id)
        if not match or not match["pending_confirmation"]:
            await interaction.response.send_message(view=warning_embed("Dieses Ergebnis steht nicht mehr zur Bestätigung an."), ephemeral=True)
            return

        reporter_team_id = match["reported_by_team_id"]
        opponent_team_id = match["team2_id"] if reporter_team_id == match["team1_id"] else match["team1_id"]
        role = await get_role_for_user(opponent_team_id, interaction.user.id)
        is_admin = await is_tournament_moderator(interaction.user)
        if not role and not is_admin:
            await interaction.response.send_message(
                view=error_embed("Nur der Manager des Gegner-Teams (oder ein Admin) kann dieses Ergebnis bestätigen."), ephemeral=True
            )
            return

        pool = get_pool()
        if decision == "yes":
            await interaction.response.defer(thinking=True)
            await finalize_match_result(self.bot, interaction.guild, match_id, match["team1_score"], match["team2_score"])
            from audit import log_action
            await log_action(
                interaction.guild_id, interaction.user, "match.result_confirmed", "match", match_id,
                f"{match['team1_score']}:{match['team2_score']}",
            )
            names = await team_name_map([match["team1_id"], match["team2_id"]])
            try:
                await interaction.followup.send(
                    view=success_embed(
                        "Ergebnis bestätigt",
                        f"**{names.get(match['team1_id'])} {match['team1_score']}:{match['team2_score']} {names.get(match['team2_id'])}**",
                    )
                )
            except discord.HTTPException:
                # Das Ergebnis ist zu diesem Zeitpunkt schon final gespeichert (finalize_match_result
                # ist bereits durchgelaufen) - nur diese abschliessende Bestaetigungs-Antwort scheitert
                # gelegentlich (vereinzelt 404 "Unknown Message" beobachtet, Ursache nicht reproduzierbar,
                # betrifft aber nie das eigentliche Ergebnis). Lieber leise loggen als eine spektakulaer
                # aussehende Fehlermeldung werfen, obwohl inhaltlich alles korrekt gelaufen ist.
                log.warning(f"Bestaetigungs-Antwort fuer Match {match_id} konnte nicht gesendet werden (Ergebnis ist trotzdem gespeichert).")
        else:
            await pool.execute(
                """
                UPDATE tournament_matches
                SET team1_score = NULL, team2_score = NULL, reported_by_team_id = NULL, pending_confirmation = false
                WHERE id = $1
                """,
                match_id,
            )
            await interaction.response.send_message(view=error_embed("Ergebnis abgelehnt", "Bitte erneut eintragen oder Admin kontaktieren."))

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")

        prefixes = ("groupaction:", "bracketaction:", "matchconfirm:", "tourney:")
        if custom_id.startswith(prefixes):
            log.info(f"TournamentCog empfaengt custom_id={custom_id!r} von user={interaction.user.id}")
            try:
                await self._route_interaction(interaction, custom_id)
            except Exception:
                log.exception(f"Fehler beim Verarbeiten von custom_id={custom_id!r}")
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(view=error_embed("Ein interner Fehler ist aufgetreten."), ephemeral=True)
                    else:
                        await interaction.response.send_message(view=error_embed("Ein interner Fehler ist aufgetreten."), ephemeral=True)
                except discord.HTTPException:
                    pass

    async def _route_interaction(self, interaction: discord.Interaction, custom_id: str):
        if custom_id.startswith("groupaction:"):
            await self.handle_group_action(interaction, custom_id)
            return
        if custom_id.startswith("bracketaction:"):
            await self.handle_bracket_action(interaction, custom_id)
            return
        if custom_id.startswith("matchconfirm:"):
            await self.handle_match_confirm(interaction, custom_id)
            return

        if not custom_id.startswith("tourney:"):
            return

        _, tid_str, action = custom_id.split(":", 2)
        tournament_id = int(tid_str)
        t = await get_tournament(tournament_id)
        if not t:
            await interaction.response.send_message(view=error_embed("Dieses Turnier existiert nicht mehr."), ephemeral=True)
            return

        pool = get_pool()

        if action == "register":
            if t["status"] != "open":
                await interaction.response.send_message(view=warning_embed("Die Anmeldung für dieses Turnier ist geschlossen."), ephemeral=True)
                return

            from cogs.moderation import get_active_ban, format_ban_reason
            ban = await get_active_ban(interaction.guild_id, interaction.user.id)
            if ban:
                await interaction.response.send_message(view=warning_embed(format_ban_reason(ban)), ephemeral=True)
                return

            team = await get_team_for_user(interaction.guild_id, interaction.user.id)
            if not team:
                channel_hint = await team_register_hint(interaction.guild_id)
                await interaction.response.send_message(
                    view=error_embed("Du brauchst zuerst ein Team", f"Erst {channel_hint} -> 'Team gründen' klicken."), ephemeral=True
                )
                return

            from cogs.moderation import get_active_team_ban, format_team_ban_reason
            team_ban = await get_active_team_ban(interaction.guild_id, team["id"])
            if team_ban:
                await interaction.response.send_message(
                    view=warning_embed(format_team_ban_reason(team_ban, team["name"])), ephemeral=True
                )
                return

            existing = await get_team_signup(tournament_id, team["id"])
            if existing and existing["status"] != "withdrawn":
                await interaction.response.send_message(
                    view=info_embed(f"{team['name']} ist bereits angemeldet", f"Status: {existing['status']}"), ephemeral=True
                )
                return

            if existing and existing["status"] == "withdrawn":
                await pool.execute(
                    "UPDATE tournament_signups SET status = 'waitlist', signup_time = now() WHERE id = $1",
                    existing["id"],
                )
            else:
                await pool.execute(
                    "INSERT INTO tournament_signups (tournament_id, team_id, status) VALUES ($1, $2, 'waitlist')",
                    tournament_id, team["id"],
                )

            await reconcile_signups(tournament_id)
            final = await get_team_signup(tournament_id, team["id"])

            from audit import log_action
            await log_action(
                interaction.guild_id, interaction.user, "signup.registered", "tournament", tournament_id,
                f"{team['name']} ({final['status'] if final else '?'})",
            )

            if final and final["status"] == "registered":
                await interaction.response.send_message(view=success_embed(f"{team['name']} ist angemeldet!"), ephemeral=True)
                if t.get("is_donation_tournament"):
                    try:
                        from cogs.tickets import create_payment_ticket
                        await create_payment_ticket(self.bot, interaction.guild, t, team)
                    except Exception:
                        log.exception(f"Fehler beim Erstellen des Zahlungs-Kanals fuer Team {team['id']} / Turnier {tournament_id}")
            else:
                await interaction.response.send_message(
                    view=info_embed(
                        "Aktuelle Turnierstufe ist voll",
                        f"**{team['name']}** steht auf der Warteliste und rückt automatisch nach, sobald genug "
                        "Teams für die nächste Stufe angemeldet sind.",
                    ),
                    ephemeral=True,
                )
            await refresh_panel(self.bot, tournament_id)

        elif action == "unregister":
            team = await get_team_for_user(interaction.guild_id, interaction.user.id)
            if not team:
                await interaction.response.send_message(view=error_embed("Du hast kein Team."), ephemeral=True)
                return

            existing = await get_team_signup(tournament_id, team["id"])
            if not existing or existing["status"] == "withdrawn":
                await interaction.response.send_message(view=info_embed(f"{team['name']} ist nicht angemeldet."), ephemeral=True)
                return

            await pool.execute(
                "UPDATE tournament_signups SET status = 'withdrawn' WHERE id = $1", existing["id"]
            )
            await reconcile_signups(tournament_id)
            from audit import log_action
            await log_action(interaction.guild_id, interaction.user, "signup.withdrawn", "tournament", tournament_id, team["name"])

            await interaction.response.send_message(view=success_embed(f"👋 {team['name']} wurde abgemeldet."), ephemeral=True)
            await refresh_panel(self.bot, tournament_id)

        elif action == "streamlink":
            is_admin = await is_tournament_admin(interaction.user)
            team = await get_team_for_user(interaction.guild_id, interaction.user.id)
            if not is_admin and not team:
                await interaction.response.send_message(view=error_embed("Nur Admins oder angemeldete Team-Manager können den Stream-Link ändern."), ephemeral=True)
                return
            await interaction.response.send_modal(TournamentStreamLinkModal(tournament_id, t.get("stream_link")))

        elif action == "close":
            if not await is_tournament_admin(interaction.user):
                await interaction.response.send_message(view=error_embed("Nur Admins können die Anmeldung schließen."), ephemeral=True)
                return
            await pool.execute("UPDATE tournaments SET status = 'closed' WHERE id = $1", tournament_id)
            from audit import log_action
            await log_action(interaction.guild_id, interaction.user, "tournament.closed", "tournament", tournament_id, t["name"])
            await interaction.response.send_message(
                view=success_embed("🔒 Anmeldung geschlossen", "Nutze das Admin-Panel um das Bracket zu erstellen."), ephemeral=True
            )
            await refresh_panel(self.bot, tournament_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(TournamentCog(bot))
