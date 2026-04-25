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
KARA_VERSION = "7.0.0"  # Intelligence Layer Update
DATA_SOURCE = os.getenv("KARA_DATA_SOURCE", "mainnet").lower() # "mainnet" | "testnet"
TRADE_MODE  = os.getenv("KARA_TRADE_MODE", "paper").lower()    # "paper" | "live"
FULL_AUTO   = True  # Force auto execution mode as requested

# ── Trading Strategy Mode ──────────────────────────────────────────────────
# Switch between "standard" (swing/positional) and "scalper" (ultra-aggressive)
# Change at runtime via Telegram /scalper or /standard, or via Dashboard.
TRADING_MODE = os.getenv("KARA_TRADING_MODE", "standard").lower()

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

# ─────────── DASHBOARD (ULTIMATE DEBUG) ───────────
DASHBOARD_HOST   = "0.0.0.0"
DASHBOARD_PORT   = int(os.getenv("PORT", 8080))

# [KARA_PORT_DEBUG] - Direct print to bypass logging
print("\n" + "="*50)
print(f"📡 [KARA_DEBUG] SYSTEM_PORT ENV: {os.getenv('PORT')}")
print(f"🚀 [KARA_DEBUG] BINDING DASHBOARD TO: {DASHBOARD_HOST}:{DASHBOARD_PORT}")
print("="*50 + "\n")

SECRET_KEY       = os.getenv("SECRET_KEY", "CHANGEME")
FERNET_KEY       = os.getenv("FERNET_KEY", "")

# ──────────────────────────────────────────────
# DATABASE & PERSISTENCE
# ──────────────────────────────────────────────
# Use /app/storage for Railway/Linux, fallback to 'data' for Local/Windows
STORAGE_DIR      = os.getenv("STORAGE_DIR", "/app/storage" if os.name == 'posix' else "data")
DB_PATH          = os.getenv("DB_PATH", os.path.join(STORAGE_DIR, "kara_data.db"))
USER_DB_PATH     = os.path.join(STORAGE_DIR, "users.json")
TG_STATE_PATH    = os.path.join(STORAGE_DIR, "telegram_state.json")
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
    paper_tp1_pct:           float = 0.015    # 1.5% TP1 (More realistic)
    paper_tp2_pct:           float = 0.030    # 3.0% TP2

    # Stop-loss / Take-profit defaults
    default_sl_pct:          float = 0.030    # 3.0% from entry (was 2.5%)
    tp1_pct:                 float = 0.018    # +1.8% -> close 40% (Scalp style)
    tp2_pct:                 float = 0.035    # +3.5% -> close 35%
    trailing_pct:            float = 0.03     # 3% trailing on remainder
    # Daily / drawdown guards
    daily_loss_limit_pct:    float = 0.80     # [RELAXED] 80% daily loss -> pause
    daily_loss_hard_pct:     float = 0.90     # [RELAXED] 90% -> full stop today
    max_drawdown_pct:        float = 0.95     # [RELAXED] 95% total drawdown kill-switch
    post_loss_cooldown_hrs:  float = 5.0      # [RELAXED] cooldown triggered after 50% daily loss

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

    # Partial TP ratios
    tp1_close_ratio:         float = 0.40
    tp2_close_ratio:         float = 0.35
    # ATR-Based (Opsi B) Calibration
    enable_atr_sl:           bool  = True     # Use volatility-based SL
    atr_multiplier:          float = 2.0      # ATR lookback buffer
    atr_lookback:            int   = 14       # candles for calculation
    
    # remaining 25% uses trailing stop

RISK = RiskConfig()

