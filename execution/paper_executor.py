"""
KARA Bot - Paper Executor 
Simulates trade execution without real money.
Mirrors live executor interface exactly so switching modes is trivial.
Includes realistic fill simulation (spread, partial fills).
"""

from __future__ import annotations
import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import RISK, EXEC
from models.schemas import (
    Order, Position, AccountState, TradeSignal, Side,
    OrderStatus, PositionStatus, BotMode, ExecutionMode
)
from risk.risk_manager import RiskManager
from utils.helpers import gen_id, format_usd, utcnow
from utils.excel_logger import get_excel_logger
from execution.base_executor import BaseExecutor
from execution.profit_lock import paper_profit_lock_fill

log = logging.getLogger("kara.paper_exec")

# Simulated starting balance for paper mode
PAPER_INITIAL_BALANCE = 1000.0


class PaperExecutor(BaseExecutor):
    """
    Paper (simulated) execution engine.
    All trades stored in-memory; persisted to SQLite via main.py.
    """

    def __init__(self, risk_manager: RiskManager, initial_balance: float = 1000.0, chat_id: str = "system"):
        self.risk  = risk_manager
        self.mode  = BotMode.PAPER
        self.chat_id = chat_id

        # State
        self._balance:    float = initial_balance
        self._available:  float = initial_balance
        self._used_margin:float = 0.0
        self._positions:  Dict[str, Position] = {}    # position_id -> Position
        self._orders:     Dict[str, Order] = {}       # order_id -> Order
        self._daily_pnl:  float = 0.0                 # realized pnl today
        self._daily_start_balance: float = initial_balance
        self._peak_balance: float = initial_balance
        self._trade_log:  List[Dict] = []              # for backtest-style analysis

        log.info(
            f" [PAPER] Paper executor ready for {self.chat_id} - "
            f"starting balance: {format_usd(self._balance)}"
        )

    def load_from_db(self, chat_id: str):
        """Restore previous state from SQLite to survive restarts."""
        from core.db import user_db
        state = user_db.load_paper_state(chat_id)
        if state:
            self._balance = state["balance"]
            self._available = state["balance"]
            # Remove direct overwrite of _daily_start_balance to use RiskManager instead
            self._peak_balance = max(state["balance"], state.get("equity", 0))
            log.info(f" [PAPER] Restored balance from DB: {format_usd(self._balance)}")
        
        positions = user_db.load_paper_positions(chat_id)
        for pos in positions:
            self._positions[pos.position_id] = pos
            # Update used margin
            self._used_margin += pos.margin_usd
            self._available   -= pos.margin_usd
            log.info(f" [PAPER] Restored position: {pos.asset} {pos.side.value}")

    # ──────────────────────────────────────────
    # ACCOUNT STATE
    # ──────────────────────────────────────────

    async def get_account_state(self) -> AccountState:
        unrealized = sum(
            p.pnl_unrealized for p in self._positions.values()
            if p.status == PositionStatus.OPEN
        )
        total_equity = self._balance + unrealized
        
        # Drawdown is calculated from the peak total equity
        if total_equity > self._peak_balance:
            self._peak_balance = total_equity
            
        drawdown = (
            (self._peak_balance - total_equity) / self._peak_balance
            if self._peak_balance > 0 else 0
        )
        
        # Daily PnL includes both realized trades today AND current floating profit
        # BASELINE: Use RiskManager's start-of-day balance (persists across restarts)
        start_bal = self.risk.status.get("session_start_balance", self._daily_start_balance)
        daily_pnl = (total_equity - start_bal)
        daily_pnl_pct = daily_pnl / max(start_bal, 1)

        return AccountState(
            total_equity=round(total_equity, 2),
            wallet_balance=round(self._balance, 2),
            available=round(self._available, 2),
            used_margin=round(self._used_margin, 2),
            unrealized_pnl=round(unrealized, 2),
            daily_pnl=round(daily_pnl, 2),
            daily_pnl_pct=round(daily_pnl_pct, 4),
            peak_balance=round(self._peak_balance, 2),
            current_drawdown_pct=round(drawdown, 4),
            positions=list(self._positions.values()),
            mode=BotMode.PAPER,
            execution_mode=ExecutionMode.SEMI_AUTO,
            is_paused=self.risk.status["paused"],
            kill_switch_active=self.risk.status["kill_switch"],
        )

    @property
    def open_positions(self) -> List[Position]:
        return [
            p for p in self._positions.values()
            if p.status == PositionStatus.OPEN
        ]

    # ──────────────────────────────────────────
    # OPEN POSITION
    # ──────────────────────────────────────────

    async def open_position(self, signal: TradeSignal) -> Optional[Position]:
        """
        Open a paper position from a confirmed signal.
        Simulates realistic fill (spread slippage).
        """
        account = await self.get_account_state()

        # Risk check
        approved, reason = self.risk.pre_trade_check(
            signal, account, self.open_positions
        )
        if not approved:
            log.warning(f" Trade blocked: {reason}")
            return None

        # Calculate size & leverage
        size_usd, contracts, actual_lev = self.risk.calculate_position_size(
            signal, self._balance
        )
        
        # Isolated margin = notional / leverage
        margin = (contracts * signal.entry_price) / actual_lev

        # Simulate fill (add small spread)
        fill_price = self._simulate_fill(signal.entry_price, signal.side)

        # Build position
        liq_price = self._calculate_liquidation_price(fill_price, signal.side, actual_lev)

        pos = Position(
            position_id=gen_id("POS"),
            asset=signal.asset,
            side=signal.side,
            entry_price=fill_price,
            size_initial=contracts,
            size_current=contracts,
            leverage=actual_lev,
            margin_usd=margin,
            stop_loss=signal.stop_loss,
            tp1=signal.tp1,
            tp2=signal.tp2,
            trailing_active=False,
            trailing_high=fill_price,
            early_profit_lock=False,
            liquidation_price=liq_price,
            signal_id=signal.signal_id,
            meta_pattern_key=getattr(signal, 'meta_pattern_key', None),
            meta_score_delta=getattr(signal, 'meta_score_delta', 0),
            trade_mode=getattr(signal, 'trade_mode', 'standard'),
            is_paper=True,
            entry_score=signal.score,
            realized_vol=getattr(signal, 'realized_vol', 0.02),
            trend_pct=getattr(signal, 'trend_pct', 0.0),
            micro_invalidation_price=getattr(signal, 'micro_invalidation_price', None),
            entry_location_quality=getattr(signal, 'entry_location_quality', 'unknown'),
        )

        # Update balances
        self._used_margin += margin
        self._available   -= margin
        self._positions[pos.position_id] = pos

        # BUG 1 FIX: Persist to SQLite
        from core.db import user_db
        cid = self.chat_id
        user_db.save_paper_position(cid, pos)
        user_db.save_paper_state(cid, self._balance, account.total_equity)

        # Log entry
        log_data = {
            "type":     "open",
            "pos_id":   pos.position_id,
            "asset":    signal.asset,
            "side":     signal.side.value,
            "entry_price": fill_price,
            "size":     contracts,
            "notional": contracts * fill_price,
            "margin":   margin,
            "score":    signal.score,
            "meta_boost": getattr(signal, "meta_score_delta", 0),
            "meta_pattern_key": getattr(signal, "meta_pattern_key", ""),
            "expected_edge": getattr(signal, "expected_edge", None),
            "funding_rate": getattr(signal, "funding_rate", 0.0),
            "realized_vol": getattr(signal, "realized_vol", 0.0),
            "trend_pct": getattr(signal, "trend_pct", 0.0),
            "timestamp":utcnow(),
        }
        self._trade_log.append(log_data)
        get_excel_logger().log_trade(self.chat_id, log_data)

        log.info(
            f" [PAPER] Opened {signal.asset} {signal.side.value.upper()} "
            f"@ {fill_price} | {contracts:.4f} contracts "
            f"| margin: {format_usd(margin)} | lev: {signal.suggested_leverage}x"
        )
        
        # 🧠 Intelligence Experience Hook (Async to avoid blocking)
        import asyncio
        from intelligence.experience_buffer import experience_buffer
        
        bd = getattr(signal, 'breakdown', None)
        fr = float(getattr(signal, 'funding_rate', 0.0) or 0.0)
        vol = float(getattr(signal, 'realized_vol', 0.0) or 0.0)
        trend = float(getattr(signal, 'trend_pct', 0.0) or 0.0)
        
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, 
                             experience_buffer.record_entry,
                              self.chat_id, pos.position_id, signal.asset, signal.side.value,
                              signal.score, getattr(signal, 'meta_score_delta', 0),
                              bd, fr, vol, trend, getattr(signal, 'expected_edge', 0.0),
                              pos.trade_mode, pos.entry_location_quality,
                              abs((pos.micro_invalidation_price or pos.stop_loss) - pos.entry_price) / pos.entry_price
                            )
                            
        return pos

    # ──────────────────────────────────────────
    # UPDATE POSITIONS (call every price tick)
    # ──────────────────────────────────────────

    async def update_positions(
        self, prices: Dict[str, float], market_states: Optional[Dict[str, Dict]] = None
    ) -> List[Dict]:
        """
        Update unrealized PnL for all open positions.
        Check TP/trailing stop conditions.
        Returns list of triggered actions.
        """
        actions = []
        # Iterate over a snapshot of IDs, not live dict (anti-spam)
        position_ids = list(self._positions.keys())
        for pos_id in position_ids:
            pos = self._positions.get(pos_id)
            # Anti-spam guard: skip already-closed positions
            if pos is None or pos.status != PositionStatus.OPEN:
                continue
            current = prices.get(pos.asset, 0)
            if current <= 0:
                continue

            # Update unrealized PnL
            pos.pnl_unrealized = pos.unrealized_pnl(current)

            # Check TP/SL
            action = self.risk.check_tp_trail(pos, current, (market_states or {}).get(pos.position_id))
            if action:
                # Only process if STILL open (prevent race condition)
                if pos.status == PositionStatus.OPEN:
                    result = await self._execute_partial_close(pos, action, current)
                    if result:
                        actions.append(result)

        # Update peak balance
        total_equity = self._balance + sum(
            p.pnl_unrealized for p in self._positions.values()
            if p.status == PositionStatus.OPEN
        )
        if total_equity > self._peak_balance:
            self._peak_balance = total_equity

        return actions

    # ──────────────────────────────────────────
    # CLOSE POSITION
    # ──────────────────────────────────────────

    async def close_position(
        self,
        position_id: str,
        current_price: float,
        reason: str = "manual",
        close_ratio: float = 1.0,
    ) -> Optional[Dict]:
        pos = self._positions.get(position_id)
        if not pos or pos.status == PositionStatus.CLOSED:
            return None

        fill_price = self._simulate_fill(current_price, Side.SHORT if pos.side == Side.LONG else Side.LONG)
        
        # The final PnL is what was made on the REMAINING size
        floating_pnl = pos.unrealized_pnl(fill_price)
        
        # Update state
        pos.pnl_realized += floating_pnl
        pos.status       = PositionStatus.CLOSED
        pos.closed_at    = utcnow()

        # Return remaining margin + final floating PnL
        # Already realized PnL was added to _balance during partial closures
        self._balance    += floating_pnl
        self._available  += pos.margin_usd + floating_pnl
        self._used_margin-= pos.margin_usd
        
        # BUG 1 FIX: Persist to SQLite
        from core.db import user_db
        user_db.remove_paper_position(position_id)
        user_db.save_paper_state(self.chat_id, self._balance, self._balance) # equity = balance when no positions

        # Total PnL for the record (cumulative)
        total_pnl = pos.pnl_realized
        self.risk.record_pnl(total_pnl, self._balance)

        # Meta-scoring feedback loop: update pattern outcome from closed trade.
        try:
            if pos.signal_id:
                sig = user_db.get_signal_by_id(pos.signal_id)
                if sig and getattr(sig, "meta_pattern_key", None):
                    user_db.update_meta_pattern_outcome(sig.meta_pattern_key, total_pnl)
                    stats = user_db.get_meta_pattern_stats(sig.meta_pattern_key)
                    n = stats["samples"] if stats else 1
                    log.info(
                        f"🧠 [META] {sig.meta_pattern_key} | "
                        f"pnl={total_pnl:+.2f} | "
                        f"wr={stats['winrate_ema']*100:.0f}% | "
                        f"n={n}"
                        if stats else
                        f"🧠 [META] {sig.meta_pattern_key} | pnl={total_pnl:+.2f} | n=1"
                    )
        except Exception as e:
            log.debug(f"[META] Failed updating pattern outcome for {pos.signal_id}: {e}")
            
        # 🧠 Intelligence Experience Hook (Async labeling)
        import asyncio
        import config as _ai_cfg
        from intelligence.experience_buffer import experience_buffer

        # ROE fraction on initial margin (price_move × lev, cumulative after partials).
        # BUGFIX: was notional-only (no lev) → showed 0.7% instead of ~14% @20x.
        from utils.helpers import pnl_roe_fraction
        full_notional = pos.size_initial * pos.entry_price if pos.size_initial > 0 else 0.0
        lev = max(int(getattr(pos, "leverage", 1) or 1), 1)
        pnl_pct_final = pnl_roe_fraction(pos.pnl_realized, full_notional, lev)
        duration_sec = (pos.closed_at - pos.opened_at).total_seconds()

        loop = asyncio.get_event_loop()
        mfe_pct = max(0.0, pos.floating_pct(pos.trailing_high))
        loop.run_in_executor(
            None, experience_buffer.update_label, position_id, pnl_pct_final, duration_sec,
            reason, mfe_pct, ""
        )

        # Retrain hanya saat ENABLE_INTELLIGENCE=True DAN data cukup DAN sudah 24 jam
        if _ai_cfg.ENABLE_INTELLIGENCE:
            from intelligence.intelligence_model import intelligence_model
            from intelligence.experience_buffer import experience_buffer as _eb
            data = _eb.get_training_data(enriched_only=True)
            min_samples = max(
                getattr(_ai_cfg, 'INTELLIGENCE_RETRAIN_MIN_SAMPLES', 300),
                getattr(_ai_cfg, 'INTELLIGENCE_RETRAIN_MIN_ENRICHED_SAMPLES', 300),
            )
            if len(data) >= min_samples:
                asyncio.create_task(intelligence_model.retrain_async())
            else:
                log.debug(
                    f"[AI] Retrain skipped — {len(data)}/{min_samples} samples "
                    f"(ENABLE_INTELLIGENCE=True tapi data belum cukup)"
                )

        log_data = {
            "type":             "close",
            "pos_id":           position_id,
            "asset":            pos.asset,
            "side":             pos.side.value,
            "reason":           reason,
            "entry_price":      pos.entry_price,
            "exit_price":       fill_price,
            "size":             pos.size_initial,
            "notional":         pos.size_initial * pos.entry_price,
            "pnl":              total_pnl,
            # Cumulative ROE on full initial margin (fraction, e.g. 0.24 = +24%)
            "pnl_pct":          pnl_pct_final,
            "score":            pos.entry_score,
            "tp1_hit":          pos.tp1_hit,
            "tp2_hit":          pos.tp2_hit,
            "early_profit_lock": getattr(pos, "early_profit_lock", False),
            "max_floating_pct": max(0.0, pos.floating_pct(pos.trailing_high)),
            "meta_boost":       getattr(pos, "meta_score_delta", 0),
            "meta_pattern_key": getattr(pos, "meta_pattern_key", ""),
            "timestamp":        utcnow(),
        }
        self._trade_log.append(log_data)
        get_excel_logger().log_trade(self.chat_id, log_data)
        user_db.save_trade(self.chat_id, log_data)

        log.info(
            f" [PAPER] Closed {pos.asset} {pos.side.value.upper()} "
            f"@ {fill_price} | PnL: {format_usd(total_pnl)} ({reason})"
        )
        # Normalize reason for telegram send_position_event
        action_name = reason
        if reason in ("manual", "manual_close"):
            action_name = "manual"
        elif reason in ("close_all", "close_all_positions"):
            action_name = "close_all"
        price_move = pos.floating_pct(fill_price)
        # Last-slice notional (remaining size at close)
        rem_size = float(getattr(pos, "size_current", 0) or 0) or float(pos.size_initial or 0)
        rem_notional = rem_size * pos.entry_price if pos.entry_price else full_notional
        slice_roe = pnl_roe_fraction(floating_pnl, rem_notional, lev) if rem_notional else 0.0
        return {
            "action": action_name,
            "position_id": position_id,
            "asset": pos.asset,
            "side": pos.side.value,
            "pnl": total_pnl,
            "pnl_slice": floating_pnl,
            "pnl_total": total_pnl,
            "pnl_pct": pnl_pct_final,
            "pnl_pct_total": pnl_pct_final,
            "pnl_pct_slice": slice_roe,
            "price_move_pct": price_move,
            "exit_price": fill_price,
            "fully_closed": True,
            "reason": action_name,
            "message": f"Closed {pos.asset}: {format_usd(total_pnl)}",
        }
    async def close_all_positions(self, prices: Dict[str, float]) -> List[Dict]:
        """Close all open positions at current prices."""
        results = []
        for pos in self.open_positions:
            price = prices.get(pos.asset, pos.entry_price)
            res = await self.close_position(pos.position_id, price, reason="close_all")
            if res:
                results.append(res)
        return results

    async def _execute_partial_close(
        self,
        pos: Position,
        action: Dict,
        current_price: float,
    ) -> Dict:
        """Handle TP1, TP2, trailing, or SL partial/full close."""
        close_ratio = action["close_ratio"]
        close_size  = pos.size_current * close_ratio

        execution_price = current_price
        if action["action"] == "profit_lock_stop":
            # Paper prices arrive on a polling cadence. Once a protected stop is
            # crossed, model stop execution from the trigger plus closing spread;
            # using the later poll price falsely turns an armed lock into a loss.
            trigger_price = float(action.get("trigger_price", pos.stop_loss))
            execution_price = paper_profit_lock_fill(
                trigger_price,
                pos.side,
                self._simulate_fill,
            )

        price_move = pos.floating_pct(execution_price)
        notional_closed = close_size * pos.entry_price
        partial_pnl = price_move * notional_closed
        lev = max(int(getattr(pos, "leverage", 1) or 1), 1)
        from utils.helpers import pnl_roe_fraction
        # ROE on margin of *this slice* = price_move × leverage
        partial_roe = pnl_roe_fraction(partial_pnl, notional_closed, lev)
        
        # Release proportional margin back to balance
        margin_to_release = pos.margin_usd * close_ratio
        
        # Update executor state
        self._balance      += partial_pnl
        self._available    += partial_pnl + margin_to_release
        self._used_margin  -= margin_to_release
        
        # Update position state
        pos.pnl_realized   += partial_pnl
        pos.margin_usd     -= margin_to_release
        pos.size_current   -= close_size
        pos.pnl_unrealized = pos.unrealized_pnl(execution_price)

        fully_closed = False
        if action["action"] == "tp1":
            pos.tp1_hit = True
            pos.trailing_active = True
            pos.trailing_high = current_price
            pos.stop_loss = pos.entry_price * 1.0005 if pos.side == Side.LONG else pos.entry_price * 0.9995
            log.info(
                f" [PAPER] TP1 hit on {pos.asset} - SL→BE | "
                f"slice={partial_pnl:+.4f} cum={pos.pnl_realized:+.4f} "
                f"move={price_move*100:.2f}% slice_ROE={partial_roe*100:.2f}% @{lev}x"
            )

        elif action["action"] == "tp2":
            pos.tp2_hit = True
            log.info(
                f" [PAPER] TP2 hit on {pos.asset} | "
                f"slice={partial_pnl:+.4f} cum={pos.pnl_realized:+.4f} "
                f"move={price_move*100:.2f}% slice_ROE={partial_roe*100:.2f}% @{lev}x"
            )

        elif action["action"] in ("trailing_stop", "stop_loss", "profit_lock_stop", "time_exit"):
            # Full close: slice PnL already booked above. Mark closed without
            # re-adding PnL in close_position (would double-count).
            fully_closed = True
            if pos.status == PositionStatus.OPEN:
                pos.status = PositionStatus.CLOSED
                pos.closed_at = utcnow()
                pos.size_current = 0.0
                pos.pnl_unrealized = 0.0
                from core.db import user_db
                user_db.remove_paper_position(pos.position_id)
                user_db.save_paper_state(self.chat_id, self._balance, self._balance)
                self.risk.record_pnl(pos.pnl_realized, self._balance)

                # Trade log + meta with *cumulative* totals
                full_notional = pos.size_initial * pos.entry_price if pos.size_initial > 0 else 0.0
                total_roe = pnl_roe_fraction(pos.pnl_realized, full_notional, lev)
                log_data = {
                    "type":             "close",
                    "pos_id":           pos.position_id,
                    "asset":            pos.asset,
                    "side":             pos.side.value,
                    "reason":           action["action"],
                    "entry_price":      pos.entry_price,
                    "exit_price":       execution_price,
                    "trigger_price":    action.get("trigger_price"),
                    "size":             pos.size_initial,
                    "notional":         full_notional,
                    "pnl":              pos.pnl_realized,   # cumulative TP1+TP2+final
                    "pnl_pct":          total_roe,          # ROE on full initial margin
                    "score":            pos.entry_score,
                    "tp1_hit":          pos.tp1_hit,
                    "tp2_hit":          pos.tp2_hit,
                    "early_profit_lock": getattr(pos, "early_profit_lock", False),
                    "max_floating_pct": max(0.0, pos.floating_pct(pos.trailing_high)),
                    "meta_boost":       getattr(pos, "meta_score_delta", 0),
                    "meta_pattern_key": getattr(pos, "meta_pattern_key", ""),
                    "timestamp":        utcnow(),
                }
                self._trade_log.append(log_data)
                get_excel_logger().log_trade(self.chat_id, log_data)
                user_db.save_trade(self.chat_id, log_data)

                # Meta pattern + AI label on full close only
                try:
                    if pos.signal_id:
                        sig = user_db.get_signal_by_id(pos.signal_id)
                        if sig and getattr(sig, "meta_pattern_key", None):
                            user_db.update_meta_pattern_outcome(sig.meta_pattern_key, pos.pnl_realized)
                except Exception as e:
                    log.debug(f"[META] full-close update failed: {e}")
                try:
                    import asyncio
                    import config as _ai_cfg
                    from intelligence.experience_buffer import experience_buffer
                    duration_sec = (pos.closed_at - pos.opened_at).total_seconds()
                    loop = asyncio.get_event_loop()
                    loop.run_in_executor(
                        None,
                        experience_buffer.update_label,
                        pos.position_id,
                        total_roe,
                        duration_sec,
                        action["action"],
                        max(0.0, pos.floating_pct(pos.trailing_high)),
                        action.get("time_exit_trigger", ""),
                    )
                    if _ai_cfg.ENABLE_INTELLIGENCE:
                        from intelligence.intelligence_model import intelligence_model
                        from intelligence.experience_buffer import experience_buffer as _eb
                        data = _eb.get_training_data(enriched_only=True)
                        min_samples = max(
                            getattr(_ai_cfg, "INTELLIGENCE_RETRAIN_MIN_SAMPLES", 300),
                            getattr(_ai_cfg, "INTELLIGENCE_RETRAIN_MIN_ENRICHED_SAMPLES", 300),
                        )
                        if len(data) >= min_samples:
                            asyncio.create_task(intelligence_model.retrain_async())
                except Exception as e:
                    log.debug(f"[AI] full-close label failed: {e}")

                log.info(
                    f" [PAPER] FULL CLOSE {pos.asset} {pos.side.value.upper()} "
                    f"@ {execution_price} | TOTAL PnL: {format_usd(pos.pnl_realized)} "
                    f"ROE={total_roe*100:.2f}% @{lev}x ({action['action']})"
                )

        # Update trailing high
        if pos.trailing_active and pos.status == PositionStatus.OPEN:
            if pos.side == Side.LONG:
                pos.trailing_high = max(pos.trailing_high, current_price)
            else:
                pos.trailing_high = min(pos.trailing_high, current_price)

        # Persist AFTER all state updates so DB always has the correct tp1_hit, stop_loss, trailing_high
        from core.db import user_db
        if pos.status == PositionStatus.OPEN:
            user_db.save_paper_position(self.chat_id, pos)
            user_db.save_paper_state(self.chat_id, self._balance, self._balance + pos.pnl_unrealized)

        full_notional = pos.size_initial * pos.entry_price if pos.size_initial > 0 else 0.0
        total_roe = pnl_roe_fraction(pos.pnl_realized, full_notional, lev)
        return {
            **action,
            "pnl": partial_pnl,                 # this slice only (status msgs)
            "pnl_slice": partial_pnl,
            "pnl_total": pos.pnl_realized,      # cumulative (TP1+TP2+final)
            "pnl_pct": total_roe if fully_closed else partial_roe,
            "pnl_pct_slice": partial_roe,
            "pnl_pct_total": total_roe,
            "price_move_pct": price_move,
            "notional_closed": notional_closed,
            "close_ratio": close_ratio,
            "fully_closed": fully_closed,
            "exit_price": execution_price,
            "trigger_price": action.get("trigger_price"),
            "position_id": pos.position_id,
        }

    # ──────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────

    def _simulate_fill(self, price: float, side: Side) -> float:
        """Add realistic spread to simulate market fill."""
        spread = price * 0.0003   # 0.03% spread (typical for BTC on HL)
        noise  = random.uniform(-0.0001, 0.0001) * price
        if side == Side.LONG:
            return round(price + spread + noise, 8)   # buy at ask
        else:
            return round(price - spread + noise, 8)   # sell at bid

    def _calculate_liquidation_price(self, entry: float, side: Side, leverage: int) -> float:
        """
        Isolated Margin Liquidation Formula (Approximate for Hyperliquid).
        Entry * (1 - 1/Lev + MMR) for Long
        Entry * (1 + 1/Lev - MMR) for Short
        Assuming MMR = 0.5% (0.005)
        """
        mmr = 0.005  # 0.5% standard maintenance margin
        if side == Side.LONG:
            return entry * (1 - (1 / leverage) + mmr)
        else:
            return entry * (1 + (1 / leverage) - mmr)
