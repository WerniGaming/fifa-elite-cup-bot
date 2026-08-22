"""
Grafik-Modul: erzeugt die 'Spielplan'-Grafik(en) fuer eine Gruppe (3 Spieltage
pro Bild, bis zu 3 Spiele pro Spieltag), mit Teamnamen und Logos ueberlagert
auf der vom Nutzer gestalteten Vorlage. Gruppen mit mehr als 3 Spieltagen
(z.B. 6er-Gruppen mit 5 Spieltagen) bekommen automatisch mehrere Bilder.
"""
from __future__ import annotations
import io
import logging
import os

import aiohttp
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("fifa-elite-cup")

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "assets", "spielplan_template.png")
FONT_BOLD = os.path.join(os.path.dirname(__file__), "assets", "fonts", "Poppins-Bold.ttf")
if not os.path.exists(FONT_BOLD):
    FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Basis-Koordinaten Spalte 1 (Spieltag 1), Reihe 1 - vermessen an der Vorlage
SHIELD_L = (65, 409, 127, 471)
NAME_L = (139, 426, 245, 472)
NAME_R = (317, 426, 422, 472)
SHIELD_R = (433, 409, 495, 471)

COL_OFFSETS = [0, 492, 978]  # Spieltag 1 / 2 / 3 innerhalb EINES Bildes
ROW_OFFSET = 158  # Abstand zwischen den 3 Reihen pro Spieltag
MATCHDAYS_PER_IMAGE = 3
ROWS_PER_COLUMN = 3


def _shift(box: tuple[int, int, int, int], dx: int, dy: int) -> tuple[int, int, int, int]:
    return (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)


def _box_for(col: int, row: int):
    dx, dy = COL_OFFSETS[col], ROW_OFFSET * row
    return _shift(SHIELD_L, dx, dy), _shift(NAME_L, dx, dy), _shift(NAME_R, dx, dy), _shift(SHIELD_R, dx, dy)


def _fit_text(draw: ImageDraw.ImageDraw, text: str, box, max_size=22, min_size=9) -> ImageFont.FreeTypeFont:
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    size = max_size
    while size > min_size:
        font = ImageFont.truetype(FONT_BOLD, size)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= w - 10 and th <= h - 6:
            return font
        size -= 1
    return ImageFont.truetype(FONT_BOLD, min_size)


def _draw_centered(draw: ImageDraw.ImageDraw, text: str, box, font, fill=(255, 215, 80)):
    x1, y1, x2, y2 = box
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    draw.text((cx - tw / 2 - bbox[0], cy - th / 2 - bbox[1]), text, font=font, fill=fill)


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


async def _render_single_image(matchdays_chunk: list[list[dict]], session: aiohttp.ClientSession) -> io.BytesIO:
    """Rendert EIN Bild mit bis zu 3 Spieltag-Spalten (matchdays_chunk hat max. 3 Eintraege)."""
    img = Image.open(TEMPLATE_PATH).convert("RGB")
    draw = ImageDraw.Draw(img)

    for col in range(len(matchdays_chunk)):
        matches = matchdays_chunk[col]
        for row in range(ROWS_PER_COLUMN):
            shield_l, name_l, name_r, shield_r = _box_for(col, row)

            if row >= len(matches):
                # Leerer Slot -> komplette Zeile diagonal durchstreichen
                x1 = shield_l[0]
                x2 = shield_r[2]
                y1 = min(shield_l[1], name_l[1])
                y2 = max(shield_l[3], name_l[3])
                draw.line([(x1, y1), (x2, y2)], fill=(120, 90, 20), width=3)
                draw.line([(x1, y2), (x2, y1)], fill=(120, 90, 20), width=3)
                continue

            m = matches[row]
            f1 = _fit_text(draw, m["team1_name"], name_l)
            f2 = _fit_text(draw, m["team2_name"], name_r)
            _draw_centered(draw, m["team1_name"], name_l, f1)
            _draw_centered(draw, m["team2_name"], name_r, f2)

            logo1 = await _fetch_logo(session, m.get("team1_logo_url"), m["team1_name"])
            logo2 = await _fetch_logo(session, m.get("team2_logo_url"), m["team2_name"])
            _paste_logo(img, logo1, shield_l)
            _paste_logo(img, logo2, shield_r)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


async def generate_group_schedule_images(matchdays: list[list[dict]]) -> list[io.BytesIO]:
    """
    matchdays: eine Liste mit EINEM Eintrag pro Spieltag (beliebig viele,
    nicht nur 3), jeder Eintrag ist eine Liste von Matches: {"team1_name",
    "team2_name","team1_logo_url","team2_logo_url"}.

    Die Vorlage hat Platz fuer 3 Spieltage x 3 Spiele pro Bild. Gruppen mit
    mehr als 3 Spieltagen (z.B. 6er-Gruppen mit 5 Spieltagen) werden auf
    mehrere Bilder aufgeteilt, damit KEIN Spieltag verloren geht.
    Gibt eine Liste von PNG-Bytes zurueck (normalerweise 1 Bild, bei groesseren
    Gruppen mehr).
    """
    chunks = [matchdays[i:i + MATCHDAYS_PER_IMAGE] for i in range(0, len(matchdays), MATCHDAYS_PER_IMAGE)] or [[]]
    images = []
    async with aiohttp.ClientSession() as session:
        for chunk in chunks:
            images.append(await _render_single_image(chunk, session))
    return images
