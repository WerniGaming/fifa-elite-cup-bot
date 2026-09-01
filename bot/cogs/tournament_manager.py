"""
Turnier-Cog: Erstellung (Admin), Anmeldung/Abmeldung mit Warteliste,
automatische Bracket-Berechnung im Hintergrund, Turnierstart mit
Runde-1-Paarungen, Team-/Turnierübersicht.
"""
from __future__ import annotations
import asyncio
import logging
import math
import os
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import discord
from discord import app_commands
from discord.ext import commands

from db import get_pool
from ui_helpers import success_embed, error_embed, info_embed, warning_embed
from permissions import is_tournament_admin, is_tournament_moderator
from typing import Literal
from cogs.team_manager import get_team_for_user, get_role_for_user, get_team_managers, is_valid_twitch_link

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

ALLOWED_BRACKET_SIZES = sorted(set(
    [n for n in range(8, 129, 4)] +  # durch 4er-Gruppen sauber teilbar: 8,12,16,20,24,28,32...
    [n for n in range(8, 129, 6)]    # durch 6er-Gruppen sauber teilbar: 12,18,24,30...
))


def group_size_for(bracket_size: int) -> int:
    """4er-Gruppen bevorzugt (mehr Spiele, kleinere Gruppen), 6er nur wenn 4 nicht sauber aufgeht."""
    return 4 if bracket_size % 4 == 0 else 6


# ---------- Hilfsfunktionen ----------

def compute_bracket_size(total_signups: int, min_teams: int, max_teams: int) -> int:
    """
    Die 'aktive Stufe' ist die groesste Turniergroesse, fuer die bereits GENUG
    Anmeldungen (registriert + Warteliste zusammen) vorliegen, um sie komplett
    zu fuellen (in sauberen 4er- oder 6er-Gruppen). Ein einzelnes Team ueber
    der aktuellen Stufe wandert also erst auf die Warteliste, statt die Stufe
    sofort hochzuschalten - die naechste Stufe wird erst 'aktiv', wenn sie
    wirklich voll waere.
    """
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


