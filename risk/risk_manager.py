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
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from core.db import user_db
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

    def __init__(self, mode_manager=None, chat_id: str = ""):
        self._chat_id         = chat_id
        self._daily_pnl:      float = 0.0
        self._peak_balance:   float = 0.0
        self._session_start_balance: float = 0.0
        self._last_reset_day: Optional[str] = None   # YYYY-MM-DD
        self._cooldown_until: Optional[float] = None  # monotonic timestamp (now reset on start to prevent lockout)
        self._kill_switch:    bool = False
        self._paused:         bool = False
        self._latest_score:   Dict[str, int] = {}     # asset -> latest score from scanner

        # --- Hydrate from persisted state if exists
        self._load_risk_state()

    def _persist_risk_state(self):
        if not self._chat_id: return
        user_db.save_risk_state(self._chat_id, {
            "daily_pnl":      self._daily_pnl,
            "peak_balance":   self._peak_balance,
            "session_start_balance": self._session_start_balance,
            "kill_switch":    self._kill_switch,
            "last_reset_day": self._last_reset_day
        })

    def _load_risk_state(self):
        if not self._chat_id: return
        state = user_db.load_risk_state(self._chat_id)
        if state:
            self._daily_pnl      = state.get("daily_pnl", 0.0)
            self._peak_balance   = state.get("peak_balance", 0.0)
            self._session_start_balance = state.get("session_start_balance", 0.0)
            self._kill_switch    = state.get("kill_switch", False)
            self._last_reset_day = state.get("last_reset_day")
            
            # Validation: if session_start_balance is 0 but we have a peak, use that as fallback
            # to prevent 'amnesia' during mid-day restarts
            if self._session_start_balance <= 0 and self._peak_balance > 0:
                self._session_start_balance = self._peak_balance

    def _cfg(self):
        """Return active mode config (SCALPER or RISK) based on current mode."""
        if self._is_scalper():
            return SCALPER
        return RISK

    def _is_scalper(self) -> bool:
        """True if scalper mode is currently active for this user."""
        if not self._chat_id: return False
        user = user_db.get_user(self._chat_id)
        if user and user.config.trading_mode == "scalper":
            return True
        return False

    def _get_user_value(self, key: str, global_fallback=None):
        """Helper to get mode-specific value from user config."""
        user = user_db.get_user(self._chat_id)
        if not user: return global_fallback
        
        is_scalper = user.config.trading_mode == "scalper"
        prefix = "scl_" if is_scalper else "std_"
        return getattr(user.config, f"{prefix}{key}", global_fallback)

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
            self._persist_risk_state()
            log.info(f"📅 Daily reset - session balance: {format_usd(current_balance)}")
            return True
        return False

    def reset_kill_switch(self, requester_id: str):
        admin_id = os.getenv("ADMIN_CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", ""))
        if requester_id != admin_id:
            raise PermissionError("Hanya admin yang bisa reset kill-switch.")
        self._kill_switch = False
        self._persist_risk_state()
        log.warning(f"Kill-switch explicitly reset by Admin {admin_id}")

    def update_score(self, asset: str, score: int):
        """Called by scanner to update the latest score for an asset."""
        self._latest_score[asset] = score

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

        cfg = self._cfg()

        # ── Concurrent positions cap (mode-aware & user-specific) ──────
        open_count = len([p for p in open_positions if p.status == PositionStatus.OPEN])
        max_pos = self._get_user_value("max_concurrent_positions", cfg.max_concurrent_positions)

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
        required_margin = self.calculate_margin_required(signal, account)
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
    ) -> Tuple[float, float, int]:
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

        # ── Leverage: Triple-Cap (Signal vs User vs Exchange) ──────────
        cfg = self._cfg()
        user_max_lev = self._get_user_value("max_leverage", cfg.max_leverage)
        
        # Get exchange-allowed max for this specific asset (Market-Aware)
        from data.hyperliquid_client import get_client
        client = get_client()
        exchange_max = 50 # Default
        if client._market_cache:
            universe, _ = client._market_cache
            for u in universe:
                if isinstance(u, dict) and u.get("name") == signal.asset:
                    exchange_max = int(u.get("maxLeverage", 50))
                    break
        
        # Apply the triple cap
        lev = min(signal.suggested_leverage, user_max_lev, exchange_max)
        
        if lev != signal.suggested_leverage:
             log.info(
                 f"🛡️ [RISK] {signal.asset} Leverage capped: "
                 f"signal={signal.suggested_leverage}x, user={user_max_lev}x, exchange={exchange_max}x -> using {lev}x"
             )

        # ── 1. Determine size_usd (margin) — mode-aware ───────────────
        cfg = self._cfg()
        
        # --- CONVICTION-WEIGHTED POSITION SIZING ---
        # Map score to risk percentage
        score = getattr(signal, 'score', 0)
        if score >= 90:
            risk_pct = 0.80  # Maximum conviction: 80% risk
        elif score >= 80:
            risk_pct = 0.50  # Very high conviction: 50% risk
        elif score >= 71:
            risk_pct = 0.30  # High conviction: 30% risk
        elif score >= 63:
            risk_pct = 0.20  # Moderate conviction: 20% risk
        else:
            risk_pct = 0.15  # Low conviction (55-62): 15% risk

        # Compound sizing
        size_usd = (account_balance * risk_pct) / max(sl_pct * lev, 0.0001)

        # Drawdown guard: if we are >15% below peak, cut risk in half!
        # Find drawdown:
        drawdown = (self._peak_balance - account_balance) / max(self._peak_balance, 1)
        if drawdown >= 0.15:
            size_usd *= 0.5
            log.warning(f"[RISK] Drawdown guard active (DD: {drawdown*100:.1f}% >= 15%). Risk halved to {risk_pct/2*100:.1f}%.")

        # ── 3. Hard Margin Cap (Safety First - 80% Max Equity) ────────
        max_allowed_margin = account_balance * 0.80
        if size_usd > max_allowed_margin:
            log.warning(f"[RISK] Margin cap hit: {format_usd(size_usd)} -> {format_usd(max_allowed_margin)} (80% limit)")
            size_usd = max_allowed_margin

        # ── 4. Calculate Contracts ────────────────────────────────────
        # isolated margin = notional / leverage -> notional = margin * leverage
        notional = size_usd * lev
        contracts = notional / entry

        log.debug(
            f"[RISK] {signal.asset}: balance={format_usd(account_balance)} "
            f"margin={format_usd(size_usd)} lev={lev}x -> {contracts:.4f} contracts"
        )
        return round(size_usd, 2), round(contracts, 4), int(lev)

    def calculate_margin_required(
        self, signal: TradeSignal, account: AccountState
    ) -> float:
        """Margin = notional / leverage"""
        # Daily loss/drawdown check
        approved, _ = self.pre_trade_check(signal, account, [])
        if not approved: return 0.0
        
        _, contracts, lev = self.calculate_position_size(signal, account.total_equity)
        notional = contracts * signal.entry_price
        return notional / lev

    def _calculate_trade_risk(
        self, signal: TradeSignal, balance: float
    ) -> float:
        """Max loss in USD if stop-loss is hit."""
        _, contracts = self.calculate_position_size(signal, balance)
        sl_pct = abs(signal.entry_price - signal.stop_loss) / signal.entry_price
        return contracts * signal.entry_price * sl_pct

    # ──────────────────────────────────────────
    # DYNAMIC TP & ATR (Opsi B Implementation)
    # ──────────────────────────────────────────

    def get_dynamic_tp_levels(
        self, 
        coin: str, 
        oi_usd: float, 
        volatility_regime: str
    ) -> Tuple[float, float]:
        """
        Calculate dynamic TP1/TP2 based on OI and Volatility.
        Mode-aware: respects Scalper vs Standard base targets.
        """
        cfg = self._cfg()
        
        # 1. Base levels based on OI (Small Cap logic)
        if oi_usd < RISK.dynamic_tp_oi_threshold:
            tp1 = RISK.small_cap_tp1_pct
            tp2 = RISK.small_cap_tp2_pct
            log.debug(f"[RISK] {coin} identified as Small Cap (OI: {format_usd(oi_usd)})")
        else:
            # Mode-aware base TP
            if MODE == "paper":
                # For standard mode, use paper-specific constants if available
                tp1 = getattr(cfg, 'paper_tp1_pct', cfg.tp1_pct)
                tp2 = getattr(cfg, 'paper_tp2_pct', cfg.tp2_pct)
            else:
                tp1 = cfg.tp1_pct
                tp2 = cfg.tp2_pct

        # 2. Volatility multiplier
        if volatility_regime in ("high_vol", "extreme", "volatile"):
            tp1 *= RISK.vol_tp_multiplier
            tp2 *= RISK.vol_tp_multiplier
            log.debug(f"[RISK] {coin} TP reduced by {int((1-RISK.vol_tp_multiplier)*100)}% due to {volatility_regime} volatility")

        return round(tp1, 6), round(tp2, 6)

    def calculate_atr(self, candles: List[Dict[str, Any]]) -> float:
        """
        Standard ATR calculation (Average True Range).
        Requires at least 2 candles. 
        Candle expected: {'h': high, 'l': low, 'c': close}
        """
        if not candles or len(candles) < 2:
            return 0.0
        
        trs = []
        for i in range(1, len(candles)):
            curr = candles[i]
            prev = candles[i-1]
            
            # True Range components
            tr1 = float(curr["h"]) - float(curr["l"])
            tr2 = abs(float(curr["h"]) - float(prev["c"]))
            tr3 = abs(float(curr["l"]) - float(prev["c"]))
            
            trs.append(max(tr1, tr2, tr3))
            
        if not trs: return 0.0
        return sum(trs) / len(trs)

    # ──────────────────────────────────────────
    # PARTIAL TP & TRAILING STOP
    # ──────────────────────────────────────────

    def check_tp_trail(
        self,
        position: Position,
        current_price: float,
    ) -> Optional[Dict]:
        """
        Check for 5-Level Professional Exit Hierarchy.
        Returns action dict or None.
        """
        now_utc = datetime.now(timezone.utc)
        duration_hrs = (now_utc - position.opened_at).total_seconds() / 3600
        floating = position.floating_pct(current_price)
        current_score = self._latest_score.get(position.asset, position.entry_score if hasattr(position, 'entry_score') else 50)
        entry_score = getattr(position, "entry_score", 50)

        # ── Level 1: Fast Exit (first 30 minutes) ───────────────────────────
        if duration_hrs <= 0.5 and current_score < 35:
            return {
                "action":       "time_exit",
                "close_ratio":  1.0,
                "price":        current_price,
                "message":      (
                    f"🏃 Level 1 Fast Exit: Score dropped to {current_score} within 30m. "
                    f"Thesis broken. Exiting at {floating*100:.2f}%."
                )
            }

        # ── Level 5: Re-evaluation on Score Drop ───────────────────────────
        # Check if score dropped by > 20 points
        score_drop = entry_score - current_score
        
        # Set a flag to tighten trailing stop if thesis is invalidated
        tighten_trail = score_drop >= 20

        # ── Level 4: Stale Position Exit ────────────────────────────────────
        if duration_hrs >= 4.0 and abs(floating) <= 0.003:
            return {
                "action":       "time_exit",
                "close_ratio":  1.0,
                "price":        current_price,
                "message":      (
                    f"🕰️ Level 4 Stale Exit: >4h hold with <0.3% move ({floating*100:.2f}%). "
                    f"Redeploying capital."
                )
            }

        # ── 2. PARTIAL TAKE PROFIT (mode-aware ratios) ────────────────
        cfg = self._cfg()
        tp1_ratio = getattr(cfg, 'tp1_close_ratio', 0.40)
        tp2_ratio = getattr(cfg, 'tp2_close_ratio', 0.35)

        # TP1: compare against actual TP1 PRICE
        tp1_hit_now = (
            (position.side == Side.LONG and current_price >= position.tp1) or
            (position.side == Side.SHORT and current_price <= position.tp1)
        )
        if not position.tp1_hit and tp1_hit_now:
            return {
                "action":       "tp1",
                "close_ratio":  tp1_ratio,
                "price":        current_price,
                "message":      (
                    f"🎯 TP1 hit! +{floating*100:.2f}%. Level 2 Breakeven Protection active."
                )
            }

        tp2_hit_now = (
            (position.side == Side.LONG and current_price >= position.tp2) or
            (position.side == Side.SHORT and current_price <= position.tp2)
        )
        if position.tp1_hit and not position.tp2_hit and tp2_hit_now:
            return {
                "action":       "tp2",
                "close_ratio":  tp2_ratio,
                "price":        current_price,
                "message":      (
                    f"🎯 TP2 hit! +{floating*100:.2f}%. Activating aggressive trailer."
                )
            }

        # ── Level 3: Trailing Stop (Primary Exit) ─────────────────────────
        # Standard: 3% trail (2% if profit > 8%), but if tighten_trail (Level 5) = 0.3%
        if position.tp1_hit or floating >= 0.08 or tighten_trail:
            if tighten_trail:
                trail_pct = 0.003  # 0.3% tight trail to close naturally
            elif self._is_scalper():
                trail_pct = getattr(SCALPER, 'trailing_pct', 0.004) # 0.40%
            else:
                trail_pct = 0.02 if floating >= 0.08 else getattr(RISK, 'trailing_pct', 0.03)
            
            if position.side == Side.LONG:
                new_high = max(position.trailing_high, current_price)
                trail_sl = new_high * (1 - trail_pct)
                if current_price <= trail_sl:
                    msg = "🛡️ Trailing Stop" if not tighten_trail else "⚠️ Level 5 Re-eval Exit (Tight Trail)"
                    return {
                        "action":       "trailing_stop",
                        "close_ratio":  1.0,
                        "price":        current_price,
                        "trail_price":  trail_sl,
                        "message":      (
                            f"{msg} ({trail_pct*100:.1f}%) hit at {trail_sl:.4f} "
                            f"(peak profit +{max(0, (new_high-position.entry_price)/position.entry_price)*100:.1f}%)."
                        )
                    }
            else:   # SHORT
                new_low = min(position.trailing_high, current_price)
                trail_sl = new_low * (1 + trail_pct)
                if current_price >= trail_sl:
                    msg = "🛡️ Trailing Stop" if not tighten_trail else "⚠️ Level 5 Re-eval Exit (Tight Trail)"
                    return {
                        "action":       "trailing_stop",
                        "close_ratio":  1.0,
                        "price":        current_price,
                        "trail_price":  trail_sl,
                        "message": (
                            f"{msg} ({trail_pct*100:.1f}%) hit at {trail_sl:.4f} "
                            f"(peak profit +{max(0, (position.entry_price-new_low)/position.entry_price)*100:.1f}%)."
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

        # Ensure we have a valid baseline for percentage calculation
        if self._session_start_balance <= 0:
            self._session_start_balance = account_balance
            log.debug(f"[RISK] Initialized mid-session start balance: ${self._session_start_balance:,.2f}")

        # Check if cooldown should be triggered (> 6% daily loss)
        daily_pnl_pct = self._daily_pnl / max(self._session_start_balance, 1)
        
        # PERSIST STATE IMMEDIATELY after update
        self._persist_risk_state()

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
            "session_start_balance": self._session_start_balance,
            "cooldown_until": self._cooldown_until,
            "in_cooldown":   bool(
                self._cooldown_until and
                time.monotonic() < self._cooldown_until
            ),
        }
