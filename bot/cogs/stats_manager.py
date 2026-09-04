"""
Stats-Cog: zieht echte Spieler-Statistiken aus der EA-API fuer alle Matches
eines Turnier-Brackets (Winner oder Loser), aggregiert sie und postet:
- Winner-/Loser-Top3 (Erster/Zweiter/Dritter)
- Awards (Bester Spieler, Torschuetze, Aufleger, Verteidiger, Torwart)
- Team of the Tournament (3-5-2)

Manuell ausgeloest ueber Buttons im Admin-Panel, NICHT automatisch - jedes
Match wird einzeln bei der EA-API abgefragt, das soll nicht ungefragt im
Hintergrund laufen (Proxy-Traffic, Zeit).
"""
from __future__ import annotations
import io
from collections import defaultdict
from dataclasses import dataclass, field

import discord
from discord.ext import commands

from db import get_pool
from ui_helpers import success_embed, WEBSITE_URL
from ea_api import EAProClubsAPI
from cogs.tournament_manager import (
    get_tournament,
    get_pool_team,
    team_name_map,
    build_bracket_finish_file,
    build_bracket_finish_text,
)

POSITION_GROUPS = {
    "goalkeeper": "GK", "gk": "GK",
    "defender": "DEF", "def": "DEF", "cb": "DEF", "rb": "DEF", "lb": "DEF", "rwb": "DEF", "lwb": "DEF",
    "midfielder": "MID", "mid": "MID", "cm": "MID", "cdm": "MID", "cam": "MID", "rm": "MID", "lm": "MID",
    "forward": "FWD", "attacker": "FWD", "fwd": "FWD", "st": "FWD", "cf": "FWD", "rw": "FWD", "lw": "FWD",
}

TOP11_FORMATION = {"GK": 1, "DEF": 3, "MID": 5, "FWD": 2}  # 3-5-2, wie im Vorbild


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _position_group(raw_pos) -> str:
    if not raw_pos:
        return "MID"
    return POSITION_GROUPS.get(str(raw_pos).strip().lower(), "MID")


@dataclass
class PlayerAgg:
    name: str
    team_id: int
    matches: int = 0
    total_rating: float = 0.0
    goals: int = 0
    assists: int = 0
    mom: int = 0
    saves: int = 0
    positions: dict = field(default_factory=lambda: defaultdict(int))

    @property
    def avg_rating(self) -> float:
        return self.total_rating / self.matches if self.matches else 0.0

    @property
    def main_position(self) -> str:
        if not self.positions:
            return "MID"
        return max(self.positions, key=self.positions.get)

    @property
    def score(self) -> float:
        """Rankingwert fuer Awards/Top-11 - schlicht die Durchschnittsbewertung ueber alle
        Matches, nicht mehr eine gewichtete Mischung aus Rating/Toren/Assists/MOM."""
        return self.avg_rating


async def try_fetch_ea_full_match(
    team1: dict, team2: dict, expected_score: tuple[int, int] | None = None
) -> dict | None:
    """Sucht in den letzten Freundschaftsspielen von Team1 das Match gegen Team2, gibt das volle
    Match-Objekt zurueck.

    expected_score (team1_tore, team2_tore): falls angegeben und mehrere Begegnungen zwischen
    denselben zwei Clubs in der Historie stehen (z.B. Gruppenphase UND KO gegeneinander), wird
    NUR das Match akzeptiert, dessen EA-Endstand mit unserem eingetragenen Turnier-Ergebnis
    uebereinstimmt - vorher wurde hier immer das erste gefundene Match genommen, was bei
    Wiederholungsbegegnungen zum falschen (oder bei Verlaengerung/Elfmeterschiessen zu einem nicht
    eindeutig zuordenbaren) Match fuehren konnte. Ohne Score-Match lieber nichts zurueckgeben als
    eine falsche Zuordnung zu riskieren."""
    if not team1.get("ea_club_id") or not team2.get("ea_club_id"):
        return None
    try:
        async with EAProClubsAPI() as api:
            matches = await api.get_matches(
                team1["ea_club_id"], team1.get("ea_platform") or "common-gen5",
            match_type="friendlyMatch", max_results=100,
            )
    except Exception:
        return None

    candidates = []
    for m in matches:
        clubs = m.get("clubs", {})
        if str(team1["ea_club_id"]) in clubs and str(team2["ea_club_id"]) in clubs:
            candidates.append(m)

    if not candidates:
        return None
    if expected_score is None:
        return candidates[0]

    s1, s2 = expected_score
    for m in candidates:
        clubs = m["clubs"]
        c1 = clubs[str(team1["ea_club_id"])]
        c2 = clubs[str(team2["ea_club_id"])]
        try:
            if int(c1.get("goals", -1)) == s1 and int(c2.get("goals", -1)) == s2:
                return m
        except (TypeError, ValueError):
            continue
    return None


