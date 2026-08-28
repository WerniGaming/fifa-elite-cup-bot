"""
Ticket-System.

Postet ein persistentes Panel (/ticket_panel_setup, Admin-only) mit einer
Kategorie-Auswahl. Beim Auswaehlen oeffnet sich ein kurzes Formular, danach
wird ein privater Kanal fuer den Nutzer + Support-Team angelegt. Im Ticket
selbst gibt es Buttons zum Uebernehmen und Schliessen (mit Transkript-Log).
"""
from __future__ import annotations
import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

from db import get_pool
from permissions import is_tournament_admin, is_ticket_support
from ui_helpers import success_embed, error_embed, info_embed, warning_embed

log = logging.getLogger("fifa-elite-cup")

TICKET_CATEGORIES = [
    ("frage", "❓ Allgemeine Frage", "Fragen zum Server, Ablauf oder allgemeine Anliegen"),
    ("turnier", "🏆 Turnier-Support", "Fragen zu laufenden Turnieren, Spielplänen oder Ergebnissen"),
    ("technisch", "🔧 Technisches Problem", "Bot funktioniert nicht, Fehler bei Anmeldung, etc."),
    ("team_problem", "⚔️ Spieler/Team melden", "Regelverstoß oder unsportliches Verhalten melden"),
    ("sperre", "🚫 Sperren-Einspruch", "Gegen eine Sperre Einspruch einlegen"),
    ("bewerbung", "📝 Bewerbung", "Bewirb dich fürs Team (Support/Moderation)"),
    ("sonstiges", "📋 Sonstiges", "Alles, was in keine andere Kategorie passt"),
]
CATEGORY_LABELS = {key: label for key, label, _ in TICKET_CATEGORIES}


