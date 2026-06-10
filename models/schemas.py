"""
KARA Bot - Pydantic Schemas
All data models used across the bot.
"""

from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, validator


# ──────────────────────────────────────────────
# ENUMS
# ──────────────────────────────────────────────

class Side(str, Enum):
    LONG  = "long"
    SHORT = "short"

class MarketRegime(str, Enum):
    TRENDING  = "trending"
    RANGING   = "ranging"
    VOLATILE  = "volatile"
    UNKNOWN   = "unknown"
    LOW_VOL   = "low_vol"
    NORMAL    = "normal"
    HIGH_VOL  = "high_vol"
    EXTREME   = "extreme"

class SignalStrength(str, Enum):
    STRONG   = "strong"    # score >= 75
    MODERATE = "moderate"  # score 60-74
    WEAK     = "weak"      # score 55-59
    NO_TRADE = "no_trade"  # score < 55

class OrderStatus(str, Enum):
    PENDING    = "pending"
    OPEN       = "open"
    PARTIAL    = "partial"
    FILLED     = "filled"
    CANCELLED  = "cancelled"
    FAILED     = "failed"

class PositionStatus(str, Enum):
    OPEN    = "open"
    CLOSED  = "closed"
    PARTIAL = "partial"    # after TP1/TP2

class BotMode(str, Enum):
    PAPER = "paper"
    LIVE  = "live"

class ExecutionMode(str, Enum):
    SEMI_AUTO = "semi_auto"  # default - needs user confirmation
    FULL_AUTO = "full_auto"  # auto execute if score >= threshold


# ──────────────────────────────────────────────
# MARKET DATA
# ──────────────────────────────────────────────

class FundingData(BaseModel):
    asset:           str
    funding_rate:    float          # current 8h rate, e.g. 0.0003
    premium:         float          # mark vs index
    predicted_rate:  Optional[float] = None
    hourly_trend:    List[float] = Field(default_factory=list)  # last 8 hourly rates
    timestamp:       datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_extreme(self) -> bool:
        from config import SIGNAL
        return abs(self.funding_rate) > SIGNAL.funding_extreme_threshold

    @property
    def direction(self) -> str:
        """positive = longs paying shorts (crowded long)"""
        if self.funding_rate > 0.0002:
            return "longs_paying"
        elif self.funding_rate < -0.0002:
            return "shorts_paying"
        return "neutral"


class OIData(BaseModel):
    asset:           str
    open_interest:   float          # USD value
    oi_change_pct:   float          # vs 1h ago
    oi_change_24h:   float          # vs 24h ago
    oracle_price:    float = 0.0    # from ctx.oraclePx
    long_short_ratio: Optional[float] = None
    timestamp:       datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LiquidationLevel(BaseModel):
    price:           float
    notional_usd:    float          # estimated liq value at this level
    side:            Side           # which side gets liquidated at this price
    distance_pct:    float          # % from current price


class LiquidationMap(BaseModel):
    asset:           str
    current_price:   float
    levels:          List[LiquidationLevel] = Field(default_factory=list)
    nearest_liq_pct: float = 0.0    # % to nearest big liq cluster
    cascade_risk:    float = 0.0    # 0-1 score, cascade probability
    timestamp:       datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class OrderbookSnapshot(BaseModel):
    asset:           str
    bids:            List[List[float]]  # [[price, size], ...]
    asks:            List[List[float]]
    mid_price:       float
    spread_pct:      float
    bid_ask_imbalance: float           # -1 (all asks) to +1 (all bids)
    vwap:            float
    vwap_deviation_pct: float          # (mid - vwap) / vwap
    timestamp:       datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ──────────────────────────────────────────────
# SIGNAL
# ──────────────────────────────────────────────