EA_DISCONNECT_RATING = 3.0  # fester Straf-Wert, den EA einem Spieler gibt, der das Match verlassen hat/disconnected ist


async def capture_match_player_stats(match_id: int, team1_id: int, team2_id: int, score1: int | None = None, score2: int | None = None):
    """Sichert die EA-Spielerdaten fuer genau EIN Match sofort nach Ergebniseintragung,
    statt bis zum Bracket-Ende zu warten (die EA-Freundschaftsspiel-Historie ist begrenzt -
    ohne fruehe Sicherung koennten aeltere Matches spaeter aus der API-Historie fallen).
    Wird als Hintergrund-Task angestossen und darf den Ergebnis-Flow niemals stoeren -
    daher werden alle Fehler hier verschluckt (nur geloggt).

    score1/score2 (unser eingetragenes Turnier-Ergebnis): wird an try_fetch_ea_full_match
    durchgereicht, um bei Wiederholungsbegegnungen (Gruppenphase + KO gegen denselben Gegner)
    zwischen mehreren moeglichen EA-Matches das richtige zu erkennen (siehe dortiger Docstring)."""
    try:
        team1 = await get_pool_team(team1_id)
        team2 = await get_pool_team(team2_id)
        expected = (score1, score2) if score1 is not None and score2 is not None else None
        ea_match = await try_fetch_ea_full_match(team1, team2, expected_score=expected)
        if not ea_match:
            return

        players_by_club = ea_match.get("players", {})
        rows = []
        for team_row, team_id in ((team1, team1_id), (team2, team2_id)):
            club_players = players_by_club.get(str(team_row.get("ea_club_id")))
            if not club_players:
                continue
            for player_id, p in club_players.items():
                rating = _safe_float(p.get("rating"))
                if rating == EA_DISCONNECT_RATING:
                    # Fester EA-Straf-Wert fuer Spieler, die das Match verlassen haben - nicht
                    # repraesentativ fuer die tatsaechliche Leistung, wuerde den Durchschnitt
                    # verfaelschen. Lieber ganz auslassen als mitzaehlen.
                    continue
                name = p.get("playername") or p.get("proName") or f"Player {player_id}"
                raw_pos = p.get("position") or p.get("pos") or ""
                rows.append((
                    match_id, team_id, name,
                    _safe_int(p.get("goals")), _safe_int(p.get("assists")),
                    rating, _safe_int(p.get("mom")), _safe_int(p.get("saves")),
                    _position_group(raw_pos),
                ))
        if not rows:
            return

        pool = get_pool()
        await pool.executemany(
            """
            INSERT INTO match_player_stats
                (match_id, team_id, player_name, goals, assists, rating, mom, saves, position_group)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (match_id, team_id, player_name) DO UPDATE SET
                goals = EXCLUDED.goals, assists = EXCLUDED.assists, rating = EXCLUDED.rating,
                mom = EXCLUDED.mom, saves = EXCLUDED.saves, position_group = EXCLUDED.position_group
            """,
            rows,
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception(f"Fehler beim Sichern der Match-Spielerdaten fuer Match {match_id}")


async def get_bracket_team_ids(tournament_id: int, bracket: str) -> list[int]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT DISTINCT team_id FROM (
            SELECT team1_id AS team_id FROM tournament_matches WHERE tournament_id = $1 AND phase = 'knockout' AND bracket = $2
            UNION
            SELECT team2_id AS team_id FROM tournament_matches WHERE tournament_id = $1 AND phase = 'knockout' AND bracket = $2
        ) sub WHERE team_id IS NOT NULL
        """,
        tournament_id, bracket,
    )
    return [r["team_id"] for r in rows]


async def get_matches_for_teams(tournament_id: int, team_ids: list[int]) -> list[dict]:
    """Alle abgeschlossenen Matches (Gruppe + KO) dieses Turniers, an denen eines der Teams beteiligt war."""
    if not team_ids:
        return []
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT * FROM tournament_matches
        WHERE tournament_id = $1 AND status = 'completed'
              AND team1_id IS NOT NULL AND team2_id IS NOT NULL
              AND (team1_id = ANY($2::int[]) OR team2_id = ANY($2::int[]))
        """,
        tournament_id, team_ids,
    )
    return [dict(r) for r in rows]