async def get_unconfirmed_teams(tournament_id: int) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT te.id, te.name FROM tournament_signups ts
        JOIN teams te ON te.id = ts.team_id
        WHERE ts.tournament_id = $1 AND ts.status = 'registered' AND ts.confirmed_active = false
        ORDER BY ts.signup_time ASC
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
    bracket_size = compute_bracket_size(total, t["min_teams"], t["max_teams"])

    for i, row in enumerate(rows, start=1):
        new_status = "registered" if i <= bracket_size else "waitlist"
        await pool.execute(
            "UPDATE tournament_signups SET status = $1 WHERE id = $2 AND status != $1", new_status, row["id"]
        )
    return bracket_size


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
                await pool.execute(
                    "UPDATE tournament_bracket_meta SET direct_entrants = NULL WHERE tournament_id = $1 AND bracket = $2",
                    tournament_id, bracket,
                )

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
    Team verlaesst mitten im Turnier: ALLE noch offenen Spiele dieses Teams
    (Gruppenphase + KO-Phase) werden automatisch 1:0 fuer den jeweiligen
    Gegner gewertet (Def-Win). Team wird zusaetzlich als 'withdrawn' markiert,
    damit es bei einem spaeteren KO-Phase-Start NICHT mehr fuer Winner-/Loser-
    Bracket qualifiziert wird, egal wie seine (eingefrorene) Tabellenposition
    aussieht. Gibt die Anzahl der betroffenen Spiele zurueck.
    """
    pool = get_pool()
    await pool.execute(
        "UPDATE tournament_signups SET status = 'withdrawn' WHERE tournament_id = $1 AND team_id = $2",
        tournament_id, team_id,
    )
    open_matches = await pool.fetch(
        """
        SELECT * FROM tournament_matches
        WHERE tournament_id = $1 AND status != 'completed'
          AND (team1_id = $2 OR team2_id = $2)
        """,
        tournament_id, team_id,
    )
    count = 0
    for m in open_matches:
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
                discord.ui.MediaGallery(discord.MediaGalleryItem(media=podium_file)),
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

        image_file = None
        if match["phase"] == "group" and match.get("group_id"):
            try:
                group = await get_pool().fetchrow("SELECT * FROM tournament_groups WHERE id = $1", match["group_id"])
                image_file = await build_group_schedule_file(dict(group))
            except Exception:
                log.exception(f"Fehler beim Erstellen der Spielplan-Grafik fuer Bestaetigungs-Embed (Match {self.match_id})")

        if image_file:
            await interaction.response.send_message(view=view, file=image_file)
        else:
            await interaction.response.send_message(view=view)


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


async def build_group_standings_text(group_id: int) -> str:
    pool = get_pool()
    team_rows = await pool.fetch("SELECT team_id FROM tournament_group_teams WHERE group_id = $1", group_id)
    standings = []
    for tr in team_rows:
        wins = await pool.fetchval(
            "SELECT COUNT(*) FROM tournament_matches WHERE group_id = $1 AND winner_id = $2",
            group_id, tr["team_id"],
        )
        goals_row = await pool.fetchrow(
            """
            SELECT
              COALESCE(SUM(CASE WHEN team1_id = $2 THEN team1_score WHEN team2_id = $2 THEN team2_score ELSE 0 END), 0) AS goals_for,
              COALESCE(SUM(CASE WHEN team1_id = $2 THEN team2_score WHEN team2_id = $2 THEN team1_score ELSE 0 END), 0) AS goals_against
            FROM tournament_matches
            WHERE group_id = $1 AND status = 'completed' AND (team1_id = $2 OR team2_id = $2)
            """,
            group_id, tr["team_id"],
        )
        goals_for = goals_row["goals_for"] or 0
        goals_against = goals_row["goals_against"] or 0
        standings.append({
            "team_id": tr["team_id"], "wins": wins,
            "goals_for": goals_for, "goals_against": goals_against,
            "goal_diff": goals_for - goals_against,
        })
    standings.sort(key=lambda x: (x["wins"], x["goal_diff"], x["goals_for"]), reverse=True)

    names = await team_name_map([s["team_id"] for s in standings])
    lines = ["**Tabelle**", ""]
    for i, s in enumerate(standings, start=1):
        lines.append(
            f"`{i}.` {names.get(s['team_id'], '?')} — `{s['wins']}` Siege · Tore `{s['goals_for']}:{s['goals_against']}` (`{s['goal_diff']:+d}`)"
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
    Landet im eigenen Panel-Kanal (nur Bot darf dort schreiben). Solange nicht
    jedes Team der Gruppe 'Team ist da' bestaetigt hat, zeigt das Panel eine
    Check-in-Checkliste statt der Tabelle - Spieltag 1 bleibt so lange blockiert.
    Die Spielplan-Grafik wird per MediaGallery eingebettet (view.schedule_file
    muss vom Aufrufer zusaetzlich in files= mitgegeben werden).
    """
    pool = get_pool()
    group = await pool.fetchrow("SELECT * FROM tournament_groups WHERE id = $1", group_id)
    team_rows = await pool.fetch(
        "SELECT team_id, confirmed_ready FROM tournament_group_teams WHERE group_id = $1", group_id
    )
    view = discord.ui.LayoutView(timeout=None)
    schedule_file = await build_group_schedule_file(dict(group))
    view.schedule_file = schedule_file
    media = discord.ui.MediaGallery(discord.MediaGalleryItem(media=schedule_file))
    all_ready = all(tr["confirmed_ready"] for tr in team_rows) if team_rows else True

    if not all_ready:
        names = await team_name_map([tr["team_id"] for tr in team_rows])
        lines = ["**Team-Check-in**", "_Erst wenn hier jedes Team bestätigt hat, kann Spieltag 1 freigegeben werden._", ""]
        for tr in team_rows:
            mark = "✅" if tr["confirmed_ready"] else "⏳"
            lines.append(f"{mark} {names.get(tr['team_id'], '?')}")
        container = discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.ActionRow(
                discord.ui.Button(label="Team ist da", style=discord.ButtonStyle.success, custom_id=f"groupaction:{group_id}:ready"),
            ),
            media,
            accent_color=discord.Color.gold(),
        )
        view.add_item(container)
        return view

    standings_text = await build_group_standings_text(group_id)
    container = discord.ui.Container(
        discord.ui.TextDisplay(standings_text),
        media,
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
    """Alle Matches einer Gruppe, nach Spieltag gruppiert, inkl. aktuellem Ergebnis (fuer die Spielplan-Grafik)."""
    pool = get_pool()
    all_group_matches = await pool.fetch(
        "SELECT * FROM tournament_matches WHERE group_id = $1 ORDER BY round, match_number", group_id
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
            "team1_score": m["team1_score"], "team2_score": m["team2_score"], "status": m["status"],
        })
    return matchdays_data


