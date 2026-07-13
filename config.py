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
FULL_AUTO   = os.getenv("KARA_FULL_AUTO", "false").lower() == "true"
EXECUTION_EXCHANGE = os.getenv("KARA_EXECUTION_EXCHANGE", "bybit").lower()

# ── Trading Strategy Mode ──────────────────────────────────────────────────
# Switch between "standard" (swing/positional) and "scalper" (ultra-aggressive)
# Change at runtime via Telegram /scalper or /standard, or via Dashboard.
#
# FORCE_SCALPER_ONLY (audit 1435 trades 13–18 Jun):
#   standard mode was net −EV (long −$30 + short −$47); scalper long +$149.
#   When True: ALL execution uses scalper rules (hold, risk, SL, thresholds).
#   Standard scorer may still run only as a SIGNAL SOURCE; any hit is executed
#   under scalper params (hold time, etc. stay scalper).
TRADING_MODE = os.getenv("KARA_TRADING_MODE", "scalper").lower()
FORCE_SCALPER_ONLY = os.getenv("KARA_FORCE_SCALPER_ONLY", "true").lower() == "true"
# When FORCE_SCALPER_ONLY: still score standard path so opportunities are not
# dropped if pure-scalper score fails; execution always remaps to scalper.
STANDARD_SIGNAL_AS_SCALPER_FALLBACK = (
    os.getenv("KARA_STD_SIGNAL_FALLBACK", "true").lower() == "true"
)

MODE = TRADE_MODE  # alias for backward compatibility


def effective_trading_mode(user_mode: str | None = None) -> str:
    """Resolve trading mode for execution. FORCE_SCALPER_ONLY always wins."""
    if FORCE_SCALPER_ONLY:
        return "scalper"
    mode = (user_mode or TRADING_MODE or "scalper").lower()
    return mode if mode in ("scalper", "standard") else "scalper"

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
# BYBIT EXECUTION
# ──────────────────────────────────────────────
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "true").lower() in ("true", "1", "yes")
BYBIT_ACCOUNT_TYPE = os.getenv("BYBIT_ACCOUNT_TYPE", "UNIFIED").upper()
BYBIT_CATEGORY = os.getenv("BYBIT_CATEGORY", "linear").lower()
BYBIT_SETTLE_COIN = os.getenv("BYBIT_SETTLE_COIN", "USDT").upper()
BYBIT_RECV_WINDOW = int(os.getenv("BYBIT_RECV_WINDOW", "5000"))
BYBIT_MAX_PRICE_GAP_PCT = float(os.getenv("BYBIT_MAX_PRICE_GAP_PCT", "0.003"))
BYBIT_MAX_SLIPPAGE_PCT = float(os.getenv("BYBIT_MAX_SLIPPAGE_PCT", "0.002"))
BYBIT_MAINNET_ACK = os.getenv("BYBIT_MAINNET_ACK", "").strip()
BYBIT_TESTNET_ONLY = os.getenv("BYBIT_TESTNET_ONLY", "true").lower() in ("true", "1", "yes")
BYBIT_LIVE_ASSET_ALLOWLIST = tuple(
    asset.strip().upper()
    for asset in os.getenv("BYBIT_LIVE_ASSET_ALLOWLIST", "BTC,ETH").split(",")
    if asset.strip()
)
# Live-only hard ceilings mirror current scalper/user defaults. Paper sizing is unchanged.
BYBIT_LIVE_MAX_LEVERAGE = int(os.getenv("BYBIT_LIVE_MAX_LEVERAGE", "20"))
BYBIT_LIVE_MAX_POSITIONS = int(os.getenv("BYBIT_LIVE_MAX_POSITIONS", "3"))
BYBIT_LIVE_MAX_RISK_PER_TRADE_PCT = float(
    os.getenv("BYBIT_LIVE_MAX_RISK_PER_TRADE_PCT", "0.035")
)
BYBIT_LIVE_MAX_TOTAL_RISK_PCT = float(
    os.getenv("BYBIT_LIVE_MAX_TOTAL_RISK_PCT", "0.105")
)
BYBIT_LIVE_MAX_SYMBOL_NOTIONAL_PCT = float(
    os.getenv("BYBIT_LIVE_MAX_SYMBOL_NOTIONAL_PCT", "7.0")
)
BYBIT_LIVE_MAX_TOTAL_NOTIONAL_PCT = float(
    os.getenv("BYBIT_LIVE_MAX_TOTAL_NOTIONAL_PCT", "21.0")
)
BYBIT_LIVE_MAX_SIGNAL_AGE_S = float(os.getenv("BYBIT_LIVE_MAX_SIGNAL_AGE_S", "30"))
BYBIT_LIVE_MAX_QUOTE_AGE_S = float(os.getenv("BYBIT_LIVE_MAX_QUOTE_AGE_S", "5"))
BYBIT_LIVE_MAX_SPREAD_PCT = float(os.getenv("BYBIT_LIVE_MAX_SPREAD_PCT", "0.0015"))
BYBIT_LIVE_MIN_DEPTH_RATIO = float(os.getenv("BYBIT_LIVE_MIN_DEPTH_RATIO", "1.0"))

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