async def aggregate_bracket_stats(tournament_id: int, bracket: str) -> tuple[dict[str, PlayerAgg], int, int]:
    """
    Aggregiert die Spielerdaten aller Teams eines Brackets (Winner/Loser) ueber die komplette
    Turnierhistorie (Gruppenphase + KO) - aus den bereits pro Match gesicherten EA-Daten
    (match_player_stats, siehe capture_match_player_stats), NICHT durch einen erneuten
    Live-Abruf bei der EA-API. Gibt (Aggregation, gefundene_Matches, Matches_gesamt) zurueck.

    Frueher wurde hier live bei der EA-API nachgefragt UND dabei nach Team-Paar dedupliziert -
    das ging schief, sobald zwei Teams sich zweimal begegneten (z.B. Gruppenphase UND KO gegen
    denselben Gegner): die zweite Begegnung wurde faelschlich als "schon gesehenes Paar"
    uebersprungen, obwohl es ein komplett anderes Match war. Ueber match_player_stats (ein
    Eintrag pro echter Match-ID) kann das nicht mehr passieren.
    """
    pool = get_pool()
    team_ids = await get_bracket_team_ids(tournament_id, bracket)
    team_ids_set = set(team_ids)
    matches = await get_matches_for_teams(tournament_id, team_ids)

    agg: dict[str, PlayerAgg] = {}
    found_count = 0
    total_count = len(matches)

    for m in matches:
        rows = await pool.fetch("SELECT * FROM match_player_stats WHERE match_id = $1", m["id"])
        if not rows:
            continue
        found_count += 1

        for r in rows:
            team_id = r["team_id"]
            # Gruppenspiele sind immer bracket='winner' getaggt, auch wenn der Gegner
            # spaeter ins Loser-Bracket eingeteilt wird - hier den Gegner ausschliessen,
            # sonst tauchen Loser-Bracket-Spieler in der Winner-Bracket-Auswertung auf.
            if team_id not in team_ids_set:
                continue
            key = f"{team_id}:{r['player_name']}"
            if key not in agg:
                agg[key] = PlayerAgg(name=r["player_name"], team_id=team_id)
            entry = agg[key]
            entry.matches += 1
            entry.total_rating += float(r["rating"])
            entry.goals += r["goals"]
            entry.assists += r["assists"]
            entry.mom += r["mom"]
            entry.saves += r["saves"]
            entry.positions[r["position_group"]] += 1

    return agg, found_count, total_count


