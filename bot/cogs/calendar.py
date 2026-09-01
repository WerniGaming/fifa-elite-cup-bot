"""
Kalender-Cog: fuer alle sichtbarer, live aktualisierter Kanal mit kommenden
Terminen (Cups, Ligen, Sonstiges). Admins legen Termine an/loeschen sie ueber
das Admin-Panel. Neue Turniere werden automatisch als Kalender-Eintrag
verknuepft (kein doppeltes Pflegen von Datum noetig). Ein Hintergrund-Task
postet 24h vorher eine Erinnerung im selben Kanal.

Gleiches Grundmuster wie die Vereins-Uebersicht (team_manager.py): eigener
Kanal, Liste getrackter Nachrichten-IDs, komplett neu gepostet bei jeder
Aenderung (einfacher/robuster als Diff-Editing bei wechselnder Terminanzahl).
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from db import get_pool
from permissions import is_tournament_admin
from ui_helpers import success_embed, error_embed, info_embed

BERLIN_TZ = ZoneInfo("Europe/Berlin")

EVENT_TYPES = {
    "cup": ("🏆", "FIFA Elite Cup"),
    "cash_cup": ("💰", "FIFA Elite Cash Cup"),
    "t_cup": ("🔥", "FIFA Elite T-Cup"),
    "special_cup": ("👑", "FIFA Elite Spezial Cup"),
    "league": ("⚽", "FIFA Elite League"),
    "sonstiges": ("📌", "Sonstiges"),
}

MONTH_NAMES_DE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


async def create_event_for_tournament(guild_id: int, tournament_id: int, name: str, start_time, created_by: int):
    """Wird beim Anlegen eines neuen Turniers aufgerufen - erstellt automatisch einen verknuepften Kalendereintrag."""
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO calendar_events (guild_id, title, event_type, start_time, tournament_id, created_by)
        VALUES ($1, $2, 'cup', $3, $4, $5)
        """,
        guild_id, name, start_time, tournament_id, created_by,
    )


async def refresh_calendar(bot: commands.Bot, guild: discord.Guild):
    pool = get_pool()
    settings = await pool.fetchrow("SELECT * FROM calendar_panel WHERE guild_id = $1", guild.id)
    if not settings:
        return

    channel = guild.get_channel(settings["channel_id"])
    if channel is None:
        try:
            channel = await guild.fetch_channel(settings["channel_id"])
        except discord.HTTPException:
            return

    for old_id in settings["message_ids"] or []:
        try:
            old_msg = await channel.fetch_message(old_id)
            await old_msg.delete()
        except discord.HTTPException:
            pass

    events = await pool.fetch(
        "SELECT * FROM calendar_events WHERE guild_id = $1 AND start_time > now() ORDER BY start_time ASC",
        guild.id,
    )
    now_ts = int(discord.utils.utcnow().timestamp())

    if not events:
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# 🗓️ Kalender\n_Aktuell sind keine Termine geplant._\n\n-# Stand: <t:{now_ts}:R>"),
            accent_color=discord.Color.gold(),
        ))
        try:
            msg = await channel.send(view=view)
            await pool.execute("UPDATE calendar_panel SET message_ids = $1 WHERE guild_id = $2", [msg.id], guild.id)
        except discord.HTTPException:
            pass
        return

    pool_conn = pool
    tournament_ids = [e["tournament_id"] for e in events if e["tournament_id"]]
    tournament_channels: dict[int, tuple[int, int | None]] = {}
    if tournament_ids:
        t_rows = await pool_conn.fetch(
            "SELECT id, channel_id, message_id FROM tournaments WHERE id = ANY($1::int[])", tournament_ids
        )
        tournament_channels = {r["id"]: (r["channel_id"], r["message_id"]) for r in t_rows}

    # Alles in EINEM durchgehenden Block: Monatsüberschriften und Termine als
    # TextDisplay/Section-Zeilen mit Separatoren dazwischen, statt vieler einzelner
    # Container (die sehen als separate Karten aus wie eigene Nachrichten).
    items: list = [discord.ui.TextDisplay(
        "# 🗓️ FIFA Elite Eventkalender\n"
        "-# Alle kommenden Cups, Ligen & Termine der FIFA Elite Organisation"
    )]
    current_month = None

    for e in events:
        start_local = e["start_time"].astimezone(BERLIN_TZ)
        month_key = (start_local.year, start_local.month)
        items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.large if month_key != current_month else discord.SeparatorSpacing.small))
        if month_key != current_month:
            current_month = month_key
            items.append(discord.ui.TextDisplay(f"## {MONTH_NAMES_DE[start_local.month - 1]} {start_local.year}"))

        emoji, label = EVENT_TYPES.get(e["event_type"], EVENT_TYPES["sonstiges"])
        ts = int(e["start_time"].timestamp())
        text = f"### {emoji} {e['title']}\n<t:{ts}:F> · <t:{ts}:R>\n-# {label}"
        if e["description"]:
            text += f"\n> {e['description']}"

        jump_url = None
        if e["tournament_id"] and e["tournament_id"] in tournament_channels:
            chan_id, msg_id = tournament_channels[e["tournament_id"]]
            jump_url = f"https://discord.com/channels/{guild.id}/{chan_id}" + (f"/{msg_id}" if msg_id else "")

        if jump_url:
            items.append(discord.ui.Section(
                discord.ui.TextDisplay(text),
                accessory=discord.ui.Button(label="Zum Anmelde-Kanal", style=discord.ButtonStyle.link, url=jump_url),
            ))
        else:
            items.append(discord.ui.TextDisplay(text))

    items.append(discord.ui.Separator())
    items.append(discord.ui.TextDisplay(f"-# Stand: <t:{now_ts}:R> · Termine können angepasst oder erweitert werden."))

    # Components V2: max. 40 Top-Level-Komponenten pro Nachricht - bei sehr vielen Terminen auf mehrere Nachrichten aufteilen
    MAX_ITEMS_PER_MSG = 35
    new_message_ids = []
    for start in range(0, len(items), MAX_ITEMS_PER_MSG):
        chunk = items[start:start + MAX_ITEMS_PER_MSG]
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(*chunk, accent_color=discord.Color.gold()))
        try:
            msg = await channel.send(view=view)
            new_message_ids.append(msg.id)
        except discord.HTTPException:
            pass

    await pool.execute("UPDATE calendar_panel SET message_ids = $1 WHERE guild_id = $2", new_message_ids, guild.id)


