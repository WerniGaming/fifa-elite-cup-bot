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
from PIL import Image, ImageDraw, ImageFilter, ImageFont

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


def _paste_logo(img: Image.Image, logo: Image.Image | None, box, ring: bool = True):
    """Fuegt ein Logo rund zugeschnitten ein (wie TeamLogo.tsx auf der Website:
    rounded-full object-cover), mit dezentem goldenen Ring drumherum - sorgt fuer
    einen einheitlichen Look statt eines eckigen Bilds hinter einem Kreis-Umriss."""
    if logo is None:
        return
    x1, y1, x2, y2 = (int(v) for v in box)
    w, h = x2 - x1, y2 - y1
    pad = 6
    target_w, target_h = w - pad * 2, h - pad * 2
    # Erst quadratisch zuschneiden (object-cover-Verhalten), dann auf Zielgroesse
    # skalieren - so wird der volle Kreis mit Bildinhalt gefuellt statt nur ein
    # kleineres thumbnail() mittig auf einen groesseren Kreis zu setzen.
    lw, lh = logo.size
    side = min(lw, lh)
    cx0, cy0 = (lw - side) // 2, (lh - side) // 2
    logo_copy = logo.crop((cx0, cy0, cx0 + side, cy0 + side)).convert("RGBA")
    size = min(target_w, target_h)
    logo_copy = logo_copy.resize((size, size), Image.LANCZOS)
    # Runde Alpha-Maske, damit das Logo wirklich als Kreis erscheint statt als
    # eckiges Bild innerhalb eines nur aufgezeichneten Ring-Umrisses.
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([(0, 0), (size - 1, size - 1)], fill=255)
    if logo_copy.mode == "RGBA":
        alpha = logo_copy.split()[3]
        mask = Image.composite(mask, Image.new("L", (size, size), 0), alpha)
    lx = x1 + pad + (target_w - size) // 2
    ly = y1 + pad + (target_h - size) // 2
    img.paste(logo_copy, (lx, ly), mask)
    if ring:
        draw = ImageDraw.Draw(img)
        draw.ellipse([(lx - 1, ly - 1), (lx + size, ly + size)], outline=(140, 115, 40), width=2)


def _gradient_rounded_rect(img: Image.Image, box, radius: int, color_top: tuple[int, int, int], color_bottom: tuple[int, int, int]):
    """Abgerundetes Rechteck mit vertikalem Farbverlauf (heller oben) statt Flat-Fill -
    gleiche 'from-gold/60 to-gold'-Optik wie die Balkendiagramme auf der Website."""
    x1, y1, x2, y2 = box
    w, h = int(x2 - x1), int(y2 - y1)
    if w <= 0 or h <= 0:
        return
    gradient = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(h - 1, 1)
        gradient.putpixel((0, y), tuple(int(color_top[c] + (color_bottom[c] - color_top[c]) * t) for c in range(3)))
    gradient = gradient.resize((w, h))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([(0, 0), (w - 1, h - 1)], radius=radius, fill=255)
    img.paste(gradient, (int(x1), int(y1)), mask)