# ──────────────────────────────────────────────
# SCALPER MODE CONFIG  ⚠️ EXTREME RISK
# Ultra-aggressive scalping: 10-40 trades/day, max 12min hold time.
# Only activate with full risk understanding.
# ──────────────────────────────────────────────
@dataclass
class ScalperConfig:
    """
    Scalper mode — ultra-aggressive, high-frequency.
    ⚠️  WARNING: 35x leverage + 13% risk per trade. Use only if you understand the risk.
    """
    # Leverage
    default_leverage:        int   = 25       # 25x default for scalper
    max_leverage:            int   = 35       # hard cap for scalper

    # Position sizing (% of equity)
    risk_per_trade_pct:      float = 0.04     # 4% per trade (Aggressive baseline)
    max_risk_per_trade_pct:  float = 0.10     # 10% absolute cap (AI Boost)
    fixed_margin_per_position: float = 0.0   # 0 = use pct, not fixed margin

    # Scalper SL/TP (Calibrated for 25x leverage)
    sl_pct:                  float = 0.0065   # 0.65% stop loss (Breathing room)
    tp1_pct:                 float = 0.0085   # 0.85% TP1 — close 60%
    tp2_pct:                 float = 0.0150   # 1.50% TP2 — close 40%
    trailing_pct:            float = 0.0040   # 0.40% trailing on remainder

    # Timing
    max_hold_minutes:        float = 12.0     # force close after 12min if no TP hit
    max_hold_grace_minutes:  float = 6.0      # extra grace if still in deeper loss
    max_hold_soft_floor_pct: float = -0.0015  # allow delay if loss worse than -0.15%
    scan_interval_seconds:   int   = 15       # scan every 15s to avoid HL rate limits

    # Score threshold — HARD THRESHOLD SCALPER = 60 (TIDAK BISA DIUBAH USER)
    min_score_to_enter:      int   = 60       # ⚠️ HARD: scalper entry gate (TETAP 60)
    signal_cooldown_minutes: int   = 5        # 5 min cooldown scalper
    mtf_confirm_enabled:     bool  = True     # require 15m trend confirmation
    mtf_confirm_interval:    str   = "15m"
    mtf_confirm_lookback:    int   = 32       # ~8h on 15m candles

    # Concurrent positions (tight — capital concentration)
    max_concurrent_positions: int  = 3        # max 3 scalper positions

    # Partial TP ratios (scalper closes more early)
    tp1_close_ratio:         float = 0.60     # 60% on TP1 (vs 40% standard)
    tp2_close_ratio:         float = 0.40     # 40% on TP2

    # Pyramid — scale in when profit > 0.4%, REQUIRES CONFIRMATION
    enable_pyramid:          bool  = False    # off by default, Telegram confirm required
    pyramid_at_profit_pct:   float = 0.004   # 0.4%

    # Daily guard (scalper can lose fast)
    daily_loss_hard_pct:     float = 0.90    # [RELAXED] 90% daily loss → stop
    max_drawdown_pct:        float = 0.95    # [RELAXED] 95% total drawdown kill-switch

    # MTF Score weights
    mtf_score_bonus:         int = 12        # bonus if 1m aligns with 15m trend
    mtf_score_penalty:       int = -15       # penalty if counter-trend

SCALPER = ScalperConfig()

# ──────────────────────────────────────────────
# SIGNAL ENGINE
# ──────────────────────────────────────────────
@dataclass
class SignalConfig:
    # Threshold: signal=55, auto_trade=60
    min_score_to_signal:     int   = 55      # STANDARD: minimum score emit signal
    min_score_to_auto_trade: int   = 60      # STANDARD: minimum score full-auto execute
    signal_cooldown_minutes: int   = 15       # cooldown per asset between signals

    # Bull-Bear gap (LONG vs SHORT berbeda threshold)
    min_bull_bear_gap:       int   = 18       # LONG: minimum gap bull vs bear pts
    min_bull_bear_gap_short: int   = 20       # SHORT: slightly higher than LONG (was 28 — too restrictive)

    # SHORT-specific filters (aktif saat ALLOW_SHORT = True)
    # Solusi 2: Funding rate confirmation
    # Real HL funding rates are typically +-0.00002; threshold must match that range
    short_min_funding_rate:  float = 0.00001  # SHORT valid if funding >= +0.00001 (longs paying)
                                               # Previously 0.0002 which is 10x too high — blocked all SHORTs
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

    # Meta-Scoring (Outcome-based learning)
    meta_learning_enabled:   bool = True
    meta_min_samples:        int = 5          # need at least 5 trades to trust winrate
    meta_boost_threshold:    float = 0.62     # winrate > 62% = +8 pts
    meta_penalty_threshold:  float = 0.40     # winrate < 40% = -12 pts
    meta_max_delta:          int = 15         # absolute max cap for adj


SIGNAL = SignalConfig()

# ──────────────────────────────────────────────
# TRADING DIRECTION FILTER
# ──────────────────────────────────────────────
# [FIX 3 - 2026-04-22] SHORT disabled - WR hanya 31.4%, total loss -$9.15
# SHORT stop_loss WR: 20%, total -$20.07
# Re-enable ONLY when paper trade menunjukkan SHORT WR > 50% untuk 30+ trades
ALLOW_SHORT = True   # Re-enabled dengan 3 filter proteksi: funding >= +0.0002, anti-trend > 2%, gap >= 28

# Intelligence ML Layer kill switch
# DEFAULT = false — model dilatih dari data rusak (ATR-SL 0.08%, SHORT WR 26%, 86% time_exit)
# Re-enable HANYA setelah: ATR-SL fix live + 500 trades baru + WR > 50%
# Set env KARA_INTELLIGENCE=true untuk aktifkan tanpa redeploy
ENABLE_INTELLIGENCE = os.getenv("KARA_INTELLIGENCE", "false").lower() == "true"

# Retrain schedule — hanya retrain saat data cukup banyak dan sudah lama nunggu
INTELLIGENCE_RETRAIN_MIN_SAMPLES = 500   # minimal 500 trade sebelum retrain
INTELLIGENCE_RETRAIN_INTERVAL_HOURS = 24  # retrain max 1x per hari

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
