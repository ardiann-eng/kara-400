"""
KARA PnL Card Generator — V3 Landscape
Landscape layout (900×560) — optimal for Telegram rendering.
Telegram displays landscape images much larger than portrait.
KARA character on right side, data on left.
"""

from __future__ import annotations
import io
import os
import logging

log = logging.getLogger("kara.pnl_card")

# ── Canvas constants (LANDSCAPE — Telegram renders at full width) ─────────────
CANVAS_W = 900
CANVAS_H = 560

# ── Color palette ─────────────────────────────────────────────────────────────
C_PROFIT     = (74, 222, 128)
C_LOSS       = (248, 113, 113)
C_TEXT       = (245, 244, 250)
C_LABEL      = (160, 155, 185)
C_DIVIDER    = (180, 160, 255, 40)
C_PILL_GREEN = (74,  222, 128)
C_PILL_RED   = (248, 113, 113)
C_WATERMARK  = (120, 115, 145)
C_DARK_BG    = (8, 6, 18)


# ─────────────────────────────────────────────────────────────────────────────
# FONT LOADER
# ─────────────────────────────────────────────────────────────────────────────

def _load_fonts():
    from PIL import ImageFont

    _here = os.path.dirname(os.path.abspath(__file__))

    bold_paths = [
        os.path.join(_here, "assets", "fonts", "bold.ttf"),    # bundled — always works
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/calibrib.ttf",
        "C:/Windows/Fonts/verdanab.ttf",
        "C:/Windows/Fonts/trebucbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    regular_paths = [
        os.path.join(_here, "assets", "fonts", "regular.ttf"), # bundled — always works
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibri.ttf",
        "C:/Windows/Fonts/verdana.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]

    bold_path    = next((p for p in bold_paths    if os.path.exists(p)), None)
    regular_path = next((p for p in regular_paths if os.path.exists(p)), None)

    if not bold_path:
        log.warning("[PnLCard] No bold font found — text sizes will be broken")
    if not regular_path:
        log.warning("[PnLCard] No regular font found — text sizes will be broken")

    def _font(path, size):
        if path:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
        return ImageFont.load_default()

    return {
        "brand":      _font(bold_path,    18),
        "asset":      _font(bold_path,    32),
        "pill":       _font(bold_path,    16),
        "hero_pct":   _font(bold_path,    88),   # PNL % — THE HERO
        "hero_usd":   _font(bold_path,    30),
        "label":      _font(regular_path, 16),
        "value":      _font(bold_path,    22),
        "exit_tag":   _font(bold_path,    14),
        "watermark":  _font(regular_path, 12),
        "equity_val": _font(bold_path,    26),
    }


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_price(price: float) -> str:
    if price >= 1000:   return f"${price:,.2f}"
    if price >= 1:      return f"${price:.4f}"
    if price >= 0.01:   return f"${price:.5f}"
    return f"${price:.7f}"


def _fmt_hold(minutes: float) -> str:
    if minutes < 60:
        return f"{int(minutes)}m"
    h = int(minutes // 60)
    m = int(minutes % 60)
    return f"{h}h {m}m"


def _exit_reason_label(reason: str) -> str:
    return {
        "trailing_stop": "TRAILING STOP",
        "tp1":           "TAKE PROFIT 1",
        "tp2":           "TAKE PROFIT 2",
        "stop_loss":     "STOP LOSS",
        "time_exit":     "TIME EXIT",
        "progress_stop": "PROGRESS STOP",
        "manual_close":  "MANUAL CLOSE",
    }.get(reason.lower(), reason.upper())


def _exit_reason_color(reason: str) -> tuple:
    r = reason.lower()
    if r == "trailing_stop": return (45, 212, 191)
    if r in ("tp1", "tp2"):  return C_PROFIT
    return C_LOSS


def _text_w(draw, text, font) -> int:
    try:
        bb = draw.textbbox((0, 0), text, font=font)
        return bb[2] - bb[0]
    except Exception:
        return len(text) * 9


def _text_h(draw, text, font) -> int:
    try:
        bb = draw.textbbox((0, 0), text, font=font)
        return bb[3] - bb[1]
    except Exception:
        return 20


def _draw_right(draw, rx, y, text, font, color):
    draw.text((rx - _text_w(draw, text, font), y), text, font=font, fill=color)


def _rrect(draw, x0, y0, x1, y1, r, fill=None, outline=None, width=1):
    try:
        draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill, outline=outline, width=width)
    except (AttributeError, TypeError):
        draw.rectangle([x0, y0, x1, y1], fill=fill, outline=outline, width=width)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

def generate_pnl_card(
    asset: str,
    side: str,
    entry_price: float,
    exit_price: float,
    pnl_usd: float,
    pnl_pct: float,
    exit_reason: str,
    hold_minutes: float,
    leverage: int,
    score: int,
    session_pnl: float,
    session_pnl_pct: float,
    total_equity: float,
    tier: str = "B",
    bg_path: str = "notify/assets/kara_bg.png",
) -> bytes:
    """Returns PNG bytes ready for Telegram send_photo."""
    from PIL import Image, ImageDraw

    # ── 1. Dark background ────────────────────────────────────────────────
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (*C_DARK_BG, 255))

    # ── 2. Place KARA character — RIGHT SIDE, full height ─────────────────
    for candidate in [
        bg_path,
        "notify/kara.bg.png",
        "kara.bg.png",
        os.path.join(os.path.dirname(__file__), "assets", "kara_bg.png"),
        os.path.join(os.path.dirname(__file__), "kara.bg.png"),
    ]:
        if os.path.exists(candidate):
            bg_path = candidate
            break

    if os.path.exists(bg_path):
        try:
            bg_full = Image.open(bg_path).convert("RGBA")
            orig_w, orig_h = bg_full.size

            # Crop face region (left 60% of original)
            crop_right  = int(orig_w * 0.60)
            crop_bottom = int(orig_h * 0.92)
            face_crop   = bg_full.crop((0, 0, crop_right, crop_bottom))

            # Resize to fill right 45% of canvas height
            kara_h = CANVAS_H
            kara_w = int(face_crop.width * kara_h / face_crop.height)
            face_resized = face_crop.resize((kara_w, kara_h), Image.LANCZOS)

            # Left fade so text stays readable
            fade = Image.new("RGBA", face_resized.size, (0, 0, 0, 0))
            fd = ImageDraw.Draw(fade)
            fade_width = min(280, kara_w)
            for i in range(fade_width):
                alpha = int(220 * (1 - i / fade_width))
                fd.line([(i, 0), (i, kara_h)], fill=(*C_DARK_BG, alpha))

            face_faded = Image.alpha_composite(face_resized, fade)

            # Position: right side, slight overflow right is fine
            kara_x = CANVAS_W - kara_w + int(kara_w * 0.05)
            canvas.paste(face_faded, (kara_x, 0), face_faded)
        except Exception as e:
            log.warning(f"[PnLCard] Failed to load KARA background: {e}")

    draw = ImageDraw.Draw(canvas)

    # ── 3. Load fonts & derive values ─────────────────────────────────────
    fonts      = _load_fonts()
    is_profit  = pnl_usd >= 0
    pnl_color  = C_PROFIT if is_profit else C_LOSS
    sign       = "+" if is_profit else "-"
    side_lower = side.lower()

    MARGIN  = 36
    # Data column occupies left ~55% of canvas
    DATA_W  = int(CANVAS_W * 0.54)

    # ═══════════════════════════════════════════════════════════════════════
    # ROW 0 — Brand (top-left) + Exit reason tag (top-right of data area)
    # ═══════════════════════════════════════════════════════════════════════
    draw.text((MARGIN, 22), "KARA", font=fonts["brand"], fill=C_LABEL)

    er_label = _exit_reason_label(exit_reason)
    er_color = _exit_reason_color(exit_reason)
    er_tw    = _text_w(draw, er_label, fonts["exit_tag"])

    er_pill_ov = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    er_pd = ImageDraw.Draw(er_pill_ov)
    er_px = DATA_W - er_tw - 20
    er_py = 18
    _rrect(er_pd, er_px, er_py, er_px + er_tw + 20, er_py + 26, 13,
           fill=(*er_color, 40), outline=(*er_color, 120), width=1)
    canvas = Image.alpha_composite(canvas, er_pill_ov)
    draw = ImageDraw.Draw(canvas)
    draw.text((er_px + 10, er_py + 5), er_label, font=fonts["exit_tag"], fill=er_color)

    # ═══════════════════════════════════════════════════════════════════════
    # ROW 1 — Side pill + Asset name (y=58)
    # ═══════════════════════════════════════════════════════════════════════
    pill_txt = f"{side_lower.upper()} {leverage}x"
    pill_tw  = _text_w(draw, pill_txt, fonts["pill"])
    pill_w   = pill_tw + 22
    pill_h   = 28
    pill_x   = MARGIN
    pill_y   = 58

    if side_lower == "long":
        pbg, pbord, ptc = (74, 222, 128, 50), (74, 222, 128, 150), C_PILL_GREEN
    else:
        pbg, pbord, ptc = (248, 113, 113, 50), (248, 113, 113, 150), C_PILL_RED

    pill_ov = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill_ov)
    _rrect(pd, pill_x, pill_y, pill_x + pill_w, pill_y + pill_h, 14,
           fill=pbg, outline=pbord, width=1)
    canvas = Image.alpha_composite(canvas, pill_ov)
    draw = ImageDraw.Draw(canvas)
    draw.text((pill_x + (pill_w - pill_tw) // 2, pill_y + 5),
              pill_txt, font=fonts["pill"], fill=ptc)

    draw.text((pill_x + pill_w + 12, pill_y - 2),
              asset.upper(), font=fonts["asset"], fill=C_TEXT)

    # ═══════════════════════════════════════════════════════════════════════
    # ROW 2 — PNL % HERO (y=102)
    # ═══════════════════════════════════════════════════════════════════════
    pct_val = pnl_pct * 100
    if abs(pct_val) >= 100:
        pct_text = f"{sign}{abs(pct_val):.0f}%"
    elif abs(pct_val) >= 10:
        pct_text = f"{sign}{abs(pct_val):.1f}%"
    else:
        pct_text = f"{sign}{abs(pct_val):.2f}%"

    draw.text((MARGIN, 100), pct_text, font=fonts["hero_pct"], fill=pnl_color)

    # ═══════════════════════════════════════════════════════════════════════
    # ROW 3 — PNL USD (y=196)
    # ═══════════════════════════════════════════════════════════════════════
    usd_text  = f"{sign}${abs(pnl_usd):.2f}"
    usd_color = tuple(int(c * 0.85) for c in pnl_color)
    draw.text((MARGIN, 200), usd_text, font=fonts["hero_usd"], fill=usd_color)

    # ═══════════════════════════════════════════════════════════════════════
    # Divider (y=245)
    # ═══════════════════════════════════════════════════════════════════════
    div_y = 248
    draw.line([(MARGIN, div_y), (DATA_W - MARGIN // 2, div_y)],
              fill=(*C_DIVIDER[:3], 60), width=1)

    # ═══════════════════════════════════════════════════════════════════════
    # ROW 4 — Stats 2×2 grid (y=262)
    # ═══════════════════════════════════════════════════════════════════════
    grid_y = 262
    col1_x = MARGIN
    col2_x = DATA_W // 2 + 10

    draw.text((col1_x, grid_y),      "Entry Price", font=fonts["label"], fill=C_LABEL)
    draw.text((col1_x, grid_y + 22), _fmt_price(entry_price), font=fonts["value"], fill=C_TEXT)

    draw.text((col2_x, grid_y),      "Exit Price",  font=fonts["label"], fill=C_LABEL)
    draw.text((col2_x, grid_y + 22), _fmt_price(exit_price),  font=fonts["value"], fill=C_TEXT)

    draw.text((col1_x, grid_y + 62), "Hold Time", font=fonts["label"], fill=C_LABEL)
    draw.text((col1_x, grid_y + 84), _fmt_hold(hold_minutes), font=fonts["value"], fill=C_TEXT)

    draw.text((col2_x, grid_y + 62), "Tier",     font=fonts["label"], fill=C_LABEL)
    draw.text((col2_x, grid_y + 84), f"S/A/B > {tier}", font=fonts["value"], fill=C_TEXT)

    # ═══════════════════════════════════════════════════════════════════════
    # Divider (y=388)
    # ═══════════════════════════════════════════════════════════════════════
    div2_y = 388
    draw.line([(MARGIN, div2_y), (DATA_W - MARGIN // 2, div2_y)],
              fill=(*C_DIVIDER[:3], 60), width=1)

    # ═══════════════════════════════════════════════════════════════════════
    # ROW 5 — Session PnL & Total Equity (y=402)
    # ═══════════════════════════════════════════════════════════════════════
    row5_y = 402

    ses_sign    = "+" if session_pnl >= 0 else "-"
    pct_abs     = abs(session_pnl_pct * 100)
    ses_text    = f"{ses_sign}${abs(session_pnl):.2f} ({ses_sign}{pct_abs:.1f}%)"
    ses_color   = C_PROFIT if session_pnl >= 0 else C_LOSS

    draw.text((col1_x, row5_y),      "Session PnL", font=fonts["label"], fill=C_LABEL)
    draw.text((col1_x, row5_y + 22), ses_text,      font=fonts["value"], fill=ses_color)

    draw.text((col2_x, row5_y),      "Total Equity", font=fonts["label"], fill=C_LABEL)
    draw.text((col2_x, row5_y + 22), f"${total_equity:,.2f}", font=fonts["equity_val"], fill=C_TEXT)

    # ═══════════════════════════════════════════════════════════════════════
    # Watermark (bottom-left)
    # ═══════════════════════════════════════════════════════════════════════
    draw.text((MARGIN, CANVAS_H - 22),
              "♦  KARA  ·  Hyperliquid Futures",
              font=fonts["watermark"], fill=C_WATERMARK)

    # ── Export ────────────────────────────────────────────────────────────
    final = canvas.convert("RGB")
    buf = io.BytesIO()
    final.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.read()