async def build_group_schedule_file(group: dict) -> discord.File:
    from graphics import render_schedule_image
    matchdays = await build_group_schedule_matchdays(group["id"])
    sections = [(f"Spieltag {i}", md) for i, md in enumerate(matchdays, start=1)]
    buf = await render_schedule_image(f"Spielplan — Gruppe {group['group_number']}", sections)
    return discord.File(buf, filename="spielplan.png")


async def build_bracket_schedule_matches(tournament_id: int, bracket: str) -> list[tuple[str, list[dict]]]:
    """Alle Matches eines Brackets, nach Runde gruppiert mit deutschem Rundennamen, inkl. Ergebnis."""
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

    sections: list[tuple[str, list[dict]]] = []
    current_round = None
    current_matches: list[dict] = []
    for m in matches:
        if m["round"] != current_round:
            if current_matches:
                label = "Spiel um Platz 3" if all(mm.get("is_third_place_match") for mm in current_matches) else round_name(len(current_matches))
                sections.append((label, current_matches))
            current_round = m["round"]
            current_matches = []
        t1 = team_rows.get(m["team1_id"]) or {"name": "Freilos", "logo_url": None}
        t2 = team_rows.get(m["team2_id"]) or {"name": "Freilos", "logo_url": None}
        current_matches.append({
            "team1_name": t1["name"], "team2_name": t2["name"],
            "team1_logo_url": t1.get("logo_url"), "team2_logo_url": t2.get("logo_url"),
            "team1_score": m["team1_score"], "team2_score": m["team2_score"], "status": m["status"],
        })
    if current_matches:
        label = "Spiel um Platz 3" if all(mm.get("is_third_place_match") for mm in current_matches) else round_name(len(current_matches))
        sections.append((label, current_matches))
    return sections


