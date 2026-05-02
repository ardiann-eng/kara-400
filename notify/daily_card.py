"""
KARA Daily PnL Card Generator
Landscape 900×560 — inspired by Axiom Pro monthly card style.
Layout: month/date prominent top-left, big PnL number, stats row, KARA character right.
"""

from __future__ import annotations
import io
import os
import logging

log = logging.getLogger("kara.daily_card")

CANVAS_W = 900
CANVAS_H = 560

C_PROFIT      = (74, 222, 128)
C_LOSS        = (248, 113, 113)
C_TEXT        = (245, 244, 250)
C_LABEL       = (160, 155, 185)
C_MUTED       = (100, 95, 125)
C_DIVIDER     = (180, 160, 255, 40)
C_WATERMARK   = (120, 115, 145)
C_DARK_BG     = (8, 6, 18)
C_ACCENT      = (130, 100, 255)   # purple accent for KARA brand


def _load_fonts():
    from PIL import ImageFont
    _here = os.path.dirname(os.path.abspath(__file__))

    bold_paths = [
        os.path.join(_here, "assets", "fonts", "bold.ttf"),
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/calibrib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
    ]
    regular_paths = [
        os.path.join(_here, "assets", "fonts", "regular.ttf"),
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibri.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
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
        "brand":       _font(bold_path,    16),
        "date_label":  _font(regular_path, 17),
        "date_main":   _font(bold_path,    36),   # "02 May 2026"
        "hero":        _font(bold_path,    96),   # big PnL number
        "hero_sub":    _font(bold_path,    28),   # PnL %
        "stat_label":  _font(regular_path, 15),
        "stat_value":  _font(bold_path,    21),
        "badge":       _font(bold_path,    14),
        "watermark":   _font(regular_path, 12),
    }


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


def _rrect(draw, x0, y0, x1, y1, r, fill=None, outline=None, width=1):
    try:
        draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill, outline=outline, width=width)
    except (AttributeError, TypeError):
        draw.rectangle([x0, y0, x1, y1], fill=fill, outline=outline, width=width)


