"""
Gemeinsame Berechtigungspruefung: gilt als 'Admin' fuer dieses Turnier-System,
wer entweder echte Discord-Administrator-Rechte hat ODER Mitglied der ueber
das Admin-Panel konfigurierten Admin-Rolle ist.
"""
from __future__ import annotations
import discord

from db import get_pool


async def is_tournament_admin(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    pool = get_pool()
    row = await pool.fetchrow("SELECT admin_role_id FROM guild_settings WHERE guild_id = $1", member.guild.id)
    if not row or not row["admin_role_id"]:
        return False
    return any(r.id == row["admin_role_id"] for r in member.roles)


async def is_ticket_support(member: discord.Member) -> bool:
    """Admin ODER die separate Ticket-Support-Rolle (z.B. fuer Moderatoren ohne vollen Admin-Zugriff)."""
    if await is_tournament_admin(member):
        return True
    pool = get_pool()
    row = await pool.fetchrow("SELECT ticket_support_role_id FROM guild_settings WHERE guild_id = $1", member.guild.id)
    if not row or not row["ticket_support_role_id"]:
        return False
    return any(r.id == row["ticket_support_role_id"] for r in member.roles)