def _draw_trophy(img: Image.Image, cx: int, bottom_y: int, height: int, color: tuple[int, int, int]):
    """Zeichnet einen stilisierten Pokal (Kelch + Henkel + Stiel + Sockel) direkt mit PIL-Formen -
    ersetzt den bisherigen platten Farbblock auf dem Podium durch eine echte Trophaee."""
    draw = ImageDraw.Draw(img)
    lighter = tuple(min(255, c + 45) for c in color)
    darker = tuple(max(0, c - 60) for c in color)

    base_w, base_h = height * 0.42, height * 0.09
    stem_w, stem_h = height * 0.10, height * 0.18
    bowl_w, bowl_h = height * 0.56, height * 0.48
    neck_w = height * 0.16

    base_top = bottom_y - base_h
    stem_top = base_top - stem_h
    bowl_bottom = stem_top
    bowl_top = bowl_bottom - bowl_h

    # Sockel (Fuss)
    draw.rounded_rectangle(
        [(cx - base_w / 2, base_top), (cx + base_w / 2, bottom_y)], radius=base_h * 0.4, fill=darker
    )
    # Stiel
    draw.rectangle([(cx - stem_w / 2, stem_top), (cx + stem_w / 2, base_top + 2)], fill=color)
    # Kelch (Trapez von schmalem Hals zu breiter Schale, oben abgerundet per Ellipse)
    draw.polygon(
        [
            (cx - neck_w / 2, bowl_bottom), (cx + neck_w / 2, bowl_bottom),
            (cx + bowl_w / 2, bowl_top + bowl_h * 0.35), (cx - bowl_w / 2, bowl_top + bowl_h * 0.35),
        ],
        fill=color,
    )
    draw.ellipse(
        [(cx - bowl_w / 2, bowl_top), (cx + bowl_w / 2, bowl_top + bowl_h * 0.42)], fill=lighter
    )
    # Henkel (zwei Ovale links/rechts der Schale)
    handle_w, handle_h = bowl_w * 0.34, bowl_h * 0.5
    handle_y = bowl_top + bowl_h * 0.28
    for side in (-1, 1):
        hx = cx + side * (bowl_w / 2 - handle_w * 0.15)
        draw.ellipse(
            [(hx - handle_w / 2, handle_y), (hx + handle_w / 2, handle_y + handle_h)],
            outline=color, width=max(3, int(height * 0.045)),
        )
    # Glanzlicht
    draw.ellipse(
        [(cx - bowl_w * 0.22, bowl_top + bowl_h * 0.08), (cx - bowl_w * 0.05, bowl_top + bowl_h * 0.28)],
        fill=(255, 255, 255, 255) if img.mode == "RGBA" else tuple(min(255, c + 70) for c in lighter),
    )
    return bowl_top  # oberste Kante, fuer Platzierung von Logo/Name darueber


