"""
Gemeinsame Embed-Bausteine, damit alle Bot-Nachrichten (Erfolg/Fehler/Info)
einheitlich im FIFA-Elite-Gold-Look aussehen, statt als reiner Text.

Aufruf-Konvention: (titel, detailtext=None) - titel ist die kurze Kernaussage,
detailtext optional fuer weitere Erklaerung.
"""
from __future__ import annotations
import discord

GOLD = discord.Color.gold()
RED = discord.Color.red()
GREEN = discord.Color.green()
ORANGE = discord.Color.orange()


def success_embed(title: str, detail: str | None = None) -> discord.Embed:
    return discord.Embed(title=f"✅ {title}", description=detail, color=GREEN)


def error_embed(title: str, detail: str | None = None) -> discord.Embed:
    return discord.Embed(title=f"⚠️ {title}", description=detail, color=RED)


def info_embed(title: str, detail: str | None = None) -> discord.Embed:
    return discord.Embed(title=title, description=detail, color=GOLD)


def warning_embed(title: str, detail: str | None = None) -> discord.Embed:
    return discord.Embed(title=f"🚫 {title}", description=detail, color=ORANGE)
