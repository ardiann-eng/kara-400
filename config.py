"""
KARA Bot - Configuration
All settings, constants, and environment loading.
Edit .env for secrets; edit this file for strategy parameters.
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────
# ENVIRONMENT
# ──────────────────────────────────────────────
KARA_VERSION = "8.0.1"  # Observability Protocol: Railway telemetry, rule-based autopsy, dynamic changelog generator
DATA_SOURCE = os.getenv("KARA_DATA_SOURCE", "mainnet").lower() # "mainnet" | "testnet"
TRADE_MODE  = os.getenv("KARA_TRADE_MODE", "paper").lower()    # "paper" | "live"
FULL_AUTO   = os.getenv("KARA_FULL_AUTO", "true").lower() == "true"

# ── Trading Strategy Mode ──────────────────────────────────────────────────
# Switch between "standard" (swing/positional) and "scalper" (ultra-aggressive)
# Change at runtime via Telegram /scalper or /standard, or via Dashboard.
TRADING_MODE = "scalper"  # KARA runs exclusively in Scalper Mode

MODE = TRADE_MODE  # alias for backward compatibility

# ──────────────────────────────────────────────
# CURRENCY & MULTI-USER PAPER DEFAULTS
# ──────────────────────────────────────────────
USD_TO_IDR = 16000.0  # Fixed exchange rate for Telegram display
PAPER_BALANCE_IDR = 1_000_000.0  # Default wallet balance in IDR
PAPER_BALANCE_USD = PAPER_BALANCE_IDR / USD_TO_IDR

# ──────────────────────────────────────────────
# HYPERLIQUID CREDENTIALS
# ──────────────────────────────────────────────
WALLET_ADDRESS   = os.getenv("HL_WALLET_ADDRESS", "").strip()
PRIVATE_KEY      = os.getenv("HL_PRIVATE_KEY", "").strip()    # Master / Fallback Key
FERNET_KEY       = os.getenv("HL_FERNET_KEY", os.getenv("FERNET_KEY", "")).strip()  # For Multi-User Encryption

# Handle placeholder values (0x..., 0x, etc.)
def _is_placeholder(val: str) -> bool:
    """Check if value is a placeholder like '0x...' or '0x'."""
    if not val:
        return False
    val = val.lower().strip()
    return val in ("0x", "0x...", "0xnone", "none", "...")

if _is_placeholder(WALLET_ADDRESS):
    WALLET_ADDRESS = ""
if _is_placeholder(PRIVATE_KEY):
    PRIVATE_KEY = ""

HL_TESTNET       = MODE == "paper"                       # auto-set from mode

# ──────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", ""))
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_allowed_ids_str = os.getenv("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS = [x.strip() for x in _allowed_ids_str.split(",") if x.strip()]
if TELEGRAM_CHAT_ID and TELEGRAM_CHAT_ID not in ALLOWED_CHAT_IDS:
    ALLOWED_CHAT_IDS.append(TELEGRAM_CHAT_ID)

# Access Code gate for new users
ACCESS_CODE      = os.getenv("KARA_ACCESS_CODE", "KARA2026")
_alt_codes_str   = os.getenv("KARA_ACCESS_CODES_ALT", "")
ACCESS_CODE_ALTS = [x.strip() for x in _alt_codes_str.split(",") if x.strip()]
# All valid codes (primary + alternates), case-insensitive match happens at runtime
ALL_ACCESS_CODES = list({ACCESS_CODE} | set(ACCESS_CODE_ALTS))
ACCESS_MAX_TRIES = 3              # block after this many wrong attempts
ACCESS_BLOCK_HOURS = 1            # block duration in hours

from dotenv import load_dotenv
load_dotenv(override=True)  # Aggressive load

# ─────────── DASHBOARD ───────────
DASHBOARD_HOST   = "0.0.0.0"
DASHBOARD_PORT   = int(os.getenv("PORT", 8080))
_railway_domain  = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
DASHBOARD_URL    = os.getenv("DASHBOARD_URL", f"https://{_railway_domain}" if _railway_domain else "")

SECRET_KEY       = os.getenv("SECRET_KEY", "CHANGEME")
# NOTE: FERNET_KEY already set above from HL_FERNET_KEY / FERNET_KEY. Do NOT re-assign here.

# ──────────────────────────────────────────────
# LIVE MODE RISK LIMITS (tighter than paper defaults)
# Set via env; paper mode keeps its own relaxed limits in RiskConfig/ScalperConfig.
# ──────────────────────────────────────────────
LIVE_MAX_DRAWDOWN_PCT    = float(os.getenv("KARA_LIVE_MAX_DRAWDOWN_PCT", "0.25"))   # 25%
LIVE_DAILY_LOSS_HARD_PCT = float(os.getenv("KARA_LIVE_DAILY_LOSS_HARD_PCT", "0.15"))  # 15%

# ──────────────────────────────────────────────
# DATABASE & PERSISTENCE
# ──────────────────────────────────────────────
# STORAGE_BASE: di Railway set ke mount point volume (/data).
# Locally defaults ke ./data agar tidak perlu ubah apapun saat dev.
STORAGE_BASE     = os.getenv("STORAGE_BASE", "data")
DB_PATH          = os.getenv("DB_PATH", os.path.join(STORAGE_BASE, "kara_data.db"))
USER_DB_PATH     = os.path.join(STORAGE_BASE, "users.json")
TG_STATE_PATH    = os.path.join(STORAGE_BASE, "telegram_state.json")
STORAGE_DIR      = STORAGE_BASE
REDIS_URL        = os.getenv("REDIS_URL", "")           # optional

# ──────────────────────────────────────────────
# TRADING ASSETS
# ──────────────────────────────────────────────
WATCHED_ASSETS = ["BTC", "ETH", "SOL", "ARB", "DOGE"]

# SCALPER_ASSETS: berdasarkan hasil paper trading aktual (win-rate terbaik)
# ZEC: 74.5% WR | kBONK: 60% WR | SPX: 100% WR | COMP: 91.7% WR
# REZ: 77.8% WR | PYTH: high WR | MON, VVV: emerging high-signal assets
SCALPER_ASSETS = ["ZEC", "kBONK", "SPX", "COMP", "REZ", "PYTH", "MON", "VVV"]

# ──────────────────────────────────────────────
# RISK MANAGEMENT  (safe defaults for students)
# ──────────────────────────────────────────────
@dataclass
class RiskConfig:
    # Leverage
    default_leverage:        int   = 10       # 10x default (scalping efficiency)
    max_leverage:            int   = 10       # hard cap
    margin_type:             str   = "isolated"

    # Position sizing
    risk_per_trade_pct:      float = 0.010    # 1.0% of equity per trade
    max_risk_per_trade_pct:  float = 0.025    # 2.5% absolute cap
    fixed_margin_per_position: float = 10.0  # [NEW] Default $10 margin per trade (allows 5-6 concurrent trades)

    # Paper trade mode (tight, for fast data collection)
    paper_sl_pct:            float = 0.020    # 2.0% stop loss
    paper_tp1_pct:           float = 0.012    # 1.2% TP1
    paper_tp2_pct:           float = 0.022    # 2.2% TP2

    # Stop-loss / Take-profit defaults
    default_sl_pct:          float = 0.030    # 3.0% from entry
    tp1_pct:                 float = 0.014    # +1.4% -> close 40%
    tp2_pct:                 float = 0.025    # +2.5% -> close 35%
    trailing_pct:            float = 0.025    # 2.5% trailing on remainder
    # Daily / drawdown guards
    # Paper mode: longgar supaya data terkumpul cepat.
    # Live mode: ketat — override oleh LIVE_RISK di bawah.
    daily_loss_limit_pct:    float = 0.80     # paper: 80% daily loss -> warning
    daily_loss_hard_pct:     float = 0.90     # paper: 90% -> full stop today
    max_drawdown_pct:        float = 0.95     # paper: 95% total drawdown kill-switch
    post_loss_cooldown_hrs:  float = 5.0      # cooldown triggered after 50% daily loss

    # Concurrent positions
    max_concurrent_positions: int  = 10       # max 10 positions
    max_concurrent_auto:       int  = 10       # max 10 positions (full-auto)

    # Time-based Exit (Solution 1)
    time_based_exit_hours:     float = 8.0      # force close after 8h if profit 1-3%
    time_based_min_profit:     float = 0.01     # 1%
    time_based_max_profit:     float = 0.03     # 3%

    # Dynamic TP (Solution 2)
    dynamic_tp_oi_threshold:   float = 50_000_000  # $50M OI threshold for small cap
    small_cap_tp1_pct:         float = 0.008       # 0.8%
    small_cap_tp2_pct:         float = 0.015       # 1.5%
    vol_tp_multiplier:         float = 0.80        # 20% reduction for high vol

    # Partial TP ratios  (4-stage: 25% each at TP1/TP2/TP3, trail last 25%)
    tp1_close_ratio:         float = 0.25     # close 25% at TP1
    tp2_close_ratio:         float = 0.333    # close 33% of remaining (~25% original) at TP2
    tp3_close_ratio:         float = 0.50     # close 50% of remaining (~25% original) at TP3
    # TP3 target = sl_pct * tp_mult * 1.5 (3:1 R:R on remainder)
    tp3_pct:                 float = 0.045    # default fallback if not calculated dynamically
    # ATR-Based (Opsi B) Calibration
    enable_atr_sl:           bool  = True     # Use volatility-based SL
    atr_multiplier:          float = 2.0      # ATR lookback buffer
    atr_lookback:            int   = 14       # candles for calculation
    # ATR-based trailing stop on last position piece
    atr_trailing_multiplier: float = 2.0      # trail = entry_atr * 2.0 (industry standard)

    # Momentum-based time exit thresholds (Fix 6)
    time_exit_pullback_pct:  float = 0.15     # if price retraces 15% of TP1 distance, exit (was 20%)
    time_exit_flatline_pct:  float = 0.0015   # < 0.15% move in 45min = dead market
    time_exit_flatline_mins: int   = 45       # window for flatline check (was 30)
    time_exit_hard_hours:    float = 6.0      # safety net: force-exit after 6h below TP1

    # remaining 37.5% uses trailing stop

RISK = RiskConfig()

# ──────────────────────────────────────────────
# SCALPER MODE CONFIG  ⚠️ EXTREME RISK
# [AUDIT OVERHAUL 2026-05-13] Dikalibrasi ulang berdasarkan 108 trade data.
# Masalah sebelumnya: SL ROE -51.6%, time exit 94%, trailing stop 2/108.
# ──────────────────────────────────────────────
@dataclass
class ScalperConfig:
    """
    Scalper mode — high-frequency scalping, dikalibrasi untuk modal 1 juta IDR (~$62.50).

    [AUDIT FIX 2026-05-13] Perubahan kritis:
    - C3: Leverage 25x→15x, SL max 2.0%→1.2% → max ROE loss -18% (was -50%)
    - C4: Grace period 35m→10m → total max hold 30m (was 55m)
    - C5: TP multiples turun → trailing stop lebih sering terpicu
    - C6: Sizing naik → profit meaningful vs fee

    Strategi objektif untuk modal $62.50:
    ┌─────────────────────────────────────────────────────────────┐
    │ Target: 2-5% equity/hari = $1.25-$3.12/hari                │
    │ Win Rate minimum: 52% (dengan R:R 1.2:1 = positive EV)     │
    │ Max loss per trade: -18% ROE = -$1.12 (pada margin $6.25)  │
    │ Avg win target: +10-15% ROE = $0.62-$0.94                  │
    │ Max daily trades: 15-20 (fee budget ~$0.20)                 │
    │ Recovery dari 1 SL: butuh 2 winning trades (was 14!)        │
    └─────────────────────────────────────────────────────────────┘
    """
    # ── [C3 FIX] Leverage — turunkan untuk mengurangi amplifikasi ROE ──
    # Sebelum: 25x × 2% SL = -50% ROE per SL hit (BENCANA)
    # Sesudah: 15x × 1.2% SL = -18% ROE per SL hit (SURVIVABLE)
    # 1 SL sekarang = 2 winning trades untuk recovery, bukan 14.
    default_leverage:        int   = 15       # [AUDIT] 25→15x: max ROE loss -18% (was -50%)
    max_leverage:            int   = 20       # [AUDIT] 35→20x: hard cap realistis

    # ── [C6 FIX] Position sizing — naik untuk modal kecil $62.50 ──
    # Sebelum: 8% risk × vol_scale → notional ~$40 → profit $0.15 < fee
    # Sesudah: 12% risk → notional ~$75-100 → profit $0.50+ > fee
    # Dengan leverage 15x dan SL 1.2%: risk = $62.5 × 0.12 = $7.50 per trade
    risk_per_trade_pct:      float = 0.12     # [AUDIT] 8%→12%: bigger size for small capital
    max_risk_per_trade_pct:  float = 0.15     # [AUDIT] 12%→15%: absolute cap raised
    min_risk_per_trade_pct:  float = 0.08     # [AUDIT] 5%→8%: floor raised for meaningful P&L
    fixed_margin_per_position: float = 0.0   # 0 = use pct, not fixed margin

    # ── [C3 FIX] SL/TP — dikalibrasi untuk 15x leverage ──
    # SL max ROE: 1.5% × 15x = -22.5% (survivable)
    # SL min ROE: 0.7% × 15x = -10.5% (noise protection floor)
    sl_pct:                  float = 0.0150   # [AUDIT] 1.2%→1.5%: fallback SL for 15x lev
    atr_sl_enabled:          bool  = True     # enable ATR-adaptive SL for scalper
    atr_sl_multiplier:       float = 2.0      # [AUDIT] 1.5→2.0: SL = 2× ATR (standard noise filter)
    sl_pct_min:              float = 0.007    # [AUDIT] 0.4%→0.7%: floor raised to avoid market noise
    sl_pct_max:              float = 0.015    # [AUDIT] 1.2%→1.5%: ceiling raised for high-vol assets
    # RR enforcement: TP1 minimum = sl_pct × tp1_min_rr ; TP2 minimum = sl_pct × tp2_min_rr
    tp1_min_rr_to_sl:        float = 0.6      # TP1 ≥ 0.6× SL distance
    tp2_min_rr_to_sl:        float = 1.5      # TP2 ≥ 1.5× SL distance (positive RR enforcement)
    tp1_pct:                 float = 0.0060   # [AUDIT] 0.75%→0.60%: more achievable in 20m
    tp2_pct:                 float = 0.0100   # [AUDIT] 1.25%→1.00%: more achievable in 20m
    trailing_pct:            float = 0.0040   # [AUDIT] 0.50%→0.40%: tighter trail

    # ── [C4 FIX] Timing — potong grace period drastis ──
    # Sebelum: 20m + 35m grace = 55m max hold → 23 big losses dari posisi yang terseret
    # Sesudah: 20m + 10m grace = 30m max hold → cut loss lebih cepat
    max_hold_minutes:        float = 20.0     # force close after 20min if no TP hit
    max_hold_grace_minutes:  float = 10.0     # [AUDIT] 35→10m: total max 30m, bukan 55m
    max_hold_soft_floor_pct: float = -0.010   # [AUDIT] -2%→-1%: cut loss lebih cepat (1%×15x = -15% ROE)
    scan_interval_seconds:   int   = 15       # scan every 15s to avoid HL rate limits

    # Score threshold — HARD THRESHOLD SCALPER (TIDAK BISA DIUBAH USER)
    min_score_to_enter:      int   = 57       # ⚠️ HARD: scalper entry gate
    signal_cooldown_minutes: int   = 5        # 5 min cooldown scalper
    mtf_confirm_enabled:     bool  = True     # require 15m trend confirmation
    mtf_confirm_interval:    str   = "15m"
    mtf_confirm_lookback:    int   = 32       # ~8h on 15m candles

    # Concurrent positions
    max_concurrent_positions: int  = 5        # max 5 scalper positions

    # Partial TP ratios (scalper: heavier close at TP1/TP2 given tight hold window)
    tp1_close_ratio:         float = 0.50     # 50% on TP1
    tp2_close_ratio:         float = 0.667    # 67% of remaining at TP2 (~33% original)
    tp3_close_ratio:         float = 1.0      # close all remaining at TP3
    tp3_pct:                 float = 0.015    # [AUDIT] 2.0%→1.5%: achievable target
    # ATR trailing — scalper uses fixed pct (trade too short for ATR trail to matter)
    atr_trailing_multiplier: float = 2.0

    # Pyramid — scale in when profit > 0.4%, REQUIRES CONFIRMATION
    enable_pyramid:          bool  = False    # off by default, Telegram confirm required
    pyramid_at_profit_pct:   float = 0.004   # 0.4%

    # Daily guard (scalper can lose fast)
    # [AUDIT] Paper mode juga perlu proteksi realistis.
    daily_loss_hard_pct:     float = 0.15    # [AUDIT] 90%→15%: stop setelah -15% equity/hari ($9.37)
    max_drawdown_pct:        float = 0.30    # [AUDIT] 95%→30%: kill-switch at -30% total ($18.75)
    post_loss_cooldown_hrs:  float = 2.0     # cooldown setelah daily limit

    # MTF Score weights
    mtf_score_bonus:         int = 12        # bonus if 1m aligns with 15m trend
    mtf_score_penalty:       int = -5        # penalty if counter-trend

    # ── Momentum exit — Multi-confirmation refactor (v2) ─────────────────
    # [AUDIT 2026-05-13] RE-ENABLED dengan leverage 15x.
    # Root cause kegagalan sebelumnya: HTF override → 2% threshold × 25x = -50% ROE.
    # Dengan 15x: 2% threshold × 15x = -30% ROE → masih besar tapi survivable.
    # Dikurangi ke 1.5% × 15x = -22.5% ROE max.
    momentum_exit_enabled:            bool  = True    # [AUDIT] RE-ENABLED: safe at 15x leverage
    momentum_exit_min_minutes:        float = 3.0     # min hold 3 menit

    # Layer 1 — Minimum pullback (anti-noise)
    momentum_exit_min_pullback_pct:   float = 0.005   # floor 0.5%
    momentum_exit_atr_pullback_mult:  float = 1.2     # threshold = max(0.5%, ATR14% * 1.2)

    # Layer 2 — Volume confirmation
    momentum_exit_volume_mult:        float = 1.3     # current vol harus >= SMA20 * 1.3

    # Layer 3 — Trend structure break (EMA cross)
    momentum_exit_ema_fast:           int   = 9       # EMA fast period
    momentum_exit_ema_slow:           int   = 21      # EMA slow period

    # Layer 4 — Momentum indicators
    momentum_exit_rsi_threshold:      float = 48.0    # RSI < 48 = momentum fading

    # Layer 5 — HTF trend filter (15m EMA)
    momentum_exit_htf_ema_fast:       int   = 20      # 15m EMA fast
    momentum_exit_htf_ema_slow:       int   = 50      # 15m EMA slow
    momentum_exit_htf_uptrend_pullback: float = 0.015  # [AUDIT] 2.0%→1.5%: 1.5%×15x = -22.5% ROE max

    # ── Early trailing: lock profit sebelum TP1 ──
    early_trail_enabled:         bool  = True
    early_trail_activation_pct:  float = 0.003  # [AUDIT] 0.4%→0.3%: lock profit lebih awal
    early_trail_distance_pct:    float = 0.002  # [AUDIT] 0.3%→0.2%: tighter trail

    # ── [C5 FIX] Partial profit & breakeven — turunkan threshold ──
    # Sebelum: TP1=1.0×SL, TP2=1.5×SL, Trail=2.0×SL → hampir tidak tercapai (2/108 trade)
    # Sesudah: TP1=0.7×SL, TP2=1.0×SL, Trail=1.3×SL → tercapai lebih sering
    # Dengan SL ~0.8%: TP1=0.56%, TP2=0.80%, Trail=1.04% → realistis dalam 20m
    partial_tp1_at_sl_multiple: float = 0.7    # [AUDIT] 1.0→0.7: close 40% lebih awal
    partial_tp2_at_sl_multiple: float = 1.0    # [AUDIT] 1.5→1.0: close 30% lebih awal
    partial_tp3_trail_at: float = 1.3          # [AUDIT] 2.0→1.3: trail aktif lebih awal
    breakeven_trigger_at_sl_multiple: float = 0.5  # [AUDIT] 0.8→0.5: breakeven lebih cepat
    scale_in_threshold_pct: float = 0.005      # +0.5% in 3min = scale in 50%
    scale_in_threshold_sec: int = 180           # 3 min window for scale-in check
    max_scale_ins: int = 1                     # max 1 add per position
    reentry_window_sec: int = 180              # 3 min window for stop-out re-entry

SCALPER = ScalperConfig()

# ──────────────────────────────────────────────
# LIVE MODE RISK OVERRIDE
# Berlaku HANYA saat TRADE_MODE=live. Paper mode pakai nilai di atas (longgar).
# Tujuan: paper mode bisa kumpulkan data sebanyak mungkin,
#         live mode melindungi modal nyata.
# ──────────────────────────────────────────────
if TRADE_MODE == "live":
    # Standard live risk: berhenti jauh sebelum bangkrut
    RISK.daily_loss_limit_pct  = 0.05   # 5% daily loss → warning & kurangi size
    RISK.daily_loss_hard_pct   = 0.08   # 8% daily loss → stop trading hari ini
    RISK.max_drawdown_pct      = 0.20   # 20% total drawdown → kill switch

    # Scalper live risk: scalper bisa rugi cepat, batas lebih ketat
    SCALPER.daily_loss_hard_pct = 0.06  # 6% daily loss → stop
    SCALPER.max_drawdown_pct    = 0.15  # 15% total drawdown → kill switch

# ──────────────────────────────────────────────
# SIGNAL ENGINE
# ──────────────────────────────────────────────
@dataclass
class SignalConfig:
    # ⚠️ HARD THRESHOLDS — TIDAK BISA DIUBAH USER (perubahan hanya via code)
    # [THRESHOLD FIX 2026-05-08] Berdasarkan data 55 trade aktual (avg skor 62.4):
    # threshold 57 = PnL +14.99 (50 trade) vs threshold 60 = hanya +2.95 (27 trade).
    # Skor 60-64 justru WR 28-30% — worst range. Threshold signal tetap 45 (filter noise saja).
    min_score_to_signal:     int   = 45       # filter awal: emit signal ke user (bukan entry gate)
    min_score_to_auto_trade: int   = 57       # entry gate: data terbukti optimal dari 55 trade
    signal_cooldown_minutes: int   = 15       # cooldown per asset between signals
    # FIX #4: SHORT trades had 57.6% WR and net -$12.55 in audit data.
    # Structural bias: positive funding/basis almost always favors LONG on Hyperliquid.
    # Raise SHORT threshold significantly to only execute highest-conviction SHORT signals.
    min_score_short_signal:  int   = 57       # [AUDIT Phase 1] SHORT same as LONG threshold (was 59)
    min_score_short_auto:    int   = 57       # [AUDIT Phase 1] SHORT auto-execute same as LONG

    # Bull-Bear gap (LONG vs SHORT berbeda threshold)
    min_bull_bear_gap:       int   = 18       # LONG: minimum gap bull vs bear pts
    min_bull_bear_gap_short: int   = 18       # [AUDIT Phase 1] SHORT same gap as LONG (was 20)

    # SHORT-specific filters (aktif saat ALLOW_SHORT = True)
    # Solusi 2: Funding rate confirmation
    # Real HL funding rates are typically +-0.00002; threshold must match that range
    # FIX: fr=0.000000 is NEUTRAL — perfectly fine for SHORT.
    # Only block SHORT when funding is strongly negative (shorts paying longs = market bullish).
    short_min_funding_rate:  float = -0.0001  # SHORT valid if funding >= -0.0001
                                               # Was 0.00001 which blocked neutral funding SHORTs
    # Solusi 3: Anti-trend filter
    short_max_uptrend_pct:   float = 0.03     # Block SHORT jika 24h trend > +3% (jangan lawan trend)

    # Session windows (UTC)
    ny_session_start_utc:    int   = 13       # 13:00 UTC = 09:00 ET
    ny_session_end_utc:      int   = 21       # 21:00 UTC = 17:00 ET
    london_start_utc:        int   = 8
    london_end_utc:          int   = 17

    # Session score bonuses / penalties (Hyperliquid volume distribution 2026)
    ny_session_bonus:        int   = 10       # NY dominates ~40% daily volume (slightly dampened)
    london_session_bonus:    int   = 4        # London-NY overlap ~25% volume
    asia_session_penalty:    int   = -10      # Asia 22:00-07:00 UTC ~20% volume — reduced penalty

    # OI / Funding thresholds
    oi_change_threshold_pct: float = 0.008     # 0.8% OI change = significant
    funding_extreme_threshold: float = 0.0003  # 0.03% per 8h = crowded
    funding_neutral_zone:    float = 0.00005   # ignore below this

    # Liquidation heatmap
    liq_cascade_threshold:   float = 0.015    # 1.5% price move triggers cascade
    liq_density_high:        float = 0.25     # 25% of OI in liq zone = high risk

    # Orderbook
    ob_imbalance_threshold:  float = 0.45     # 45% one-sided = imbalanced
    vwap_deviation_pct:      float = 0.002    # 0.2% from VWAP = notable

    # Market Structure (HH/HL) scoring weights
    structure_scalper_bonus: int = 8          # 1m HH/HL alignment bonus
    structure_standard_bonus:int = 6          # trend alignment bonus (cached regime trend)
    structure_mismatch_penalty: int = -4      # against structure direction



SIGNAL = SignalConfig()

# ──────────────────────────────────────────────
# TRADING DIRECTION FILTER
# ──────────────────────────────────────────────
# [FIX 3 - 2026-04-22] SHORT disabled - WR hanya 31.4%, total loss -$9.15
# SHORT stop_loss WR: 20%, total -$20.07
# Re-enable ONLY when paper trade menunjukkan SHORT WR > 50% untuk 30+ trades
ALLOW_SHORT = True   # Re-enabled dengan 3 filter proteksi: funding >= +0.0002, anti-trend > 2%, gap >= 28

# ── HARD RESET ON DEPLOY ──────────────────────────────────────────────────────
# Set KARA_HARD_RESET=true di env variable Railway/Docker sebelum deploy.
# Bot akan menghapus SEMUA data saat startup:
#   - Semua posisi terbuka          (paper_positions)
#   - Semua saldo user              (paper_state → reset ke Rp1.000.000)
#   - Semua journal/trade history   (trade_history)
#   - Semua sinyal history          (signals_history)
#   - Volatility cache              (vol_cache)
#   - Risk state per user           (risk_state)
# Yang TIDAK dihapus: konfigurasi user, wallet address, akses Telegram.
# PENTING: Ubah kembali ke false setelah deploy agar tidak reset terus!
HARD_RESET_ON_DEPLOY = os.getenv("KARA_HARD_RESET", "false").lower() == "true"

# ──────────────────────────────────────────────
# BLOCKED TRADING HOURS (UTC)
# ──────────────────────────────────────────────
# [FIX 4 - 2026-04-22] London open blocked - data 124 trades:
# 08:00 UTC: WR 7.1%, -$7.81 | 09:00 UTC: WR 21.4%, -$7.85
# Total 2 jam: -$15.66 = hampir seluruh account loss
# Volatilitas London open menyebabkan SL langsung kena opening spike
BLOCKED_HOURS_UTC = [8, 9]

# ──────────────────────────────────────────────
# MARKET SCANNING (smart selection filter)
# ──────────────────────────────────────────────
@dataclass
class MarketScanConfig:
    """Filter & select only top liquid markets (profit-oriented)."""

    # Scanning mode
    scan_mode: str = "top_volume"  # "all" | "top_volume" | "manual"

    # Liquid market filters (realistic 2026 params)
    max_markets_to_scan: int = 40        # top 40 markets by OI
    min_open_interest_usd: float = 1_500_000      # $1.5M minimum OI
    min_24h_volume_usd: float = 8_000_000         # $8M minimum volume
    min_funding_rate_abs: float = 0.00005         # absolute funding > 0.005%

    # Leverage filter (avoid very illiquid)
    min_max_leverage: int = 10            # must support ≥10x

    # Cache settings (don't spam API)
    market_cache_ttl_minutes: int = 5     # refresh list every 5 min

    # Fallback (if API fails, use these safe defaults)
    fallback_markets: list = field(default_factory=lambda: [
        "BTC", "ETH", "SOL", "HYPE", "FARTCOIN", "JUP", "ARB", "DOGE", "XRP",
        "ADA", "LINK", "UNI", "NEAR", "AVAX", "MATIC", "PEPE", "WIF", "BONK", "TIA",
        "OP", "SUI", "APT", "VINE", "FET", "RENDER", "INJ"
    ])

MARKET_SCAN = MarketScanConfig()

# ──────────────────────────────────────────────
# EXECUTION
# ──────────────────────────────────────────────
@dataclass
class ExecConfig:
    order_type:              str   = "post_only"  # post_only = maker rebate
    slippage_tolerance_pct:  float = 0.001        # 0.1%
    order_retry_count:       int   = 3
    order_retry_delay_s:     float = 1.5
    partial_fill_timeout_s:  int   = 30
    ws_reconnect_max_retries: int  = 10
    ws_reconnect_base_delay_s: float = 1.0
    ws_health_check_interval_s: int = 30

EXEC = ExecConfig()

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE  = os.getenv("LOG_FILE", "kara.log")
EXCEL_LOG_PATH = os.getenv("EXCEL_LOG_PATH", os.path.join(STORAGE_DIR, "trade_history.xlsx"))

# ──────────────────────────────────────────────
# BACKTEST
# ──────────────────────────────────────────────
BACKTEST_START = "2024-01-01"
BACKTEST_END   = "2024-12-31"
BACKTEST_INITIAL_CAPITAL = 1000.0   # USD (realistic for students)