class TicketPanel(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        text = (
            "# 🎫 SUPPORT\n"
            "Brauchst du Hilfe oder hast eine Frage? Öffne ein Ticket und unser Team kümmert sich "
            "schnellstmöglich um dein Anliegen.\n"
            "-----\n"
            "### » So funktioniert's\n"
            "› Kategorie unten auswählen\n"
            "› Anliegen kurz beschreiben\n"
            "› Optional: Screenshot dazu hochladen\n"
            "› Ein privater Kanal wird für dich erstellt, unser Team meldet sich dort\n"
            "-----\n"
            "### » Wichtig\n"
            "› Bitte nur **ein Ticket pro Anliegen** öffnen\n"
            "› Je genauer die Beschreibung, desto schneller können wir helfen\n"
            "› Bleib bitte respektvoll - wir helfen dir gerne!\n"
            "-# FIFA Elite Cup - Support"
        )
        select = discord.ui.Select(
            placeholder="Kategorie auswählen, um ein Ticket zu öffnen...",
            options=[
                discord.SelectOption(label=label, value=key, description=desc)
                for key, label, desc in TICKET_CATEGORIES
            ],
            custom_id="ticket:open_select",
        )
        container = discord.ui.Container(
            discord.ui.TextDisplay(text),
            discord.ui.ActionRow(select),
            accent_color=discord.Color.gold(),
        )
        self.add_item(container)


async def create_ticket_channel(
    interaction: discord.Interaction,
    category_key: str,
    description_text: str,
    screenshot_upload: discord.ui.FileUpload | None = None,
):
    """Gemeinsame Logik fuer JEDES Ticket-Formular: prueft Duplikate, legt Kanal + DB-Eintrag an, postet Intro."""
    pool = get_pool()

    existing = await pool.fetchrow(
        "SELECT * FROM tickets WHERE guild_id = $1 AND opener_discord_id = $2 AND status = 'open'",
        interaction.guild_id, interaction.user.id,
    )
    if existing:
        channel_mention = f"<#{existing['channel_id']}>" if existing["channel_id"] else "unbekannt"
        await interaction.followup.send(
            view=warning_embed("Du hast bereits ein offenes Ticket", f"Siehe {channel_mention}"), ephemeral=True
        )
        return

    settings = await pool.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", interaction.guild_id)
    if not settings or not settings["ticket_category_id"]:
        await interaction.followup.send(
            view=error_embed("Ticket-System ist noch nicht eingerichtet.", "Ein Admin muss zuerst eine Kategorie festlegen."),
            ephemeral=True,
        )
        return

    category_channel = interaction.guild.get_channel(settings["ticket_category_id"])
    if category_channel is None:
        await interaction.followup.send(view=error_embed("Ticket-Kategorie nicht gefunden. Bitte Admin kontaktieren."), ephemeral=True)
        return

    ticket_number = await pool.fetchval(
        "UPDATE guild_settings SET ticket_counter = ticket_counter + 1 WHERE guild_id = $1 RETURNING ticket_counter",
        interaction.guild_id,
    )

    try:
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if settings["ticket_support_role_id"]:
            role = interaction.guild.get_role(settings["ticket_support_role_id"])
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        safe_name = "".join(c for c in interaction.user.display_name.lower() if c.isalnum() or c in "-_")[:20] or "user"
        channel = await interaction.guild.create_text_channel(
            f"ticket-{ticket_number:04d}-{safe_name}",
            category=category_channel,
            overwrites=overwrites,
        )

        await pool.execute(
            """
            INSERT INTO tickets (guild_id, ticket_number, channel_id, opener_discord_id, category, description)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            interaction.guild_id, ticket_number, channel.id, interaction.user.id, category_key, description_text or None,
        )

        intro_text = (
            f"# 🎫 Ticket #{ticket_number:04d}\n"
            f"**Kategorie:** {CATEGORY_LABELS.get(category_key, category_key)}\n"
            f"**Geöffnet von:** {interaction.user.mention}\n"
        )
        if description_text:
            intro_text += f"\n{description_text}\n"
        intro_text += "\n-# Support-Team wurde benachrichtigt. Bitte habt etwas Geduld."

        support_ping = f"<@&{settings['ticket_support_role_id']}>" if settings["ticket_support_role_id"] else ""
        if support_ping:
            intro_text = f"{support_ping}\n" + intro_text
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(intro_text),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                discord.ui.ActionRow(
                    discord.ui.Button(label="🙋 Übernehmen", style=discord.ButtonStyle.primary, custom_id=f"ticket:claim:{ticket_number}"),
                    discord.ui.Button(label="🔒 Schließen", style=discord.ButtonStyle.danger, custom_id=f"ticket:close:{ticket_number}"),
                ),
                accent_color=discord.Color.gold(),
            )
        )
        await channel.send(view=view)

        if screenshot_upload is not None:
            screenshot_values = getattr(screenshot_upload, "values", None) or getattr(screenshot_upload, "attachments", None) or []
            if screenshot_values:
                try:
                    data = await screenshot_values[0].read()
                    await channel.send(
                        content="📎 Screenshot vom Ticket-Ersteller:",
                        file=discord.File(io.BytesIO(data), filename=screenshot_values[0].filename),
                    )
                except Exception:
                    log.exception(f"Fehler beim Weiterleiten des Ticket-Screenshots (Ticket {ticket_number})")

        await interaction.followup.send(view=success_embed("Ticket erstellt", f"Siehe {channel.mention}"), ephemeral=True)
    except Exception:
        log.exception(f"Fehler beim Erstellen von Ticket #{ticket_number:04d}")
        await interaction.followup.send(
            view=error_embed(
                f"Kanal für Ticket #{ticket_number:04d} wurde erstellt, aber danach ist ein Fehler aufgetreten.",
                "Bitte im Bot-Log nachschauen (`sudo journalctl -u fifa-elite-cup-v2 -n 40 --no-pager`).",
            ),
            ephemeral=True,
        )


class TicketDescriptionModal(discord.ui.Modal, title="Ticket erstellen"):
    description = discord.ui.TextInput(
        label="Was können wir für dich tun?", style=discord.TextStyle.paragraph, required=True, max_length=1000
    )

    def __init__(self, category_key: str):
        super().__init__()
        self.category_key = category_key
        self.screenshot_upload = discord.ui.FileUpload(
            custom_id="ticket_screenshot", min_values=0, max_values=1, required=False
        )
        self.add_item(
            discord.ui.Label(
                text="Screenshot (optional)",
                description="Hilft uns, dein Anliegen schneller zu verstehen.",
                component=self.screenshot_upload,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        description_text = f"**Beschreibung:** {self.description.value}" if self.description.value else ""
        await create_ticket_channel(interaction, self.category_key, description_text, self.screenshot_upload)


class TicketApplicationModal(discord.ui.Modal, title="Bewerbung - Fürs Team"):
    age = discord.ui.TextInput(label="Wie alt bist du?", required=True, max_length=10)
    motivation = discord.ui.TextInput(
        label="Warum möchtest du Teil des Teams werden?", style=discord.TextStyle.paragraph, required=True, max_length=500
    )
    experience = discord.ui.TextInput(
        label="Hast du Erfahrung (z.B. Moderation/Support)?", style=discord.TextStyle.paragraph, required=False, max_length=500
    )
    availability = discord.ui.TextInput(label="Wie viel Zeit kannst du wöchentlich einbringen?", required=True, max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        description_text = (
            f"**Alter:** {self.age.value}\n"
            f"**Motivation:** {self.motivation.value}\n"
            f"**Erfahrung:** {self.experience.value or '-'}\n"
            f"**Verfügbarkeit:** {self.availability.value}"
        )
        await create_ticket_channel(interaction, "bewerbung", description_text)


class TicketCloseConfirmView(discord.ui.View):
    def __init__(self, ticket_number: int):
        super().__init__(timeout=120)
        self.ticket_number = ticket_number

    @discord.ui.button(label="Ja, schließen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await close_ticket(interaction.client, interaction.guild, self.ticket_number, interaction.user)
        await interaction.followup.send(view=success_embed("Ticket wird geschlossen..."), ephemeral=True)

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Abgebrochen.", view=None)


async def create_payment_ticket(bot: commands.Bot, guild: discord.Guild, tournament: dict, team: dict):
    """
    Wird automatisch aufgerufen, wenn sich ein Team fuer ein Spendenturnier anmeldet.
    Legt einen privaten Kanal fuer ALLE Manager des Teams + Support-Team an, mit den
    hinterlegten Zahlungsdetails und einem 'Ich habe bezahlt'-Button.
    """
    pool = get_pool()
    settings = await pool.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", guild.id)
    if not settings or not settings["ticket_category_id"]:
        log.warning(f"Spendenturnier {tournament['id']}: kein Ticket-Kategorie eingerichtet, Zahlungs-Kanal uebersprungen.")
        return

    category_channel = guild.get_channel(settings["ticket_category_id"])
    if category_channel is None:
        log.warning(f"Spendenturnier {tournament['id']}: Ticket-Kategorie nicht gefunden.")
        return

    from cogs.team_manager import get_team_managers
    managers = await get_team_managers(team["id"])

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    for m in managers:
        member = guild.get_member(m["discord_id"])
        if member:
            overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    if settings["ticket_support_role_id"]:
        role = guild.get_role(settings["ticket_support_role_id"])
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    ticket_number = await pool.fetchval(
        "UPDATE guild_settings SET ticket_counter = ticket_counter + 1 WHERE guild_id = $1 RETURNING ticket_counter",
        guild.id,
    )

    safe_name = "".join(c for c in team["name"].lower() if c.isalnum() or c in "-_")[:20] or "team"
    try:
        channel = await guild.create_text_channel(
            f"zahlung-{ticket_number:04d}-{safe_name}", category=category_channel, overwrites=overwrites
        )
    except discord.HTTPException:
        log.exception(f"Fehler beim Erstellen des Zahlungs-Kanals fuer Team {team['id']} / Turnier {tournament['id']}")
        return

    await pool.execute(
        """
        INSERT INTO tickets (guild_id, ticket_number, channel_id, opener_discord_id, category, tournament_id, team_id, payment_status)
        VALUES ($1, $2, $3, $4, 'spende', $5, $6, 'pending')
        """,
        guild.id, ticket_number, channel.id, team["owner_discord_id"], tournament["id"], team["id"],
    )

    mentions = " ".join(f"<@{m['discord_id']}>" for m in managers)
    info_block = tournament.get("donation_info") or "_Noch keine Zahlungsdetails hinterlegt - bitte Admin kontaktieren._"
    text = (
        (f"{mentions}\n" if mentions else "")
        + f"# 💳 Zahlung erforderlich\n"
        f"**Team:** {team['name']}\n"
        f"**Turnier:** {tournament['name']}\n"
        "-----\n"
        f"{info_block}\n"
        "-----\n"
        "-# Klickt unten, sobald ihr bezahlt habt - das Support-Team prüft und bestätigt dann."
    )
    try:
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text),
                discord.ui.ActionRow(
                    discord.ui.Button(label="✅ Ich habe bezahlt", style=discord.ButtonStyle.success, custom_id=f"ticket:paidclaim:{ticket_number}"),
                ),
                accent_color=discord.Color.gold(),
            )
        )
        await channel.send(view=view)
    except Exception:
        log.exception(f"Fehler beim Senden der Zahlungs-Nachricht (Ticket #{ticket_number:04d}, Team {team['id']})")
        try:
            await channel.send(
                f"⚠️ Fehler beim Aufbauen der Zahlungsnachricht. Bitte Admin kontaktieren (Ticket #{ticket_number:04d})."
            )
        except discord.HTTPException:
            pass


async def close_ticket(bot: commands.Bot, guild: discord.Guild, ticket_number: int, closed_by: discord.Member):
    pool = get_pool()
    ticket = await pool.fetchrow(
        "SELECT * FROM tickets WHERE guild_id = $1 AND ticket_number = $2", guild.id, ticket_number
    )
    if not ticket:
        return

    channel = guild.get_channel(ticket["channel_id"])
    if channel is None:
        try:
            channel = await guild.fetch_channel(ticket["channel_id"])
        except discord.HTTPException:
            channel = None

    if channel:
        try:
            messages = [msg async for msg in channel.history(limit=500, oldest_first=True)]
            lines = [f"Ticket #{ticket_number:04d} - Transkript", f"Kategorie: {CATEGORY_LABELS.get(ticket['category'], ticket['category'])}", ""]
            for m in messages:
                timestamp = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
                content = m.content or "(kein Text - evtl. Embed/Anhang)"
                lines.append(f"[{timestamp}] {m.author}: {content}")
            transcript_text = "\n".join(lines)

            settings = await pool.fetchrow("SELECT ticket_log_channel_id FROM guild_settings WHERE guild_id = $1", guild.id)
            if settings and settings["ticket_log_channel_id"]:
                log_channel = guild.get_channel(settings["ticket_log_channel_id"])
                if log_channel is None:
                    try:
                        log_channel = await guild.fetch_channel(settings["ticket_log_channel_id"])
                    except discord.HTTPException:
                        log_channel = None
                if log_channel:
                    buf = io.BytesIO(transcript_text.encode("utf-8"))
                    await log_channel.send(
                        content=f"🔒 Ticket #{ticket_number:04d} geschlossen von {closed_by.mention}",
                        file=discord.File(buf, filename=f"ticket-{ticket_number:04d}-transkript.txt"),
                    )
        except Exception:
            log.exception(f"Fehler beim Erstellen des Transkripts fuer Ticket {ticket_number}")

        try:
            await channel.delete(reason=f"Ticket geschlossen von {closed_by}")
        except discord.HTTPException:
            pass

    await pool.execute(
        "UPDATE tickets SET status = 'closed', closed_at = now() WHERE guild_id = $1 AND ticket_number = $2",
        guild.id, ticket_number,
    )


class TicketsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(TicketPanel())

    @app_commands.command(name="ticket_panel_setup", description="Postet das Ticket-Panel in diesem Kanal (Admin)")
    async def ticket_panel_setup(self, interaction: discord.Interaction):
        if not await is_tournament_admin(interaction.user):
            await interaction.response.send_message(view=error_embed("Nur Admins können das Ticket-Panel posten."), ephemeral=True)
            return
        await interaction.response.send_message(view=TicketPanel())

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("ticket:"):
            return

        try:
            if custom_id == "ticket:open_select":
                category_key = interaction.data["values"][0]
                if category_key == "bewerbung":
                    await interaction.response.send_modal(TicketApplicationModal())
                else:
                    await interaction.response.send_modal(TicketDescriptionModal(category_key))
                return

            parts = custom_id.split(":")
            action = parts[1]

            if action == "claim":
                ticket_number = int(parts[2])
                if not await is_ticket_support(interaction.user):
                    await interaction.response.send_message(view=error_embed("Nur das Support-Team kann Tickets übernehmen."), ephemeral=True)
                    return
                pool = get_pool()
                await pool.execute(
                    "UPDATE tickets SET claimed_by = $1 WHERE guild_id = $2 AND ticket_number = $3",
                    interaction.user.id, interaction.guild_id, ticket_number,
                )
                await interaction.response.send_message(view=info_embed(f"🙋 Ticket übernommen von {interaction.user.mention}"))

            elif action == "close":
                ticket_number = int(parts[2])
                if not await is_ticket_support(interaction.user):
                    pool = get_pool()
                    ticket = await pool.fetchrow(
                        "SELECT * FROM tickets WHERE guild_id = $1 AND ticket_number = $2", interaction.guild_id, ticket_number
                    )
                    if not ticket or ticket["opener_discord_id"] != interaction.user.id:
                        await interaction.response.send_message(
                            view=error_embed("Nur der Ticket-Ersteller oder das Support-Team kann schließen."), ephemeral=True
                        )
                        return
                await interaction.response.send_message(
                    content="Ticket wirklich schließen? Ein Transkript wird gespeichert, der Kanal danach gelöscht.",
                    view=TicketCloseConfirmView(ticket_number),
                    ephemeral=True,
                )

            elif action == "paidclaim":
                ticket_number = int(parts[2])
                pool = get_pool()
                ticket = await pool.fetchrow(
                    "SELECT * FROM tickets WHERE guild_id = $1 AND ticket_number = $2", interaction.guild_id, ticket_number
                )
                if not ticket or ticket["payment_status"] != "pending":
                    await interaction.response.send_message(view=warning_embed("Diese Zahlung wurde bereits bearbeitet."), ephemeral=True)
                    return
                await pool.execute(
                    "UPDATE tickets SET payment_status = 'claimed' WHERE guild_id = $1 AND ticket_number = $2",
                    interaction.guild_id, ticket_number,
                )
                settings = await pool.fetchrow("SELECT ticket_support_role_id FROM guild_settings WHERE guild_id = $1", interaction.guild_id)
                support_ping = f"<@&{settings['ticket_support_role_id']}>" if settings and settings["ticket_support_role_id"] else ""
                view = discord.ui.View(timeout=None)
                view.add_item(
                    discord.ui.Button(
                        label="✔️ Zahlung bestätigen", style=discord.ButtonStyle.success,
                        custom_id=f"ticket:confirmpayment:{ticket_number}",
                    )
                )
                await interaction.response.send_message(
                    content=f"{support_ping}\n⏳ **{interaction.user.mention} hat die Zahlung als erledigt markiert.** Bitte prüfen und bestätigen.",
                    view=view,
                )

            elif action == "confirmpayment":
                ticket_number = int(parts[2])
                if not await is_ticket_support(interaction.user):
                    await interaction.response.send_message(view=error_embed("Nur das Support-Team kann Zahlungen bestätigen."), ephemeral=True)
                    return
                pool = get_pool()
                ticket = await pool.fetchrow(
                    "SELECT * FROM tickets WHERE guild_id = $1 AND ticket_number = $2", interaction.guild_id, ticket_number
                )
                if not ticket or ticket["payment_status"] != "claimed":
                    await interaction.response.send_message(view=warning_embed("Diese Zahlung steht nicht (mehr) zur Bestätigung an."), ephemeral=True)
                    return
                await pool.execute(
                    "UPDATE tickets SET payment_status = 'confirmed' WHERE guild_id = $1 AND ticket_number = $2",
                    interaction.guild_id, ticket_number,
                )
                await interaction.response.send_message(
                    view=success_embed("Zahlung bestätigt", f"Von {interaction.user.mention} - danke fürs Prüfen!")
                )
                if ticket.get("tournament_id"):
                    try:
                        from cogs.tournament_manager import refresh_panel
                        await refresh_panel(interaction.client, ticket["tournament_id"])
                    except Exception:
                        log.exception(f"Fehler beim Aktualisieren des Anmelde-Panels nach Zahlungsbestaetigung (Ticket {ticket_number})")
        except Exception:
            log.exception(f"Fehler beim Verarbeiten von Ticket-Interaktion {custom_id!r}")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(view=error_embed("Ein interner Fehler ist aufgetreten."), ephemeral=True)
                else:
                    await interaction.response.send_message(view=error_embed("Ein interner Fehler ist aufgetreten."), ephemeral=True)
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketsCog(bot))