# ─────────── DASHBOARD (ULTIMATE DEBUG) ───────────
DASHBOARD_HOST   = "0.0.0.0"
DASHBOARD_PORT   = int(os.getenv("PORT", 8080))

# [KARA_PORT_DEBUG] - Direct print to bypass logging
print("\n" + "="*50)
print(f"📡 [KARA_DEBUG] SYSTEM_PORT ENV: {os.getenv('PORT')}")
print(f"🚀 [KARA_DEBUG] BINDING DASHBOARD TO: {DASHBOARD_HOST}:{DASHBOARD_PORT}")
print("="*50 + "\n")

SECRET_KEY       = os.getenv("SECRET_KEY", "CHANGEME")

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

    # Partial TP ratios  (Fix 5: close less at TP1, let winners run)
    tp1_close_ratio:         float = 0.25     # was 0.40 — close only 25% at TP1
    tp2_close_ratio:         float = 0.50     # 50% of remaining (37.5% of original)
    # ATR-Based (Opsi B) Calibration
    enable_atr_sl:           bool  = True     # Use volatility-based SL
    atr_multiplier:          float = 2.0      # ATR lookback buffer
    atr_lookback:            int   = 14       # candles for calculation

    # Momentum-based time exit thresholds (Fix 6) — LONG defaults
    time_exit_pullback_pct:  float = 0.20     # if price retraces 20% of TP1 distance, exit
    time_exit_flatline_pct:  float = 0.0015   # < 0.15% move in 30min = dead market
    time_exit_flatline_mins: int   = 30       # window for flatline check
    time_exit_hard_hours:    float = 6.0      # safety net: force-exit after 6h below TP1

    # ── SHORT-specific levels & exit (audit 206 shorts) ────────────────
    # Wins averaged ~0.34% move; theoretical TP1 3.8%+ never hit → trail dead.
    # Keep SHORT quantity: moderate TP (hit-able), not ultra-tight scalp-only.
    short_sl_floor:          float = 0.018    # 1.8% min SL (above ~0.9% noise ATR traps)
    short_sl_floor_high_vol: float = 0.022    # 2.2% high vol
    short_sl_floor_extreme:  float = 0.028    # 2.8% extreme/volatile
    short_sl_cap:            float = 0.045    # don't over-widen short SL
    short_tp1_pct:           float = 0.0060   # 0.60% — near p75 win move, still frequent hits
    short_tp2_pct:           float = 0.0110   # 1.10% — room for runners without fantasy 7% TP
    short_tp1_vol_scale:     float = 0.12     # mild vol widen of TP (capped)
    short_tp1_max:           float = 0.0090   # cap TP1 0.90%
    short_tp2_max:           float = 0.0150   # cap TP2 1.50%
    # SHORT time-exit: looser than long so we don't harvest 0.10% crumbs
    short_time_exit_flatline_pct:  float = 0.0030  # 0.30% (was 0.15% — too tight)
    short_time_exit_flatline_mins: int   = 45      # 45m before flatline (was 30)
    short_time_exit_pullback_pct:  float = 0.35    # need deeper giveback than long 20%
    short_time_exit_pullback_mins: int   = 40
    short_time_exit_min_mfe_for_pullback: float = 0.004  # only pullback-exit if had ≥0.40% MFE
    # Soft EV for short scalp-style (hit TP1 often, not long RR)
    short_ev_use_tp1_weight: float = 0.85
    short_ev_sl_haircut:     float = 0.90     # not every loss is full SL

    # remaining 37.5% uses trailing stop

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

    # Scalper SL/TP — aligned to ~12m hold (don't set TP2 outside typical MFE window).
    # Audit: p75 win ~0.57%, p90 ~1.02%, but 12m often time_exits before TP2 0.90%.
    # Ladder: TP1 bank early, TP2 reachable, trail for anything beyond.
    sl_pct:                  float = 0.0080   # 0.80% SL — above 25x noise
    tp1_pct:                 float = 0.0045   # 0.45% TP1 — hit more often inside 12m
    tp2_pct:                 float = 0.0075   # 0.75% TP2 — was 0.90%, often missed in 12m
    trailing_pct:            float = 0.0025   # 0.25% trail after arm
    # Timing
    max_hold_minutes:        float = 12.0     # force close after 12min if no edge left
    max_hold_grace_minutes:  float = 6.0      # extra grace if still in deeper loss
    max_hold_soft_floor_pct: float = -0.0015  # allow delay if loss worse than -0.15%
    # Crypto 1m scalps: database showed 12-18m winners, but 18m+ losers. Grace is
    # reserved for an impulse that is holding structure, never for a red trade alone.
    max_hold_state_check_minutes: float = 10.0
    max_hold_retest_mfe_pct: float = 0.0035   # same impulse size that arms scalper trail
    max_hold_adverse_exit_pct: float = -0.0030  # cut invalid 1m setup at -0.30%, before 0.80% SL
    max_hold_grace_loss_floor_pct: float = -0.0015  # retest may be mildly red, not a loser drift
    # Don't force time_exit if trail is managing a winner (max-profit)
    max_hold_respect_trail:  bool  = True
    max_hold_trail_min_mfe:  float = 0.0035   # need at least +0.35% MFE to skip force-exit
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
    mtf_bonus_floor_score:   int = 65        # audit: MTF align + score <65 had negative EV
    mtf_bonus_high_score:    int = 72        # audit: 72+ bucket had positive EV
    mtf_mid_bonus:           int = 4         # small confirmation only
    mtf_high_bonus:          int = 6         # confirmation, not primary edge

    # Entry location quality gate (adaptive, soft gate)
    entry_location_gate_enabled: bool = True
    entry_location_weak_penalty: int = 8
    entry_location_excellent_bonus: int = 3
    entry_location_weak_min_score: int = 72
    # Weak entries require one new 1m candle with same-side structure and
    # follow-through. Timeout covers two candles without tuning price thresholds.
    weak_confirmation_enabled: bool = True
    weak_confirmation_timeout_seconds: int = 150

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

    # Bull-Bear gap — equalized until 50+ SHORT trades justify a stricter SHORT gap
    min_bull_bear_gap:       int   = 18       # LONG: minimum gap bull vs bear pts
    min_bull_bear_gap_short: int   = 18       # SHORT: same as LONG (was 20/28 — asymmetry blocked shorts)

    # SHORT-specific filters (aktif saat ALLOW_SHORT = True)
    # Thesis setups (OR): breakdown | failed-rally (real up-leg) | cascade
    # Calibrated for QUANTITY + quality: tight enough to cut micro-noise thesis,
    # loose enough that valid shorts still fire (audit 206 shorts: failed_rally spam).
    short_min_funding_rate:  float = 0.00003  # meaningful crowded-long (was 0.00001 micro-noise)
    # Failed-rally geometry (moderate — keep fill rate)
    short_rally_lookback_mins: int = 45       # swing high window
    short_rally_min_up_pct:    float = 0.0025 # need ≥0.25% up-leg before rejection
    short_rally_reject_pct:    float = 0.0012 # give back ≥0.12% from swing high
    short_rally_max_1h_pct:    float = 0.0020 # 1h still not strongly up (≤+0.20%)
    # HTF momentum for SHORT only (soft): 5m veto if strongly bull, not hard multi-TF stack
    short_htf_veto_enabled:    bool = True
    short_htf_bull_candles:    int = 3        # 3/4 last 5m green → veto short
    short_htf_bull_move_pct:   float = 0.004  # or 5m net move > +0.40%
    # P1: cascade short requires red 1h OR 5m not in hard bull (block short-into-pump)
    short_cascade_require_red_context: bool = True
    # P1: counter-trend SHORT (24h up or 5m bull) only gets fraction of session bonus
    short_countertrend_session_mult: float = 0.35  # +14 session → ~+5 when fighting pump
    # Regime hard filters (symmetric)
    short_max_uptrend_pct:   float = 0.03     # Block SHORT jika 24h trend > +3% (jangan lawan trend)
    long_max_downtrend_pct:  float = -0.03    # Block LONG  jika 24h trend < -3% (jangan catch knife)

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
    # Four-level memory prevents 220 trades splitting into 150 mostly-neutral keys.
    # Specific patterns can promote only with evidence; broader groups can only
    # apply a smaller penalty to contain repeated low-EV crypto-perp conditions.
    meta_min_samples:        int = 5          # minimum specific evidence for a decision
    meta_boost_samples:      int = 5          # specific setup needs five closed outcomes for +5
    meta_boost_threshold:    float = 0.62     # positive expectancy plus at least 62% EMA WR
    meta_penalty_samples:    int = 5          # specific / asset-side loss evidence
    meta_side_bucket_penalty_samples: int = 20
    meta_side_penalty_samples: int = 30
    meta_penalty_threshold:  float = 0.40
    meta_min_pnl_ema_for_boost: float = 0.0   # boost only if EV proxy is positive
    meta_penalty_pnl_ema:    float = 0.0      # penalize if rolling pnl is negative
    meta_specific_boost:     int = 5
    meta_specific_penalty:   int = -7
    meta_asset_side_penalty: int = -4
    meta_side_bucket_penalty:int = -3
    meta_side_penalty:       int = -2
    meta_max_delta:          int = 7          # meta refines a setup; it never replaces 1m scorer

    # Asset concentration guard: avoid repeated trades on same coin unless score is stronger
    asset_concentration_enabled: bool = True
    asset_concentration_window_minutes: int = 60
    asset_concentration_max_signals: int = 2
    asset_concentration_threshold_step: int = 4
    asset_concentration_max_threshold_add: int = 12