async def build_bracket_schedule_file(tournament_id: int, bracket: str) -> discord.File | None:
    from graphics import render_schedule_image
    sections = await build_bracket_schedule_matches(tournament_id, bracket)
    if not sections:
        return None
    label = "Winner Bracket" if bracket == "winner" else "Loser Bracket"
    buf = await render_schedule_image(label, sections)
    return discord.File(buf, filename="bracket.png")


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

    panel_channel = await guild.create_text_channel(
        f"gruppe-{group['group_number']}-panel", category=category, overwrites=overwrites
    )
    panel = await build_group_panel(group["id"])
    msg = await panel_channel.send(view=panel, files=[panel.schedule_file])
    await pool.execute(
        "UPDATE tournament_groups SET panel_channel_id = $1, panel_message_id = $2 WHERE id = $3",
        panel_channel.id, msg.id, group["id"],
    )

    main_channel = guild.get_channel(group.get("channel_id")) if group.get("channel_id") else None
    if main_channel:
        try:
            await main_channel.send(view=build_group_actions_view(group["id"]))
        except discord.HTTPException:
            log.exception(f"Fehler beim erneuten Posten der Aktions-Buttons im Gruppenkanal (Gruppe {group['id']})")

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
                    f"{prefix} **{names.get(s['team_id'], '?')}** — `{s['wins']}` Siege · "
                    f"Tore `{s['goals_for']}:{s['goals_against']}` (`{s['goal_diff']:+d}`)"
                )

            open_matches = [m for m in matches if m["status"] != "completed"]
            if open_matches:
                m_names = await team_name_map(
                    [m["team1_id"] for m in open_matches] + [m["team2_id"] for m in open_matches]
                )
                block.append("")
                block.append("**Offene Spiele:**")
                for m in open_matches:
                    block.append(
                        f"🔴 `ST {m['round']}` {m_names.get(m['team1_id'], '?')} 🆚 {m_names.get(m['team2_id'], '?')}"
                    )

            completed_matches = [m for m in matches if m["status"] == "completed"]
            if completed_matches:
                c_names = await team_name_map(
                    [m["team1_id"] for m in completed_matches] + [m["team2_id"] for m in completed_matches]
                )
                block.append("")
                block.append("**Ergebnisse:**")
                for m in completed_matches:
                    block.append(
                        f"✅ `ST {m['round']}` {c_names.get(m['team1_id'], '?')} `{m['team1_score']}:{m['team2_score']}` {c_names.get(m['team2_id'], '?')}"
                    )

            items.append(discord.ui.TextDisplay("\n".join(block)))
            items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))

    for bracket, label, icon in (("winner", "Winner Bracket", "🏆"), ("loser", "Loser Bracket", "🥊")):
        matches = await pool.fetch(
            """
            SELECT * FROM tournament_matches WHERE tournament_id = $1 AND phase = 'knockout' AND bracket = $2
            ORDER BY round, match_number
            """,
            tournament_id, bracket,
        )
        if not matches:
            continue
        ids = [m["team1_id"] for m in matches] + [m["team2_id"] for m in matches]
        names = await team_name_map(ids)
        block = [f"### {icon} {label}", ""]
        current_round = None
        for m in matches:
            if m["round"] != current_round:
                current_round = m["round"]
                block.append(f"**Runde {current_round}**")
            t1 = names.get(m["team1_id"], "Freilos") if m["team1_id"] else "Freilos"
            t2 = names.get(m["team2_id"], "Freilos") if m["team2_id"] else "Freilos"
            if m["status"] == "completed" and m["team1_score"] is not None:
                winner_mark = "🟢" if m["winner_id"] else "⚪"
                block.append(f"{winner_mark} {t1} `{m['team1_score']}:{m['team2_score']}` {t2}")
            else:
                block.append(f"⏳ {t1} 🆚 {t2}")
        items.append(discord.ui.TextDisplay("\n".join(block)))
        items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))

    now_ts = int(discord.utils.utcnow().timestamp())
    items.append(discord.ui.TextDisplay(f"-# Zuletzt aktualisiert: <t:{now_ts}:R>"))

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_color=discord.Color.gold()))
    return view


async def refresh_live_schedule(bot: commands.Bot, guild: discord.Guild, tournament_id: int):
    channel = await get_live_schedule_channel(bot, guild)
    if not channel:
        return
    pool = get_pool()
    t = await get_tournament(tournament_id)
    view = await build_live_schedule_view(tournament_id)

    if t.get("live_schedule_message_id"):
        try:
            msg = await channel.fetch_message(t["live_schedule_message_id"])
            await msg.edit(view=view)
            return
        except discord.HTTPException:
            pass

    msg = await channel.send(view=view)
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


async def release_ko_round(bot: commands.Bot, channel: discord.abc.Messageable, matches: list[dict], round_label: str):
    """
    Postet die Paarungen einer KO-Runde mit EA-Club-Namen, Manager-Erwaehnungen und
    5-Minuten-Timer - analog zu release_matchday() in der Gruppenphase.
    """
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
            pairing_lines.append(
                f"> **{names.get(m['team1_id'], '?')}** {manager_mentions.get(m['team1_id'], '')} vs. "
                f"**{ea_names.get(m['team2_id'], '?')}** {manager_mentions.get(m['team2_id'], '')} "
                f"— EA-Club-Namen: `{ea_names.get(m['team1_id'], '?')}` vs. `{ea_names.get(m['team2_id'], '?')}`"
            )

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(f"# 📢 {round_label} ist freigegeben\nSo wird gespielt:"),
        discord.ui.Separator(),
        discord.ui.TextDisplay("\n".join(pairing_lines)),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "-# Ihr habt **5 Minuten**, um den Gegner unter dem oben genannten EA-Club-Namen ins Spiel einzuladen."
        ),
        accent_color=discord.Color.gold(),
    ))
    await channel.send(view=view)
    asyncio.create_task(send_ko_round_reminder(channel, round_label))