def _glow(img: Image.Image, center: tuple[int, int], radius: int, color: tuple[int, int, int] | None = None, alpha: int = 70):
    """Weicher, verwaschener Farbfleck hinter Titeln/Logos - dasselbe 'Glow'-Element wie die
    goldenen Blur-Kreise hinter den Bento-Karten auf der Website (radial-gradient-artig)."""
    color = color or GOLD
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(layer)
    cx, cy = center
    ldraw.ellipse([(cx - radius, cy - radius), (cx + radius, cy + radius)], fill=(*color, alpha))
    layer = layer.filter(ImageFilter.GaussianBlur(radius // 2))
    img.paste(Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB"), (0, 0))


GOLD = (255, 215, 80)
# Auf das Website-Redesign abgestimmt (echtes Schwarz statt Blaugrau, gleiche
# Card-/Border-Farbwerte wie app/globals.css --background/--card/--card-border).
DARK_BG = (0, 0, 0)
CARD_BG = (19, 19, 19)
CARD_BORDER = (38, 38, 38)
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
    _glow(img, (width - 100, 20), 180, alpha=55)
    draw = ImageDraw.Draw(img)

    draw.text((40, 30), title, font=_font(40), fill=GOLD)
    draw.text((40, 78), subtitle, font=_font(22), fill=GREY)
    draw.line([(40, 120), (width - 40, 120)], fill=GOLD, width=2)

    async with aiohttp.ClientSession() as session:
        y = 150
        for award_name, player_name, team_name, stat_text, logo_url in awards:
            draw.rounded_rectangle([(40, y), (width - 40, y + row_h - 20)], radius=14, fill=CARD_BG, outline=CARD_BORDER, width=1)
            logo = await _fetch_logo(session, logo_url, team_name)
            _paste_logo(img, logo, (55, y + 15, 55 + (row_h - 50), y + row_h - 35))
            text_x = 55 + (row_h - 50) + 20
            draw.text((text_x, y + 12), award_name, font=_font(20), fill=GOLD)
            draw.text((text_x, y + 40), player_name, font=_font(30), fill=WHITE)
            draw.text((text_x, y + 74), f"{team_name} · {stat_text}", font=_font(20), fill=GREY)
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
    width, height = 1150, 1480
    img = Image.new("RGB", (width, height), PITCH_GREEN)
    draw = ImageDraw.Draw(img)

    header_h = 120
    draw.rectangle([(0, 0), (width, header_h)], fill=DARK_BG)
    _glow(img, (width - 140, 32), 180, alpha=60)
    draw = ImageDraw.Draw(img)
    draw.text((44, 22), title, font=_font(42), fill=GOLD)
    draw.text((44, 72), subtitle, font=_font(22), fill=GREY)

    pitch_top = header_h + 20
    draw.rectangle([(20, pitch_top), (width - 20, height - 20)], outline=PITCH_LINE, width=4)
    mid_y = pitch_top + (height - 20 - pitch_top) // 2
    draw.line([(20, mid_y), (width - 20, mid_y)], fill=PITCH_LINE, width=3)
    draw.ellipse([(width / 2 - 100, mid_y - 100), (width / 2 + 100, mid_y + 100)], outline=PITCH_LINE, width=3)
    draw.rectangle([(width / 2 - 200, height - 20 - 155), (width / 2 + 200, height - 20)], outline=PITCH_LINE, width=3)

    logo_size = 82

    def _fit_name(name: str, max_w: int) -> tuple[str, "ImageFont.FreeTypeFont", int]:
        """Waehlt die groesstmoegliche Schriftgroesse (mit Untergrenze), die noch
        in max_w passt, und kuerzt den Namen als letzten Ausweg - verhindert, dass
        sich Label benachbarter Spieler bei langen Namen ueberlappen."""
        for size in (26, 24, 22, 20, 18, 16, 15, 14):
            f = _font(size)
            tw, _ = _text_size(draw, name, f)
            if tw <= max_w:
                return name, f, tw
        f = _font(14)
        truncated = name
        while len(truncated) > 3:
            truncated = truncated[:-1]
            candidate = truncated.rstrip() + "…"
            tw, _ = _text_size(draw, candidate, f)
            if tw <= max_w:
                return candidate, f, tw
        return truncated, f, _text_size(draw, truncated, f)[0]

    async with aiohttp.ClientSession() as session:
        for group, slots in TOP11_LAYOUT.items():
            xs = sorted(fx for fx, _ in slots)
            min_gap_px = min((xs[i + 1] - xs[i]) * width for i in range(len(xs) - 1)) if len(xs) > 1 else width * 0.9
            max_label_w = int(min_gap_px - 24)

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

                label_text, name_font, tw = _fit_name(player_name, max_label_w)
                label_y = cy + logo_size // 2 + 10
                draw.rounded_rectangle(
                    [(cx - tw / 2 - 10, label_y), (cx + tw / 2 + 10, label_y + 34)], radius=6, fill=DARK_BG
                )
                draw.text((cx - tw / 2, label_y + 6), label_text, font=name_font, fill=WHITE)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


PODIUM_COLORS = {1: (255, 215, 80), 2: (200, 205, 212), 3: (200, 140, 80)}
PODIUM_TROPHY_HEIGHTS = {1: 190, 2: 145, 3: 120}
PODIUM_ORDER = [2, 1, 3]  # Anzeige-Reihenfolge links -> rechts


async def render_podium_image(title: str, subtitle: str, places: dict[int, tuple[str, str | None]]) -> io.BytesIO:
    """
    Siegertreppchen-Grafik mit echten Pokal-Formen (statt flachem Farbblock) - 1. Platz
    bekommt den groessten/hellsten Pokal, mittig und erhoeht wie ein echtes Podium.
    places: {1: (team_name, logo_url), 2: (...), 3: (...)} - 2/3 optional.
    """
    width, height = 900, 560
    img = Image.new("RGB", (width, height), DARK_BG)
    _glow(img, (width // 2, height - 140), 300, alpha=40)
    _glow(img, (90, 20), 160, alpha=55)
    draw = ImageDraw.Draw(img)
    draw.text((36, 26), title, font=_font(38), fill=GOLD)
    draw.text((36, 76), subtitle, font=_font(21), fill=GREY)

    base_y = height - 40
    slot_w = 240
    gap = 40
    total_w = slot_w * 3 + gap * 2
    start_x = (width - total_w) // 2
    logo_size = 88

    async with aiohttp.ClientSession() as session:
        for i, place in enumerate(PODIUM_ORDER):
            if place not in places:
                continue
            team_name, logo_url = places[place]
            x = start_x + i * (slot_w + gap)
            cx = x + slot_w // 2
            color = PODIUM_COLORS[place]
            trophy_h = PODIUM_TROPHY_HEIGHTS[place]

            # Podest-Sockel darunter, hoeher fuer Platz 1 - traegt die Trophaee
            pedestal_h = {1: 70, 2: 46, 3: 30}[place]
            _gradient_rounded_rect(
                img, (x + 20, base_y - pedestal_h, x + slot_w - 20, base_y), radius=8,
                color_top=CARD_BORDER, color_bottom=CARD_BG,
            )
            draw = ImageDraw.Draw(img)
            place_font = _font(28)
            place_text = f"PLATZ {place}"
            pw, _ = _text_size(draw, place_text, place_font)
            draw.text((cx - pw / 2, base_y - pedestal_h / 2 - 14), place_text, font=place_font, fill=color)

            bowl_top = _draw_trophy(img, cx, base_y - pedestal_h, trophy_h, color)
            draw = ImageDraw.Draw(img)

            logo = await _fetch_logo(session, logo_url, team_name)
            logo_top = bowl_top - logo_size - 18
            if logo:
                _paste_logo(img, logo, (cx - logo_size // 2, logo_top, cx + logo_size // 2, logo_top + logo_size))
            else:
                draw.ellipse(
                    [(cx - logo_size // 2, logo_top), (cx + logo_size // 2, logo_top + logo_size)],
                    fill=CARD_BG, outline=color, width=3,
                )

            name_font = _font(24)
            name = team_name[:20]
            nw, _ = _text_size(draw, name, name_font)
            draw.text((cx - nw / 2, logo_top - 36), name, font=name_font, fill=WHITE)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


async def render_club_stats_card(
    team_name: str, ea_club_name: str | None, logo_url: str | None,
    division_text: str | None, medals: list[str], record_text: str | None, goals_text: str | None,
) -> io.BytesIO:
    """Kompakte Stat-Karte fuer /club_stats - Kopfbereich (Identitaet + Titel + Bilanz), Details bleiben Text darunter."""
    width, height = 900, 320
    img = Image.new("RGB", (width, height), DARK_BG)
    _glow(img, (width - 80, 40), 170, alpha=45)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([(0, 0), (width - 1, height - 1)], radius=20, outline=(70, 60, 30), width=2)

    logo_size = 110
    async with aiohttp.ClientSession() as session:
        logo = await _fetch_logo(session, logo_url, team_name)
    if logo:
        _paste_logo(img, logo, (36, 36, 36 + logo_size, 36 + logo_size))
    else:
        draw.ellipse([(36, 36), (36 + logo_size, 36 + logo_size)], fill=CARD_BG, outline=GOLD, width=2)

    text_x = 36 + logo_size + 28
    draw.text((text_x, 34), team_name, font=_font(38), fill=WHITE)
    if ea_club_name:
        draw.text((text_x, 78), f"EA-Club: {ea_club_name}", font=_font(20), fill=GREY)
    if division_text:
        draw.text((text_x, 108), division_text, font=_font(20), fill=GOLD)

    y = 36 + logo_size + 24
    draw.line([(36, y), (width - 36, y)], fill=(60, 52, 28), width=1)
    y += 24

    if medals:
        draw.text((36, y), "  ".join(medals), font=_font(30), fill=GOLD)
        y += 44
    if record_text:
        draw.text((36, y), record_text, font=_font(25), fill=WHITE)
        y += 34
    if goals_text:
        draw.text((36, y), goals_text, font=_font(22), fill=GREY)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


async def render_bracket_tree_image(title: str, sections: list[tuple[str, list[dict]]]) -> io.BytesIO:
    """
    Echter Turnierbaum fuer die KO-Phase: eine Spalte pro Runde, Spiele als kompakte
    Karten, mit Verbindungslinien zur naechsten Runde (klassische Bracket-Optik, wie
    die Baum-Ansicht auf der Website). Nimmt dieselbe sections-Struktur wie
    render_schedule_image (Liste von (Rundenname, Matches)).
    """
    sections = [s for s in sections if s[1]]
    if not sections:
        img = Image.new("RGB", (600, 150), DARK_BG)
        ImageDraw.Draw(img).text((30, 60), "Noch keine KO-Matches.", font=_font(24), fill=GREY)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf

    card_w, card_h = 300, 90
    col_gap = 110
    header_h = 90
    row_gap0 = 26  # vertikaler Abstand zwischen Karten in Runde 1
    slot_h = card_h + row_gap0

    # 1. Durchgang (ohne zu zeichnen): Positionen anhand echter Team-IDs berechnen, damit die
    # Bildhoehe hinterher genau passt - Runden mit nachrueckenden/unverbundenen Teams koennen
    # weiter nach unten rutschen als Runde 1 allein vorgibt.
    def compute_layout() -> tuple[list[list[int]], list[list[tuple[int | None, int | None]]], int]:
        team_center: dict[int | None, int] = {}
        all_centers: list[list[int]] = []
        all_anchors: list[list[tuple[int | None, int | None]]] = []
        max_cy = header_h + 20 + card_h // 2
        for matches in (s[1] for s in sections):
            anchors: list[tuple[int | None, int | None]] = []
            centers: list[int] = []
            prev_cy: int | None = None
            for m in matches:
                a = team_center.get(m.get("team1_id"))
                b = team_center.get(m.get("team2_id"))
                anchors.append((a, b))
                if a is not None and b is not None:
                    ideal = (a + b) // 2
                elif a is not None:
                    ideal = a
                elif b is not None:
                    ideal = b
                else:
                    ideal = None
                if ideal is None:
                    cy = (prev_cy + slot_h) if prev_cy is not None else header_h + 20 + card_h // 2
                else:
                    cy = ideal
                    if prev_cy is not None and cy < prev_cy + slot_h:
                        cy = prev_cy + slot_h
                centers.append(cy)
                prev_cy = cy
                max_cy = max(max_cy, cy)
            for m, cy in zip(matches, centers):
                team_center[m.get("team1_id")] = cy
                team_center[m.get("team2_id")] = cy
            all_centers.append(centers)
            all_anchors.append(anchors)
        return all_centers, all_anchors, max_cy

    layout_centers, layout_anchors, max_cy = compute_layout()
    height = max_cy + card_h // 2 + 30
    width = header_h - 30 + len(sections) * (card_w + col_gap)

    img = Image.new("RGB", (max(width, 700), max(height, 260)), DARK_BG)
    _glow(img, (120, 20), 170, alpha=55)
    draw = ImageDraw.Draw(img)
    draw.text((36, 26), title, font=_font(34), fill=GOLD)
    draw.line([(36, header_h - 15), (img.width - 36, header_h - 15)], fill=GOLD, width=2)

    name_font = _font(19)
    score_font = _font(20)
    round_label_font = _font(18)

    async def draw_card(session: aiohttp.ClientSession, x: int, cy: int, m: dict) -> int:
        top = cy - card_h // 2
        draw.rounded_rectangle([(x, top), (x + card_w, top + card_h)], radius=10, fill=CARD_BG, outline=CARD_BORDER, width=1)
        mid = top + card_h // 2
        draw.line([(x + 12, mid), (x + card_w - 12, mid)], fill=CARD_BORDER, width=1)

        completed = m.get("status") == "completed" and m.get("team1_score") is not None
        s1 = str(m.get("team1_score")) if completed else ""
        s2 = str(m.get("team2_score")) if completed else ""
        win1 = completed and m["team1_score"] > m["team2_score"]
        win2 = completed and m["team2_score"] > m["team1_score"]

        logo_s = 26
        l1 = await _fetch_logo(session, m.get("team1_logo_url"), m.get("team1_name", ""))
        l2 = await _fetch_logo(session, m.get("team2_logo_url"), m.get("team2_name", ""))
        _paste_logo(img, l1, (x + 12, top + 10, x + 12 + logo_s, top + 10 + logo_s), ring=False)
        _paste_logo(img, l2, (x + 12, top + card_h - 10 - logo_s, x + 12 + logo_s, top + card_h - 10), ring=False)

        name1 = (m.get("team1_name") or "Freilos")[:18]
        name2 = (m.get("team2_name") or "Freilos")[:18]
        draw.text((x + 12 + logo_s + 10, top + 12), name1, font=name_font, fill=GOLD if win1 else WHITE)
        draw.text((x + 12 + logo_s + 10, top + card_h - 12 - 20), name2, font=name_font, fill=GOLD if win2 else WHITE)

        if completed:
            draw.text((x + card_w - 34, top + 12), s1, font=score_font, fill=GOLD if win1 else GREY)
            draw.text((x + card_w - 34, top + card_h - 12 - 22), s2, font=score_font, fill=GOLD if win2 else GREY)
        return mid

    # 2. Durchgang: mit den vorberechneten Positionen tatsaechlich zeichnen.
    x = header_h - 30
    async with aiohttp.ClientSession() as session:
        for round_idx, (round_label, matches) in enumerate(sections):
            draw.text((x + 4, header_h - 8), round_label, font=round_label_font, fill=GREY)
            centers = layout_centers[round_idx]
            anchors = layout_anchors[round_idx]

            # Verbindungslinien nur dort, wo ein Team wirklich aus der vorherigen Runde kommt
            if round_idx > 0:
                conn_x = x - col_gap // 2
                for (a, b), cy in zip(anchors, centers):
                    if a is not None:
                        draw.line([(x - col_gap, a), (conn_x, a)], fill=CARD_BORDER, width=2)
                    if b is not None:
                        draw.line([(x - col_gap, b), (conn_x, b)], fill=CARD_BORDER, width=2)
                    if a is not None and b is not None:
                        draw.line([(conn_x, min(a, b)), (conn_x, max(a, b))], fill=CARD_BORDER, width=2)
                    if a is not None or b is not None:
                        draw.line([(conn_x, cy), (x, cy)], fill=CARD_BORDER, width=2)

            for m, cy in zip(matches, centers):
                await draw_card(session, x, cy, m)

            x += card_w + col_gap

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


async def render_schedule_image(title: str, sections: list[tuple[str, list[dict]]]) -> io.BytesIO:
    """
    Programmatischer Spielplan im Awards/Top11-Stil (dunkel/gold), kein Template noetig -
    passt sich automatisch an die Anzahl Abschnitte/Spiele an. Ergebnisse werden direkt mit
    angezeigt. Dient sowohl fuer Gruppen-Spieltage als auch KO-Bracket-Runden.
    sections: Liste von (Abschnitts-Label, Matches), z.B. ("Spieltag 1", [...]) oder
    ("Halbfinale", [...]). Match-Dict: team1_name, team2_name, team1_logo_url,
    team2_logo_url, team1_score, team2_score, status.
    """
    width = 1000
    header_h = 100
    section_header_h = 50
    row_h = 86
    padding_bottom = 20

    sections_with_games = [s for s in sections if s[1]]
    total_rows = sum(len(matches) for _, matches in sections_with_games)
    height = header_h + len(sections_with_games) * section_header_h + total_rows * row_h + padding_bottom

    img = Image.new("RGB", (width, max(height, 200)), DARK_BG)
    _glow(img, (width - 100, 10), 160, alpha=55)
    draw = ImageDraw.Draw(img)
    draw.text((36, 26), title, font=_font(34), fill=GOLD)
    draw.line([(36, header_h - 15), (width - 36, header_h - 15)], fill=GOLD, width=2)

    y = header_h
    logo_size = 54
    name_font = _font(21)
    score_font = _font(26)

    async with aiohttp.ClientSession() as session:
        for section_label, matches in sections:
            if not matches:
                continue
            draw.text((36, y + 10), section_label, font=_font(20), fill=GREY)
            y += section_header_h
            for m in matches:
                row_bottom = y + row_h - 12
                draw.rounded_rectangle([(26, y), (width - 26, row_bottom)], radius=12, fill=CARD_BG, outline=CARD_BORDER, width=1)
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
                    score_text, score_fill, pill_outline = f"{m['team1_score']} : {m['team2_score']}", GOLD, GOLD
                else:
                    score_text, score_fill, pill_outline = "vs", GREY, CARD_BORDER
                stw, sth = _text_size(draw, score_text, score_font)
                pill_pad_x, pill_pad_y = 16, 8
                pill_box = (width / 2 - stw / 2 - pill_pad_x, cy - sth / 2 - pill_pad_y, width / 2 + stw / 2 + pill_pad_x, cy + sth / 2 + pill_pad_y)
                draw.rounded_rectangle(pill_box, radius=14, outline=pill_outline, width=2)
                draw.text((width / 2 - stw / 2, cy - 14), score_text, font=score_font, fill=score_fill)

                y += row_h

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