class ScoreBreakdown(BaseModel):
    """Detailed breakdown of the 0-100 signal score."""
    # Component scores (max shown)
    oi_funding_score:      int = 0   # max 25 pts
    liquidation_score:     int = 0   # max 25 pts
    orderbook_score:       int = 0   # max 25 pts
    session_bonus:         int = 0   # max 15 pts (can be negative)
    regime_multiplier:     float = 1.0  # applied last

    # Bull vs Bear evidence tally
    total_bull:            int = 0
    total_bear:            int = 0

    # Raw
    raw_score:             int = 0
    final_score:           int = 0

    # Per-sub-component scores (OB, EMA, RSI, CVD, FUND, LIQ, MTF)
    components:            Dict[str, int] = Field(default_factory=dict)

    # Momentum gate result
    momentum_gate_passed:  Optional[bool] = None
    momentum_move_pct:     float = 0.0
    momentum_candles:      str = ""   # e.g. "3/5"

    # 1H HTF regime (was 4H — Audit #14 redesign for scalper)
    htf_regime:            str = ""
    htf_threshold_adj:     int = 0

    # Explanation strings for Telegram / Dashboard
    reasons:               List[str] = Field(default_factory=list)
    warnings:              List[str] = Field(default_factory=list)


class TradeSignal(BaseModel):
    signal_id:        str
    asset:            str
    side:             Side
    score:            int                    # 0-100
    strength:         SignalStrength
    regime:           MarketRegime
    breakdown:        ScoreBreakdown
    is_pyramid:       bool = False           # True if scaling into existing position
    auto_executed:    bool = False           # True if bot took the trade automatically
    trade_mode:       str = "scalper"        # scalper only

    # Levels
    entry_price:      float
    stop_loss:        float
    tp1:              float
    tp2:              float
    tp3:              float = 0.0   # third target (3:1 R:R); 0 = not set
    suggested_leverage: int

    # Position sizing (filled by RiskManager)
    suggested_size_usd: Optional[float] = None
    suggested_contracts: Optional[float] = None
    realized_vol:       float = 0.02             # daily realized vol — used for trail distance
    entry_atr:          float = 0.0              # ATR% at entry — for ATR trailing stop
    funding_rate:       Optional[float] = None   # last known funding rate at signal time
    size_mult:          float = 1.0              # [v10] gate sizing modifier (tier A/B × vol tier)
    v10_tier:           str = "B"                # [v10] gate tier (S/A/B)
    v10_setup:          str = "none"             # [v10] setup label (sweep/breakout/pullback/momentum)
    gate_ob_dir:        int = 0                  # [RANK] OB strength signed aligned to trade — untuk ranking
    gate_net_move:      float = 0.0              # [RANK] 5m net displacement (signed aligned) — untuk ranking
    gate_cvd_dir:       float = 0.0              # [RANK] CVD directional (signed aligned) — untuk ranking

    gate_expectancy_bucket: str = ""             # [AUDIT] side/setup/tier bucket for PF tracking
    gate_quality_flags: List[str] = Field(default_factory=list)

    # Execution Engine telemetry
    execution_playbook:       str = "none"       # short_momentum / long_reclaim / pullback_limit / etc.
    execution_order_type:     str = "market"     # market / aggressive_limit / passive_limit / wait_retest / cancel
    execution_status:         str = "ready"      # ready / pending / cancelled / shadow_ready
    execution_trigger:        str = ""           # break_3bar_low / reclaim_ema13 / retest_level / etc.
    execution_cancel_reason:  Optional[str] = None
    execution_reference_level: Optional[float] = None
    execution_invalidation_level: Optional[float] = None
    execution_intended_entry: Optional[float] = None
    execution_actual_entry:   Optional[float] = None
    execution_ttl_sec:        int = 0
    execution_wait_sec:       float = 0.0
    execution_spread_bps:     float = 0.0
    execution_cost_bps:       float = 0.0
    execution_chase_pct:      float = 0.0
    execution_notes:          List[str] = Field(default_factory=list)

    # Meta
    timestamp:        datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expiry:           Optional[datetime] = None   # signal expires if not acted on
    confirmed:        bool = False                # user confirmed (semi-auto)
    auto_executed:    bool = False                # full-auto bypass

    def localize_for_user(self, mode: str, atr_value: float = 0.0):
        """
        Localize signal parameters (SL/TP/Leverage) based on user mode.
        If atr_value provided, uses dynamic ATR-based SL (Opsi B).
        """
        import config
        from models.schemas import Side

        self.trade_mode = mode
        if mode == "scalper":
            cfg = config.SCALPER
            sl_pct  = cfg.sl_pct
            tp1_pct = cfg.tp1_pct
            tp2_pct = cfg.tp2_pct
            self.suggested_leverage = min(cfg.default_leverage, cfg.max_leverage)
        else:
            cfg = config.RISK
            sl_pct  = cfg.default_sl_pct
            tp1_pct = cfg.tp1_pct
            tp2_pct = cfg.tp2_pct
            self.suggested_leverage = min(cfg.default_leverage, cfg.max_leverage)

        # ── 1. Calculate Stop Loss ────────────────────────────────────
        if atr_value > 0 and config.RISK.enable_atr_sl:
            # atr_value adalah PERSENTASE (misal 0.015 = 1.5%), bukan harga absolut.
            # sl_pct_atr = atr_pct * multiplier, lalu clamp ke minimum sl_pct
            sl_pct_atr = atr_value * config.RISK.atr_multiplier
            sl_pct_atr = max(sl_pct_atr, sl_pct)   # tidak boleh lebih tipis dari fixed SL
            if self.side == Side.LONG:
                self.stop_loss = round(self.entry_price * (1 - sl_pct_atr), 8)
            else:
                self.stop_loss = round(self.entry_price * (1 + sl_pct_atr), 8)
        else:
            # Fixed Percentage SL
            if self.side == Side.LONG:
                self.stop_loss = round(self.entry_price * (1 - sl_pct), 8)
            else:
                self.stop_loss = round(self.entry_price * (1 + sl_pct), 8)

        # ── 2. Take Profit Calculation — ikut sl_pct_atr supaya RR konsisten ──
        if atr_value > 0 and config.RISK.enable_atr_sl:
            effective_sl = sl_pct_atr
            tp1_eff = effective_sl * 1.5   # 1:1 R:R-ish
            tp2_eff = effective_sl * 2.5   # 2:1 R:R
            tp3_eff = effective_sl * 3.5   # 3:1 R:R
        else:
            tp1_eff = tp1_pct
            tp2_eff = tp2_pct
            tp3_eff = getattr(cfg, 'tp3_pct', tp2_pct * 1.5)

        # Store entry ATR so trailing stop can use it later
        self.entry_atr = atr_value

        if self.side == Side.LONG:
            self.tp1 = round(self.entry_price * (1 + tp1_eff), 8)
            self.tp2 = round(self.entry_price * (1 + tp2_eff), 8)
            self.tp3 = round(self.entry_price * (1 + tp3_eff), 8)
        else:
            self.tp1 = round(self.entry_price * (1 - tp1_eff), 8)
            self.tp2 = round(self.entry_price * (1 - tp2_eff), 8)
            self.tp3 = round(self.entry_price * (1 - tp3_eff), 8)

    @property
    def risk_reward_ratio(self) -> float:
        """
        True risk:reward based on actual stop_loss distance.
        [AUDIT FIX 2026] Removed hardcoded 0.7% scalper risk that masked the real
        SL distance and made all RR displays fictitious. With ATR-adaptive SL now
        in place (sl_pct_min 0.6%, sl_pct_max 2.0%), the real distance is
        meaningful and should be shown directly.
        """
        if self.side == Side.LONG:
            reward = self.tp2 - self.entry_price
            risk   = self.entry_price - self.stop_loss
        else:
            reward = self.entry_price - self.tp2
            risk   = self.stop_loss - self.entry_price

        if risk <= 0 or risk < self.entry_price * 0.001:
            return 0.0
        return round(min(reward / risk, 20.0), 2)