SIGNAL = SignalConfig()

# ──────────────────────────────────────────────
# TRADING DIRECTION FILTER
# ──────────────────────────────────────────────
# [FIX 3 - 2026-04-22] SHORT disabled - WR hanya 31.4%, total loss -$9.15
# SHORT stop_loss WR: 20%, total -$20.07
# Re-enable ONLY when paper trade menunjukkan SHORT WR > 50% untuk 30+ trades
ALLOW_SHORT = True   # Re-enabled: breakdown | failed-rally | cascade SHORT theses (OR)

# ── HARD RESET ON DEPLOY ──────────────────────────────────────────────────────
# Set KARA_HARD_RESET=true di env variable Railway/Docker sebelum deploy.
# Bot akan menghapus SEMUA data saat startup:
#   - Semua posisi terbuka          (paper_positions)
#   - Semua saldo user              (paper_state → reset ke Rp1.000.000)
#   - Semua journal/trade history   (trade_history)
#   - Semua sinyal history          (signals_history)
#   - Meta learning stats           (meta_pattern_stats)
#   - Volatility cache              (vol_cache)
#   - Risk state per user           (risk_state)
#   - Seluruh ML experience buffer  (kara_ml.db dihapus)
#   - Trained Intelligence model    (kara_intelligence.pkl dihapus)
# Yang TIDAK dihapus: konfigurasi user, wallet address, akses Telegram.
# PENTING: Ubah kembali ke false setelah deploy agar tidak reset terus!
HARD_RESET_ON_DEPLOY = False