async def release_matchday(bot: commands.Bot, guild: discord.Guild, group_id: int, matchday: int):
    """Gibt einen Spieltag frei: postet Paarungen im Gruppenkanal, DMt alle Manager, startet 5-Min-Reminder."""
    pool = get_pool()
    group = await pool.fetchrow("SELECT * FROM tournament_groups WHERE id = $1", group_id)
    if not group:
        return

    matches = await pool.fetch(
        "SELECT * FROM tournament_matches WHERE group_id = $1 AND round = $2 ORDER BY match_number", group_id, matchday
    )
    team_ids = [m["team1_id"] for m in matches] + [m["team2_id"] for m in matches]
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
            pairing_lines.append(
                f"> **{names.get(m['team1_id'], '?')}** {manager_mentions.get(m['team1_id'], '')} vs. "
                f"**{ea_names.get(m['team2_id'], '?')}** {manager_mentions.get(m['team2_id'], '')} "
                f"— EA-Club-Namen: `{ea_names.get(m['team1_id'], '?')}` vs. `{ea_names.get(m['team2_id'], '?')}`"
            )

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(f"# 📢 Spieltag {matchday} ist freigegeben\nSo wird gespielt:"),
        discord.ui.Separator(),
        discord.ui.TextDisplay("\n".join(pairing_lines)),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "-# Ihr habt **5 Minuten**, um den Gegner unter dem oben genannten EA-Club-Namen ins Spiel einzuladen."
        ),
        accent_color=discord.Color.gold(),
    ))

    channel = guild.get_channel(group["channel_id"])
    if channel is None:
        try:
            channel = await guild.fetch_channel(group["channel_id"])
        except discord.HTTPException:
            channel = None

    if channel:
        await channel.send(view=view)
        await pool.execute("UPDATE tournament_groups SET released_round = $1 WHERE id = $2", matchday, group_id)
        asyncio.create_task(send_matchday_reminder(channel, matchday))

        try:
            schedule_file = await build_group_schedule_file(dict(group))
            schedule_view = discord.ui.LayoutView(timeout=None)
            schedule_view.add_item(discord.ui.Container(
                discord.ui.TextDisplay(f"### 📋 Aktueller Spielplan — Gruppe {group['group_number']}"),
                discord.ui.MediaGallery(discord.MediaGalleryItem(media=schedule_file)),
                accent_color=discord.Color.gold(),
            ))
            await channel.send(view=schedule_view, files=[schedule_file])
        except Exception:
            log.exception(f"Fehler beim Erstellen der Spielplan-Grafik fuer Gruppe {group_id}")

    bye_team_ids = {(m["team1_id"] or m["team2_id"]) for m in matches if m["team1_id"] is None or m["team2_id"] is None}
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
                else:
                    await user.send(
                        f"📢 **Spieltag {matchday}** in Gruppe {group['group_number']} ({t['name']}) wurde freigegeben! "
                        "Ladet euren Gegner jetzt ins Spiel ein - ihr habt 5 Minuten Zeit."
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

    bracket_size = compute_bracket_size(len(team_ids), MIN_BRACKET_SIZE, t["max_teams"])
    random.shuffle(team_ids)
    while len(team_ids) < bracket_size:
        team_ids.append(None)  # Freilos - fehlende Teams bis zur Turnierstufe auffuellen

    group_size = group_size_for(bracket_size)
    num_groups = max(1, bracket_size // group_size)
    groups: list[list[int | None]] = [[] for _ in range(num_groups)]
    for i, tid in enumerate(team_ids):
        groups[i % num_groups].append(tid)

    category = await guild.create_category(f"{t['name']} Gruppenphase"[:100])
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
        standings = []
        for tr in team_rows:
            wins = await pool.fetchval(
                "SELECT COUNT(*) FROM tournament_matches WHERE group_id = $1 AND winner_id = $2",
                g["id"], tr["team_id"],
            )
            goals_row = await pool.fetchrow(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN team1_id = $2 THEN team1_score WHEN team2_id = $2 THEN team2_score ELSE 0 END), 0) AS goals_for,
                  COALESCE(SUM(CASE WHEN team1_id = $2 THEN team2_score WHEN team2_id = $2 THEN team1_score ELSE 0 END), 0) AS goals_against
                FROM tournament_matches
                WHERE group_id = $1 AND status = 'completed' AND (team1_id = $2 OR team2_id = $2)
                """,
                g["id"], tr["team_id"],
            )
            goals_for = goals_row["goals_for"] or 0
            goals_against = goals_row["goals_against"] or 0
            standings.append({
                "team_id": tr["team_id"], "wins": wins,
                "goals_for": goals_for, "goals_against": goals_against,
                "goal_diff": goals_for - goals_against,
            })
        # Sortierung: 1. Siege, 2. Torverhaeltnis (Tiebreaker), 3. geschossene Tore
        standings.sort(key=lambda x: (x["wins"], x["goal_diff"], x["goals_for"]), reverse=True)
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
    current_round = None
    for m in matches:
        if m["round"] != current_round:
            current_round = m["round"]
            normal_matches_in_round = [mm for mm in matches if mm["round"] == current_round and not mm.get("is_third_place_match")]
            third_place_in_round = any(mm.get("is_third_place_match") for mm in matches if mm["round"] == current_round)
            if normal_matches_in_round:
                block.append(f"**{round_name(len(normal_matches_in_round))}**")
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
        items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(media=schedule_file)))
    view.add_item(discord.ui.Container(*items, accent_color=discord.Color.gold()))
    return view


async def create_bracket_panel_channel(guild: discord.Guild, tournament_id: int, bracket: str, role: discord.Role) -> discord.TextChannel:
    """Legt den 'nur Panel'-Kanal fuer ein Bracket an (z.B. 'winner-bracket-panel'), nur Bot darf dort schreiben."""
    pool = get_pool()
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False), guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)

    panel_channel = await guild.create_text_channel(f"{bracket}-bracket-panel"[:100], overwrites=overwrites)
    panel = await build_bracket_panel_view(tournament_id, bracket)
    msg = await panel_channel.send(view=panel, files=[panel.schedule_file] if panel.schedule_file else [])
    await pool.execute(
        "UPDATE tournament_bracket_meta SET panel_channel_id = $1, panel_message_id = $2 WHERE tournament_id = $3 AND bracket = $4",
        panel_channel.id, msg.id, tournament_id, bracket,
    )
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


async def create_bracket(bot: commands.Bot, guild: discord.Guild, tournament_id: int, t: dict, bracket: str, team_ids: list[int]) -> list[dict]:
    """Erstellt Rolle+Kanal fuer ein einzelnes Bracket (winner/loser) und die Runde-1-Paarungen."""
    if not team_ids:
        return []
    pool = get_pool()

    existing_meta = await pool.fetchrow(
        "SELECT * FROM tournament_bracket_meta WHERE tournament_id = $1 AND bracket = $2", tournament_id, bracket
    )
    if existing_meta:
        existing_matches = await pool.fetch(
            """
            SELECT * FROM tournament_matches
            WHERE tournament_id = $1 AND phase = 'knockout' AND bracket = $2 AND round = 1
            ORDER BY match_number
            """,
            tournament_id, bracket,
        )
        return [dict(m) for m in existing_matches]

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
                    pass

    try:
        await asyncio.wait_for(assign_roles(), timeout=30)
    except asyncio.TimeoutError:
        log.error(f"Timeout beim Zuweisen der Rollen fuer Bracket '{bracket}' (Turnier {tournament_id}) - mache trotzdem weiter")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    try:
        channel = await asyncio.wait_for(
            guild.create_text_channel(f"{t['name'][:35]}-{bracket}-bracket", overwrites=overwrites), timeout=15
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
        await create_bracket_panel_channel(guild, tournament_id, bracket, role)
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
        if b["role_id"]:
            role = guild.get_role(b["role_id"])
            if role:
                try:
                    await role.delete(reason="KO-Phase zurueckgesetzt")
                except discord.HTTPException:
                    pass

    await pool.execute("DELETE FROM tournament_matches WHERE tournament_id = $1 AND phase = 'knockout'", tournament_id)
    await pool.execute("DELETE FROM tournament_bracket_meta WHERE tournament_id = $1", tournament_id)
    await pool.execute(
        "UPDATE tournaments SET phase = 'groups', winner_champion_id = NULL, loser_champion_id = NULL WHERE id = $1",
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

    winner_seeds: list[dict] = []
    loser_seeds: list[dict] = []
    for g in standings:
        eligible = [s for s in g["standings"] if s["team_id"] not in withdrawn_team_ids]
        winner_n = len(g["standings"]) // 2  # 4er-Gruppe -> 2, 6er-Gruppe -> 3 (Sollgroesse bleibt gleich)
        for tier, s in enumerate(eligible[:winner_n]):
            winner_seeds.append({**s, "tier": tier})
        for tier, s in enumerate(eligible[winner_n:]):
            loser_seeds.append({**s, "tier": tier})

    # Seeding gruppenuebergreifend: erst Gruppenplatz (alle Gruppensieger vor allen
    # Gruppenzweiten usw.), bei gleichem Platz dann Siege/Tordifferenz/Tore. Sonst
    # koennte ein Gruppenzweiter vor einem Gruppenersten mit klar besserer Bilanz
    # direkt in die KO-Hauptrunde rutschen, waehrend der Erste in die Quali muss.
    def _seed_key(s):
        return (s["tier"], -s["wins"], -s["goal_diff"], -s["goals_for"])

    winner_seeds.sort(key=_seed_key)
    loser_seeds.sort(key=_seed_key)
    winner_teams = [s["team_id"] for s in winner_seeds]
    loser_teams = [s["team_id"] for s in loser_seeds]

    await create_bracket(bot, guild, tournament_id, t, "winner", winner_teams)
    await create_bracket(bot, guild, tournament_id, t, "loser", loser_teams)


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
    bracket_size = compute_bracket_size(effective_count, MIN_BRACKET_SIZE, t["max_teams"])
    group_size = group_size_for(bracket_size)
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
        group_size = group_size_for(bracket_size)
        num_groups = max(1, bracket_size // group_size)

        # Block: Kopf - Name, Größe, Eckdaten
        header_lines = [f"# 🏆 {t['name']}", f"`{bracket_size} Teams` · {num_groups} Gruppen à {group_size} Teams · Winner + Loser Bracket"]
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
                f"**KO-Phase** _(geschätzt {schedule['ko_rounds']} Runden, Winner + Loser parallel)_",
                "",
                f"🏁 **Voraussichtliches Ende:** {fmt_time_de(schedule['ko_end'])}",
                f"-# Schätzung für {bracket_size}er Turnier — kann sich noch verschieben",
            ]
            schedule_block = discord.ui.TextDisplay("\n".join(schedule_lines))

        # Block: Mannschaftsliste + Warteliste
        activity_check_phase = t["status"] == "closed" and t.get("phase") == "signup"
        confirmed_count = sum(1 for tm in registered_teams if tm.get("confirmed_active")) if activity_check_phase else 0
        paid_team_ids = t.get("_paid_team_ids", set()) if t.get("is_donation_tournament") else set()

        team_lines = ["### 📋 Gemeldete Teams"]
        if activity_check_phase:
            team_lines.append(f"✅ **Aktivitätscheck:** {confirmed_count}/{registered} Teams bestätigt")
        if t.get("is_donation_tournament"):
            team_lines.append(f"💰 **Bezahlt:** {len(paid_team_ids)}/{registered} Teams")
        team_lines.append("")
        for i in range(1, bracket_size + 1):
            if i <= registered:
                team = registered_teams[i - 1]
                mark = " ✅" if activity_check_phase and team.get("confirmed_active") else ""
                paid_mark = " 💰" if team["id"] in paid_team_ids else ""
                team_lines.append(f"`{i}.` **{team['name']}** (<@{team['owner_discord_id']}>){mark}{paid_mark}")
            else:
                team_lines.append(f"`{i}.` –")

        if waitlist_teams:
            team_lines += ["", f"**⏳ Warteliste ({len(waitlist_teams)}):**"]
            for i, team in enumerate(waitlist_teams, start=1):
                team_lines.append(f"`{i}.` {team['name']} (<@{team['owner_discord_id']}>)")
        team_block = discord.ui.TextDisplay("\n".join(team_lines))

        # Block: Erklaerungstext (nach den Buttons)
        explanation = discord.ui.TextDisplay(
            "-# Die Turniergröße wächst automatisch mit den Anmeldungen — jede weitere Anmeldung kann das Turnier "
            "also noch größer machen. Passt ein Team nicht mehr in die aktuelle Stufe, wartet es auf der Warteliste "
            "und rückt nach, sobald genug Anmeldungen für die nächste Stufe da sind."
        )

        closed = t["status"] != "open"
        items = []
        if os.path.exists(TOURNAMENT_BANNER_PATH):
            self.banner_file = discord.File(TOURNAMENT_BANNER_PATH, filename="tournament_banner.jpg")
            items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(media=self.banner_file)))
        items.append(header_block)
        if schedule_block:
            items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large))
            items.append(schedule_block)
        items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large))
        items.append(team_block)
        items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large))

        if activity_check_phase:
            items.append(
                discord.ui.ActionRow(
                    discord.ui.Button(
                        label="✅ Team ist da", style=discord.ButtonStyle.success,
                        custom_id=f"tourney:{t['id']}:confirmactive",
                    ),
                    discord.ui.Button(
                        label="Stream-Link ändern", style=discord.ButtonStyle.secondary,
                        custom_id=f"tourney:{t['id']}:streamlink",
                    ),
                )
            )
        else:
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
        items.append(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large))
        items.append(explanation)
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
                    "Format muss genau so aussehen: `https://www.twitch.tv/name`",
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


class TournamentCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def handle_group_action(self, interaction: discord.Interaction, custom_id: str):
        _, gid_str, action = custom_id.split(":", 2)
        group_id = int(gid_str)
        pool = get_pool()
        group = await pool.fetchrow("SELECT * FROM tournament_groups WHERE id = $1", group_id)
        if not group:
            await interaction.response.send_message(view=error_embed("Gruppe nicht gefunden."), ephemeral=True)
            return

        team = await get_team_for_user(interaction.guild_id, interaction.user.id)
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
            await refresh_group_panel(self.bot, group_id)
            await interaction.response.send_message(view=success_embed(f"{team['name']} ist bereit! ✅"), ephemeral=True)

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
            await interaction.response.send_message(view=view)

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

        team = await get_team_for_user(interaction.guild_id, interaction.user.id)
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
            await interaction.response.send_message(view=view)

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
            names = await team_name_map([match["team1_id"], match["team2_id"]])
            await interaction.followup.send(
                view=success_embed(
                    "Ergebnis bestätigt",
                    f"**{names.get(match['team1_id'])} {match['team1_score']}:{match['team2_score']} {names.get(match['team2_id'])}**",
                )
            )
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
                await interaction.response.send_message(
                    view=error_embed("Du brauchst zuerst ein Team", "siehe Team Manager Panel -> 'Team verknüpfen'."), ephemeral=True
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

            await interaction.response.send_message(view=success_embed(f"👋 {team['name']} wurde abgemeldet."), ephemeral=True)
            await refresh_panel(self.bot, tournament_id)

        elif action == "confirmactive":
            team = await get_team_for_user(interaction.guild_id, interaction.user.id)
            if not team:
                await interaction.response.send_message(view=error_embed("Du hast kein Team."), ephemeral=True)
                return
            signup = await get_team_signup(tournament_id, team["id"])
            if not signup or signup["status"] != "registered":
                await interaction.response.send_message(
                    view=warning_embed(f"{team['name']} ist nicht als registriert für dieses Turnier eingetragen."), ephemeral=True
                )
                return
            await pool.execute(
                "UPDATE tournament_signups SET confirmed_active = true WHERE id = $1", signup["id"]
            )
            await interaction.response.send_message(view=success_embed(f"{team['name']} ist als aktiv bestätigt!"), ephemeral=True)
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
            await interaction.response.send_message(
                view=success_embed("🔒 Anmeldung geschlossen", "Nutze das Admin-Panel um das Bracket zu erstellen."), ephemeral=True
            )
            await refresh_panel(self.bot, tournament_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(TournamentCog(bot))
