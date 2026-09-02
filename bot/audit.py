"""
Zentrales Audit-Log: zeichnet auf, wer wann was gemacht hat (Team-/Turnier-
Verwaltung, Bans, Kalender, Anmeldungen). Wird von den einzelnen Cogs bei
jeder relevanten Aktion aufgerufen. Sichtbar per /audit_log (Discord) und
im Website-Dashboard (gleiche Tabelle, Admins only).
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from db import get_pool

log = logging.getLogger("fifa-elite-cup")

_bot: commands.Bot | None = None


def set_bot(bot: commands.Bot):
    """Wird einmal beim Bot-Start aufgerufen (bot.py), damit log_action() Nachrichten
    posten kann, ohne dass jeder Aufrufer den Bot durchreichen muss."""
    global _bot
    _bot = bot

# Menschlich lesbare Labels fuer die Action-Codes - gemeinsam genutzt von
# der /audit_log-Ansicht im Discord und der Website.
ACTION_LABELS: dict[str, str] = {
    "team.created": "Team erstellt",
    "team.deleted": "Team gelöscht",
    "team.logo_updated": "Team-Logo aktualisiert",
    "team.renamed": "Team umbenannt",
    "tournament.created": "Turnier erstellt",
    "tournament.closed": "Anmeldung geschlossen",
    "tournament.deleted": "Turnier gelöscht",
    "tournament.bracket_created": "Bracket erstellt",
    "signup.registered": "Team angemeldet",
    "signup.withdrawn": "Team abgemeldet",
    "ban.user_added": "User gesperrt",
    "ban.user_removed": "User-Sperre aufgehoben",
    "ban.team_added": "Team gesperrt",
    "ban.team_removed": "Team-Sperre aufgehoben",
    "calendar.event_created": "Termin angelegt",
    "calendar.event_deleted": "Termin gelöscht",
    "calendar.channel_set": "Kalender-Kanal geändert",
    "match.result_reported": "Ergebnis gemeldet",
    "match.result_confirmed": "Ergebnis bestätigt",
}


async def log_action(
    guild_id: int,
    actor: discord.abc.User | discord.Member | None,
    action: str,
    target_type: str | None = None,
    target_id: str | int | None = None,
    details: str | None = None,
):
    """Schreibt einen Audit-Log-Eintrag. actor=None fuer System-Aktionen (z.B. automatisches Nachruecken)."""
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO audit_log (guild_id, actor_discord_id, actor_name, action, target_type, target_id, details)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        guild_id,
        actor.id if actor else None,
        str(actor) if actor else "System",
        action,
        target_type,
        str(target_id) if target_id is not None else None,
        details,
    )
    await _post_live(guild_id, actor, action, details)


async def _post_live(guild_id: int, actor: discord.abc.User | discord.Member | None, action: str, details: str | None):
    """Postet den Eintrag sofort in den konfigurierten Live-Log-Kanal, falls einer eingerichtet ist."""
    if _bot is None:
        return
    pool = get_pool()
    row = await pool.fetchrow("SELECT audit_log_channel_id FROM guild_settings WHERE guild_id = $1", guild_id)
    channel_id = row["audit_log_channel_id"] if row else None
    if not channel_id:
        return

    channel = _bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await _bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return

    label = ACTION_LABELS.get(action, action)
    who = actor.mention if actor else "*System*"
    ts = discord.utils.format_dt(discord.utils.utcnow(), style="T")
    text = f"`{ts}` **{label}** · {who}"
    if details:
        text += f" — {details}"

    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        log.warning(f"Konnte Audit-Log-Eintrag nicht in Kanal {channel_id} posten.")


async def get_recent_entries(guild_id: int, limit: int = 25, action_filter: str | None = None) -> list[dict]:
    pool = get_pool()
    if action_filter:
        rows = await pool.fetch(
            "SELECT * FROM audit_log WHERE guild_id = $1 AND action = $2 ORDER BY created_at DESC LIMIT $3",
            guild_id, action_filter, limit,
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM audit_log WHERE guild_id = $1 ORDER BY created_at DESC LIMIT $2",
            guild_id, limit,
        )
    return [dict(r) for r in rows]