# ──────────────────────────────────────────────
# ORDERS & POSITIONS
# ──────────────────────────────────────────────

class Order(BaseModel):
    order_id:         str
    idempotency_key:  str
    asset:            str
    side:             Side
    size:             float                 # contracts
    price:            float
    order_type:       str = "post_only"     # post_only | limit | market
    status:           OrderStatus = OrderStatus.PENDING
    filled_size:      float = 0.0
    avg_fill_price:   float = 0.0
    fee_paid:         float = 0.0
    created_at:       datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at:       datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    signal_id:        Optional[str] = None
    is_paper:         bool = True


class Position(BaseModel):
    position_id:      str
    asset:            str
    side:             Side
    entry_price:      float
    size_initial:     float      # original contracts
    size_current:     float      # after partial closes
    leverage:         int
    margin_usd:       float      # initial margin locked

    # Levels
    stop_loss:        float
    tp1:              float
    tp2:              float
    tp3:              float = 0.0    # third target (3:1 R:R); 0 = not set (trail remainder)
    trailing_active:  bool = False
    trailing_high:    float = 0.0    # highest price reached (for long trailing)
    trailing_stop_price: float = 0.0  # current ratcheted ATR trailing stop level
    entry_atr:        float = 0.0    # ATR% at entry — used for ATR trailing stop
    liquidation_price: Optional[float] = None  # estimated or actual liquidation price

    # State
    status:           PositionStatus = PositionStatus.OPEN
    tp1_hit:          bool = False
    tp2_hit:          bool = False
    tp3_hit:          bool = False
    pnl_realized:     float = 0.0
    pnl_unrealized:   float = 0.0

    # Meta
    signal_id:         Optional[str] = None
    trade_mode:        str = "scalper"    # scalper only
    opened_at:         datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at:         Optional[datetime] = None
    is_paper:          bool = True
    entry_score:       int = 50
    entry_tier:        str = "B"          # [v10] gate entry quality S/A/B
    entry_setup:       str = "none"       # [v10] setup label at entry
    realized_vol:      float = 0.02       # daily realized vol at entry — used for trail distance
    gate_expectancy_bucket: str = ""      # side/tier/setup/rv bucket at entry
    gate_quality_flags: List[str] = Field(default_factory=list)

    # [QUANT AGGRESSION] Partial exit & scale-in tracking
    partial_exits_done: List[str] = Field(default_factory=list)   # track which exits done: "tp1","tp2","tp3","breakeven"
    scaled_in:         bool = False
    original_entry_price: float = 0.0  # untuk breakeven reference
    scale_in_count:    int = 0         # track berapa kali scaled in
    extended_deadline:  Optional[datetime] = None  # time_exit grace for runners

    # Rolling 1m candle OHLCV — diupdate setiap monitor tick untuk exit logic
    candle_closes:     List[float] = Field(default_factory=list)
    candle_highs:      List[float] = Field(default_factory=list)
    candle_lows:       List[float] = Field(default_factory=list)
    candle_volumes:    List[float] = Field(default_factory=list)
    # 15m candle closes untuk HTF trend filter (momentum exit Layer 5)
    htf_candle_closes: List[float] = Field(default_factory=list)
    candles_refreshed_at: Optional[datetime] = None  # timestamp of last OHLCV refresh

    # [POST-MORTEM] Autopsy context — populated at entry, updated during hold
    max_unrealized_loss: float = 0.0          # deepest unrealized PnL (negative = loss)
    entry_funding_rate: float = 0.0           # funding rate at entry time
    trend_pct: float = 0.0                    # 1h trend % at entry
    atr_pct: float = 0.0                      # ATR(14) % at entry
    autopsy: str = ""                         # rule-based autopsy text after close

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == Side.LONG:
            pnl = (current_price - self.entry_price) / self.entry_price
        else:
            pnl = (self.entry_price - current_price) / self.entry_price
        return pnl * self.size_current * self.entry_price

    def floating_pct(self, current_price: float) -> float:
        """Raw price move percentage (unleveraged)."""
        if self.side == Side.LONG:
            return (current_price - self.entry_price) / self.entry_price
        else:
            return (self.entry_price - current_price) / self.entry_price

    def roe_pct(self, current_price: float) -> float:
        """Return on Equity (leverage-adjusted)."""
        return self.floating_pct(current_price) * self.leverage