def generate_daily_card(
    date_str: str,           # e.g. "02 May 2026"
    daily_pnl_usd: float,
    daily_pnl_pct: float,
    start_balance: float,
    end_balance: float,
    total_trades: int,
    win_trades: int,
    loss_trades: int,
    best_trade_pnl: float,   # USD
    worst_trade_pnl: float,  # USD
    max_drawdown_pct: float,
    trading_mode: str,       # "STANDARD" or "SCALPER"
    bg_path: str = "notify/assets/kara_bg.png",
) -> bytes:
    """Returns PNG bytes of the daily summary card."""
    from PIL import Image, ImageDraw

    is_profit  = daily_pnl_usd >= 0
    pnl_color  = C_PROFIT if is_profit else C_LOSS
    sign       = "+" if is_profit else "-"

    # ── 1. Base canvas ────────────────────────────────────────────────────
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (*C_DARK_BG, 255))

    # ── 2. Subtle gradient overlay (top purple tint like Axiom) ──────────
    grad = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    gd   = ImageDraw.Draw(grad)
    for i in range(180):
        alpha = int(35 * (1 - i / 180))
        gd.line([(0, i), (CANVAS_W, i)], fill=(*C_ACCENT, alpha))
    canvas = Image.alpha_composite(canvas, grad)

    # ── 3. KARA character — right side ────────────────────────────────────
    for candidate in [
        bg_path,
        os.path.join(os.path.dirname(__file__), "assets", "kara_bg.png"),
    ]:
        if os.path.exists(candidate):
            bg_path = candidate
            break

    if os.path.exists(bg_path):
        try:
            bg_full = Image.open(bg_path).convert("RGBA")
            orig_w, orig_h = bg_full.size
            crop_right  = int(orig_w * 0.60)
            crop_bottom = int(orig_h * 0.92)
            face_crop   = bg_full.crop((0, 0, crop_right, crop_bottom))

            kara_h = CANVAS_H
            kara_w = int(face_crop.width * kara_h / face_crop.height)
            face_resized = face_crop.resize((kara_w, kara_h), Image.LANCZOS)

            # Stronger left fade so text area stays clean
            fade = Image.new("RGBA", face_resized.size, (0, 0, 0, 0))
            fd   = ImageDraw.Draw(fade)
            fade_width = min(320, kara_w)
            for i in range(fade_width):
                alpha = int(240 * (1 - i / fade_width))
                fd.line([(i, 0), (i, kara_h)], fill=(*C_DARK_BG, alpha))

            face_faded = Image.alpha_composite(face_resized, fade)
            kara_x = CANVAS_W - kara_w + int(kara_w * 0.05)
            canvas.paste(face_faded, (kara_x, 0), face_faded)
        except Exception as e:
            log.warning(f"[DailyCard] Failed to load background: {e}")

    draw  = ImageDraw.Draw(canvas)
    fonts = _load_fonts()

    MARGIN = 38
    DATA_W = int(CANVAS_W * 0.55)   # left data zone

    # ═══════════════════════════════════════════════════════════════════════
    # TOP BAR — brand left, trading mode badge right
    # ═══════════════════════════════════════════════════════════════════════
    draw.text((MARGIN, 22), "KARA", font=fonts["brand"], fill=C_LABEL)
    draw.text((MARGIN + 62, 22), "·  Hyperliquid Futures", font=fonts["brand"], fill=C_MUTED)

    # Mode badge (top-right of data zone)
    mode_color  = C_PROFIT if trading_mode.upper() == "SCALPER" else C_ACCENT
    mode_label  = "⚡ SCALPER" if trading_mode.upper() == "SCALPER" else "📊 STANDARD"
    mode_tw     = _text_w(draw, mode_label, fonts["badge"])
    mode_ov     = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    mode_pd     = ImageDraw.Draw(mode_ov)
    mx0 = DATA_W - mode_tw - 28
    my0 = 15
    _rrect(mode_pd, mx0, my0, mx0 + mode_tw + 20, my0 + 26, 13,
           fill=(*mode_color, 35), outline=(*mode_color, 110), width=1)
    canvas = Image.alpha_composite(canvas, mode_ov)
    draw   = ImageDraw.Draw(canvas)
    draw.text((mx0 + 10, my0 + 5), mode_label, font=fonts["badge"], fill=mode_color)

    # ═══════════════════════════════════════════════════════════════════════
    # DATE — "Daily Report" label + "02 May 2026"
    # ═══════════════════════════════════════════════════════════════════════
    draw.text((MARGIN, 60), "Daily Report", font=fonts["date_label"], fill=C_LABEL)
    draw.text((MARGIN, 82), date_str, font=fonts["date_main"], fill=C_TEXT)

    # ═══════════════════════════════════════════════════════════════════════
    # HERO — Big PnL USD (Axiom-style: prominent colored number in block)
    # ═══════════════════════════════════════════════════════════════════════
    pnl_abs  = abs(daily_pnl_usd)
    if pnl_abs >= 10000:
        hero_text = f"{sign}${pnl_abs:,.0f}"
    elif pnl_abs >= 100:
        hero_text = f"{sign}${pnl_abs:,.1f}"
    else:
        hero_text = f"{sign}${pnl_abs:.2f}"

    draw.text((MARGIN, 132), hero_text, font=fonts["hero"], fill=pnl_color)

    # PnL % below hero
    pct_abs = abs(daily_pnl_pct * 100) if abs(daily_pnl_pct) < 10 else abs(daily_pnl_pct)
    if pct_abs >= 100:
        pct_text = f"{sign}{pct_abs:.0f}%"
    elif pct_abs >= 10:
        pct_text = f"{sign}{pct_abs:.1f}%"
    else:
        pct_text = f"{sign}{pct_abs:.2f}%"
    draw.text((MARGIN, 240), pct_text, font=fonts["hero_sub"], fill=pnl_color)

    # ═══════════════════════════════════════════════════════════════════════
    # STATS ROW 1 — PNL / Start Balance / End Balance  (Axiom-style 3-col)
    # ═══════════════════════════════════════════════════════════════════════
    div1_y = 285
    draw.line([(MARGIN, div1_y), (DATA_W - MARGIN // 2, div1_y)],
              fill=(*C_DIVIDER[:3], 55), width=1)

    stat_y  = div1_y + 14
    col_gap = (DATA_W - MARGIN * 2) // 3
    c1x = MARGIN
    c2x = MARGIN + col_gap
    c3x = MARGIN + col_gap * 2

    # PNL
    draw.text((c1x, stat_y),      "PNL",           font=fonts["stat_label"], fill=C_LABEL)
    draw.text((c1x, stat_y + 20), pct_text,         font=fonts["stat_value"], fill=pnl_color)

    # Start Balance
    draw.text((c2x, stat_y),      "Start Balance",  font=fonts["stat_label"], fill=C_LABEL)
    draw.text((c2x, stat_y + 20), f"${start_balance:,.2f}", font=fonts["stat_value"], fill=C_TEXT)

    # End Balance
    draw.text((c3x, stat_y),      "End Balance",    font=fonts["stat_label"], fill=C_LABEL)
    draw.text((c3x, stat_y + 20), f"${end_balance:,.2f}",   font=fonts["stat_value"], fill=C_TEXT)

    # ═══════════════════════════════════════════════════════════════════════
    # STATS ROW 2 — Trades / Win / Loss / Best / Worst / MaxDD
    # ═══════════════════════════════════════════════════════════════════════
    div2_y = stat_y + 58
    draw.line([(MARGIN, div2_y), (DATA_W - MARGIN // 2, div2_y)],
              fill=(*C_DIVIDER[:3], 45), width=1)

    s2y     = div2_y + 14
    col2_gap = (DATA_W - MARGIN * 2) // 3

    # Trades + win/loss breakdown
    win_rate = int(win_trades / total_trades * 100) if total_trades > 0 else 0
    wl_color = C_PROFIT if win_rate >= 50 else C_LOSS
    draw.text((c1x, s2y),      "Trades",          font=fonts["stat_label"], fill=C_LABEL)
    draw.text((c1x, s2y + 20), f"{total_trades}",  font=fonts["stat_value"], fill=C_TEXT)
    # tiny win/loss sub-label
    draw.text((c1x, s2y + 44), f"{win_trades}W  {loss_trades}L  ({win_rate}%)",
              font=fonts["stat_label"], fill=wl_color)

    # Best / Worst trade
    best_sign  = "+" if best_trade_pnl  >= 0 else ""
    worst_sign = "+" if worst_trade_pnl >= 0 else ""
    draw.text((c2x, s2y),      "Best Trade",      font=fonts["stat_label"], fill=C_LABEL)
    draw.text((c2x, s2y + 20), f"{best_sign}${best_trade_pnl:.2f}",
              font=fonts["stat_value"], fill=C_PROFIT if best_trade_pnl >= 0 else C_LOSS)

    draw.text((c3x, s2y),      "Worst Trade",     font=fonts["stat_label"], fill=C_LABEL)
    draw.text((c3x, s2y + 20), f"{worst_sign}${worst_trade_pnl:.2f}",
              font=fonts["stat_value"], fill=C_PROFIT if worst_trade_pnl >= 0 else C_LOSS)

    # Max Drawdown sub-row
    draw.text((c2x, s2y + 44), "Max DD",           font=fonts["stat_label"], fill=C_LABEL)
    dd_color = C_LOSS if max_drawdown_pct > 5 else C_LABEL
    draw.text((c3x, s2y + 44), f"{max_drawdown_pct:.1f}%", font=fonts["stat_label"], fill=dd_color)

    # ═══════════════════════════════════════════════════════════════════════
    # WATERMARK
    # ═══════════════════════════════════════════════════════════════════════
    draw.text((MARGIN, CANVAS_H - 22),
              "♦  KARA  ·  Hyperliquid Futures",
              font=fonts["watermark"], fill=C_WATERMARK)

    # ── Export ────────────────────────────────────────────────────────────
    final = canvas.convert("RGB")
    buf   = io.BytesIO()
    final.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.read()