async def persist_player_stats(tournament_id: int, bracket: str, agg: dict[str, "PlayerAgg"]):
    """Schreibt die aggregierten Spielerdaten dauerhaft weg (fuer die Website-Statistikseite).
    UPSERT pro (Turnier, Bracket, Team, Spielername) - kein zusaetzlicher EA-API-Call noetig,
    nutzt nur die Daten, die aggregate_bracket_stats() ohnehin schon abgerufen hat."""
    if not agg:
        return
    pool = get_pool()
    rows = [
        (tournament_id, bracket, e.team_id, e.name, e.matches, e.goals, e.assists, e.mom, e.saves, round(e.avg_rating, 2), e.main_position)
        for e in agg.values()
    ]
    await pool.executemany(
        """
        INSERT INTO tournament_player_stats
            (tournament_id, bracket, team_id, player_name, matches, goals, assists, mom, saves, avg_rating, position_group)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (tournament_id, bracket, team_id, player_name) DO UPDATE SET
            matches = EXCLUDED.matches, goals = EXCLUDED.goals, assists = EXCLUDED.assists,
            mom = EXCLUDED.mom, saves = EXCLUDED.saves, avg_rating = EXCLUDED.avg_rating,
            position_group = EXCLUDED.position_group, updated_at = now()
        """,
        rows,
    )


def compute_awards(agg: dict[str, PlayerAgg]) -> dict[str, PlayerAgg]:
    players = list(agg.values())
    if not players:
        return {}
    awards = {}
    awards["Bester Spieler"] = max(players, key=lambda p: p.score)
    scorers = [p for p in players if p.goals > 0]
    if scorers:
        awards["Bester Torschütze"] = max(scorers, key=lambda p: p.goals)
    assisters = [p for p in players if p.assists > 0]
    if assisters:
        awards["Bester Aufleger"] = max(assisters, key=lambda p: p.assists)
    defenders = [p for p in players if p.main_position == "DEF"]
    if defenders:
        awards["Bester Verteidiger"] = max(defenders, key=lambda p: p.score)
    keepers = [p for p in players if p.main_position == "GK"]
    if keepers:
        awards["Goldener Handschuh"] = max(keepers, key=lambda p: p.score)
    return awards


def compute_top11(agg: dict[str, PlayerAgg]) -> dict[str, list[PlayerAgg]]:
    by_group: dict[str, list[PlayerAgg]] = defaultdict(list)
    for p in agg.values():
        by_group[p.main_position].append(p)
    for group in by_group:
        by_group[group].sort(key=lambda p: p.score, reverse=True)

    result: dict[str, list[PlayerAgg]] = {}
    used = set()
    for group, count in TOP11_FORMATION.items():
        picks = []
        for p in by_group.get(group, []):
            if id(p) in used:
                continue
            picks.append(p)
            used.add(id(p))
            if len(picks) == count:
                break
        result[group] = picks
    return result


async def build_awards_text(bracket: str, awards: dict[str, PlayerAgg]) -> str:
    """Vollstaendige Auflistung aller Awards als lesbarer Text (fuer die Components-V2-Nachricht neben der Grafik)."""
    if not awards:
        return "_Keine EA-Match-Daten gefunden._"
    team_names = await team_name_map([p.team_id for p in awards.values()])
    metric_texts = {
        "Bester Spieler": lambda p: f"⌀ {p.score:.2f}",
        "Bester Torschütze": lambda p: f"{p.goals} Tore",
        "Bester Aufleger": lambda p: f"{p.assists} Vorlagen",
        "Bester Verteidiger": lambda p: f"⌀ {p.score:.2f}",
        "Goldener Handschuh": lambda p: f"⌀ {p.score:.2f}",
    }
    lines = []
    for award_name, p in awards.items():
        lines.append(f"**{award_name}:** {p.name} ({team_names.get(p.team_id, '?')}) — {metric_texts[award_name](p)}")
    return "\n".join(lines)


