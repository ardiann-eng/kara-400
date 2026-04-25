"""
KARA PnL Card Generator
Left half: KARA character (untouched). Right half: trade data overlay.
"""

from __future__ import annotations
import io
import os
import random
import logging
from typing import Optional

log = logging.getLogger("kara.pnl_card")

# ── Canvas constants ──────────────────────────────────────────────────────────
CANVAS_W      = 1456
CANVAS_H      = 816
DATA_X        = 620        # data zone left edge (clear of character face)
DATA_CENTER_X = 1020       # center of data zone (620→1420)
DATA_RIGHT_X  = 1420       # right edge
BLEND_START_X = 580        # gradient blend start

# ── Color palette ─────────────────────────────────────────────────────────────
C_PROFIT    = (74, 222, 128)         # green
C_LOSS      = (248, 113, 113)        # red
C_TEXT      = (245, 244, 250)        # near-white
C_LABEL     = (160, 155, 185)        # muted purple-grey
C_DIVIDER   = (180, 160, 255, 55)
C_MATRIX    = (192, 132, 252)
C_SCORE_BAR = (192, 132, 252)
C_WATERMARK = (100, 95, 120)
C_DARK_BG   = (8, 6, 18)


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
        "tiny":        _font(regular_path, 13),
        "small":       _font(regular_path, 15),
        "label":       _font(regular_path, 18),
        "medium":      _font(bold_path,    22),
        "large":       _font(bold_path,    32),
        "hero":        _font(bold_path,    72),
        "pct":         _font(bold_path,    26),
        "score_label": _font(regular_path, 13),
        "pill":        _font(bold_path,    14),
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

    # ── 1. Load background ────────────────────────────────────────────────
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
        bg = Image.open(bg_path).convert("RGBA")
        bg = bg.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)
    else:
        log.warning(f"[PnLCard] Background not found, using plain dark canvas")
        bg = Image.new("RGBA", (CANVAS_W, CANVAS_H), (*C_DARK_BG, 255))

    canvas = bg.copy()

    # ── 2. Dark overlay on right half — solid enough to read text ─────────
    overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    # Gradient fade: BLEND_START_X → DATA_X (580 → 620)
    fade_w = DATA_X - BLEND_START_X
    for i in range(fade_w):
        alpha = int(195 * (i / fade_w))
        od.line([(BLEND_START_X + i, 0), (BLEND_START_X + i, CANVAS_H)],
                fill=(*C_DARK_BG, alpha))

    # Solid dark panel from DATA_X to right edge
    od.rectangle([DATA_X, 0, CANVAS_W, CANVAS_H], fill=(*C_DARK_BG, 195))

    canvas = Image.alpha_composite(canvas, overlay)
    draw   = ImageDraw.Draw(canvas)

    # ── 3. Fonts & derived values ─────────────────────────────────────────
    fonts      = _load_fonts()
    is_profit  = pnl_usd >= 0
    pnl_color  = C_PROFIT if is_profit else C_LOSS
    sign       = "+" if is_profit else "-"
    side_lower = side.lower()

    LX = DATA_X + 24    # left margin inside data zone

    # ─── ROW 0 — Branding (y=44) ──────────────────────────────────────────
    draw.text((LX, 44), "K A R A", font=fonts["small"], fill=C_LABEL)

    # Divider
    div_y = 68
    draw.line([(LX, div_y), (DATA_RIGHT_X - 10, div_y)], fill=(*C_DIVIDER[:3], 70), width=1)

    # ─── ROW 1 — Asset + Side pill + Exit reason (y=78) ──────────────────
    asset_text = asset.upper()
    draw.text((LX, 78), asset_text, font=fonts["large"], fill=C_TEXT)

    # Side pill — right next to asset name
    aw       = _text_w(draw, asset_text, fonts["large"])
    pill_x   = LX + aw + 14
    pill_txt = side_lower.upper()
    pill_tw  = _text_w(draw, pill_txt, fonts["pill"])
    pill_w   = pill_tw + 22
    pill_h   = 26
    pill_y   = 82
    pill_r   = 13

    if side_lower == "long":
        pbg   = (74,  222, 128, 55)
        pbord = (74,  222, 128, 160)
        ptc   = C_PROFIT
    else:
        pbg   = (248, 113, 113, 55)
        pbord = (248, 113, 113, 160)
        ptc   = C_LOSS

    pill_ov = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    pd      = ImageDraw.Draw(pill_ov)
    _rrect(pd, pill_x, pill_y, pill_x + pill_w, pill_y + pill_h, pill_r,
           fill=pbg, outline=pbord, width=1)
    canvas = Image.alpha_composite(canvas, pill_ov)
    draw   = ImageDraw.Draw(canvas)

    ptx = pill_x + (pill_w - pill_tw) // 2
    pty = pill_y + (pill_h - 15) // 2
    draw.text((ptx, pty), pill_txt, font=fonts["pill"], fill=ptc)

    # Exit reason — right-aligned, same row
    er_label = _exit_reason_label(exit_reason)
    er_color = _exit_reason_color(exit_reason)
    _draw_right(draw, DATA_RIGHT_X - 10, 86, er_label, fonts["small"], er_color)

    # ─── ROW 2 — PnL hero number (y=128) ─────────────────────────────────
    pnl_text = f"{sign}${abs(pnl_usd):.2f}"
    _draw_centered(draw, DATA_CENTER_X, 128, pnl_text, fonts["hero"], pnl_color)

    pct_text   = f"({sign}{abs(pnl_pct) * 100:.2f}%)"
    pct_dimmed = tuple(int(c * 0.75) for c in pnl_color)
    _draw_centered(draw, DATA_CENTER_X, 212, pct_text, fonts["pct"], pct_dimmed)

    # ─── ROW 3 — Score bar (y=250) ────────────────────────────────────────
    sc        = max(0, min(score, 100))
    bar_x0    = LX
    bar_x1    = DATA_RIGHT_X - 10
    bar_y     = 264
    bar_h_px  = 8
    bar_total = bar_x1 - bar_x0
    fill_w    = int(bar_total * sc / 100)

    draw.text((bar_x0, 248), "Score", font=fonts["score_label"], fill=C_LABEL)
    _draw_right(draw, bar_x1, 248, str(sc), fonts["score_label"], C_SCORE_BAR)

    draw.rectangle([bar_x0, bar_y, bar_x1, bar_y + bar_h_px], fill=(255, 255, 255, 25))
    if fill_w > 3:
        draw.rectangle([bar_x0, bar_y, bar_x0 + fill_w, bar_y + bar_h_px],
                       fill=(*C_SCORE_BAR, 210))

    # Divider
    div2_y = 292
    draw.line([(bar_x0, div2_y), (bar_x1, div2_y)], fill=(*C_DIVIDER[:3], 70), width=1)

    # ─── ROW 4 — 4-column stats grid (y=304) ─────────────────────────────
    #   Each column: label on top (tiny), value below (medium)
    col_centers = [700, 870, 1060, 1260]
    col_labels  = ["ENTRY",            "EXIT",             "HOLD TIME",          "LEVERAGE"]
    col_values  = [
        _fmt_price(entry_price),
        _fmt_price(exit_price),
        _fmt_hold(hold_minutes),
        f"{leverage}x",
    ]

    for cx, lbl, val in zip(col_centers, col_labels, col_values):
        _draw_centered(draw, cx, 304, lbl, fonts["tiny"],   C_LABEL)
        _draw_centered(draw, cx, 326, val, fonts["medium"], C_TEXT)

    # Divider
    div3_y = 374
    draw.line([(bar_x0, div3_y), (bar_x1, div3_y)], fill=(*C_DIVIDER[:3], 70), width=1)

    # ─── ROW 5 — Session PnL & Total Equity (y=386) ──────────────────────
    ses_sign  = "+" if session_pnl >= 0 else "-"
    ses_text  = f"{ses_sign}${abs(session_pnl):.2f} ({ses_sign}{abs(session_pnl_pct) * 100:.2f}%)"
    ses_color = C_PROFIT if session_pnl >= 0 else C_LOSS

    _draw_centered(draw, 760,  386, "Session PnL",  fonts["tiny"],  C_LABEL)
    _draw_centered(draw, 760,  408, ses_text,        fonts["label"], ses_color)

    eq_text = f"${total_equity:,.2f}"
    _draw_centered(draw, 1280, 386, "Total Equity",  fonts["tiny"],  C_LABEL)
    _draw_centered(draw, 1280, 408, eq_text,          fonts["label"], C_TEXT)

    # ─── Matrix rain — decorative right edge ─────────────────────────────
    matrix_chars = "0123456789ABCDEF"
    random.seed(score + len(asset) * 7)
    n_chars = 14

    mat_ov = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    md     = ImageDraw.Draw(mat_ov)

    for col_x in (1370, 1410):
        for i in range(n_chars):
            ch   = random.choice(matrix_chars)
            my   = 68 + i * 30
            frac = i / (n_chars - 1)
            alpha = 200 if frac < 0.35 else (100 if frac < 0.65 else 35)
            md.text((col_x, my), ch, font=fonts["score_label"], fill=(*C_MATRIX, alpha))

    canvas = Image.alpha_composite(canvas, mat_ov)
    draw   = ImageDraw.Draw(canvas)

    # ─── Watermark ────────────────────────────────────────────────────────
    _draw_centered(draw, DATA_CENTER_X, 778,
                   "♦  KARA  ·  Hyperliquid Futures",
                   fonts["tiny"], C_WATERMARK)

    # ── Export ────────────────────────────────────────────────────────────
    final = canvas.convert("RGB")
    buf   = io.BytesIO()
    final.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.read()