class EventTypeSelectView(discord.ui.View):
    """Erster Schritt beim Termin erstellen: Typ waehlen, dann oeffnet sich das Modal."""

    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(
        placeholder="Terminart wählen...",
        options=[
            discord.SelectOption(label="FIFA Elite Cup", value="cup", emoji="🏆"),
            discord.SelectOption(label="FIFA Elite Cash Cup", value="cash_cup", emoji="💰"),
            discord.SelectOption(label="FIFA Elite T-Cup", value="t_cup", emoji="🔥"),
            discord.SelectOption(label="FIFA Elite Spezial Cup", value="special_cup", emoji="👑"),
            discord.SelectOption(label="FIFA Elite League", value="league", emoji="⚽"),
            discord.SelectOption(label="Sonstiges", value="sonstiges", emoji="📌", description="Alles andere"),
        ],
    )
    async def select_type(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.send_modal(EventCreateModal(select.values[0]))


class EventCreateModal(discord.ui.Modal, title="Termin erstellen"):
    def __init__(self, event_type: str):
        super().__init__()
        self.event_type = event_type

    title_input = discord.ui.TextInput(label="Titel", max_length=100)
    datum = discord.ui.TextInput(label="Datum (TT.MM.JJJJ HH:MM)", placeholder="04.09.2026 21:00", max_length=20)
    beschreibung = discord.ui.TextInput(
        label="Beschreibung (optional)", required=False, max_length=300, style=discord.TextStyle.paragraph
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            naive_dt = datetime.strptime(self.datum.value.strip(), "%d.%m.%Y %H:%M")
            start_time = naive_dt.replace(tzinfo=BERLIN_TZ)
        except ValueError:
            await interaction.response.send_message(
                view=error_embed("Ungültiges Datum", "Format muss sein: `TT.MM.JJJJ HH:MM`, z.B. `04.09.2026 21:00`"),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        pool = get_pool()
        await pool.execute(
            """
            INSERT INTO calendar_events (guild_id, title, event_type, description, start_time, created_by)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            interaction.guild_id, self.title_input.value, self.event_type,
            self.beschreibung.value or None, start_time, interaction.user.id,
        )
        from audit import log_action
        await log_action(interaction.guild_id, interaction.user, "calendar.event_created", "event", None, self.title_input.value)
        await refresh_calendar(interaction.client, interaction.guild)
        await interaction.followup.send(view=success_embed(f"Termin '{self.title_input.value}' angelegt."), ephemeral=True)


class EventDeleteSelect(discord.ui.View):
    def __init__(self, events: list[dict]):
        super().__init__(timeout=120)
        options = [
            discord.SelectOption(
                label=e["title"][:100],
                value=str(e["id"]),
                description=e["start_time"].astimezone(BERLIN_TZ).strftime("%d.%m.%Y %H:%M"),
                emoji=EVENT_TYPES.get(e["event_type"], EVENT_TYPES["sonstiges"])[0],
            )
            for e in events[:25]
        ]
        select = discord.ui.Select(placeholder="Termin zum Löschen wählen...", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        event_id = int(interaction.data["values"][0])
        pool = get_pool()
        row = await pool.fetchrow("DELETE FROM calendar_events WHERE id = $1 RETURNING title", event_id)
        from audit import log_action
        await log_action(interaction.guild_id, interaction.user, "calendar.event_deleted", "event", event_id, row["title"] if row else None)
        await refresh_calendar(interaction.client, interaction.guild)
        await interaction.response.edit_message(
            content=None, view=success_embed(f"Termin '{row['title'] if row else '?'}' gelöscht.")
        )


class CalendarChannelSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        select = discord.ui.ChannelSelect(placeholder="Kalender-Kanal wählen...", channel_types=[discord.ChannelType.text])
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        channel_id = int(interaction.data["values"][0])
        pool = get_pool()
        await pool.execute(
            "INSERT INTO calendar_panel (guild_id, channel_id, message_ids) VALUES ($1, $2, '{}') "
            "ON CONFLICT (guild_id) DO UPDATE SET channel_id = $2, message_ids = '{}'",
            interaction.guild_id, channel_id,
        )
        from audit import log_action
        await log_action(interaction.guild_id, interaction.user, "calendar.channel_set", "channel", channel_id)
        await interaction.response.edit_message(view=success_embed(f"Kalender-Kanal gesetzt", f"<#{channel_id}>"))
        await refresh_calendar(interaction.client, interaction.guild)


class CalendarMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Termin erstellen", style=discord.ButtonStyle.success)
    async def create_event(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            content="Welche Art von Termin?", view=EventTypeSelectView(), ephemeral=True
        )

    @discord.ui.button(label="Termin löschen", style=discord.ButtonStyle.danger)
    async def delete_event(self, interaction: discord.Interaction, button: discord.ui.Button):
        pool = get_pool()
        events = await pool.fetch(
            "SELECT * FROM calendar_events WHERE guild_id = $1 ORDER BY start_time DESC LIMIT 25",
            interaction.guild_id,
        )
        if not events:
            await interaction.response.send_message(view=info_embed("Keine Termine vorhanden."), ephemeral=True)
            return
        await interaction.response.send_message(
            content="Welchen Termin löschen?", view=EventDeleteSelect([dict(e) for e in events]), ephemeral=True
        )

    @discord.ui.button(label="Kalender-Kanal einstellen", style=discord.ButtonStyle.secondary)
    async def set_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            content="In welchem Kanal soll der Kalender gepostet werden?", view=CalendarChannelSelectView(), ephemeral=True
        )


class CalendarCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self._reminder_task.start()

    def cog_unload(self):
        self._reminder_task.cancel()

    @tasks.loop(minutes=30)
    async def _reminder_task(self):
        pool = get_pool()
        upcoming = await pool.fetch(
            """
            SELECT * FROM calendar_events
            WHERE reminder_sent = false AND start_time > now() AND start_time <= now() + interval '24 hours'
            """
        )
        for e in upcoming:
            panel = await pool.fetchrow("SELECT channel_id FROM calendar_panel WHERE guild_id = $1", e["guild_id"])
            if panel:
                channel = self.bot.get_channel(panel["channel_id"])
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(panel["channel_id"])
                    except discord.HTTPException:
                        channel = None
                if channel:
                    emoji, label = EVENT_TYPES.get(e["event_type"], EVENT_TYPES["sonstiges"])
                    ts = int(e["start_time"].timestamp())
                    try:
                        await channel.send(f"⏰ **Erinnerung:** {emoji} **{e['title']}** ({label}) startet <t:{ts}:R> — <t:{ts}:F>")
                    except discord.HTTPException:
                        pass
            await pool.execute("UPDATE calendar_events SET reminder_sent = true WHERE id = $1", e["id"])

    @_reminder_task.before_loop
    async def _before_reminder(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="calendar_setup", description="Richtet den Kalender-Kanal in diesem Kanal ein (Admin)")
    async def calendar_setup(self, interaction: discord.Interaction):
        if not await is_tournament_admin(interaction.user):
            await interaction.response.send_message(view=error_embed("Nur Admins können den Kalender einrichten."), ephemeral=True)
            return
        pool = get_pool()
        await pool.execute(
            "INSERT INTO calendar_panel (guild_id, channel_id, message_ids) VALUES ($1, $2, '{}') "
            "ON CONFLICT (guild_id) DO UPDATE SET channel_id = $2, message_ids = '{}'",
            interaction.guild_id, interaction.channel_id,
        )
        await interaction.response.send_message(view=success_embed("Kalender wird eingerichtet..."), ephemeral=True)
        await refresh_calendar(interaction.client, interaction.guild)


async def setup(bot: commands.Bot):
    await bot.add_cog(CalendarCog(bot))
