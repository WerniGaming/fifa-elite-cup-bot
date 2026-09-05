"""
Taeglicher Reset (04:00 Berlin-Zeit) fuer die beiden "Boersen"-Systeme
(Aushilfen + Freundschaftsspiele) - alle Eintraege werden komplett aus der
Datenbank geloescht und beide Panels danach leer neu aufgebaut, damit sie
nicht mit veralteten Angeboten/Anfragen vollmuellen.
"""
from __future__ import annotations
import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from db import get_pool
from cogs.substitutes import refresh_substitute_panel
from cogs.friendlies import refresh_friendly_panel

BERLIN_TZ = ZoneInfo("Europe/Berlin")
RESET_TIME = datetime.time(hour=4, minute=0, tzinfo=BERLIN_TZ)


class DailyResetCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self._reset_task.start()

    def cog_unload(self):
        self._reset_task.cancel()

    @tasks.loop(time=RESET_TIME)
    async def _reset_task(self):
        pool = get_pool()

        await pool.execute("DELETE FROM substitute_offer_candidates")
        await pool.execute("DELETE FROM substitute_request_candidates")
        await pool.execute("DELETE FROM substitute_offers")
        await pool.execute("DELETE FROM substitute_requests")

        await pool.execute("DELETE FROM friendly_candidates")
        await pool.execute("DELETE FROM friendly_slots")
        await pool.execute("DELETE FROM friendly_requests")

        rows = await pool.fetch(
            "SELECT guild_id, substitute_channel_id, friendly_channel_id FROM guild_settings "
            "WHERE substitute_channel_id IS NOT NULL OR friendly_channel_id IS NOT NULL"
        )
        for r in rows:
            guild = self.bot.get_guild(r["guild_id"])
            if guild is None:
                try:
                    guild = await self.bot.fetch_guild(r["guild_id"])
                except discord.HTTPException:
                    continue
            if r["substitute_channel_id"]:
                await refresh_substitute_panel(self.bot, guild)
            if r["friendly_channel_id"]:
                await refresh_friendly_panel(self.bot, guild)

    @_reset_task.before_loop
    async def _before_reset_task(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(DailyResetCog(bot))
