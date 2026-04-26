"""
KARA PnL Card Generator — V2 Redesign
Portrait layout (800×1000) for maximum Telegram visibility.
Inspired by Rainbow/BloFin card style: big PNL %, clean info.
KARA character sits at bottom-left, data overlays top+right.
"""

from __future__ import annotations
import io
import os
import random
import logging
from typing import Optional

log = logging.getLogger("kara.pnl_card")

# ── Canvas constants (PORTRAIT — Telegram renders these MUCH larger) ──────────
CANVAS_W = 800
CANVAS_H = 1000

# ── Color palette ─────────────────────────────────────────────────────────────
C_PROFIT     = (74, 222, 128)         # green
C_LOSS       = (248, 113, 113)        # red
C_TEXT       = (245, 244, 250)        # near-white
C_LABEL      = (160, 155, 185)        # muted purple-grey
C_DIVIDER    = (180, 160, 255, 40)
C_PILL_GREEN = (74,  222, 128)
C_PILL_RED   = (248, 113, 113)
C_WATERMARK  = (120, 115, 145)
C_DARK_BG    = (8, 6, 18)
C_CARD_BG    = (18, 14, 35)           # slightly lighter card bg


# ─────────────────────────────────────────────────────────────────────────────
# FONT LOADER
# ─────────────────────────────────────────────────────────────────────────────

