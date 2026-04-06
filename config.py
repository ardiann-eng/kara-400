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
PRIVATE_KEY      = os.getenv("HL_PRIVATE_KEY", "").strip()    # NEVER commit this

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

# ──────────────────────────────────────────────
# DASHBOARD
# ──────────────────────────────────────────────
DASHBOARD_HOST   = os.getenv("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT   = int(os.getenv("PORT", os.getenv("DASHBOARD_PORT", "8888")))
SECRET_KEY       = os.getenv("SECRET_KEY", "kara-secret-change-me")

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
    paper_tp1_pct:           float = 0.025    # 2.5% TP1 (pushed slightly wider)
    paper_tp2_pct:           float = 0.040    # 4.0% TP2

    # Stop-loss / Take-profit defaults
    default_sl_pct:          float = 0.025    # 2.5% from entry
    tp1_pct:                 float = 0.04     # +4% -> close 40%
    tp2_pct:                 float = 0.08     # +8% -> close 35%
    trailing_pct:            float = 0.03     # 3% trailing on remainder

    # Daily / drawdown guards
    daily_loss_limit_pct:    float = 0.08     # 8% daily loss -> pause
    daily_loss_hard_pct:     float = 0.10     # 10% -> full stop today
    max_drawdown_pct:        float = 0.20     # 20% total drawdown kill-switch
    post_loss_cooldown_hrs:  float = 5.0      # hours cooldown after daily loss > 6%

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
    risk_per_trade_pct:      float = 0.13     # 13% per trade — VERY aggressive
    fixed_margin_per_position: float = 0.0   # 0 = use pct, not fixed margin

    # Scalper SL/TP (very tight)
    sl_pct:                  float = 0.0025   # 0.25% stop loss
    tp1_pct:                 float = 0.0035   # 0.35% TP1 — close 60%
    tp2_pct:                 float = 0.0070   # 0.70% TP2 — close 40%
    trailing_pct:            float = 0.0020   # 0.20% trailing on remainder

    # Timing
    max_hold_minutes:        float = 12.0     # force close after 12min if no TP hit
    scan_interval_seconds:   int   = 5        # scan every 5 seconds

    # Score threshold (more signals, lower bar)
    min_score_to_enter:      int   = 45       # entry threshold (vs 56 for standard)
    signal_cooldown_minutes: int   = 1        # 1 min cooldown (vs 15 min standard)

    # Concurrent positions (tight — capital concentration)
    max_concurrent_positions: int  = 3        # max 3 scalper positions

    # Partial TP ratios (scalper closes more early)
    tp1_close_ratio:         float = 0.60     # 60% on TP1 (vs 40% standard)
    tp2_close_ratio:         float = 0.40     # 40% on TP2

    # Pyramid — scale in when profit > 0.4%, REQUIRES CONFIRMATION
    enable_pyramid:          bool  = False    # off by default, Telegram confirm required
    pyramid_at_profit_pct:   float = 0.004   # 0.4%

    # Daily guard (scalper can lose fast)
    daily_loss_hard_pct:     float = 0.15    # 15% daily loss → stop
    max_drawdown_pct:        float = 0.25    # 25% total drawdown kill-switch

SCALPER = ScalperConfig()

# ──────────────────────────────────────────────
# SIGNAL ENGINE
# ──────────────────────────────────────────────
@dataclass
class SignalConfig:
    min_score_to_signal:     int   = 56       # minimum score to emit signal
    min_score_to_auto_trade: int   = 56       # minimum score for full-auto execution
    signal_cooldown_minutes: int   = 15       # cooldown per asset between signals

    # Session windows (UTC)
    ny_session_start_utc:    int   = 13       # 13:00 UTC = 09:00 ET
    ny_session_end_utc:      int   = 21       # 21:00 UTC = 17:00 ET
    london_start_utc:        int   = 8
    london_end_utc:          int   = 17

    # Session score bonuses / penalties
    ny_session_bonus:        int   = 8
    london_session_bonus:    int   = 4
    asia_session_penalty:    int   = -5       # 22:00-07:00 UTC

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

SIGNAL = SignalConfig()

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
        "BTC", "ETH", "SOL", "ARB", "AVAX", "MATIC", "OP", "BASE",
        "BLAST", "DOGE", "XRP", "ADA", "LINK", "UNI"
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
EXCEL_LOG_PATH = os.getenv("EXCEL_LOG_PATH", "trade_history.xlsx")

# ──────────────────────────────────────────────
# BACKTEST
# ──────────────────────────────────────────────
BACKTEST_START = "2024-01-01"
BACKTEST_END   = "2024-12-31"
BACKTEST_INITIAL_CAPITAL = 1000.0   # USD (realistic for students)
