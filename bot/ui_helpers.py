"""
Gemeinsame Components-V2-Bausteine, damit alle Bot-Nachrichten (Erfolg/Fehler/
Info/Warnung) einheitlich und hochwertig aussehen - echte Container mit
Akzentfarbe statt klassischer discord.Embed.

Aufruf-Konvention: (titel, detailtext=None) - titel ist die kurze Kernaussage,
detailtext optional fuer weitere Erklaerung. Rueckgabe ist ein discord.ui.LayoutView,
wird ueber view=... statt embed=... verschickt.
"""
from __future__ import annotations
import discord

GOLD = discord.Color.gold()
RED = discord.Color.red()
GREEN = discord.Color.green()
ORANGE = discord.Color.orange()


def _build(icon: str, title: str, detail: str | None, color: discord.Color) -> discord.ui.LayoutView:
    text = f"### {icon} {title}"
    if detail:
        text += f"\n{detail}"
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_color=color))
    return view


def success_embed(title: str, detail: str | None = None) -> discord.ui.LayoutView:
    return _build("✅", title, detail, GREEN)


def error_embed(title: str, detail: str | None = None) -> discord.ui.LayoutView:
    return _build("⚠️", title, detail, RED)


def info_embed(title: str, detail: str | None = None) -> discord.ui.LayoutView:
    return _build("ℹ️", title, detail, GOLD)


def warning_embed(title: str, detail: str | None = None) -> discord.ui.LayoutView:
    return _build("🚫", title, detail, ORANGE)
