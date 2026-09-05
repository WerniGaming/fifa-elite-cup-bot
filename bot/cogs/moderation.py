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
import re

import discord
from discord import app_commands
from discord.ext import commands

from db import get_pool
from ui_helpers import success_embed, error_embed, info_embed, warning_embed
from permissions import is_tournament_admin
from cogs.team_manager import get_team_managers, get_team_for_user
from cogs.tournament_manager import reconcile_signups, refresh_panel
from audit import ACTION_LABELS

URL_PATTERN = re.compile(r"https?://\S+")


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
    rows = await pool.fetch(
        "SELECT id, name FROM teams WHERE guild_id = $1 AND dissolved_at IS NULL ORDER BY name", guild_id
    )
    return [dict(r) for r in rows]


async def search_guild_teams(guild_id: int, query: str, limit: int = 25) -> list[dict]:
    """Teamsuche per Teilstring - noetig weil ein Discord-Select maximal 25 Optionen zeigen
    kann und der Server inzwischen deutlich mehr Teams hat als das (vorher wurden Teams jenseits
    der ersten 25 beim Sperren stillschweigend gar nicht erst angezeigt)."""
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT id, name FROM teams WHERE guild_id = $1 AND dissolved_at IS NULL AND name ILIKE $2 ORDER BY name LIMIT $3",
        guild_id, f"%{query}%", limit,
    )
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
            from audit import log_action
            await log_action(interaction.guild_id, interaction.user, "ban.user_added", "user", self.target_id, self.reason.value or until_text)

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
            from audit import log_action
            await log_action(interaction.guild_id, interaction.user, "ban.team_added", "team", self.target_id, self.reason.value or until_text)

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


