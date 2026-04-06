"""
KARA Bot - Pydantic Schemas
All data models used across the bot.
"""

from __future__ import annotations
from datetime import datetime
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
    timestamp:       datetime = Field(default_factory=datetime.utcnow)

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
    timestamp:       datetime = Field(default_factory=datetime.utcnow)


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
    timestamp:       datetime = Field(default_factory=datetime.utcnow)


class OrderbookSnapshot(BaseModel):
    asset:           str
    bids:            List[List[float]]  # [[price, size], ...]
    asks:            List[List[float]]
    mid_price:       float
    spread_pct:      float
    bid_ask_imbalance: float           # -1 (all asks) to +1 (all bids)
    vwap:            float
    vwap_deviation_pct: float          # (mid - vwap) / vwap
    timestamp:       datetime = Field(default_factory=datetime.utcnow)


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

    # Levels
    entry_price:      float
    stop_loss:        float
    tp1:              float
    tp2:              float
    suggested_leverage: int

    # Position sizing (filled by RiskManager)
    suggested_size_usd: Optional[float] = None
    suggested_contracts: Optional[float] = None

    # Meta
    timestamp:        datetime = Field(default_factory=datetime.utcnow)
    expiry:           Optional[datetime] = None   # signal expires if not acted on
    confirmed:        bool = False                # user confirmed (semi-auto)
    auto_executed:    bool = False                # full-auto bypass

    def localize_for_user(self, mode: str):
        """Dynamic adjustment of TP/SL/Lev based on user's trading mode."""
        import config
        from models.schemas import Side

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
            self.suggested_leverage = 10  # default standard

        if self.side == Side.LONG:
            self.stop_loss = round(self.entry_price * (1 - sl_pct), 8)
            self.tp1       = round(self.entry_price * (1 + tp1_pct), 8)
            self.tp2       = round(self.entry_price * (1 + tp2_pct), 8)
        else:
            self.stop_loss = round(self.entry_price * (1 + sl_pct), 8)
            self.tp1       = round(self.entry_price * (1 - tp1_pct), 8)
            self.tp2       = round(self.entry_price * (1 - tp2_pct), 8)

    @property
    def risk_reward_ratio(self) -> float:
        if self.side == Side.LONG:
            risk   = self.entry_price - self.stop_loss
            reward = self.tp2 - self.entry_price
        else:
            risk   = self.stop_loss - self.entry_price
            reward = self.entry_price - self.tp2
        return round(reward / risk, 2) if risk > 0 else 0.0


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
    created_at:       datetime = Field(default_factory=datetime.utcnow)
    updated_at:       datetime = Field(default_factory=datetime.utcnow)
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
    trailing_active:  bool = False
    trailing_high:    float = 0.0    # highest price reached (for long trailing)

    # State
    status:           PositionStatus = PositionStatus.OPEN
    tp1_hit:          bool = False
    tp2_hit:          bool = False
    pnl_realized:     float = 0.0
    pnl_unrealized:   float = 0.0

    # Meta
    signal_id:        Optional[str] = None
    opened_at:        datetime = Field(default_factory=datetime.utcnow)
    closed_at:        Optional[datetime] = None
    is_paper:         bool = True

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == Side.LONG:
            pnl = (current_price - self.entry_price) / self.entry_price
        else:
            pnl = (self.entry_price - current_price) / self.entry_price
        return pnl * self.size_current * self.entry_price

    def floating_pct(self, current_price: float) -> float:
        if self.side == Side.LONG:
            return (current_price - self.entry_price) / self.entry_price
        else:
            return (self.entry_price - current_price) / self.entry_price


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
    updated_at:       datetime = Field(default_factory=datetime.utcnow)


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
    trading_mode:  str = "standard"        # standard | scalper
    bot_mode:      BotMode = BotMode.PAPER # paper | live
    risk_pct:      float = 0.02            # 2% of equity
    max_positions: int = 5

class User(BaseModel):
    chat_id:           str
    username:          str = ""
    paper_balance_usd: float               # internally USD
    created_at:        datetime = Field(default_factory=datetime.utcnow)
    config:            UserConfig = Field(default_factory=UserConfig)
    
    # Live mode auth
    hl_agent_address:  Optional[str] = None
    hl_agent_secret:   Optional[str] = None
    wallet_authorized: bool = False
