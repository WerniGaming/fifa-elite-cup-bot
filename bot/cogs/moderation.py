"""
Moderation-Cog: Sperren-System fuer Spieler UND Teams.

Admins waehlen ueber ein Dropdown-Menu (UserSelect fuer Spieler, eigenes
Team-Select fuer Teams) aus, wen sie sperren wollen, geben dann Grund +
Dauer per Popup ein. Gesperrte Spieler koennen keine Teams mehr erstellen
oder sich anmelden. Gesperrte Teams koennen sich nicht mehr fuer Turniere
anmelden (egal welcher Manager es versucht).
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from db import get_pool
from ui_helpers import success_embed, error_embed, info_embed, warning_embed
from cogs.team_manager import get_team_managers
from cogs.tournament_manager import reconcile_signups, refresh_panel


# ---------- Spieler-Sperren ----------

async def get_active_ban(guild_id: int, discord_id: int) -> dict | None:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM banned_users WHERE guild_id = $1 AND discord_id = $2", guild_id, discord_id
    )
    if not row:
        return None
    row = dict(row)
    if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
        await pool.execute("DELETE FROM banned_users WHERE id = $1", row["id"])
        return None
    return row


def format_ban_reason(ban: dict) -> str:
    reason = ban.get("reason") or "kein Grund angegeben"
    if ban.get("expires_at"):
        until = ban["expires_at"].strftime("%d.%m.%Y %H:%M")
        return f"Du bist bis zum {until} Uhr gesperrt. Grund: {reason}"
    return f"Du bist dauerhaft gesperrt. Grund: {reason}"


# ---------- Team-Sperren ----------

async def get_active_team_ban(guild_id: int, team_id: int) -> dict | None:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM banned_teams WHERE guild_id = $1 AND team_id = $2", guild_id, team_id
    )
    if not row:
        return None
    row = dict(row)
    if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
        await pool.execute("DELETE FROM banned_teams WHERE id = $1", row["id"])
        return None
    return row


def format_team_ban_reason(ban: dict, team_name: str) -> str:
    reason = ban.get("reason") or "kein Grund angegeben"
    if ban.get("expires_at"):
        until = ban["expires_at"].strftime("%d.%m.%Y %H:%M")
        return f"**{team_name}** ist bis zum {until} Uhr gesperrt. Grund: {reason}"
    return f"**{team_name}** ist dauerhaft gesperrt. Grund: {reason}"


# ---------- Gemeinsame Abfragen fuer die Verwaltung ----------

async def get_all_bans(guild_id: int) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT * FROM banned_users WHERE guild_id = $1 ORDER BY banned_at DESC", guild_id
    )
    return [dict(r) for r in rows]


async def get_all_team_bans(guild_id: int) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT bt.*, te.name AS team_name
        FROM banned_teams bt
        JOIN teams te ON te.id = bt.team_id
        WHERE bt.guild_id = $1
        ORDER BY bt.banned_at DESC
        """,
        guild_id,
    )
    return [dict(r) for r in rows]


async def get_all_guild_teams(guild_id: int) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch("SELECT id, name FROM teams WHERE guild_id = $1 ORDER BY name", guild_id)
    return [dict(r) for r in rows]


