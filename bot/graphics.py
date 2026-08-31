"""
Grafik-Modul: rendert alle Bot-Grafiken (Spielplan, Awards, Team of the
Tournament) programmatisch mit PIL im einheitlichen dunkel/gold-Design -
kein statisches Bild-Template mehr noetig, Layouts passen sich automatisch
an Team-/Gruppengroesse an.
"""
from __future__ import annotations
import io
import logging
import os

import aiohttp
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("fifa-elite-cup")

FONT_BOLD = os.path.join(os.path.dirname(__file__), "assets", "fonts", "Poppins-Bold.ttf")
if not os.path.exists(FONT_BOLD):
    FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


async def _fetch_logo(session: aiohttp.ClientSession, url: str | None, label: str = "") -> Image.Image | None:
    if not url:
        return None
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                log.warning(f"Logo-Download fehlgeschlagen ({label}): HTTP {resp.status} fuer {url}")
                return None
            data = await resp.read()
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        log.exception(f"Logo-Download-Fehler ({label}) fuer URL {url}")
        return None


def _paste_logo(img: Image.Image, logo: Image.Image | None, box):
    if logo is None:
        return
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    pad = 6
    target_w, target_h = w - pad * 2, h - pad * 2
    logo_copy = logo.copy()
    logo_copy.thumbnail((target_w, target_h), Image.LANCZOS)
    lx = x1 + pad + (target_w - logo_copy.width) // 2
    ly = y1 + pad + (target_h - logo_copy.height) // 2
    img.paste(logo_copy, (lx, ly), logo_copy)


GOLD = (255, 215, 80)
DARK_BG = (16, 18, 24)
CARD_BG = (28, 31, 40)
WHITE = (235, 235, 240)
GREY = (150, 150, 160)
PITCH_GREEN = (24, 92, 48)
PITCH_LINE = (230, 230, 230)


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD, size)


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


async def render_awards_image(title: str, subtitle: str, awards: list[tuple[str, str, str, str, str | None]]) -> io.BytesIO:
    """
    Einfaches generisches Karten-Layout (kein Template vorhanden).
    awards: Liste von (award_name, player_name, team_name, stat_text, logo_url).
    """
    row_h = 130
    width = 900
    height = 170 + row_h * max(1, len(awards))
    img = Image.new("RGB", (width, height), DARK_BG)
    draw = ImageDraw.Draw(img)

    draw.text((40, 30), title, font=_font(36), fill=GOLD)
    draw.text((40, 78), subtitle, font=_font(20), fill=GREY)
    draw.line([(40, 120), (width - 40, 120)], fill=GOLD, width=2)

    async with aiohttp.ClientSession() as session:
        y = 150
        for award_name, player_name, team_name, stat_text, logo_url in awards:
            draw.rounded_rectangle([(40, y), (width - 40, y + row_h - 20)], radius=14, fill=CARD_BG)
            logo = await _fetch_logo(session, logo_url, team_name)
            _paste_logo(img, logo, (55, y + 15, 55 + (row_h - 50), y + row_h - 35))
            text_x = 55 + (row_h - 50) + 20
            draw.text((text_x, y + 12), award_name, font=_font(18), fill=GOLD)
            draw.text((text_x, y + 40), player_name, font=_font(26), fill=WHITE)
            draw.text((text_x, y + 74), f"{team_name} · {stat_text}", font=_font(18), fill=GREY)
            y += row_h

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# Formation-Positionen (3-5-2), Anteile der Bildbreite/-hoehe (0..1), von oben (Sturm) nach unten (Tor)
TOP11_LAYOUT = {
    "FWD": [(0.32, 0.16), (0.68, 0.16)],
    "MID": [(0.12, 0.40), (0.32, 0.40), (0.5, 0.40), (0.68, 0.40), (0.88, 0.40)],
    "DEF": [(0.25, 0.64), (0.5, 0.64), (0.75, 0.64)],
    "GK": [(0.5, 0.86)],
}


