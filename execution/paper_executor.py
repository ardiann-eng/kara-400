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

log = logging.getLogger("kara.paper_exec")

# Simulated starting balance for paper mode
PAPER_INITIAL_BALANCE = 1000.0


class PaperExecutor:
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
            liquidation_price=liq_price,
            signal_id=signal.signal_id,
            trade_mode=getattr(signal, 'trade_mode', 'standard'),
            is_paper=True,
            entry_score=signal.score,
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
        fr = float(getattr(getattr(signal, 'raw_data', None), 'funding_rate', 0.0))  # Best effort if raw_data is available
        vol = 0.0  # Ideally passed from kwargs, but 0.0 acts as baseline gracefully
        
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, 
                             experience_buffer.record_entry,
                             self.chat_id, pos.position_id, signal.asset, signal.side.value,
                             signal.score, getattr(signal, 'meta_score_delta', 0),
                             bd, fr, vol, 0.0, getattr(signal, 'expected_edge', 0.0)
                            )
                            
        return pos

    # ──────────────────────────────────────────
    # UPDATE POSITIONS (call every price tick)
    # ──────────────────────────────────────────

    async def update_positions(
        self, prices: Dict[str, float]
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
            action = self.risk.check_tp_trail(pos, current)
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
        except Exception as e:
            log.debug(f"[META] Failed updating pattern outcome for {pos.signal_id}: {e}")
            
        # 🧠 Intelligence Experience Hook (Async labeling)
        import asyncio
        import config as _ai_cfg
        from intelligence.experience_buffer import experience_buffer

        pnl_pct_final = pos.floating_pct(fill_price)
        duration_sec = (pos.closed_at - pos.opened_at).total_seconds()

        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, experience_buffer.update_label, position_id, pnl_pct_final, duration_sec)

        # Retrain hanya saat ENABLE_INTELLIGENCE=True DAN data cukup DAN sudah 24 jam
        if _ai_cfg.ENABLE_INTELLIGENCE:
            from intelligence.intelligence_model import intelligence_model
            from intelligence.experience_buffer import experience_buffer as _eb
            data = _eb.get_training_data()
            min_samples = getattr(_ai_cfg, 'INTELLIGENCE_RETRAIN_MIN_SAMPLES', 500)
            if len(data) >= min_samples:
                asyncio.create_task(intelligence_model.retrain_async())
            else:
                log.debug(
                    f"[AI] Retrain skipped — {len(data)}/{min_samples} samples "
                    f"(ENABLE_INTELLIGENCE=True tapi data belum cukup)"
                )

        log_data = {
            "type":      "close",
            "pos_id":    position_id,
            "asset":     pos.asset,
            "side":      pos.side.value,
            "reason":    reason,
            "entry_price": pos.entry_price,
            "exit_price": fill_price,
            "size":      pos.size_initial,
            "notional":  pos.size_initial * pos.entry_price,
            "pnl":       total_pnl,
            "pnl_pct":   pos.floating_pct(fill_price),
            "score":     pos.entry_score,
            "timestamp": utcnow(),
        }
        self._trade_log.append(log_data)
        get_excel_logger().log_trade(self.chat_id, log_data)
        user_db.save_trade(self.chat_id, log_data)

        log.info(
            f" [PAPER] Closed {pos.asset} {pos.side.value.upper()} "
            f"@ {fill_price} | PnL: {format_usd(total_pnl)} ({reason})"
        )
        return {
            "position_id": position_id,
            "pnl": total_pnl,
            "reason": reason,
            "message": f"{'' if total_pnl > 0 else ''} Closed {pos.asset}: {format_usd(total_pnl)}"
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
        
        partial_pnl = (
            pos.floating_pct(current_price) * 
            close_size * pos.entry_price
        )
        
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
        pos.pnl_unrealized = pos.unrealized_pnl(current_price)

        if action["action"] == "tp1":
            pos.tp1_hit = True
            pos.trailing_active = True
            pos.trailing_high = current_price
            pos.stop_loss = pos.entry_price * 1.0005 if pos.side == Side.LONG else pos.entry_price * 0.9995
            log.info(f" [PAPER] TP1 hit on {pos.asset} - SL moved to breakeven")

        elif action["action"] == "tp2":
            pos.tp2_hit = True
            log.info(f" [PAPER] TP2 hit on {pos.asset}")

        elif action["action"] in ("trailing_stop", "stop_loss", "time_exit"):
            # Full close — guard: only if still OPEN
            if pos.status == PositionStatus.OPEN:
                await self.close_position(
                    pos.position_id, current_price, action["action"]
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

        return {**action, "pnl": partial_pnl, "position_id": pos.position_id}

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
