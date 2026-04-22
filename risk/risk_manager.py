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
        self._cooldown_until: Optional[datetime] = None  # UTC datetime — persists across restarts
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
            "last_reset_day": self._last_reset_day,
            # Store as ISO string so it survives restart (monotonic() would not)
            "cooldown_until": self._cooldown_until.isoformat() if self._cooldown_until else None,
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

            # Restore cooldown as UTC datetime
            raw_cd = state.get("cooldown_until")
            if raw_cd:
                try:
                    cd = datetime.fromisoformat(raw_cd)
                    # Only restore if it's still in the future
                    if cd > datetime.now(timezone.utc):
                        self._cooldown_until = cd
                        log.warning(
                            f"[RISK] Cooldown restored from DB — expires at {cd.isoformat()}"
                        )
                except Exception:
                    pass

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
        cfg = self._cfg()
        max_dd = cfg.max_drawdown_pct if hasattr(cfg, 'max_drawdown_pct') else RISK.max_drawdown_pct
        
        # Auto-reset if limit was increased or drawdown improved
        if self._kill_switch and account.current_drawdown_pct < max_dd:
            log.info(f"🔄 [RISK] Max drawdown recovered ({account.current_drawdown_pct*100:.1f}% < {max_dd*100:.0f}%). Resetting kill switch.")
            self._kill_switch = False

        if self._kill_switch or account.kill_switch_active:
            return False, "🚨 KILL SWITCH ACTIVE - trading stopped (max drawdown hit)"
            
        # ── Intelligence Filter (ML Expected Edge) ────────────────────
        edge = getattr(signal, 'expected_edge', 1.0)
        if edge is not None and edge < 0.4:
            return False, f"🤖 [AI ABORT] Expected Edge too low ({edge*100:.1f}% win prob < 40%)"

        # ── Paused ────────────────────────────────────────────────────
        if self._paused or account.is_paused:
            return False, "⏸️  Bot is paused by user"

        # ── Post-loss cooldown ─────────────────────────────────────────
        if self._cooldown_until and datetime.now(timezone.utc) < self._cooldown_until:
            remaining = int((self._cooldown_until - datetime.now(timezone.utc)).total_seconds())
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
        # Dynamic Risk Sizing using Intelligence Model
        from intelligence.dynamic_risk import calculate_risk_multiplier
        edge = getattr(signal, 'expected_edge', 1.0)
        multiplier = calculate_risk_multiplier(edge)

        # Scale leverage and risk parameter
        cfg = self._cfg()
        default_lev = signal.suggested_leverage
        actual_lev = min(int(default_lev * multiplier), cfg.max_leverage)
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
        lev = min(actual_lev, user_max_lev, exchange_max)
        
        if lev != signal.suggested_leverage:
             log.info(
                 f"🛡️ [RISK] {signal.asset} Leverage capped: "
                 f"signal={signal.suggested_leverage}x, user={user_max_lev}x, exchange={exchange_max}x -> using {lev}x"
             )

        # ── 1. Determine size_usd (margin) — mode-aware ───────────────
        cfg = self._cfg()
        
        # --- CONVICTION-WEIGHTED POSITION SIZING (AGGRESSIVE) ---
        score = getattr(signal, 'score', 0)
        risk_pct = self.get_risk_pct(score, account_balance)
        
        # Apply AI Multiplier to Risk!
        risk_pct = min(risk_pct * multiplier, cfg.max_risk_per_trade_pct)

        # Compound sizing
        size_usd = (account_balance * risk_pct) / max(sl_pct * lev, 0.0001)

        # Drawdown guard: if we are >15% below peak, cut risk in half!
        # Find drawdown:
        drawdown = (self._peak_balance - account_balance) / max(self._peak_balance, 1)
        if drawdown >= 0.15:
            size_usd *= 0.5
            log.warning(f"[RISK] Drawdown guard active (DD: {drawdown*100:.1f}% >= 15%). Risk halved to {risk_pct/2*100:.1f}%.")

        # ── 3. Hard Margin Cap (Safety First - 35% Max Equity) ────────
        max_allowed_margin = account_balance * 0.35
        if size_usd > max_allowed_margin:
            log.warning(f"[RISK] Margin cap hit: {format_usd(size_usd)} -> {format_usd(max_allowed_margin)} (35% limit)")
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

    def get_risk_pct(self, score: int, equity: float) -> float:
        """
        [FIX 5 - 2026-04-22] Score-based position sizing.
        
        Data 124 trades: Losers avg notional $295 vs Winners avg $258.
        Trade low confidence malah pakai capital lebih banyak.
        
        Tier baru:
          Score >= 75: 2.5% risk (full confidence)
          Score >= 68: 2.0% risk
          Score >= 62: 1.5% risk (minimum threshold)
          Score <  62: 1.0% risk (seharusnya tidak masuk, tapi safety net)
        """
        # [FIX 5 - 2026-04-22] Increased risk tiers based on user request
        if score >= 75:
            risk_pct = 0.035   # 3.5% - high conviction trade (was 2.5%)
        elif score >= 68:
            risk_pct = 0.030   # 3.0% (was 2.0%)
        elif score >= 60:
            risk_pct = 0.025   # 2.5% (was 1.5%)
        else:
            risk_pct = 0.020   # 2.0% - minimum risk (was 1.0%)
        
        # Equity protection multiplier
        ratio = equity / self._session_start_balance if self._session_start_balance > 0 else 1.0
        if ratio >= 1.5:   equity_mult = 0.8   # protect gains
        elif ratio <= 0.8: equity_mult = 0.5   # damaged mode
        else:              equity_mult = 1.0
        
        return risk_pct * equity_mult

    # ──────────────────────────────────────────
    # DYNAMIC TP & SL (Fix 10)
    # ──────────────────────────────────────────

    def calculate_tp_levels(self, asset: str, entry_price: float, side: Side, realized_vol: float) -> Tuple[float, float, float]:
        """
        [FIX 1 - 2026-04-22] Widen SL based on realized daily volatility.
        
        Root cause dari 28% WR: SL rata-rata -0.38% = di dalam market noise zone.
        81% trades kena SL sebelum sempat bergerak.
        Trailing stop 100% WR membuktikan sinyal benar, tapi SL terlalu sempit.
        
        Logika baru berdasarkan vol_cache:
          Vol > 4%/hari  (aset volatile): SL 2.0%, TP1 3.0%, TP2 5.0%
          Vol 2-4%/hari  (aset normal):   SL 1.5%, TP1 2.5%, TP2 4.0%
          Vol < 2%/hari  (aset calm):     SL 1.0%, TP1 1.8%, TP2 3.0%
        """
        daily_vol = realized_vol
        
        if daily_vol > 0.04:        # volatile asset (> 4% daily)
            sl_pct  = 0.030         # 3.0% - wide margin to survive noise
            tp1_pct = 0.025         # 2.5% - quick take profit (scalp style)
            tp2_pct = 0.045         # 4.5%
        elif daily_vol > 0.02:      # normal asset (2-4% daily)
            sl_pct  = 0.025         # 2.5%
            tp1_pct = 0.018         # 1.8% - scalp style
            tp2_pct = 0.035         # 3.5%
        else:                       # calm asset (< 2% daily)
            sl_pct  = 0.020         # 2.0%
            tp1_pct = 0.012         # 1.2% - scalp style
            tp2_pct = 0.025         # 2.5%
            
        return sl_pct, tp1_pct, tp2_pct

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
        Check for simplified 4-Rule Exit Hierarchy.
        Returns action dict or None.
        """
        floating = position.floating_pct(current_price)
        
        if position.side == Side.LONG:
            new_high = max(position.trailing_high, current_price)
            max_floating = (new_high - position.entry_price) / position.entry_price
        else:
            new_low = min(position.trailing_high, current_price)
            max_floating = (position.entry_price - new_low) / position.entry_price

        # Exit Rule A: Hard SL hit
        if (position.side == Side.LONG and current_price <= position.stop_loss) or \
           (position.side == Side.SHORT and current_price >= position.stop_loss):
            return {
                "action":       "stop_loss",
                "close_ratio":  1.0,
                "price":        current_price,
                "message":      (
                    f"🛑 Stop-loss hit at {position.stop_loss:.4f}. "
                    f"Loss: {floating*100:.2f}%. Protecting capital first. 💪"
                )
            }

        cfg = self._cfg()
        tp1_ratio = getattr(cfg, 'tp1_close_ratio', 0.40)
        tp2_ratio = getattr(cfg, 'tp2_close_ratio', 0.35)

        # Exit Rule B: TP1 hit -> Log TP1, take 40%, move SL to Breakeven
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

        # Exit Rule C: TP2 hit -> Log TP2, take 35%, Trail 0.3%
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

        # Exit Rule D: Standard Trailing
        if position.tp1_hit:
            tp1_diff_pct = abs(position.entry_price - position.tp1) / position.entry_price
            activation_threshold = tp1_diff_pct + 0.005
            
            if max_floating >= activation_threshold:
                trail_pct = 0.003
                if position.side == Side.LONG:
                    trail_sl = new_high * (1 - trail_pct)
                    if current_price <= trail_sl:
                        return {
                            "action":       "trailing_stop",
                            "close_ratio":  1.0,
                            "price":        current_price,
                            "trail_price":  trail_sl,
                            "message":      (
                                f"🛡️ Trailing Stop ({trail_pct*100:.1f}%) hit at {trail_sl:.4f} "
                                f"(peak profit +{max_floating*100:.1f}%)."
                            )
                        }
                else:
                    trail_sl = new_low * (1 + trail_pct)
                    if current_price >= trail_sl:
                        return {
                            "action":       "trailing_stop",
                            "close_ratio":  1.0,
                            "price":        current_price,
                            "trail_price":  trail_sl,
                            "message": (
                                f"🛡️ Trailing Stop ({trail_pct*100:.1f}%) hit at {trail_sl:.4f} "
                                f"(peak profit +{max_floating*100:.1f}%)."
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

        if daily_pnl_pct < -0.50 and not self._cooldown_until:
            cooldown_hrs = RISK.post_loss_cooldown_hrs
            from datetime import timedelta
            self._cooldown_until = datetime.now(timezone.utc) + timedelta(hours=cooldown_hrs)
            self._persist_risk_state()  # Persist immediately so restart doesn't bypass cooldown
            log.warning(
                f"❄️  Daily loss {daily_pnl_pct*100:.1f}% > 50% - "
                f"cooldown activated until {self._cooldown_until.isoformat()}"
            )

    def pause(self):
        self._paused = True
        log.info("⏸️  Risk manager: trading paused")

    def resume(self):
        self._paused = False
        self._cooldown_until = None
        self._persist_risk_state()
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
            "cooldown_until": self._cooldown_until.isoformat() if self._cooldown_until else None,
            "in_cooldown":   bool(
                self._cooldown_until and
                datetime.now(timezone.utc) < self._cooldown_until
            ),
        }