# Intelligence ML Layer kill switch
# Set env KARA_INTELLIGENCE=false untuk matikan tanpa redeploy.
ENABLE_INTELLIGENCE = os.getenv("KARA_INTELLIGENCE", "true").lower() == "true"

# Retrain schedule
INTELLIGENCE_RETRAIN_MIN_SAMPLES = 300   # minimum before observe-only model retrain
INTELLIGENCE_RETRAIN_MIN_ENRICHED_SAMPLES = 300  # require complete 1m entry/exit feature contract
INTELLIGENCE_RETRAIN_INTERVAL_HOURS = 12  # retrain max 2x per hari

# ──────────────────────────────────────────────
# BLOCKED TRADING HOURS (UTC)
# ──────────────────────────────────────────────
# [FIX 4 - 2026-04-22] London open blocked - data 124 trades:
# 08:00 UTC: WR 7.1%, -$7.81 | 09:00 UTC: WR 21.4%, -$7.85
# Total 2 jam: -$15.66 = hampir seluruh account loss
# Volatilitas London open menyebabkan SL langsung kena opening spike
BLOCKED_HOURS_UTC = []

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

# ──────────────────────────────────────────────
# WEEKLY AI REVIEW (analyst → human approver)
# ──────────────────────────────────────────────
@dataclass
class WeeklyReviewConfig:
    """Weekly LLM strategy audit — AI proposes concrete changes; human applies."""
    enabled: bool = os.getenv("KARA_REVIEW_ENABLED", "true").lower() == "true"
    lookback_days: int = int(os.getenv("KARA_REVIEW_LOOKBACK_DAYS", "7"))
    # Extra baseline window for anti-overfit (compare this week vs prior baseline)
    baseline_lookback_days: int = int(os.getenv("KARA_REVIEW_BASELINE_DAYS", "30"))
    min_samples_for_significance: int = int(os.getenv("KARA_REVIEW_MIN_SAMPLES", "30"))
    output_dir: str = os.path.join(STORAGE_BASE, "reviews")
    schedule_hour_utc: int = int(os.getenv("KARA_REVIEW_HOUR_UTC", "6"))  # Monday 06:00 UTC
    # OpenAI-compatible router (default: user-provided gateway)
    model_id: str = os.getenv("KARA_REVIEW_MODEL", "mimo/mimo-v2.5-pro")
    model_fallback: str = os.getenv("KARA_REVIEW_MODEL_FALLBACK", "mimo-v2.5-pro")
    min_confidence_to_suggest: str = "medium"  # low/medium/high
    max_relative_delta_pct: float = 0.50       # >50% Δ auto-flags "large_change"
    max_tokens: int = int(os.getenv("KARA_REVIEW_MAX_TOKENS", "8192"))
    timeout_sec: int = int(os.getenv("KARA_REVIEW_TIMEOUT_SEC", "120"))

WEEKLY_REVIEW = WeeklyReviewConfig()

# Default AI router credentials (overridden by env; never hardcode secrets in git)
# KARA_REVIEW_* preferred; MIMO_* accepted as aliases for the same endpoint.
AI_API_KEY = (
    os.getenv("KARA_REVIEW_API_KEY")
    or os.getenv("MIMO_API_KEY")
    or os.getenv("OPENAI_API_KEY")
    or ""
)
AI_BASE_URL = (
    os.getenv("KARA_REVIEW_BASE_URL")
    or os.getenv("MIMO_BASE_URL")
    or os.getenv("OPENAI_BASE_URL")
    or "https://api.ojwgeoubcweojfb.shop/v1"
)
AI_MODEL = (
    os.getenv("KARA_REVIEW_MODEL")
    or os.getenv("MIMO_MODEL")
    or "mimo/mimo-v2.5-pro"
)