class TeamBanSearchModal(discord.ui.Modal, title="Team suchen"):
    """Erster Schritt vorm Sperren: Teamname (oder Teil davon) suchen - ein Discord-Select
    kann nur 25 Optionen zeigen, bei deutlich mehr Teams auf dem Server wurden vorher alle
    jenseits der ersten 25 (alphabetisch) beim Sperren gar nicht erst angezeigt."""
    query_input = discord.ui.TextInput(label="Teamname (auch Teil reicht)", max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        matches = await search_guild_teams(interaction.guild_id, self.query_input.value)
        if not matches:
            await interaction.response.send_message(
                view=error_embed(f"Kein Team gefunden für „{self.query_input.value}“."), ephemeral=True
            )
            return
        # Bewusst IMMER die Auswahl zeigen (auch bei nur einem Treffer) statt direkt ins naechste
        # Modal zu springen - ein Modal kann bei dieser discord.py-Version nicht zuverlaessig
        # direkt aus einem anderen Modal heraus geoeffnet werden (400 Invalid Form Body), ueber
        # einen Button/Select-Klick (normale Component-Interaction) funktioniert es dagegen.
        await interaction.response.send_message(
            content=f"{len(matches)} Treffer - welches Team?", view=TeamBanView(matches), ephemeral=True
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
            from audit import log_action
            await log_action(interaction.guild_id, interaction.user, "ban.user_removed", "user", target_id)
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
            from audit import log_action
            await log_action(interaction.guild_id, interaction.user, "ban.team_removed", "team", target_id)
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


# ---------- Audit-Log ----------

PAGE_SIZE = 15


def _format_entry(e: dict) -> str:
    label = ACTION_LABELS.get(e["action"], e["action"])
    ts = int(e["created_at"].timestamp())
    who = f"<@{e['actor_discord_id']}>" if e["actor_discord_id"] else "*System*"
    line = f"<t:{ts}:R> · **{label}** · {who}"
    if e["details"]:
        line += f" — {e['details']}"
    return line


class AuditLogView(discord.ui.View):
    def __init__(self, guild_id: int, offset: int = 0, action_filter: str | None = None):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.offset = offset
        self.action_filter = action_filter

    async def render(self) -> discord.ui.LayoutView:
        pool = get_pool()
        if self.action_filter:
            entries = await pool.fetch(
                "SELECT * FROM audit_log WHERE guild_id = $1 AND action = $2 ORDER BY created_at DESC OFFSET $3 LIMIT $4",
                self.guild_id, self.action_filter, self.offset, PAGE_SIZE,
            )
        else:
            entries = await pool.fetch(
                "SELECT * FROM audit_log WHERE guild_id = $1 ORDER BY created_at DESC OFFSET $2 LIMIT $3",
                self.guild_id, self.offset, PAGE_SIZE,
            )
        entries = [dict(e) for e in entries]

        filter_note = f" · Filter: {ACTION_LABELS.get(self.action_filter, self.action_filter)}" if self.action_filter else ""
        lines = "\n".join(_format_entry(e) for e in entries) if entries else "_Keine Einträge auf dieser Seite._"
        text = f"# 📋 Audit-Log\n-# Einträge {self.offset + 1}–{self.offset + len(entries)}{filter_note}\n\n{lines}"

        self.clear_items()
        prev_btn = discord.ui.Button(label="⬅️ Neuer", style=discord.ButtonStyle.secondary, disabled=self.offset == 0)
        next_btn = discord.ui.Button(label="Älter ➡️", style=discord.ButtonStyle.secondary, disabled=len(entries) < PAGE_SIZE)
        prev_btn.callback = self._make_nav(-PAGE_SIZE)
        next_btn.callback = self._make_nav(PAGE_SIZE)

        view = discord.ui.LayoutView(timeout=180)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(text),
            discord.ui.ActionRow(prev_btn, next_btn),
            accent_color=discord.Color.gold(),
        ))
        return view

    def _make_nav(self, delta: int):
        async def callback(interaction: discord.Interaction):
            self.offset = max(0, self.offset + delta)
            new_view = await self.render()
            await interaction.response.edit_message(view=new_view)
        return callback


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Kanal-Regeln, die technisch nur per on_message durchsetzbar sind (echte
        ephemeral-Antworten gehen bei normalen Kanal-Nachrichten nicht, nur bei
        Slash-Command-Interaktionen - daher ueberall die selbstloeschende Variante):
        - Spieler-Suche-Kanal: nur Vereinsmanager duerfen dort schreiben.
        - Medien-Kanal: nur Bilder/Videos, kein Text erlaubt."""
        if message.author.bot or not message.guild:
            return
        pool = get_pool()
        row = await pool.fetchrow(
            "SELECT player_search_channel_id, team_register_channel_id, media_only_channel_id "
            "FROM guild_settings WHERE guild_id = $1",
            message.guild.id,
        )
        if not row:
            return
        is_mod = message.author.guild_permissions.administrator or message.author.guild_permissions.manage_messages

        if row["player_search_channel_id"] and message.channel.id == row["player_search_channel_id"] and not is_mod:
            team = await get_team_for_user(message.guild.id, message.author.id)
            if team is None:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                register_hint = f"<#{row['team_register_channel_id']}>" if row["team_register_channel_id"] else "dem Team-Registrieren-Kanal"
                try:
                    await message.channel.send(
                        f"{message.author.mention} nur **Vereinsmanager** dürfen hier schreiben. "
                        f"Registriere zuerst dein Team in {register_hint}.",
                        delete_after=8,
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                except discord.HTTPException:
                    pass
            return

        if row["media_only_channel_id"] and message.channel.id == row["media_only_channel_id"] and not is_mod:
            content = message.content.strip()
            has_link = bool(URL_PATTERN.search(content))
            if content and not has_link:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                try:
                    await message.channel.send(
                        f"{message.author.mention} hier sind nur **Bilder, Videos & Links** erlaubt, kein reiner Text.",
                        delete_after=8,
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                except discord.HTTPException:
                    pass
            return

    @app_commands.command(name="audit_log", description="Zeigt das Audit-Log (wer hat wann was gemacht) - nur Admins")
    @app_commands.describe(aktion="Nur diese Art von Aktion anzeigen (optional)")
    @app_commands.choices(aktion=[
        app_commands.Choice(name=label, value=key) for key, label in ACTION_LABELS.items()
    ])
    async def audit_log(self, interaction: discord.Interaction, aktion: app_commands.Choice[str] | None = None):
        if not await is_tournament_admin(interaction.user):
            await interaction.response.send_message(view=error_embed("Nur Admins können das Audit-Log einsehen."), ephemeral=True)
            return
        pager = AuditLogView(interaction.guild_id, action_filter=aktion.value if aktion else None)
        view = await pager.render()
        await interaction.response.send_message(view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