async def build_top11_text(top11: dict[str, list[PlayerAgg]]) -> str:
    """Vollstaendige Top-11-Aufstellung als lesbarer Text (fuer die Components-V2-Nachricht neben der Grafik)."""
    all_players = [p for group in top11.values() for p in group]
    if not all_players:
        return "_Keine EA-Match-Daten gefunden._"
    team_names = await team_name_map([p.team_id for p in all_players])
    group_labels = {"GK": "Torwart", "DEF": "Verteidigung", "MID": "Mittelfeld", "FWD": "Sturm"}
    blocks = []
    for group in ["GK", "DEF", "MID", "FWD"]:
        players = top11.get(group, [])
        if not players:
            continue
        names = "\n".join(f"{p.name} ({team_names.get(p.team_id, '?')}) — ⌀ {p.score:.2f}" for p in players)
        blocks.append(f"**{group_labels[group]}**\n{names}")
    return "\n\n".join(blocks)


async def build_awards_embed(tournament_id: int, bracket: str, awards: dict[str, PlayerAgg]) -> discord.Embed:
    t = await get_tournament(tournament_id)
    label = "Winner Bracket" if bracket == "winner" else "Loser Bracket"
    embed = discord.Embed(title=f"🏅 Turnier-Awards — {label}", description=t["name"], color=discord.Color.gold())
    if not awards:
        embed.description += "\n\nKeine EA-Match-Daten gefunden."
        return embed

    team_names = await team_name_map([p.team_id for p in awards.values()])
    metric_texts = {
        "Bester Spieler": lambda p: f"⌀ {p.score:.2f}",
        "Bester Torschütze": lambda p: f"{p.goals} Tore",
        "Bester Aufleger": lambda p: f"{p.assists} Vorlagen",
        "Bester Verteidiger": lambda p: f"⌀ {p.score:.2f}",
        "Goldener Handschuh": lambda p: f"⌀ {p.score:.2f}",
    }
    for award_name, p in awards.items():
        embed.add_field(
            name=award_name,
            value=f"**{p.name}** ({team_names.get(p.team_id, '?')})\n{metric_texts[award_name](p)}",
            inline=True,
        )
    return embed


async def build_top11_embed(tournament_id: int, bracket: str, top11: dict[str, list[PlayerAgg]]) -> discord.Embed:
    t = await get_tournament(tournament_id)
    label = "Winner Bracket" if bracket == "winner" else "Loser Bracket"
    embed = discord.Embed(
        title=f"⭐ Team des Turniers — {label}", description=f"{t['name']}\nFormation: 3-5-2", color=discord.Color.gold()
    )
    all_players = [p for group in top11.values() for p in group]
    if not all_players:
        embed.description += "\n\nKeine EA-Match-Daten gefunden."
        return embed

    team_names = await team_name_map([p.team_id for p in all_players])
    group_labels = {"GK": "Torwart", "DEF": "Verteidigung", "MID": "Mittelfeld", "FWD": "Sturm"}
    for group in ["GK", "DEF", "MID", "FWD"]:
        players = top11.get(group, [])
        if not players:
            continue
        value = "\n".join(f"{p.name} ({team_names.get(p.team_id, '?')}) — ⌀ {p.score:.2f}" for p in players)
        embed.add_field(name=group_labels[group], value=value, inline=False)
    return embed


async def team_logo_map(team_ids: list[int]) -> dict[int, str | None]:
    ids = [i for i in team_ids if i is not None]
    if not ids:
        return {}
    pool = get_pool()
    rows = await pool.fetch("SELECT id, logo_url FROM teams WHERE id = ANY($1::int[])", ids)
    return {r["id"]: r["logo_url"] for r in rows}


async def build_awards_image(tournament_id: int, bracket: str, awards: dict[str, PlayerAgg]) -> io.BytesIO | None:
    if not awards:
        return None
    t = await get_tournament(tournament_id)
    label = "Winner Bracket" if bracket == "winner" else "Loser Bracket"
    team_names = await team_name_map([p.team_id for p in awards.values()])
    team_logos = await team_logo_map([p.team_id for p in awards.values()])
    metric_texts = {
        "Bester Spieler": lambda p: f"⌀ {p.score:.2f}",
        "Bester Torschütze": lambda p: f"{p.goals} Tore",
        "Bester Aufleger": lambda p: f"{p.assists} Vorlagen",
        "Bester Verteidiger": lambda p: f"⌀ {p.score:.2f}",
        "Goldener Handschuh": lambda p: f"⌀ {p.score:.2f}",
    }
    entries = [
        (award_name, p.name, team_names.get(p.team_id, "?"), metric_texts[award_name](p), team_logos.get(p.team_id))
        for award_name, p in awards.items()
    ]
    from graphics import render_awards_image
    return await render_awards_image(f"Turnier-Awards — {label}", t["name"], entries)


