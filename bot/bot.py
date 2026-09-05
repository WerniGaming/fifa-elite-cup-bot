import os
import logging

import asyncpg
import discord
from discord.ext import commands
from dotenv import load_dotenv

import audit
import db

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fifa-elite-cup")

INITIAL_COGS = [
    "cogs.team_manager",
    "cogs.tournament_manager",
    "cogs.stats_manager",
    "cogs.moderation",
    "cogs.embed_builder",
    "cogs.tickets",
    "cogs.welcome",
    "cogs.admin_panel",
    "cogs.calendar",
    "cogs.public_commands",
    "cogs.rules",
    "cogs.feedback",
    "cogs.friendlies",
    "cogs.polls",
    "cogs.staff_overview",
]


class FifaEliteCupBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # nötig für das Logo-Upload per Chat-Nachricht
        intents.members = True  # noetig damit role.members zuverlaessig gefuellt ist (Staff-Uebersicht u.a.)
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await db.init_pool()
        log.info("Datenbank-Pool initialisiert.")
        audit.set_bot(self)

        for ext in INITIAL_COGS:
            await self.load_extension(ext)
            log.info(f"Cog geladen: {ext}")

        await self._start_signup_listener()

    async def _start_signup_listener(self):
        """Hoert auf pg_notify('tournament_signup_changed', 'tournament_id:team_id'), das die
        Website nach jeder An-/Abmeldung ueber die DB feuert (Postgres LISTEN/NOTIFY-Bruecke,
        vermeidet doppelte Discord-spezifische Logik in TypeScript). Braucht eine eigene,
        dauerhaft offene Connection - der Pool selbst unterstuetzt add_listener() nicht."""
        self._signup_listener_conn = await asyncpg.connect(dsn=os.environ["DATABASE_URL"])

        async def on_notify(connection, pid, channel, payload):
            try:
                tid_str, team_id_str = payload.split(":")
                tournament_id, team_id = int(tid_str), int(team_id_str)
            except ValueError:
                log.warning(f"Ungueltiges Signup-Notify-Payload: {payload!r}")
                return
            try:
                from cogs.tournament_manager import handle_external_signup_change
                await handle_external_signup_change(self, tournament_id, team_id)
            except Exception:
                log.exception(f"Fehler beim Verarbeiten der Website-Anmeldung fuer Turnier {tournament_id} / Team {team_id}")

        await self._signup_listener_conn.add_listener("tournament_signup_changed", on_notify)
        log.info("Postgres-Listener fuer Website-An-/Abmeldungen gestartet.")

        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info(f"Slash-Commands sofort fuer Guild {guild_id} synchronisiert.")
        await self.tree.sync()
        log.info("Slash-Commands global synchronisiert (Propagation kann dauern).")


bot = FifaEliteCupBot()


@bot.event
async def on_ready():
    log.info(f"Eingeloggt als {bot.user} (ID: {bot.user.id})")
    proxy_set = "ja" if os.getenv("EA_PROXY_URL") else "NEIN - EA-Calls werden vermutlich geblockt!"
    log.info(f"EA_PROXY_URL gesetzt: {proxy_set}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN fehlt in .env")
    if not os.getenv("DATABASE_URL"):
        raise SystemExit("DATABASE_URL fehlt in .env")
    if not os.getenv("EA_PROXY_URL"):
        log.warning("EA_PROXY_URL ist nicht gesetzt! EA-API-Calls werden wahrscheinlich geblockt.")
    bot.run(TOKEN)
