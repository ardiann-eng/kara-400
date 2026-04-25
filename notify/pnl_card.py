"""
KARA PnL Card Generator
Generates a premium PNG card using the KARA character image as background.
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
CANVAS_W = 1456
CANVAS_H = 816
RIGHT_START_X = 540       # data zone starts here
BLEND_START_X = 520       # soft gradient blend starts here
RIGHT_CENTER_X = 990      # midpoint of data zone (540-1440)
RIGHT_END_X = 1400

# ── Color palette ─────────────────────────────────────────────────────────────
C_PROFIT    = (134, 239, 172)        # mint green
C_LOSS      = (252, 165, 165)        # soft red
C_TEXT      = (241, 240, 245)        # near-white
C_LABEL     = (157, 154, 176)        # muted purple-grey
C_DIVIDER   = (196, 181, 253, 60)    # purple tint, low opacity
C_MATRIX    = (192, 132, 252)        # matrix rain purple
C_SCORE_BAR = (192, 132, 252)        # score fill purple
C_SCORE_BG  = (255, 255, 255, 25)    # score track
C_WATERMARK = (92, 90, 110)
C_OVERLAY   = (13, 13, 20)


# ─────────────────────────────────────────────────────────────────────────────
# FONT LOADER
# ─────────────────────────────────────────────────────────────────────────────

def _load_fonts():
    """Load system fonts with graceful fallback to ImageFont.load_default()."""
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

    bold_path = None
    regular_path = None

    for p in bold_paths:
        if os.path.exists(p):
            bold_path = p
            break

    for p in regular_paths:
        if os.path.exists(p):
            regular_path = p
            break

    def _font(path, size):
        if path:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
        return ImageFont.load_default()

    return {
        "tiny":   _font(regular_path, 11),
        "small":  _font(regular_path, 13),
        "label":  _font(regular_path, 16),
        "medium": _font(bold_path, 20),
        "large":  _font(bold_path, 28),
        "hero":   _font(bold_path, 64),
        "pct":    _font(bold_path, 22),
        "score_label": _font(regular_path, 12),
    }


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_price(price: float) -> str:
    if price >= 1000:
        return f"${price:,.2f}"
    elif price >= 1:
        return f"${price:.4f}"
    elif price >= 0.01:
        return f"${price:.5f}"
    else:
        return f"${price:.7f}"


def _fmt_hold(minutes: float) -> str:
    if minutes < 60:
        return f"{int(minutes)}m"
    h = int(minutes // 60)
    m = int(minutes % 60)
    return f"{h}h {m}m"


def _exit_reason_label(reason: str) -> str:
    mapping = {
        "trailing_stop": "TRAILING STOP",
        "tp1": "TP1",
        "tp2": "TP2",
        "stop_loss": "STOP LOSS",
    }
    return mapping.get(reason.lower(), reason.upper())


def _exit_reason_color(reason: str) -> tuple:
    r = reason.lower()
    if r == "trailing_stop":
        return (45, 212, 191)       # teal
    elif r in ("tp1", "tp2"):
        return C_PROFIT
    else:
        return C_LOSS


def _text_width(draw, text, font) -> int:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    except Exception:
        return len(text) * 8


def _draw_centered(draw, cx: int, y: int, text: str, font, color):
    w = _text_width(draw, text, font)
    draw.text((cx - w // 2, y), text, font=font, fill=color)


def _draw_right_aligned(draw, rx: int, y: int, text: str, font, color):
    w = _text_width(draw, text, font)
    draw.text((rx - w, y), text, font=font, fill=color)


def _rounded_rect(draw, x0, y0, x1, y1, radius: int, fill=None, outline=None, width=1):
    """Draw a rounded rectangle (Pillow-compatible, works on all versions)."""
    try:
        draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill, outline=outline, width=width)
    except (AttributeError, TypeError):
        # Fallback for older Pillow
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
    if not os.path.exists(bg_path):
        alt_paths = [
            "notify/kara.bg.png",
            os.path.join(os.path.dirname(__file__), "assets", "kara_bg.png"),
            os.path.join(os.path.dirname(__file__), "kara.bg.png"),
        ]
        for ap in alt_paths:
            if os.path.exists(ap):
                bg_path = ap
                break

    if os.path.exists(bg_path):
        bg = Image.open(bg_path).convert("RGBA")
        bg = bg.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)
    else:
        # No background found — create dark canvas
        log.warning(f"[PnLCard] Background not found at {bg_path}, using plain dark canvas")
        bg = Image.new("RGBA", (CANVAS_W, CANVAS_H), (8, 8, 16, 255))

    canvas = bg.copy()
    draw = ImageDraw.Draw(canvas)

    # ── 2. Soft blend gradient (x=520→580) ───────────────────────────────
    overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    blend_width = 60  # 520 to 580
    for i in range(blend_width):
        x = BLEND_START_X + i
        alpha = int(60 * (i / blend_width))
        od.line([(x, 0), (x, CANVAS_H)], fill=(*C_OVERLAY, alpha))
    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)

    # ── 3. Load fonts ─────────────────────────────────────────────────────
    fonts = _load_fonts()

    is_profit = pnl_usd >= 0
    pnl_color = C_PROFIT if is_profit else C_LOSS
    sign = "+" if is_profit else "-"
    side_lower = side.lower()

    # ── SECTION 1: Header ─────────────────────────────────────────────────

    # "K A R A" label — pushed right to stay off character
    draw.text((RIGHT_START_X + 20, 55), "K A R A", font=fonts["small"], fill=C_LABEL)

    # Divider line
    draw.line([(RIGHT_START_X + 20, 78), (RIGHT_END_X, 78)], fill=(*C_DIVIDER[:3], 80), width=1)

    # Asset name
    asset_text = asset.upper()
    ax = RIGHT_START_X + 20
    asset_w = _text_width(draw, asset_text, fonts["large"])
    draw.text((ax, 90), asset_text, font=fonts["large"], fill=C_TEXT)

    # Side pill
    pill_x = ax + asset_w + 12
    pill_text = side_lower.upper()
    pill_tw = _text_width(draw, pill_text, fonts["score_label"])
    pill_w = pill_tw + 24
    pill_h = 26
    pill_y = 92
    pill_r = 13

    if side_lower == "long":
        pill_bg   = (134, 239, 172, 60)
        pill_bord = (134, 239, 172, 180)
        pill_tc   = C_PROFIT
    else:
        pill_bg   = (252, 165, 165, 60)
        pill_bord = (252, 165, 165, 180)
        pill_tc   = C_LOSS

    pill_overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill_overlay)
    _rounded_rect(pd, pill_x, pill_y, pill_x + pill_w, pill_y + pill_h, pill_r, fill=pill_bg, outline=pill_bord, width=1)
    canvas = Image.alpha_composite(canvas, pill_overlay)
    draw = ImageDraw.Draw(canvas)

    pill_tx = pill_x + (pill_w - pill_tw) // 2
    pill_ty = pill_y + (pill_h - 13) // 2
    draw.text((pill_tx, pill_ty), pill_text, font=fonts["score_label"], fill=pill_tc)

    # Exit reason tag (right-aligned)
    er_label = _exit_reason_label(exit_reason)
    er_color = _exit_reason_color(exit_reason)
    _draw_right_aligned(draw, RIGHT_END_X, 96, er_label, fonts["score_label"], er_color)

    # ── SECTION 2: PnL Headline ───────────────────────────────────────────

    # Sign + dollar symbol + amount — build as single string so it renders correctly
    pnl_text = f"{sign}${abs(pnl_usd):.2f}"
    _draw_centered(draw, RIGHT_CENTER_X, 140, pnl_text, fonts["hero"], pnl_color)

    pct_text = f"({sign}{abs(pnl_pct) * 100:.2f}%)"
    # Draw pct directly (no alpha overlay needed — just use 80% brightness via color)
    pct_dimmed = tuple(int(c * 0.80) for c in pnl_color)
    pct_w = _text_width(draw, pct_text, fonts["pct"])
    draw.text((RIGHT_CENTER_X - pct_w // 2, 215), pct_text, font=fonts["pct"], fill=pct_dimmed)

    # ── Score bar ─────────────────────────────────────────────────────────

    score_clamped = max(0, min(score, 100))
    bar_x0 = RIGHT_START_X + 20
    bar_x1 = RIGHT_END_X
    bar_y  = 258
    bar_h  = 7
    bar_total_w = bar_x1 - bar_x0
    fill_w = int(bar_total_w * score_clamped / 100)

    draw.text((bar_x0, 243), "Score", font=fonts["score_label"], fill=C_LABEL)
    _draw_right_aligned(draw, bar_x1, 243, str(score), fonts["score_label"], C_SCORE_BAR)

    # Track (semi-transparent white bg)
    draw.rectangle([bar_x0, bar_y, bar_x1, bar_y + bar_h], fill=(255, 255, 255, 30))
    # Fill
    if fill_w > 3:
        draw.rectangle([bar_x0, bar_y, bar_x0 + fill_w, bar_y + bar_h], fill=(*C_SCORE_BAR, 220))

    # Divider at y=280
    draw.line([(bar_x0, 280), (RIGHT_END_X, 280)], fill=(*C_DIVIDER[:3], 80), width=1)

    # ── SECTION 3: 4-column stats grid ───────────────────────────────────

    cols = [
        (680,  "ENTRY",     _fmt_price(entry_price)),
        (870,  "EXIT",      _fmt_price(exit_price)),
        (1070, "HOLD TIME", _fmt_hold(hold_minutes)),
        (1270, "LEVERAGE",  f"{leverage}x"),
    ]

    for cx, label, value in cols:
        _draw_centered(draw, cx, 298, label, fonts["tiny"], C_LABEL)
        _draw_centered(draw, cx, 320, value, fonts["medium"], C_TEXT)

    # Divider at y=365
    draw.line([(bar_x0, 365), (RIGHT_END_X, 365)], fill=(*C_DIVIDER[:3], 80), width=1)

    # ── SECTION 4: Account stats ──────────────────────────────────────────

    ses_sign = "+" if session_pnl >= 0 else "-"
    ses_text = f"{ses_sign}${abs(session_pnl):.2f} ({ses_sign}{abs(session_pnl_pct) * 100:.2f}%)"
    ses_color = C_PROFIT if session_pnl >= 0 else C_LOSS

    _draw_centered(draw, 730, 378, "Session PnL", fonts["tiny"], C_LABEL)
    _draw_centered(draw, 730, 398, ses_text, fonts["label"], ses_color)

    eq_text = f"${total_equity:,.2f}"
    _draw_centered(draw, 1250, 378, "Total Equity", fonts["tiny"], C_LABEL)
    _draw_centered(draw, 1250, 398, eq_text, fonts["label"], C_TEXT)

    # ── SECTION 5: Matrix rain — two columns, larger font ────────────────

    matrix_chars = "0123456789ABCDEF"
    random.seed(score + len(asset) * 7)
    n_chars = 14

    matrix_overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    md = ImageDraw.Draw(matrix_overlay)

    for col_x in (1360, 1400):
        for i in range(n_chars):
            ch = random.choice(matrix_chars)
            my = 70 + i * 30
            frac = i / (n_chars - 1)
            if frac < 0.35:
                alpha = 210
            elif frac < 0.65:
                alpha = 110
            else:
                alpha = 40
            md.text((col_x, my), ch, font=fonts["score_label"], fill=(*C_MATRIX, alpha))

    canvas = Image.alpha_composite(canvas, matrix_overlay)
    draw = ImageDraw.Draw(canvas)

    # ── SECTION 6: Bottom watermark ──────────────────────────────────────

    wm_text = "♦  KARA  ·  Hyperliquid Futures"
    _draw_centered(draw, RIGHT_CENTER_X, 770, wm_text, fonts["tiny"], C_WATERMARK)

    # ── 7. Export to bytes ────────────────────────────────────────────────

    final = canvas.convert("RGB")
    buf = io.BytesIO()
    final.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.read()