# ──────────────────────────────────────────────
# ACCOUNT
# ──────────────────────────────────────────────

class AccountState(BaseModel):
    total_equity:     float       # wallet_balance + unrealized_pnl
    wallet_balance:   float       # actual deposited / realized base currency
    available:        float       # free margin (borrowing power)
    used_margin:      float
    unrealized_pnl:   float
    daily_pnl:        float       # today's total pnl (realized + floating)
    daily_pnl_pct:    float
    peak_balance:     float       # for drawdown calc
    current_drawdown_pct: float
    positions:        List[Position] = Field(default_factory=list)
    mode:             BotMode = BotMode.PAPER
    execution_mode:   ExecutionMode = ExecutionMode.SEMI_AUTO
    is_paused:        bool = False
    kill_switch_active: bool = False
    updated_at:       datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ──────────────────────────────────────────────
# TELEGRAM COMMAND RESPONSES
# ──────────────────────────────────────────────

class TelegramResponse(BaseModel):
    message:          str
    parse_mode:       str = "HTML"
    signal:           Optional[TradeSignal] = None
    account:          Optional[AccountState] = None


# ──────────────────────────────────────────────
# MULTI-USER DB
# ──────────────────────────────────────────────

class UserConfig(BaseModel):
    trading_mode:  str = "scalper"          # scalper only
    bot_mode:      BotMode = BotMode.PAPER # paper | live
    risk_pct:      float = 0.02            # 2% of equity

    # ── Standard Mode Settings ────────────────
    # Recalibrated: session bonus and OI magnitude removed from score (~13-15 pts lower).
    std_min_score_to_signal:     int = 45   # emit signal ke user (bukan entry gate)
    std_min_score_to_auto_trade: int = 57   # entry gate: data 55 trade — threshold 57 optimal
    std_max_leverage:            int = 10
    std_max_concurrent_positions: int = 10

    # ── Scalper Mode Settings ─────────────────
    scl_min_score_to_signal:     int = 45   # emit signal ke user (bukan entry gate)
    scl_min_score_to_auto_trade: int = 57   # entry gate: sama dengan standard (data-driven)
    scl_max_leverage:            int = 20
    scl_max_concurrent_positions: int = 5   # approved: 5 concurrent scalper positions

    # ── Bitget Execution Override ─────────────
    # Leverage cap khusus saat eksekusi di Bitget. Akan di-min vs
    # bitget_max_leverage per asset oleh BitgetExecutor.
    # 0 = pakai scl_max_leverage / std_max_leverage (sesuai trading_mode).
    bitget_max_leverage: int = 0