async def render_top11_image(title: str, subtitle: str, formation_slots: dict[str, list[tuple[str, str, str | None]]]) -> io.BytesIO:
    """
    Einfaches generisches Fußballfeld-Layout (3-5-2), kein Template vorhanden.
    formation_slots: {"GK": [(player_name, team_name, logo_url)], "DEF": [...], "MID": [...], "FWD": [...]}
    """
    width, height = 1000, 1300
    img = Image.new("RGB", (width, height), PITCH_GREEN)
    draw = ImageDraw.Draw(img)

    header_h = 110
    draw.rectangle([(0, 0), (width, header_h)], fill=DARK_BG)
    draw.text((40, 20), title, font=_font(34), fill=GOLD)
    draw.text((40, 66), subtitle, font=_font(18), fill=GREY)

    pitch_top = header_h + 20
    draw.rectangle([(20, pitch_top), (width - 20, height - 20)], outline=PITCH_LINE, width=4)
    mid_y = pitch_top + (height - 20 - pitch_top) // 2
    draw.line([(20, mid_y), (width - 20, mid_y)], fill=PITCH_LINE, width=3)
    draw.ellipse([(width / 2 - 90, mid_y - 90), (width / 2 + 90, mid_y + 90)], outline=PITCH_LINE, width=3)
    draw.rectangle([(width / 2 - 180, height - 20 - 140), (width / 2 + 180, height - 20)], outline=PITCH_LINE, width=3)

    logo_size = 70
    async with aiohttp.ClientSession() as session:
        for group, slots in TOP11_LAYOUT.items():
            players = formation_slots.get(group, [])
            for i, (fx, fy) in enumerate(slots):
                if i >= len(players):
                    continue
                player_name, team_name, logo_url = players[i]
                cx = int(fx * width)
                cy = pitch_top + int(fy * (height - 20 - pitch_top))

                logo = await _fetch_logo(session, logo_url, team_name)
                if logo:
                    _paste_logo(img, logo, (cx - logo_size // 2, cy - logo_size // 2, cx + logo_size // 2, cy + logo_size // 2))
                else:
                    draw.ellipse(
                        [(cx - logo_size // 2, cy - logo_size // 2), (cx + logo_size // 2, cy + logo_size // 2)],
                        fill=CARD_BG, outline=GOLD, width=2,
                    )

                name_font = _font(20)
                tw, _ = _text_size(draw, player_name, name_font)
                label_y = cy + logo_size // 2 + 8
                draw.rounded_rectangle(
                    [(cx - tw / 2 - 10, label_y), (cx + tw / 2 + 10, label_y + 30)], radius=6, fill=DARK_BG
                )
                draw.text((cx - tw / 2, label_y + 4), player_name, font=name_font, fill=WHITE)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


async def render_group_schedule_image(group_label: str, matchdays: list[list[dict]]) -> io.BytesIO:
    """
    Programmatischer Spielplan im Awards/Top11-Stil (dunkel/gold), kein Template noetig -
    passt sich automatisch an Anzahl Spieltage/Spiele an. Ergebnisse werden direkt mit
    angezeigt (Aufruf bei jeder Ergebnis-Aenderung, siehe refresh_group_panel).
    Match-Dict: team1_name, team2_name, team1_logo_url, team2_logo_url, team1_score,
    team2_score, status.
    """
    width = 1000
    header_h = 90
    matchday_header_h = 46
    row_h = 78
    padding_bottom = 20

    matchdays_with_games = [md for md in matchdays if md]
    total_rows = sum(len(md) for md in matchdays_with_games)
    height = header_h + len(matchdays_with_games) * matchday_header_h + total_rows * row_h + padding_bottom

    img = Image.new("RGB", (width, max(height, 200)), DARK_BG)
    draw = ImageDraw.Draw(img)
    draw.text((36, 26), f"📋 Spielplan — {group_label}", font=_font(30), fill=GOLD)
    draw.line([(36, header_h - 15), (width - 36, header_h - 15)], fill=GOLD, width=2)

    y = header_h
    logo_size = 48
    name_font = _font(19)
    score_font = _font(23)

    async with aiohttp.ClientSession() as session:
        for md_idx, matches in enumerate(matchdays, start=1):
            if not matches:
                continue
            draw.text((36, y + 10), f"Spieltag {md_idx}", font=_font(18), fill=GREY)
            y += matchday_header_h
            for m in matches:
                row_bottom = y + row_h - 12
                draw.rounded_rectangle([(26, y), (width - 26, row_bottom)], radius=12, fill=CARD_BG)
                cy = (y + row_bottom) // 2

                logo1 = await _fetch_logo(session, m.get("team1_logo_url"), m["team1_name"])
                _paste_logo(img, logo1, (44, cy - logo_size // 2, 44 + logo_size, cy + logo_size // 2))
                t1_name = m["team1_name"][:22]
                draw.text((44 + logo_size + 16, cy - 12), t1_name, font=name_font, fill=WHITE)

                logo2 = await _fetch_logo(session, m.get("team2_logo_url"), m["team2_name"])
                _paste_logo(img, logo2, (width - 44 - logo_size, cy - logo_size // 2, width - 44, cy + logo_size // 2))
                t2_name = m["team2_name"][:22]
                t2w, _ = _text_size(draw, t2_name, name_font)
                draw.text((width - 44 - logo_size - 16 - t2w, cy - 12), t2_name, font=name_font, fill=WHITE)

                if m.get("status") == "completed" and m.get("team1_score") is not None:
                    score_text, score_fill = f"{m['team1_score']} : {m['team2_score']}", GOLD
                else:
                    score_text, score_fill = "vs", GREY
                stw, _ = _text_size(draw, score_text, score_font)
                draw.text((width / 2 - stw / 2, cy - 14), score_text, font=score_font, fill=score_fill)

                y += row_h

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


