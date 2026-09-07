"""
Öffentliche Info-Befehle - kein Admin nötig. Bringt die wichtigsten
Website-Funktionen (Formkurve, Vergleich, Hall of Fame, Rangliste,
Topscorer) auch direkt in den Discord-Chat, ohne dass man dafür die
Website öffnen muss.
"""
from __future__ import annotations
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import logging
import os

import discord
from discord import app_commands
from discord.ext import commands, tasks

from db import get_pool
from ui_helpers import error_embed, info_embed, WEBSITE_URL

log = logging.getLogger("fifa-elite-cup")
WEBSITE_BANNER_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "website_banner.jpg")

BERLIN_TZ = ZoneInfo("Europe/Berlin")
EVENT_EMOJI = {"cup": "🏆", "cash_cup": "💰", "t_cup": "🔥", "special_cup": "👑", "league": "⚽", "sonstiges": "📌"}


async def _team_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT id, name FROM teams WHERE guild_id = $1 AND name ILIKE $2 ORDER BY name LIMIT 20",
        interaction.guild_id, f"%{current}%",
    )
    return [app_commands.Choice(name=r["name"], value=str(r["id"])) for r in rows]


class PublicCommandsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="website", description="Link zur FIFA Elite Cup Website")
    async def website(self, interaction: discord.Interaction):
        banner_file = discord.File(WEBSITE_BANNER_PATH, filename="website_banner.jpg")
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://website_banner.jpg")),
            discord.ui.TextDisplay(f"### 🌐 FIFA Elite Cup Website\n{WEBSITE_URL} — Turniere, Teams, Statistiken, Kalender, Hall of Fame."),
            accent_color=discord.Color.gold(),
        ))
        await interaction.response.send_message(view=view, file=banner_file)

    @app_commands.command(name="naechstes_event", description="Zeigt den nächsten anstehenden Termin")
    async def naechstes_event(self, interaction: discord.Interaction):
        pool = get_pool()
        row = await pool.fetchrow(
            "SELECT * FROM calendar_events WHERE guild_id = $1 AND start_time > now() ORDER BY start_time ASC LIMIT 1",
            interaction.guild_id,
        )
        if not row:
            await interaction.response.send_message(view=info_embed("Aktuell ist kein Termin geplant."))
            return
        emoji = EVENT_EMOJI.get(row["event_type"], "📌")
        ts = int(row["start_time"].timestamp())
        detail = f"<t:{ts}:F> · <t:{ts}:R>"
        if row["description"]:
            detail += f"\n{row['description']}"
        await interaction.response.send_message(view=info_embed(f"{emoji} {row['title']}", detail))

    @app_commands.command(name="team_form", description="Letzte 5 Ergebnisse eines Teams")
    @app_commands.autocomplete(team=_team_autocomplete)
    async def team_form(self, interaction: discord.Interaction, team: str):
        pool = get_pool()
        try:
            team_id = int(team)
        except ValueError:
            await interaction.response.send_message(view=error_embed("Bitte ein Team aus der Vorschlagsliste wählen."), ephemeral=True)
            return
        team_row = await pool.fetchrow("SELECT name FROM teams WHERE id = $1 AND guild_id = $2", team_id, interaction.guild_id)
        if not team_row:
            await interaction.response.send_message(view=error_embed("Team nicht gefunden."), ephemeral=True)
            return
        rows = await pool.fetch(
            """
            SELECT winner_id FROM tournament_matches
            WHERE status = 'completed' AND (team1_id = $1 OR team2_id = $1)
            ORDER BY id DESC LIMIT 5
            """,
            team_id,
        )
        if not rows:
            await interaction.response.send_message(view=info_embed(f"{team_row['name']} hat noch keine gespielten Matches."))
            return
        symbols = {"W": "🟢", "D": "⚪", "L": "🔴"}
        form = []
        for r in reversed(rows):
            if r["winner_id"] is None:
                form.append("D")
            elif r["winner_id"] == team_id:
                form.append("W")
            else:
                form.append("L")
        line = " ".join(symbols[f] for f in form)
        await interaction.response.send_message(view=info_embed(f"Form von {team_row['name']}", line))

    @app_commands.command(name="topscorer", description="Top 5 Torschützen über alle Turniere")
    async def topscorer(self, interaction: discord.Interaction):
        pool = get_pool()
        rows = await pool.fetch(
            """
            SELECT tps.player_name, te.name AS team_name, SUM(tps.goals) AS goals
            FROM tournament_player_stats tps
            JOIN teams te ON te.id = tps.team_id
            WHERE te.guild_id = $1
            GROUP BY tps.player_name, te.name
            ORDER BY goals DESC LIMIT 5
            """,
            interaction.guild_id,
        )
        if not rows:
            await interaction.response.send_message(view=info_embed("Noch keine Spielerdaten vorhanden."))
            return
        lines = [f"`{i}.` **{r['player_name']}** ({r['team_name']}) — {r['goals']} Tore" for i, r in enumerate(rows, start=1)]
        await interaction.response.send_message(
            view=info_embed("⚽ Top-Torschützen", "\n".join(lines) + f"\n\n-# Mehr auf {WEBSITE_URL}/stats")
        )

    @app_commands.command(name="rangliste", description="All-Time-Tabelle - Top 10 Teams über alle Turniere")
    async def rangliste(self, interaction: discord.Interaction):
        pool = get_pool()
        rows = await pool.fetch(
            """
            SELECT te.id, te.name,
                COUNT(*) FILTER (WHERE tm.winner_id = te.id) AS wins,
                COUNT(*) FILTER (WHERE tm.winner_id IS NULL) AS draws,
                COUNT(*) FILTER (WHERE tm.winner_id IS NOT NULL AND tm.winner_id != te.id) AS losses
            FROM teams te
            JOIN tournament_matches tm ON (tm.team1_id = te.id OR tm.team2_id = te.id) AND tm.status = 'completed'
            WHERE te.guild_id = $1
            GROUP BY te.id, te.name
            """,
            interaction.guild_id,
        )
        if not rows:
            await interaction.response.send_message(view=info_embed("Noch keine gespielten Matches vorhanden."))
            return
        ranked = sorted(rows, key=lambda r: r["wins"] * 3 + r["draws"], reverse=True)[:10]
        lines = [f"`{i}.` **{r['name']}** — {r['wins'] * 3 + r['draws']} Pkt ({r['wins']}S {r['draws']}U {r['losses']}N)" for i, r in enumerate(ranked, start=1)]
        await interaction.response.send_message(
            view=info_embed("📊 All-Time-Tabelle", "\n".join(lines) + f"\n\n-# Komplette Tabelle auf {WEBSITE_URL}/hall-of-fame")
        )

    @app_commands.command(name="hall_of_fame", description="Alle Turniersieger")
    async def hall_of_fame(self, interaction: discord.Interaction):
        pool = get_pool()
        rows = await pool.fetch(
            """
            SELECT t.name AS tournament_name, tw.name AS winner_name
            FROM tournaments t
            JOIN teams tw ON tw.id = t.winner_champion_id
            WHERE t.guild_id = $1 AND t.status = 'finished'
            ORDER BY t.start_time DESC NULLS LAST LIMIT 10
            """,
            interaction.guild_id,
        )
        if not rows:
            await interaction.response.send_message(view=info_embed("Noch kein Turnier beendet."))
            return
        lines = [f"🏆 **{r['winner_name']}** — {r['tournament_name']}" for r in rows]
        await interaction.response.send_message(
            view=info_embed("Hall of Fame", "\n".join(lines) + f"\n\n-# Alle Sieger & Rekorde auf {WEBSITE_URL}/hall-of-fame")
        )

    @app_commands.command(name="mein_team", description="Schnellübersicht zu deinem eigenen Team")
    async def mein_team(self, interaction: discord.Interaction):
        pool = get_pool()
        row = await pool.fetchrow(
            """
            SELECT t.id, t.name FROM teams t
            JOIN team_managers tm ON tm.team_id = t.id
            WHERE t.guild_id = $1 AND tm.discord_id = $2
            """,
            interaction.guild_id, interaction.user.id,
        )
        if not row:
            await interaction.response.send_message(
                view=error_embed("Du hast kein Team", "Siehe Team-Manager-Panel -> 'Team gründen'."), ephemeral=True
            )
            return
        stats = await pool.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE tm.winner_id = $1) AS wins,
                COUNT(*) FILTER (WHERE tm.winner_id IS NULL) AS draws,
                COUNT(*) FILTER (WHERE tm.winner_id IS NOT NULL AND tm.winner_id != $1) AS losses
            FROM tournament_matches tm
            WHERE (tm.team1_id = $1 OR tm.team2_id = $1) AND tm.status = 'completed'
            """,
            row["id"],
        )
        titles = await pool.fetchval("SELECT COUNT(*) FROM tournaments WHERE winner_champion_id = $1", row["id"])
        await interaction.response.send_message(
            view=info_embed(
                f"🧢 {row['name']}",
                f"{stats['wins']}S {stats['draws']}U {stats['losses']}N · {titles}× Titel\n"
                f"-# Mehr Details auf {WEBSITE_URL}/teams/{row['id']}",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="vergleich", description="Direkter Vergleich zwischen zwei Teams")
    @app_commands.autocomplete(team1=_team_autocomplete, team2=_team_autocomplete)
    async def vergleich(self, interaction: discord.Interaction, team1: str, team2: str):
        try:
            id1, id2 = int(team1), int(team2)
        except ValueError:
            await interaction.response.send_message(view=error_embed("Bitte Teams aus der Vorschlagsliste wählen."), ephemeral=True)
            return
        if id1 == id2:
            await interaction.response.send_message(view=error_embed("Bitte zwei unterschiedliche Teams wählen."), ephemeral=True)
            return

        pool = get_pool()
        names = await pool.fetch("SELECT id, name FROM teams WHERE id = ANY($1::int[])", [id1, id2])
        name_map = {r["id"]: r["name"] for r in names}
        if id1 not in name_map or id2 not in name_map:
            await interaction.response.send_message(view=error_embed("Team nicht gefunden."), ephemeral=True)
            return

        matches = await pool.fetch(
            """
            SELECT team1_id, team2_id, team1_score, team2_score, winner_id
            FROM tournament_matches
            WHERE status = 'completed' AND ((team1_id = $1 AND team2_id = $2) OR (team1_id = $2 AND team2_id = $1))
            """,
            id1, id2,
        )
        wins1 = sum(1 for m in matches if m["winner_id"] == id1)
        wins2 = sum(1 for m in matches if m["winner_id"] == id2)
        draws = len(matches) - wins1 - wins2

        detail = f"**{wins1}** Siege — **{draws}** Unentschieden — **{wins2}** Siege\n{len(matches)} direkte Duelle insgesamt"
        await interaction.response.send_message(
            view=info_embed(f"{name_map[id1]} vs. {name_map[id2]}", detail + f"\n\n-# Details auf {WEBSITE_URL}/vergleich?a={id1}&b={id2}")
        )

    @app_commands.command(name="rivalen", description="Der meistgespielte Gegner eines Teams")
    @app_commands.autocomplete(team=_team_autocomplete)
    async def rivalen(self, interaction: discord.Interaction, team: str):
        pool = get_pool()
        try:
            team_id = int(team)
        except ValueError:
            await interaction.response.send_message(view=error_embed("Bitte ein Team aus der Vorschlagsliste wählen."), ephemeral=True)
            return
        team_row = await pool.fetchrow("SELECT name FROM teams WHERE id = $1 AND guild_id = $2", team_id, interaction.guild_id)
        if not team_row:
            await interaction.response.send_message(view=error_embed("Team nicht gefunden."), ephemeral=True)
            return

        row = await pool.fetchrow(
            """
            SELECT
                CASE WHEN tm.team1_id = $1 THEN tm.team2_id ELSE tm.team1_id END AS opponent_id,
                COUNT(*) AS matches,
                COUNT(*) FILTER (WHERE tm.winner_id = $1) AS wins,
                COUNT(*) FILTER (WHERE tm.winner_id IS NOT NULL AND tm.winner_id != $1) AS losses,
                COUNT(*) FILTER (WHERE tm.winner_id IS NULL) AS draws
            FROM tournament_matches tm
            WHERE tm.status = 'completed' AND (tm.team1_id = $1 OR tm.team2_id = $1)
            GROUP BY opponent_id
            ORDER BY matches DESC LIMIT 1
            """,
            team_id,
        )
        if not row:
            await interaction.response.send_message(view=info_embed(f"{team_row['name']} hat noch keine gespielten Matches."))
            return
        opponent = await pool.fetchrow("SELECT name FROM teams WHERE id = $1", row["opponent_id"])
        await interaction.response.send_message(
            view=info_embed(
                f"🥊 Größter Rivale von {team_row['name']}",
                f"**{opponent['name']}** — {row['matches']} Duelle ({row['wins']}S {row['draws']}U {row['losses']}N)",
            )
        )

    @app_commands.command(name="bracket", description="KO-Phase des aktuellsten Turniers als Textbaum")
    async def bracket(self, interaction: discord.Interaction):
        pool = get_pool()
        t = await pool.fetchrow(
            """
            SELECT id, name FROM tournaments
            WHERE guild_id = $1 AND phase IN ('knockout', 'finished')
            ORDER BY created_at DESC LIMIT 1
            """,
            interaction.guild_id,
        )
        if not t:
            await interaction.response.send_message(view=info_embed("Aktuell läuft keine KO-Phase."))
            return

        rows = await pool.fetch(
            """
            SELECT tm.round, tm.bracket, tm.team1_score, tm.team2_score, tm.status,
                t1.name AS team1_name, t2.name AS team2_name
            FROM tournament_matches tm
            LEFT JOIN teams t1 ON t1.id = tm.team1_id
            LEFT JOIN teams t2 ON t2.id = tm.team2_id
            WHERE tm.tournament_id = $1 AND tm.phase = 'knockout' AND tm.is_third_place_match = false
            ORDER BY tm.bracket, tm.round
            """,
            t["id"],
        )
        if not rows:
            await interaction.response.send_message(view=info_embed("Für dieses Turnier gibt es noch keine KO-Matches."))
            return

        blocks = []
        for bracket_name in ("winner", "loser"):
            bracket_rows = [r for r in rows if r["bracket"] == bracket_name]
            if not bracket_rows:
                continue
            lines = [f"**{'Winner' if bracket_name == 'winner' else 'Loser'} Bracket**"]
            current_round = None
            for r in bracket_rows:
                if r["round"] != current_round:
                    current_round = r["round"]
                    lines.append(f"_Runde {current_round}_")
                score = f"{r['team1_score']}:{r['team2_score']}" if r["status"] == "completed" else "vs"
                lines.append(f"`{r['team1_name'] or 'Freilos'}` {score} `{r['team2_name'] or 'Freilos'}`")
            blocks.append("\n".join(lines))

        await interaction.response.send_message(
            view=info_embed(f"🏆 {t['name']}", "\n\n".join(blocks) + f"\n\n-# Visueller Baum auf {WEBSITE_URL}/turniere/{t['id']}")
        )

    @app_commands.command(name="matchtag", description="Offene Spiele des aktuell laufenden Turniers")
    async def matchtag(self, interaction: discord.Interaction):
        pool = get_pool()
        rows = await pool.fetch(
            """
            SELECT tm.round, tm.bracket, t1.name AS team1_name, t2.name AS team2_name, t.name AS tournament_name
            FROM tournament_matches tm
            JOIN tournaments t ON t.id = tm.tournament_id
            LEFT JOIN teams t1 ON t1.id = tm.team1_id
            LEFT JOIN teams t2 ON t2.id = tm.team2_id
            WHERE t.guild_id = $1 AND t.status = 'started' AND tm.status != 'completed'
                AND tm.team1_id IS NOT NULL AND tm.team2_id IS NOT NULL
            ORDER BY tm.round LIMIT 15
            """,
            interaction.guild_id,
        )
        if not rows:
            await interaction.response.send_message(view=info_embed("Aktuell stehen keine offenen Spiele an."))
            return
        lines = [f"⚽ **{r['team1_name']}** vs **{r['team2_name']}** _({r['tournament_name']})_" for r in rows]
        await interaction.response.send_message(view=info_embed("📅 Offene Spiele", "\n".join(lines)))


class WeeklyDigestCog(commands.Cog):
    """Postet jeden Montag eine kurze Wochenzusammenfassung (Topscorer, Ergebnisse) in den
    Live-Ergebnis-Kanal, falls einer konfiguriert ist - kein zusaetzlicher Kanal noetig."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self._digest_task.start()

    def cog_unload(self):
        self._digest_task.cancel()

    @tasks.loop(hours=24)
    async def _digest_task(self):
        now_berlin = datetime.now(BERLIN_TZ)
        if now_berlin.weekday() != 0:  # nur montags
            return
        pool = get_pool()
        guild_rows = await pool.fetch("SELECT DISTINCT guild_id FROM guild_settings WHERE results_feed_channel_id IS NOT NULL")
        for g in guild_rows:
            channel_row = await pool.fetchrow(
                "SELECT results_feed_channel_id FROM guild_settings WHERE guild_id = $1", g["guild_id"]
            )
            channel = self.bot.get_channel(channel_row["results_feed_channel_id"])
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_row["results_feed_channel_id"])
                except discord.HTTPException:
                    continue

            week_ago = datetime.now(timezone.utc)
            matches = await pool.fetchval(
                """
                SELECT COUNT(*) FROM tournament_matches tm
                JOIN teams te ON te.id = tm.team1_id
                WHERE te.guild_id = $1 AND tm.status = 'completed' AND tm.created_at > now() - interval '7 days'
                """,
                g["guild_id"],
            )
            top_scorer = await pool.fetchrow(
                """
                SELECT tps.player_name, SUM(tps.goals) AS goals
                FROM tournament_player_stats tps
                JOIN teams te ON te.id = tps.team_id
                WHERE te.guild_id = $1
                GROUP BY tps.player_name ORDER BY goals DESC LIMIT 1
                """,
                g["guild_id"],
            )
            if not matches:
                continue
            text = f"📊 **Wochenrückblick**\n{matches} Spiele in den letzten 7 Tagen."
            if top_scorer:
                text += f"\n⚽ Aktueller Topscorer: **{top_scorer['player_name']}** ({top_scorer['goals']} Tore)"
            text += f"\n-# Mehr auf {WEBSITE_URL}/stats"
            try:
                await channel.send(text)
            except discord.HTTPException:
                pass

    @_digest_task.before_loop
    async def _before_digest(self):
        await self.bot.wait_until_ready()


class BotStatusCog(commands.Cog):
    """Aktualisiert den Bot-Status ('Watching X Teams') periodisch - kleine, aber staendig
    sichtbare Erinnerung, dass der Bot laeuft, ohne dass jemand extra nachschauen muss."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self._status_task.start()

    def cog_unload(self):
        self._status_task.cancel()

    @tasks.loop(minutes=30)
    async def _status_task(self):
        pool = get_pool()
        team_count = await pool.fetchval("SELECT COUNT(*) FROM teams")
        try:
            await self.bot.change_presence(
                activity=discord.Activity(type=discord.ActivityType.watching, name=f"{team_count} Teams · fifaelite.de")
            )
        except discord.HTTPException:
            log.warning("Konnte Bot-Status nicht aktualisieren.")

    @_status_task.before_loop
    async def _before_status(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(PublicCommandsCog(bot))
    await bot.add_cog(WeeklyDigestCog(bot))
    await bot.add_cog(BotStatusCog(bot))
