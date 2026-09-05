"""
Gemeinsame Berechtigungspruefung: gilt als 'Admin' fuer dieses Turnier-System,
wer entweder echte Discord-Administrator-Rechte hat ODER Mitglied der ueber
das Admin-Panel konfigurierten Admin-Rolle ist.
"""
from __future__ import annotations
import discord

from db import get_pool


async def is_tournament_admin(member: discord.Member) -> bool:
    """Echte Discord-Administrator-Rechte ODER Mitglied der (einzelnen) admin_role_id ODER
    einer der admin_role_ids (mehrere gleichrangige Admin-Raenge, z.B. Head Moderator zusaetzlich
    zu Administrator/Verwaltung, die ueber echte Discord-Admin-Rechte laufen)."""
    if member.guild_permissions.administrator:
        return True
    pool = get_pool()
    row = await pool.fetchrow("SELECT admin_role_id, admin_role_ids FROM guild_settings WHERE guild_id = $1", member.guild.id)
    if not row:
        return False
    role_ids = {r.id for r in member.roles}
    if row["admin_role_id"] and row["admin_role_id"] in role_ids:
        return True
    return bool(row["admin_role_ids"]) and any(rid in role_ids for rid in row["admin_role_ids"])


async def is_tournament_moderator(member: discord.Member) -> bool:
    """Admin ODER die globale Moderator-Rolle(n) (darf Ergebnisse fuer alle Turniere/Gruppen
    melden/bestaetigen, ohne vollen Admin-Zugriff). mod_role_ids erlaubt mehrere gleichrangige
    Raenge (z.B. Trial Moderator + Moderator + Head Moderator)."""
    if await is_tournament_admin(member):
        return True
    pool = get_pool()
    row = await pool.fetchrow("SELECT mod_role_id, mod_role_ids FROM guild_settings WHERE guild_id = $1", member.guild.id)
    if not row:
        return False
    role_ids = {r.id for r in member.roles}
    if row["mod_role_id"] and row["mod_role_id"] in role_ids:
        return True
    return bool(row["mod_role_ids"]) and any(rid in role_ids for rid in row["mod_role_ids"])


async def can_correct_results(member: discord.Member) -> bool:
    """Admin ODER ein Rang mit Erlaubnis, bereits abgeschlossene Ergebnisse nachtraeglich zu
    korrigieren (z.B. Moderator + Head Moderator, aber NICHT Trial Moderator)."""
    if await is_tournament_admin(member):
        return True
    pool = get_pool()
    row = await pool.fetchrow("SELECT correct_role_ids FROM guild_settings WHERE guild_id = $1", member.guild.id)
    if not row or not row["correct_role_ids"]:
        return False
    role_ids = {r.id for r in member.roles}
    return any(rid in role_ids for rid in row["correct_role_ids"])


async def is_ticket_support(member: discord.Member) -> bool:
    """Admin ODER die separate Ticket-Support-Rolle ODER einer der Cup-Staff-Raenge (Trial
    Moderator und aufwaerts duerfen alle Tickets bearbeiten, die sie sehen koennen - welche
    Kategorien sie ueberhaupt sehen, steuert bereits die Kanal-Sichtbarkeit beim Erstellen)."""
    if await is_tournament_admin(member):
        return True
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT ticket_support_role_id, cup_staff_role_ids FROM guild_settings WHERE guild_id = $1", member.guild.id
    )
    if not row:
        return False
    role_ids = {r.id for r in member.roles}
    if row["ticket_support_role_id"] and row["ticket_support_role_id"] in role_ids:
        return True
    return bool(row["cup_staff_role_ids"]) and any(rid in role_ids for rid in row["cup_staff_role_ids"])