def _load_fonts():
    from PIL import ImageFont

    bold_paths = [
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

    def _font(path, size):
        if path:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
        return ImageFont.load_default()

    return {
        "brand":      _font(bold_path,    20),
        "asset":      _font(bold_path,    42),
        "pill":       _font(bold_path,    18),
        "hero_pct":   _font(bold_path,    96),     # PNL % — THE HERO
        "hero_usd":   _font(bold_path,    36),     # PNL USD
        "label":      _font(regular_path, 18),
        "value":      _font(bold_path,    24),
        "small":      _font(regular_path, 16),
        "exit_tag":   _font(bold_path,    16),
        "watermark":  _font(regular_path, 13),
        "equity_val": _font(bold_path,    28),
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
        "manual_close":  "MANUAL CLOSE",
    }.get(reason.lower(), reason.upper())


def _exit_reason_color(reason: str) -> tuple:
    r = reason.lower()
    if r == "trailing_stop": return (45, 212, 191)   # teal
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


def _draw_centered(draw, cx, y, text, font, color):
    draw.text((cx - _text_w(draw, text, font) // 2, y), text, font=font, fill=color)


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
    bg_path: str = "notify/assets/kara_bg.png",
) -> bytes:
    """Returns PNG bytes ready for Telegram send_photo."""
    from PIL import Image, ImageDraw

    # ── 1. Build canvas with dark background ──────────────────────────────
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (*C_DARK_BG, 255))

    # ── 2. Place KARA character at bottom-left (cropped face region) ──────
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

            # Crop the KARA face region (left ~60% of original, top ~85%)
            crop_right = int(orig_w * 0.55)
            crop_bottom = int(orig_h * 0.90)
            face_crop = bg_full.crop((0, 0, crop_right, crop_bottom))

            # Resize to fit bottom-left of our portrait canvas
            kara_h = int(CANVAS_H * 0.55)
            kara_w = int(face_crop.width * kara_h / face_crop.height)
            face_resized = face_crop.resize((kara_w, kara_h), Image.LANCZOS)

            # Apply fade overlay so KARA doesn't overpower text
            fade = Image.new("RGBA", face_resized.size, (0, 0, 0, 0))
            fd = ImageDraw.Draw(fade)

            # Top fade (text area needs to be readable) — stronger
            for i in range(min(250, kara_h)):
                alpha = int(210 * (1 - i / 250))
                fd.line([(0, i), (kara_w, i)], fill=(*C_DARK_BG, alpha))

            # Right fade (smooth blend)
            for i in range(min(200, kara_w)):
                x = kara_w - 200 + i
                alpha = int(180 * (i / 200))
                fd.line([(x, 0), (x, kara_h)], fill=(*C_DARK_BG, alpha))

            face_faded = Image.alpha_composite(face_resized, fade)

            # Position: bottom-left, shifted left and pushed lower
            kara_x = -int(kara_w * 0.15)
            kara_y = CANVAS_H - kara_h + int(kara_h * 0.12)
            canvas.paste(face_faded, (kara_x, kara_y), face_faded)
        except Exception as e:
            log.warning(f"[PnLCard] Failed to load KARA background: {e}")

    draw = ImageDraw.Draw(canvas)

    # ── 3. Load fonts & derive values ─────────────────────────────────────
    fonts     = _load_fonts()
    is_profit = pnl_usd >= 0
    pnl_color = C_PROFIT if is_profit else C_LOSS
    sign      = "+" if is_profit else "-"
    side_lower = side.lower()

    MARGIN = 40
    CX = CANVAS_W // 2  # center x

    # ═══════════════════════════════════════════════════════════════════════
    # ROW 0 — Brand + Exit Reason tag (y=30)
    # ═══════════════════════════════════════════════════════════════════════
    draw.text((MARGIN, 32), "KARA", font=fonts["brand"], fill=C_LABEL)

    # Exit reason tag — right side
    er_label = _exit_reason_label(exit_reason)
    er_color = _exit_reason_color(exit_reason)
    er_tw = _text_w(draw, er_label, fonts["exit_tag"])

    # Draw exit reason pill
    er_pill_ov = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    er_pd = ImageDraw.Draw(er_pill_ov)
    er_px = CANVAS_W - MARGIN - er_tw - 20
    er_py = 28
    _rrect(er_pd, er_px, er_py, er_px + er_tw + 20, er_py + 28, 14,
           fill=(*er_color, 40), outline=(*er_color, 120), width=1)
    canvas = Image.alpha_composite(canvas, er_pill_ov)
    draw = ImageDraw.Draw(canvas)
    draw.text((er_px + 10, er_py + 5), er_label, font=fonts["exit_tag"], fill=er_color)

    # ═══════════════════════════════════════════════════════════════════════
    # ROW 1 — Side pill + Asset name (y=75)
    # ═══════════════════════════════════════════════════════════════════════

    # Side pill
    pill_txt = f"{side_lower.upper()} {leverage}x"
    pill_tw = _text_w(draw, pill_txt, fonts["pill"])
    pill_w = pill_tw + 24
    pill_h = 30
    pill_x = MARGIN
    pill_y = 78

    if side_lower == "long":
        pbg   = (74,  222, 128, 50)
        pbord = (74,  222, 128, 150)
        ptc   = C_PILL_GREEN
    else:
        pbg   = (248, 113, 113, 50)
        pbord = (248, 113, 113, 150)
        ptc   = C_PILL_RED

    pill_ov = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill_ov)
    _rrect(pd, pill_x, pill_y, pill_x + pill_w, pill_y + pill_h, 15,
           fill=pbg, outline=pbord, width=1)
    canvas = Image.alpha_composite(canvas, pill_ov)
    draw = ImageDraw.Draw(canvas)

    ptx = pill_x + (pill_w - pill_tw) // 2
    pty = pill_y + (pill_h - 18) // 2
    draw.text((ptx, pty), pill_txt, font=fonts["pill"], fill=ptc)

    # Asset name right after pill
    asset_text = asset.upper()
    draw.text((pill_x + pill_w + 14, pill_y - 4), asset_text, font=fonts["asset"], fill=C_TEXT)

    # ═══════════════════════════════════════════════════════════════════════
    # ROW 2 — PNL % HERO (y=140) — THE BIGGEST ELEMENT
    # ═══════════════════════════════════════════════════════════════════════
    pct_val = pnl_pct * 100 if abs(pnl_pct) < 10 else pnl_pct  # handle if already %

    # Format: show appropriate decimals (use abs to avoid double-negative)
    if abs(pct_val) >= 100:
        pct_text = f"{sign}{abs(pct_val):.0f}%"
    elif abs(pct_val) >= 10:
        pct_text = f"{sign}{abs(pct_val):.1f}%"
    else:
        pct_text = f"{sign}{abs(pct_val):.2f}%"

    # Draw PNL % — LEFT ALIGNED, HUGE
    draw.text((MARGIN, 135), pct_text, font=fonts["hero_pct"], fill=pnl_color)

    # ═══════════════════════════════════════════════════════════════════════
    # ROW 3 — PNL USD (y=245)
    # ═══════════════════════════════════════════════════════════════════════
    usd_text = f"{sign}${abs(pnl_usd):.2f}"
    usd_color = tuple(int(c * 0.85) for c in pnl_color)
    draw.text((MARGIN, 250), usd_text, font=fonts["hero_usd"], fill=usd_color)

    # ═══════════════════════════════════════════════════════════════════════
    # Divider line (y=305)
    # ═══════════════════════════════════════════════════════════════════════
    div_y = 310
    draw.line([(MARGIN, div_y), (CANVAS_W - MARGIN, div_y)],
              fill=(*C_DIVIDER[:3], 60), width=1)

    # ═══════════════════════════════════════════════════════════════════════
    # ROW 4 — Stats grid 2×2 (y=330)
    # ═══════════════════════════════════════════════════════════════════════
    grid_y = 330
    col1_x = MARGIN
    col2_x = CANVAS_W // 2 + 20

    # Entry Price
    draw.text((col1_x, grid_y), "Entry Price", font=fonts["label"], fill=C_LABEL)
    draw.text((col1_x, grid_y + 24), _fmt_price(entry_price), font=fonts["value"], fill=C_TEXT)

    # Exit Price
    draw.text((col2_x, grid_y), "Exit Price", font=fonts["label"], fill=C_LABEL)
    draw.text((col2_x, grid_y + 24), _fmt_price(exit_price), font=fonts["value"], fill=C_TEXT)

    # Hold Time
    draw.text((col1_x, grid_y + 72), "Hold Time", font=fonts["label"], fill=C_LABEL)
    draw.text((col1_x, grid_y + 96), _fmt_hold(hold_minutes), font=fonts["value"], fill=C_TEXT)

    # Score
    draw.text((col2_x, grid_y + 72), "Score", font=fonts["label"], fill=C_LABEL)
    draw.text((col2_x, grid_y + 96), f"{max(0, min(score, 100))}/100", font=fonts["value"], fill=C_TEXT)

    # ═══════════════════════════════════════════════════════════════════════
    # Divider line (y=470)
    # ═══════════════════════════════════════════════════════════════════════
    div2_y = 475
    draw.line([(MARGIN, div2_y), (CANVAS_W - MARGIN, div2_y)],
              fill=(*C_DIVIDER[:3], 60), width=1)

    # ═══════════════════════════════════════════════════════════════════════
    # ROW 5 — Session PnL & Total Equity (y=490)
    # ═══════════════════════════════════════════════════════════════════════
    row5_y = 495

    # Session PnL
    ses_sign = "+" if session_pnl >= 0 else "-"
    ses_pct_val = abs(session_pnl_pct * 100) if abs(session_pnl_pct) < 10 else abs(session_pnl_pct)
    ses_text = f"{ses_sign}${abs(session_pnl):.2f} ({ses_sign}{ses_pct_val:.1f}%)"
    ses_color = C_PROFIT if session_pnl >= 0 else C_LOSS

    draw.text((col1_x, row5_y), "Session PnL", font=fonts["label"], fill=C_LABEL)
    draw.text((col1_x, row5_y + 24), ses_text, font=fonts["value"], fill=ses_color)

    # Total Equity
    eq_text = f"${total_equity:,.2f}"
    draw.text((col2_x, row5_y), "Total Equity", font=fonts["label"], fill=C_LABEL)
    draw.text((col2_x, row5_y + 24), eq_text, font=fonts["equity_val"], fill=C_TEXT)

    # ═══════════════════════════════════════════════════════════════════════
    # Watermark (bottom)
    # ═══════════════════════════════════════════════════════════════════════
    _draw_centered(draw, CX, CANVAS_H - 30,
                   "♦  KARA  ·  Hyperliquid Futures",
                   fonts["watermark"], C_WATERMARK)

    # ── Export ────────────────────────────────────────────────────────────
    final = canvas.convert("RGB")
    buf = io.BytesIO()
    final.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.read()