class User(BaseModel):
    chat_id:           str
    username:          str = ""
    paper_balance_usd: float               # internally USD
    created_at:        datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    config:            UserConfig = Field(default_factory=UserConfig)
    is_active:         bool = True         # Allow disabling users
    
    # Live mode auth — Hyperliquid
    hl_main_address:   Optional[str] = None
    hl_agent_address:  Optional[str] = None
    hl_agent_secret:   Optional[str] = None
    wallet_authorized: bool = False
    tos_agreed:        bool = False

    # Live mode auth — Bitget (USDT-M futures execution)
    # Disimpan terenkripsi via Fernet (sama seperti hl_agent_secret).
    bitget_api_key:     Optional[str] = None
    bitget_api_secret:  Optional[str] = None
    bitget_passphrase:  Optional[str] = None
    bitget_authorized:  bool = False    # True setelah verify_credentials sukses

    # Access Code Gate
    is_authorized:     bool = False                # True setelah akses code benar
    authorized_at:     Optional[datetime] = None   # Timestamp persetujuan
    access_attempts:   int = 0                     # Jumlah percobaan kode salah
    access_blocked_until: Optional[datetime] = None  # Blokir sementara jika melebihi limit
    
    # Version Tracking
    last_seen_version: str = "0.0.0"               # Untuk notifikasi "What's New"