async def withdraw_team_from_open_tournaments(bot: commands.Bot, team_id: int, team_name: str) -> list[str]:
    """
    Zieht ein Team aus allen Turnieren zurueck, bei denen die Anmeldung noch
    offen ist (status='open'). Fuer bereits gestartete Turniere (Gruppen-/
    KO-Phase) wird NICHT automatisch eingegriffen - das muss ein Admin manuell
    regeln. Gibt eine Liste der Turniernamen zurueck, aus denen zurueckgezogen wurde.
    """
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT ts.id AS signup_id, t.id AS tournament_id, t.name AS tournament_name
        FROM tournament_signups ts
        JOIN tournaments t ON t.id = ts.tournament_id
        WHERE ts.team_id = $1 AND ts.status IN ('registered', 'waitlist') AND t.status = 'open'
        """,
        team_id,
    )
    affected_names = []
    for row in rows:
        await pool.execute("UPDATE tournament_signups SET status = 'withdrawn' WHERE id = $1", row["signup_id"])
        await reconcile_signups(row["tournament_id"])
        await refresh_panel(bot, row["tournament_id"])
        affected_names.append(row["tournament_name"])
    return affected_names


async def get_bans_log_channel(bot: commands.Bot, guild: discord.Guild) -> discord.abc.Messageable | None:
    pool = get_pool()
    row = await pool.fetchrow("SELECT bans_log_channel_id FROM guild_settings WHERE guild_id = $1", guild.id)
    if not row or not row["bans_log_channel_id"]:
        return None
    channel = guild.get_channel(row["bans_log_channel_id"])
    if channel is None:
        try:
            channel = await guild.fetch_channel(row["bans_log_channel_id"])
        except discord.HTTPException:
            return None
    return channel


# ---------- Grund/Dauer-Modal (fuer beide Sperr-Arten) ----------

class BanReasonModal(discord.ui.Modal):
    reason = discord.ui.TextInput(label="Grund", required=False, max_length=200)
    duration_days = discord.ui.TextInput(label="Dauer in Tagen (leer = dauerhaft)", required=False, max_length=5)

    def __init__(self, target_type: str, target_id: int, target_label: str):
        super().__init__(title=f"Sperren: {target_label}"[:45])
        self.target_type = target_type  # "user" oder "team"
        self.target_id = target_id
        self.target_label = target_label

    async def on_submit(self, interaction: discord.Interaction):
        expires_at = None
        if self.duration_days.value.strip():
            try:
                days = int(self.duration_days.value.strip())
                expires_at = datetime.now(timezone.utc) + timedelta(days=days)
            except ValueError:
                await interaction.response.send_message(view=error_embed("Dauer muss eine Zahl (Tage) sein."), ephemeral=True)
                return

        await interaction.response.defer(ephemeral=True, thinking=True)
        pool = get_pool()
        until_text = f"bis {expires_at.strftime('%d.%m.%Y %H:%M')}" if expires_at else "dauerhaft"

        if self.target_type == "user":
            await pool.execute(
                """
                INSERT INTO banned_users (guild_id, discord_id, reason, banned_by, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (guild_id, discord_id) DO UPDATE
                SET reason = $3, banned_by = $4, expires_at = $5, banned_at = now()
                """,
                interaction.guild_id, self.target_id, self.reason.value or None, interaction.user.id, expires_at,
            )

            try:
                user = await interaction.client.fetch_user(self.target_id)
                await user.send(
                    f"🚫 Du wurdest auf **{interaction.guild.name}** gesperrt ({until_text}). "
                    f"Grund: {self.reason.value or 'kein Grund angegeben'}"
                )
            except discord.HTTPException:
                pass

            # Falls der gesperrte Spieler ein Team fuehrt, dieses auch aus offenen Anmeldungen ziehen
            team_row = await pool.fetchrow(
                """
                SELECT t.id, t.name FROM teams t
                JOIN team_managers tm ON tm.team_id = t.id
                WHERE t.guild_id = $1 AND tm.discord_id = $2
                """,
                interaction.guild_id, self.target_id,
            )
            if team_row:
                await withdraw_team_from_open_tournaments(interaction.client, team_row["id"], team_row["name"])

        else:
            await pool.execute(
                """
                INSERT INTO banned_teams (guild_id, team_id, reason, banned_by, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (guild_id, team_id) DO UPDATE
                SET reason = $3, banned_by = $4, expires_at = $5, banned_at = now()
                """,
                interaction.guild_id, self.target_id, self.reason.value or None, interaction.user.id, expires_at,
            )

            managers = await get_team_managers(self.target_id)
            for m in managers:
                try:
                    user = await interaction.client.fetch_user(m["discord_id"])
                    await user.send(
                        f"🚫 Dein Team **{self.target_label}** wurde auf **{interaction.guild.name}** gesperrt "
                        f"({until_text}). Grund: {self.reason.value or 'kein Grund angegeben'}"
                    )
                except discord.HTTPException:
                    pass

            affected = await withdraw_team_from_open_tournaments(interaction.client, self.target_id, self.target_label)
            if affected:
                for m in managers:
                    try:
                        user = await interaction.client.fetch_user(m["discord_id"])
                        await user.send(
                            f"ℹ️ **{self.target_label}** wurde außerdem aus folgenden offenen Anmeldungen entfernt: "
                            + ", ".join(affected)
                        )
                    except discord.HTTPException:
                        pass

        log_channel = await get_bans_log_channel(interaction.client, interaction.guild)
        if log_channel:
            kind = "Spieler" if self.target_type == "user" else "Team"
            mention = f"<@{self.target_id}>" if self.target_type == "user" else f"**{self.target_label}**"
            await log_channel.send(
                f"🔨 **{kind} gesperrt:** {mention}\n"
                f"**Grund:** {self.reason.value or 'kein Grund angegeben'}\n"
                f"**Dauer:** {until_text}\n"
                f"**Von:** <@{interaction.user.id}>"
            )

        await interaction.followup.send(view=success_embed(f"{self.target_label} wurde gesperrt", f"Dauer: {until_text}"), ephemeral=True)


# ---------- Auswahl-Views ----------

class PlayerBanView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Spieler zum Sperren auswählen...")
    async def select_user(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        user = select.values[0]
        await interaction.response.send_modal(
            BanReasonModal(target_type="user", target_id=user.id, target_label=user.display_name)
        )


class TeamBanView(discord.ui.View):
    def __init__(self, teams: list[dict]):
        super().__init__(timeout=180)
        self.team_names = {t["id"]: t["name"] for t in teams}
        options = [discord.SelectOption(label=t["name"][:100], value=str(t["id"])) for t in teams[:25]]
        select = discord.ui.Select(placeholder="Team zum Sperren auswählen...", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        team_id = int(interaction.data["values"][0])
        team_name = self.team_names.get(team_id, f"Team {team_id}")
        await interaction.response.send_modal(
            BanReasonModal(target_type="team", target_id=team_id, target_label=team_name)
        )


class UnbanSelect(discord.ui.View):
    """Kombinierte Entsperr-Auswahl fuer Spieler UND Teams (Praefix im value)."""

    def __init__(self, user_bans: list[dict], team_bans: list[dict]):
        super().__init__(timeout=180)
        self.team_names = {b["team_id"]: b["team_name"] for b in team_bans}
        options = []
        for b in user_bans[:15]:
            options.append(discord.SelectOption(label=f"Spieler: {b['discord_id']}"[:100], value=f"user:{b['discord_id']}"))
        for b in team_bans[:10]:
            options.append(discord.SelectOption(label=f"Team: {b['team_name']}"[:100], value=f"team:{b['team_id']}"))
        if not options:
            options.append(discord.SelectOption(label="(niemand gesperrt)", value="none"))
        select = discord.ui.Select(placeholder="Wen/was entsperren?", options=options[:25])
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        value = interaction.data["values"][0]
        if value == "none":
            await interaction.response.send_message(view=info_embed("Niemand zu entsperren."), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        target_type, target_id_str = value.split(":", 1)
        target_id = int(target_id_str)
        pool = get_pool()

        if target_type == "user":
            await pool.execute(
                "DELETE FROM banned_users WHERE guild_id = $1 AND discord_id = $2", interaction.guild_id, target_id
            )
            try:
                user = await interaction.client.fetch_user(target_id)
                await user.send(f"✅ Du wurdest auf **{interaction.guild.name}** wieder entsperrt.")
            except discord.HTTPException:
                pass

            log_channel = await get_bans_log_channel(interaction.client, interaction.guild)
            if log_channel:
                await log_channel.send(f"✅ **Spieler entsperrt:** <@{target_id}> - von <@{interaction.user.id}>")

            await interaction.followup.send(view=success_embed(f"<@{target_id}> wurde entsperrt."), ephemeral=True)
        else:
            await pool.execute(
                "DELETE FROM banned_teams WHERE guild_id = $1 AND team_id = $2", interaction.guild_id, target_id
            )
            team_name = self.team_names.get(target_id, f"Team {target_id}")
            managers = await get_team_managers(target_id)
            for m in managers:
                try:
                    user = await interaction.client.fetch_user(m["discord_id"])
                    await user.send(f"✅ Dein Team **{team_name}** wurde auf **{interaction.guild.name}** wieder entsperrt.")
                except discord.HTTPException:
                    pass

            log_channel = await get_bans_log_channel(interaction.client, interaction.guild)
            if log_channel:
                await log_channel.send(f"✅ **Team entsperrt:** {team_name} - von <@{interaction.user.id}>")

            await interaction.followup.send(view=success_embed(f"{team_name} wurde entsperrt."), ephemeral=True)


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
