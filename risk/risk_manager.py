"""
KARA Bot - Risk Manager
THE most critical module. Protects student capital.
Never bypassed. Never disabled. Always running.

Features:
- Position sizing formula (account-aware, mode-aware)
- Daily loss limit enforcement (per-mode thresholds)
- Max drawdown kill-switch
- Post-loss cooldown
- Concurrent position limits (3 scalper / 10 standard)
- Margin check before execution
- Time-based Exit, Dynamic TP, Aggressive Trailing Stop
- Scalper Mode: 12-min force-exit, 0.20% trailing
"""

from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from config import RISK, SCALPER, MODE
from models.schemas import (
    AccountState, Position, TradeSignal, Side, PositionStatus,
    BotMode, ExecutionMode
)
from utils.helpers import format_usd

log = logging.getLogger("kara.risk")


class RiskViolation(Exception):
    """Raised when a trade violates risk rules. Non-fatal."""
    pass


class RiskManager:
    """
    Enforces all risk rules before any trade is executed.
    Also manages trailing stops and TP logic.
    """

    def __init__(self, mode_manager=None):
        self._daily_pnl:      float = 0.0
        self._peak_balance:   float = 0.0
        self._session_start_balance: float = 0.0
        self._last_reset_day: Optional[str] = None   # YYYY-MM-DD
        self._cooldown_until: Optional[float] = None  # monotonic timestamp
        self._kill_switch:    bool = False
        self._paused:         bool = False
        # ModeManager injected from main.py (lazy to avoid circular import)
        self._mode_mgr = mode_manager

    def set_mode_manager(self, mode_manager):
        """Inject ModeManager after construction (avoids circular import)."""
        self._mode_mgr = mode_manager

    def _cfg(self):
        """Return active mode config (SCALPER or RISK) based on current mode."""
        if self._mode_mgr and self._mode_mgr.is_scalper():
            return SCALPER
        return RISK

    def _is_scalper(self) -> bool:
        """True if scalper mode is currently active."""
        return bool(self._mode_mgr and self._mode_mgr.is_scalper())

    # ──────────────────────────────────────────
    # DAILY RESET
    # ──────────────────────────────────────────

    def reset_daily(self, current_balance: float) -> bool:
        """Call at midnight UTC or on first run. Returns True if reset happened."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._last_reset_day:
            self._daily_pnl     = 0.0
            self._session_start_balance = current_balance
            self._last_reset_day = today
            log.info(f"📅 Daily reset - session balance: {format_usd(current_balance)}")
            return True
        return False

    # ──────────────────────────────────────────
    # PRE-TRADE CHECK
    # ──────────────────────────────────────────

    def pre_trade_check(
        self,
        signal: TradeSignal,
        account: AccountState,
        open_positions: List[Position],
    ) -> Tuple[bool, str]:
        """
        Full risk check before executing a trade.
        Returns (approved: bool, reason: str)
        """
        # ── Kill switch ───────────────────────────────────────────────
        if self._kill_switch or account.kill_switch_active:
            return False, "🚨 KILL SWITCH ACTIVE - trading stopped (max drawdown hit)"

        # ── Paused ────────────────────────────────────────────────────
        if self._paused or account.is_paused:
            return False, "⏸️  Bot is paused by user"

        # ── Post-loss cooldown ─────────────────────────────────────────
        if self._cooldown_until and time.monotonic() < self._cooldown_until:
            remaining = int(self._cooldown_until - time.monotonic())
            hrs = remaining // 3600
            mins = (remaining % 3600) // 60
            return False, f"❄️  Post-loss cooldown active - {hrs}h {mins}m remaining"

        # ── Concurrent positions cap (mode-aware) ─────────────────────
        cfg = self._cfg()
        open_count = len([p for p in open_positions if p.status == PositionStatus.OPEN])
        max_pos = cfg.max_concurrent_positions
        if open_count >= max_pos:
            mode_tag = "[SCALPER]" if self._is_scalper() else "[STANDARD]"
            return False, f"⛔ {mode_tag} Max concurrent positions ({max_pos}) reached"

        # ── Same asset already open (Pyramid Logic) ───────────────────
        asset_positions = [
            p for p in open_positions
            if p.asset == signal.asset and p.status == PositionStatus.OPEN
        ]
        if asset_positions:
            # Check for Pyramid Scale-in (Scalper only)
            if self._is_scalper() and cfg.enable_pyramid:
                # Need at least 0.4% profit on existing position
                p = asset_positions[0]
                # mark_price for profit calc from current signal entry
                profit = p.floating_pct(signal.entry_price)
                if profit >= cfg.pyramid_at_profit_pct:
                    log.info(f"📐 [PYRAMID] Found profitable position on {signal.asset} ({profit*100:.2f}%). Allowing scale-in.")
                    signal.is_pyramid = True  # set flag for telegram/executor handling
                else:
                    return False, f"📌 Already holding {signal.asset} but profit {profit*100:.2f}% < {cfg.pyramid_at_profit_pct*100:.1f}% for pyramid"
            else:
                return False, f"📌 Already have an open position on {signal.asset}"

        # ── Daily loss limit (mode-specific) ───────────────────────────
        daily_pnl_pct = self._daily_pnl / max(account.total_equity, 1)
        daily_hard = cfg.daily_loss_hard_pct if hasattr(cfg, 'daily_loss_hard_pct') else RISK.daily_loss_hard_pct

        if abs(daily_pnl_pct) >= daily_hard and self._daily_pnl < 0:
            self._paused = True
            return False, (
                f"🚫 Daily loss limit reached: {daily_pnl_pct*100:.1f}% "
                f"(limit: {daily_hard*100:.0f}%) - trading paused for today"
            )

        if hasattr(RISK, 'daily_loss_limit_pct') and abs(daily_pnl_pct) >= RISK.daily_loss_limit_pct and self._daily_pnl < 0:
            log.warning(f"⚠️  Daily loss at {daily_pnl_pct*100:.1f}% — approaching limit")

        # ── Max drawdown kill-switch (mode-specific) ───────────────────
        max_dd = cfg.max_drawdown_pct if hasattr(cfg, 'max_drawdown_pct') else RISK.max_drawdown_pct
        if account.current_drawdown_pct >= max_dd:
            self._kill_switch = True
            return False, (
                f"🚨 MAX DRAWDOWN KILL-SWITCH: {account.current_drawdown_pct*100:.1f}% "
                f"(limit: {max_dd*100:.0f}%) - ALL trading stopped."
            )

        # ── Available margin check ─────────────────────────────────────
        required_margin = self.calculate_margin_required(signal, account.total_equity)
        if required_margin > account.available:
            return False, (
                f"💸 Insufficient margin - need {format_usd(required_margin)}, "
                f"have {format_usd(account.available)}"
            )

        return True, "✅ Risk check passed"

    # ──────────────────────────────────────────
    # POSITION SIZING
    # ──────────────────────────────────────────

    def calculate_position_size(
        self,
        signal: TradeSignal,
        account_balance: float,
    ) -> Tuple[float, float]:
        """
        Returns (size_usd, size_contracts)
        Formula: (balance * risk_pct) / (entry * sl_pct * leverage)
        OR Fixed Margin: size_usd = fixed_margin
        """
        entry = signal.entry_price
        if entry <= 0:
            raise ValueError("Invalid entry price")

        sl_pct = abs(signal.entry_price - signal.stop_loss) / signal.entry_price
        if sl_pct <= 0:
            sl_pct = RISK.default_sl_pct

        lev = signal.suggested_leverage

        # ── 1. Determine size_usd (margin) — mode-aware ───────────────
        cfg = self._cfg()
        fixed_margin = getattr(cfg, 'fixed_margin_per_position', 0.0)

        if fixed_margin > 0:
            # Fixed $ margin per trade (standard mode default)
            size_usd = fixed_margin
            log.debug(f"[RISK] Using fixed margin: {format_usd(size_usd)}")
        else:
            # Percentage-based risk (scalper uses 13%, standard uses 1%)
            risk_pct = cfg.risk_per_trade_pct
            size_usd = (account_balance * risk_pct) / max(sl_pct * lev, 0.0001)

            # Hard cap — for scalper mode no secondary cap (risk_pct IS the cap)
            if not self._is_scalper():
                max_size = (account_balance * RISK.max_risk_per_trade_pct) / max(sl_pct * lev, 0.0001)
                size_usd = min(size_usd, max_size)

        # ── 2. Calculate Contracts ────────────────────────────────────
        # isolated margin = notional / leverage -> notional = margin * leverage
        notional = size_usd * lev
        contracts = notional / entry

        log.debug(
            f"[RISK] {signal.asset}: balance={format_usd(account_balance)} "
            f"margin={format_usd(size_usd)} lev={lev}x -> {contracts:.4f} contracts"
        )
        return round(size_usd, 2), round(contracts, 4)

    def calculate_margin_required(
        self, signal: TradeSignal, account_balance: float
    ) -> float:
        """Margin = notional / leverage"""
        _, contracts = self.calculate_position_size(signal, account_balance)
        notional = contracts * signal.entry_price
        return notional / signal.suggested_leverage

    def _calculate_trade_risk(
        self, signal: TradeSignal, balance: float
    ) -> float:
        """Max loss in USD if stop-loss is hit."""
        _, contracts = self.calculate_position_size(signal, balance)
        sl_pct = abs(signal.entry_price - signal.stop_loss) / signal.entry_price
        return contracts * signal.entry_price * sl_pct

    # ──────────────────────────────────────────
    # DYNAMIC TP (Solution 2)
    # ──────────────────────────────────────────

    def get_dynamic_tp_levels(
        self, 
        coin: str, 
        oi_usd: float, 
        volatility_regime: str
    ) -> Tuple[float, float]:
        """
        Calculate dynamic TP1/TP2 based on OI and Volatility.
        - OI < $50M: TP1 0.8%, TP2 1.5%
        - OI >= $50M: Use defaults (1.5%/4% paper, 4%/8% live)
        - HIGH_VOL/EXTREME: Reduce TP by 20%
        """
        # 1. Base levels based on OI
        if oi_usd < RISK.dynamic_tp_oi_threshold:
            tp1 = RISK.small_cap_tp1_pct
            tp2 = RISK.small_cap_tp2_pct
            log.debug(f"[RISK] {coin} identified as Small Cap (OI: {format_usd(oi_usd)})")
        else:
            # Defaults - detect if paper or live via config
            if MODE == "paper":
                tp1 = RISK.paper_tp1_pct
                tp2 = RISK.paper_tp2_pct
            else:
                tp1 = RISK.tp1_pct
                tp2 = RISK.tp2_pct

        # 2. Volatility multiplier
        # Expecting enum value strings from MarketRegime
        if volatility_regime in ("high_vol", "extreme", "volatile"):
            tp1 *= RISK.vol_tp_multiplier
            tp2 *= RISK.vol_tp_multiplier
            log.debug(f"[RISK] {coin} TP reduced by {int((1-RISK.vol_tp_multiplier)*100)}% due to {volatility_regime} volatility")

        return round(tp1, 4), round(tp2, 4)

    # ──────────────────────────────────────────
    # PARTIAL TP & TRAILING STOP
    # ──────────────────────────────────────────

    def check_tp_trail(
        self,
        position: Position,
        current_price: float,
    ) -> Optional[Dict]:
        """
        Check for TP1, TP2, Trailing Stop, or Time-based Exit.
        Mode-aware: scalper uses 12-min max hold and 0.20% trailing.
        Returns action dict or None.
        """
        now_utc = datetime.now(timezone.utc)
        duration_hrs = (now_utc - position.opened_at).total_seconds() / 3600
        floating = position.floating_pct(current_price)

        # ── 1a. SCALPER: Force exit after 12 minutes ───────────────────
        if self._is_scalper():
            max_hold_hrs = SCALPER.max_hold_minutes / 60
            if duration_hrs >= max_hold_hrs:
                return {
                    "action":       "time_exit",
                    "close_ratio":  1.0,
                    "price":        current_price,
                    "message":      (
                        f"⚡ Scalper max hold ({SCALPER.max_hold_minutes:.0f}min) reached. "
                        f"Force exit at {floating*100:.2f}% profit/loss."
                    )
                }

        # ── 1b. STANDARD: Time-based exit (8h, 1-3% profit) ───────────
        elif duration_hrs >= RISK.time_based_exit_hours:
            if RISK.time_based_min_profit <= floating <= RISK.time_based_max_profit:
                return {
                    "action":       "time_exit",
                    "close_ratio":  1.0,
                    "price":        current_price,
                    "message":      (
                        f"⏳ Time-based exit triggered: profit locked after "
                        f"{int(duration_hrs)} hours ({floating*100:.1f}%)"
                    )
                }

        # ── 2. PARTIAL TAKE PROFIT (mode-aware ratios) ────────────────
        cfg = self._cfg()
        tp1_ratio = cfg.tp1_close_ratio
        tp2_ratio = cfg.tp2_close_ratio

        # TP1: Use dynamic level stored in position
        if not position.tp1_hit and floating >= position.tp1:
            return {
                "action":       "tp1",
                "close_ratio":  tp1_ratio,
                "price":        current_price,
                "message":      (
                    f"🎯 TP1 hit! +{floating*100:.2f}% floating. "
                    f"Closing {tp1_ratio*100:.0f}% of position. "
                    f"Trailing stop now active."
                )
            }

        # TP2: Use dynamic level stored in position
        if position.tp1_hit and not position.tp2_hit and floating >= position.tp2:
            return {
                "action":       "tp2",
                "close_ratio":  tp2_ratio,
                "price":        current_price,
                "message":      (
                    f"🎯 TP2 hit! +{floating*100:.2f}% floating. "
                    f"Closing {tp2_ratio*100:.0f}% more. "
                    f"Aggressive trailing active."
                )
            }

        # ── 3. TRAILING STOP (mode-aware) ─────────────────────────────
        # Scalper: 0.20% tight trail, activates at TP1
        # Standard: 3% trail (2% if profit > 8% — aggressive protect)
        if position.tp1_hit or floating >= 0.08:
            if self._is_scalper():
                trail_pct = SCALPER.trailing_pct          # 0.20%
            else:
                trail_pct = 0.02 if floating >= 0.08 else RISK.trailing_pct  # 2% or 3%
            
            if position.side == Side.LONG:
                new_high = max(position.trailing_high, current_price)
                trail_sl = new_high * (1 - trail_pct)
                if current_price <= trail_sl:
                    return {
                        "action":       "trailing_stop",
                        "close_ratio":  1.0,
                        "price":        current_price,
                        "trail_price":  trail_sl,
                        "message":      (
                            f"🛡️ Aggressive trailing stop ({trail_pct*100:.0f}%) hit at {trail_sl:.4f} "
                            f"(peak profit was +{max(0, (new_high-position.entry_price)/position.entry_price)*100:.1f}%)."
                        )
                    }
            else:   # SHORT
                new_low = min(position.trailing_high, current_price)
                trail_sl = new_low * (1 + trail_pct)
                if current_price >= trail_sl:
                    return {
                        "action":       "trailing_stop",
                        "close_ratio":  1.0,
                        "price":        current_price,
                        "trail_price":  trail_sl,
                        "message": (
                            f"🛡️ Aggressive trailing stop ({trail_pct*100:.0f}%) hit at {trail_sl:.4f} "
                            f"(peak profit was +{max(0, (position.entry_price-new_low)/position.entry_price)*100:.1f}%)."
                        )
                    }

        # ── 4. HARD STOP-LOSS ─────────────────────────────────────────
        if position.side == Side.LONG and current_price <= position.stop_loss:
            return {
                "action":       "stop_loss",
                "close_ratio":  1.0,
                "price":        current_price,
                "message":      (
                    f"🛑 Stop-loss hit at {position.stop_loss:.4f}. "
                    f"Loss: {floating*100:.2f}%. Protecting capital first. 💪"
                )
            }
        if position.side == Side.SHORT and current_price >= position.stop_loss:
            return {
                "action":       "stop_loss",
                "close_ratio":  1.0,
                "price":        current_price,
                "message":      (
                    f"🛑 Stop-loss hit at {position.stop_loss:.4f}. "
                    f"Loss: {floating*100:.2f}%. Protecting capital first. 💪"
                )
            }

        return None

    # ──────────────────────────────────────────
    # STATE UPDATES
    # ──────────────────────────────────────────

    def record_pnl(self, pnl_usd: float, account_balance: float):
        """Update daily PnL and check limits."""
        self._daily_pnl += pnl_usd

        # Update peak balance
        if account_balance > self._peak_balance:
            self._peak_balance = account_balance

        # Check if cooldown should be triggered (> 6% daily loss)
        daily_pnl_pct = self._daily_pnl / max(self._session_start_balance, 1)
        if daily_pnl_pct < -0.06 and not self._cooldown_until:
            cooldown_hrs = RISK.post_loss_cooldown_hrs
            self._cooldown_until = time.monotonic() + cooldown_hrs * 3600
            log.warning(
                f"❄️  Daily loss {daily_pnl_pct*100:.1f}% > 6% - "
                f"cooldown activated for {cooldown_hrs:.0f} hours"
            )

    def pause(self):
        self._paused = True
        log.info("⏸️  Risk manager: trading paused")

    def resume(self):
        self._paused = False
        self._cooldown_until = None
        log.info("▶️  Risk manager: trading resumed")

    def reset_kill_switch(self):
        """Only call after manual review. NEVER auto-reset."""
        log.warning("🔓 Kill switch manually reset by user")
        self._kill_switch = False

    @property
    def status(self) -> Dict:
        return {
            "paused":        self._paused,
            "kill_switch":   self._kill_switch,
            "daily_pnl":     self._daily_pnl,
            "peak_balance":  self._peak_balance,
            "cooldown_until": self._cooldown_until,
            "in_cooldown":   bool(
                self._cooldown_until and
                time.monotonic() < self._cooldown_until
            ),
        }
