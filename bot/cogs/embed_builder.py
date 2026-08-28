"""
Embed-Builder-Cog: Admins bauen eine eigene Embed-Nachricht (Titel, Text,
Farbe, Bild, Footer) und posten sie in einen beliebigen Kanal - aehnlich wie
Discohook, aber direkt im Discord-Client ueber ein Formular.

Bewusst einfach gehalten (klassisches discord.Embed, kein Components-V2-
Overkill), weil das genau das Format ist, das die meisten Ankuendigungen
brauchen. Fuer alles Komplexere (Buttons, Turnier-Panels) gibt's die
dedizierten Panels.
"""
from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands

from ui_helpers import success_embed, error_embed


def parse_color(raw: str) -> discord.Color:
    raw = (raw or "").strip().lstrip("#")
    if not raw:
        return discord.Color.gold()
    try:
        return discord.Color(int(raw, 16))
    except ValueError:
        return discord.Color.gold()


class EmbedBuilderModal(discord.ui.Modal, title="Nachricht erstellen"):
    embed_title = discord.ui.TextInput(label="Titel", required=False, max_length=256)
    description = discord.ui.TextInput(
        label="Beschreibung", required=False, max_length=3800, style=discord.TextStyle.paragraph
    )
    color_hex = discord.ui.TextInput(label="Farbe (Hex, z.B. FFD700)", required=False, max_length=7, default="FFD700")
    image_url = discord.ui.TextInput(label="Bild-URL (optional)", required=False, max_length=300)
    footer = discord.ui.TextInput(label="Footer-Text (optional)", required=False, max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title=self.embed_title.value or None,
            description=self.description.value or None,
            color=parse_color(self.color_hex.value),
        )
        if self.image_url.value:
            embed.set_image(url=self.image_url.value)
        if self.footer.value:
            embed.set_footer(text=self.footer.value)

        await interaction.response.send_message(
            "**Vorschau** - so würde die Nachricht aussehen. Wähle einen Kanal zum Posten, oder brich ab:",
            embed=embed,
            view=EmbedPostView(embed),
            ephemeral=True,
        )


class EmbedPostView(discord.ui.View):
    def __init__(self, embed: discord.Embed):
        super().__init__(timeout=300)
        self.embed = embed
        select = discord.ui.ChannelSelect(
            placeholder="Kanal zum Posten wählen...", channel_types=[discord.ChannelType.text]
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        channel_id = int(interaction.data["values"][0])
        channel = interaction.guild.get_channel(channel_id)
        if channel is None:
            await interaction.response.send_message(view=error_embed("Kanal nicht gefunden."), ephemeral=True)
            return
        await channel.send(embed=self.embed)
        await interaction.response.send_message(view=success_embed(f"Nachricht gepostet in {channel.mention}"), ephemeral=True)

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Abgebrochen.", embed=None, view=None)


class EmbedBuilderCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="embed_builder", description="Erstellt eine eigene Nachricht zum Posten (Admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def embed_builder(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EmbedBuilderModal())


async def setup(bot: commands.Bot):
    await bot.add_cog(EmbedBuilderCog(bot))