async def build_top11_image(tournament_id: int, bracket: str, top11: dict[str, list[PlayerAgg]]) -> io.BytesIO | None:
    all_players = [p for group in top11.values() for p in group]
    if not all_players:
        return None
    t = await get_tournament(tournament_id)
    label = "Winner Bracket" if bracket == "winner" else "Loser Bracket"
    team_names = await team_name_map([p.team_id for p in all_players])
    team_logos = await team_logo_map([p.team_id for p in all_players])
    formation_slots = {
        group: [(p.name, team_names.get(p.team_id, "?"), team_logos.get(p.team_id)) for p in players]
        for group, players in top11.items()
    }
    from graphics import render_top11_image
    return await render_top11_image(f"Team des Turniers — {label}", f"{t['name']} · Formation 3-5-2", formation_slots)


def build_stat_image_view(title_text: str, file_obj: discord.File, body_text: str | None = None) -> discord.ui.LayoutView:
    """Bettet eine generierte Grafik (Podium/Awards/Top11) sauber in Components V2 ein, MIT voller Textauflistung
    (nicht nur ein Titel) - der komplette Inhalt (Namen, Teams, Werte) steht so auch als durchsuchbarer Text da,
    nicht nur in der Grafik."""
    view = discord.ui.LayoutView(timeout=None)
    items = [discord.ui.TextDisplay(title_text)]
    if body_text:
        items.append(discord.ui.Separator())
        items.append(discord.ui.TextDisplay(body_text))
    items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(media=f"attachment://{file_obj.filename}")))
    items.append(discord.ui.ActionRow(
        discord.ui.Button(label="🌐 Mehr Statistiken auf der Website", style=discord.ButtonStyle.link, url=f"{WEBSITE_URL}/stats"),
    ))
    view.add_item(discord.ui.Container(*items, accent_color=discord.Color.gold()))
    return view


async def get_guild_settings(guild_id: int) -> dict:
    pool = get_pool()
    row = await pool.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", guild_id)
    return dict(row) if row else {}


async def post_bracket_stats(bot: commands.Bot, guild: discord.Guild, tournament_id: int, bracket: str) -> str:
    """Fuehrt die komplette Stats-Pipeline aus und postet in die konfigurierten Kanaele. Gibt Status-Text zurueck."""
    settings = await get_guild_settings(guild.id)
    top3_channel_id = settings.get("winner_top3_channel_id") if bracket == "winner" else settings.get("loser_top3_channel_id")
    awards_channel_id = settings.get("awards_channel_id")
    top11_channel_id = settings.get("top11_channel_id")

    if not (top3_channel_id and awards_channel_id and top11_channel_id):
        return "Nicht alle Stats-Kanäle sind konfiguriert. Bitte erst im Admin-Panel unter 'Stats-Kanäle einstellen' festlegen."

    pool = get_pool()
    t = await get_tournament(tournament_id)
    champion_field = "winner_champion_id" if bracket == "winner" else "loser_champion_id"
    row = await pool.fetchrow(f"SELECT {champion_field} AS champ FROM tournaments WHERE id = $1", tournament_id)
    champion_id = row["champ"] if row else None
    if not champion_id:
        return "Dieses Bracket hat noch keinen Sieger (noch nicht beendet)."

    async def get_ch(cid):
        ch = guild.get_channel(cid)
        if ch is None:
            try:
                ch = await guild.fetch_channel(cid)
            except discord.HTTPException:
                return None
        return ch

    top3_channel = await get_ch(top3_channel_id)
    awards_channel = await get_ch(awards_channel_id)
    top11_channel = await get_ch(top11_channel_id)

    bracket_label = "Winner Bracket" if bracket == "winner" else "Loser Bracket"

    if top3_channel:
        podium_file = await build_bracket_finish_file(tournament_id, champion_id, bracket)
        podium_text = await build_bracket_finish_text(tournament_id, champion_id, bracket)
        await top3_channel.send(
            view=build_stat_image_view(f"# 🏆 {bracket_label} Champion\n{t['name']}", podium_file, podium_text),
            files=[podium_file],
        )

    agg, found_count, total_count = await aggregate_bracket_stats(tournament_id, bracket)
    await persist_player_stats(tournament_id, bracket, agg)
    awards = compute_awards(agg)
    top11 = compute_top11(agg)

    if awards_channel:
        image = await build_awards_image(tournament_id, bracket, awards)
        if image:
            awards_file = discord.File(image, filename="awards.png")
            awards_text = await build_awards_text(bracket, awards)
            await awards_channel.send(
                view=build_stat_image_view(f"# 🏅 Turnier-Awards — {bracket_label}\n{t['name']}", awards_file, awards_text),
                files=[awards_file],
            )
        else:
            await awards_channel.send(embed=await build_awards_embed(tournament_id, bracket, awards))
    if top11_channel:
        image = await build_top11_image(tournament_id, bracket, top11)
        if image:
            top11_file = discord.File(image, filename="top11.png")
            top11_text = await build_top11_text(top11)
            await top11_channel.send(
                view=build_stat_image_view(f"# ⭐ Team des Turniers — {bracket_label}\n{t['name']} · Formation 3-5-2", top11_file, top11_text),
                files=[top11_file],
            )
        else:
            await top11_channel.send(embed=await build_top11_embed(tournament_id, bracket, top11))

    coverage = f"{found_count}/{total_count} Spiele mit EA-Daten gefunden"
    if not agg:
        return f"Top3 gepostet, aber keine EA-Match-Daten für Awards/Top-11 gefunden ({coverage})."
    return f"✅ Statistiken gepostet ({len(agg)} Spieler erfasst, {coverage})."


class StatsChannelsConfigView(discord.ui.View):
    """Ein View mit bis zu 5 Kanal-Auswahlmenüs auf einmal (Discord-Limit pro Nachricht)."""

    ALL_FIELDS = [
        ("winner_top3_channel_id", "Winner-Top3"),
        ("loser_top3_channel_id", "Loser-Top3"),
        ("awards_channel_id", "Awards"),
        ("top11_channel_id", "Top-11"),
        ("bans_log_channel_id", "Sperren-Log"),
        ("live_schedule_channel_id", "Live-Spielplan"),
        ("logo_storage_channel_id", "Logo-Speicher"),
        ("stream_list_channel_id", "Stream-Übersicht"),
    ]

    def __init__(self, guild_id: int, fields=None):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.fields = fields or self.ALL_FIELDS[:5]
        for field_name, label in self.fields:
            select = discord.ui.ChannelSelect(
                placeholder=f"Kanal für '{label}' wählen...",
                channel_types=[discord.ChannelType.text],
            )
            select.callback = self._make_callback(field_name, label)
            self.add_item(select)

    def _make_callback(self, field_name: str, label: str):
        async def callback(interaction: discord.Interaction):
            channel_id = int(interaction.data["values"][0])
            pool = get_pool()
            await pool.execute(
                f"""
                INSERT INTO guild_settings (guild_id, {field_name}) VALUES ($1, $2)
                ON CONFLICT (guild_id) DO UPDATE SET {field_name} = $2
                """,
                self.guild_id, channel_id,
            )
            await interaction.response.send_message(view=success_embed(f"{label}-Kanal gesetzt", f"<#{channel_id}>"), ephemeral=True)
        return callback


class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot


async def setup(bot: commands.Bot):
    await bot.add_cog(StatsCog(bot))
