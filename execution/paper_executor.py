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

    [AUDIT FIX 2026-05-13] Realistic slippage:
    - Uses live orderbook from WS cache to calculate actual spread + market impact
    - Walks the book based on order notional to compute volume-weighted fill price
    - Fallback to tiered spread model when orderbook unavailable

    [AUDIT FIX 2026-05-13] Exchange-aware leverage:
    - Caps leverage at Hyperliquid's per-asset maxLeverage from exchange metadata
    - Prevents paper mode from using leverage that would be rejected on mainnet
    """

    def __init__(self, risk_manager: RiskManager, initial_balance: float = 1000.0, chat_id: str = "system", market_cache=None):
        self.risk  = risk_manager
        self.mode  = BotMode.PAPER
        self.chat_id = chat_id
        self._market_cache = market_cache  # WS MarketDataCache for orderbook slippage

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
            # Compounding fix: peak_balance pakai equity tersimpan (bukan hardcode balance)
            # equity sudah = balance + unrealized saat disimpan, jadi ini nilai tertinggi yang valid
            self._peak_balance = max(state.get("equity", state["balance"]), state["balance"])
            log.debug(f" [PAPER] Restored balance from DB: {format_usd(self._balance)}")
        
        positions = user_db.load_paper_positions(chat_id)
        for pos in positions:
            self._positions[pos.position_id] = pos
            # Update used margin
            self._used_margin += pos.margin_usd
            self._available   -= pos.margin_usd
            log.debug(f" [PAPER] Restored position: {pos.asset} {pos.side.value}")

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
        Simulates realistic fill using live orderbook slippage.
        """
        account = await self.get_account_state()

        # Risk check
        approved, reason = self.risk.pre_trade_check(
            signal, account, self.open_positions
        )
        if not approved:
            log.warning(f" Trade blocked: {reason}")
            return None

        # ── [AUDIT FIX] Exchange-aware leverage cap ──────────────────────
        # Cap leverage at Hyperliquid's actual maxLeverage for this asset.
        # Prevents paper mode from using leverage that would be rejected live.
        exchange_max_lev = await self._get_exchange_max_leverage(signal.asset)
        if signal.suggested_leverage > exchange_max_lev:
            log.info(
                f"[PAPER-LEV] {signal.asset}: leverage {signal.suggested_leverage}x "
                f"> exchange max {exchange_max_lev}x → capped"
            )
            signal.suggested_leverage = exchange_max_lev

        # Calculate size & leverage
        size_usd, contracts, actual_lev = self.risk.calculate_position_size(
            signal, self._balance
        )

        # ── [AUDIT FIX] Double-check actual_lev against exchange max ─────
        actual_lev = min(actual_lev, exchange_max_lev)
        
        # Isolated margin = notional / leverage
        margin = (contracts * signal.entry_price) / actual_lev

        # ── [AUDIT FIX] Realistic orderbook-based fill simulation ────────
        notional_usd = contracts * signal.entry_price
        fill_price = self._simulate_fill(
            signal.entry_price, signal.side,
            asset=signal.asset, notional_usd=notional_usd
        )

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
            tp3=getattr(signal, 'tp3', 0.0),
            trailing_active=False,
            trailing_high=fill_price,
            trailing_stop_price=0.0,
            entry_atr=getattr(signal, 'entry_atr', 0.0),
            liquidation_price=liq_price,
            signal_id=signal.signal_id,
            trade_mode=getattr(signal, 'trade_mode', 'scalper'),
            is_paper=True,
            entry_score=signal.score,
            realized_vol=getattr(signal, 'realized_vol', 0.02),
            original_entry_price=signal.entry_price,  # [QUANT AGGRESSION] breakeven reference
            # [POST-MORTEM] Entry context for autopsy
            entry_funding_rate=getattr(signal, 'funding_rate', 0.0) or 0.0,
            atr_pct=getattr(signal, 'entry_atr', 0.0),
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

        # Log entry with slippage info
        slippage_bps = abs(fill_price - signal.entry_price) / signal.entry_price * 10000
        log_data = {
            "type":     "open",
            "pos_id":   pos.position_id,
            "asset":    signal.asset,
            "side":     signal.side.value,
            "entry_price": fill_price,
            "mark_price": signal.entry_price,
            "slippage_bps": round(slippage_bps, 2),
            "size":     contracts,
            "notional": contracts * fill_price,
            "margin":   margin,
            "leverage":  actual_lev,
            "exchange_max_lev": exchange_max_lev,
            "score":    signal.score,
            "timestamp":utcnow(),
        }
        self._trade_log.append(log_data)
        get_excel_logger().log_trade(self.chat_id, log_data)

        # Record per-asset trade for repeat guard
        self.risk.record_asset_trade(signal.asset)

        log.info(
            f" [PAPER] Opened {signal.asset} {signal.side.value.upper()} "
            f"@ {fill_price} (mark={signal.entry_price:.6f}, slip={slippage_bps:.1f}bps) "
            f"| {contracts:.4f} contracts "
            f"| margin: {format_usd(margin)} | lev: {actual_lev}x/{exchange_max_lev}x(max)"
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

            # [POST-MORTEM] Track deepest unrealized loss for autopsy
            if pos.pnl_unrealized < pos.max_unrealized_loss:
                pos.max_unrealized_loss = pos.pnl_unrealized

            # Refresh OHLCV history so check_tp_trail has data for momentum/HTF/emergency exits.
            # Without this, those exit layers silently no-op (Position OHLCV fields stay empty).
            try:
                from data.hyperliquid_client import get_client as _get_hl_client
                _hl_client = _get_hl_client()
                if _hl_client is not None:
                    await self.risk.refresh_position_candles(pos, _hl_client)
            except Exception as _refresh_err:
                log.debug(f"[PAPER] Candle refresh skipped for {pos.asset}: {_refresh_err}")

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

        # [AUDIT FIX] Use orderbook-based slippage for exit too
        exit_side = Side.SHORT if pos.side == Side.LONG else Side.LONG
        exit_notional = pos.size_current * current_price
        fill_price = self._simulate_fill(
            current_price, exit_side,
            asset=pos.asset, notional_usd=exit_notional
        )
        
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
        
        # Remove from in-memory dict and DB
        self._positions.pop(position_id, None)
        from core.db import user_db
        user_db.remove_paper_position(position_id)
        # Compounding fix: equity = realized balance + unrealized dari posisi lain yang masih buka
        _unrealized_others = sum(
            p.pnl_unrealized for p in self._positions.values()
            if p.status == PositionStatus.OPEN
        )
        user_db.save_paper_state(self.chat_id, self._balance, self._balance + _unrealized_others)

        # Total PnL for the record (cumulative)
        total_pnl = pos.pnl_realized
        self.risk.record_pnl(total_pnl, self._balance)

        # [POST-MORTEM] Generate rule-based autopsy before position is finalized
        try:
            from memory.autopsy_engine import autopsy_engine
            time_held = (pos.closed_at - pos.opened_at).total_seconds() / 60 if pos.closed_at and pos.opened_at else 0
            autopsy_data = {
                "trade_id": position_id,
                "asset": pos.asset,
                "side": pos.side.value,
                "score": pos.entry_score,
                "entry_price": pos.entry_price,
                "exit_price": fill_price,
                "pnl": total_pnl,
                "pnl_pct": pos.roe_pct(fill_price),
                "exit_reason": reason,
                "max_drawdown": pos.max_unrealized_loss,
                "time_held_min": time_held,
                "regime": pos.trade_mode,
                "trend_pct": pos.trend_pct,
                "funding_rate": pos.entry_funding_rate,
                "realized_vol": pos.realized_vol,
                "sl_distance_pct": abs(pos.stop_loss - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0,
                "tp2_distance_pct": abs(pos.tp2 - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0,
                "atr_pct": pos.atr_pct,
                "candle_count": len(pos.candle_closes),
                "notional": pos.size_initial * pos.entry_price,
            }
            pos.autopsy = autopsy_engine.generate(autopsy_data)
            log.info(f"[AUTOPSY] {position_id} | {pos.autopsy}")
        except Exception as _ae:
            log.debug(f"[AUTOPSY] Failed for {position_id}: {_ae}")

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
            "pnl_pct":          pos.roe_pct(fill_price),
            "score":            pos.entry_score,
            "timestamp":        utcnow(),
            "autopsy":          pos.autopsy,
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
        # SL must fill at stop_loss price, not current_price.
        fill_price = pos.stop_loss if action["action"] == "stop_loss" else current_price

        # Full-close actions (SL, trailing, time, momentum): delegate entirely to
        # close_position() which owns the balance update + meta + ML labeling.
        # Do NOT touch balance/pnl here — close_position handles it all.
        if action["action"] in ("trailing_stop", "stop_loss", "time_exit", "momentum_exit", "early_trail"):
            if pos.status == PositionStatus.OPEN:
                result = await self.close_position(pos.position_id, fill_price, action["action"])
                return {**action, "pnl": (result or {}).get("pnl", 0), "position_id": pos.position_id}
            return {**action, "pnl": 0, "position_id": pos.position_id}

        # Partial close (TP1 / TP2): calculate and apply partial PnL only
        close_ratio = action["close_ratio"]
        close_size  = pos.size_current * close_ratio

        partial_pnl = (
            pos.floating_pct(fill_price) *
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
        pos.pnl_unrealized = pos.unrealized_pnl(fill_price)

        if action["action"] == "tp1":
            pos.tp1_hit = True
            pos.trailing_active = True
            pos.trailing_high = fill_price
            # [QUANT AGGRESSION] SL → breakeven+0.1% (set by risk_manager breakeven trigger,
            # but also enforce here as safety net)
            be_ref = getattr(pos, 'original_entry_price', pos.entry_price) or pos.entry_price
            pos.stop_loss = be_ref * 1.001 if pos.side == Side.LONG else be_ref * 0.999
            if not hasattr(pos, 'partial_exits_done') or pos.partial_exits_done is None:
                pos.partial_exits_done = []
            if 'tp1' not in pos.partial_exits_done:
                pos.partial_exits_done.append('tp1')
            log.info(f" [PAPER] TP1 hit on {pos.asset} - SL moved to breakeven {pos.stop_loss:.4f}")

        elif action["action"] == "tp2":
            pos.tp2_hit = True
            if not hasattr(pos, 'partial_exits_done') or pos.partial_exits_done is None:
                pos.partial_exits_done = []
            if 'tp2' not in pos.partial_exits_done:
                pos.partial_exits_done.append('tp2')
            log.info(f" [PAPER] TP2 hit on {pos.asset}")

        elif action["action"] == "tp3":
            pos.tp3_hit = True
            if not hasattr(pos, 'partial_exits_done') or pos.partial_exits_done is None:
                pos.partial_exits_done = []
            if 'tp3' not in pos.partial_exits_done:
                pos.partial_exits_done.append('tp3')
            log.info(f" [PAPER] TP3 hit on {pos.asset} - ATR trail on last piece")

        # Update trailing high (always, even pre-TP1, so ATR trail has correct peak)
        if pos.status == PositionStatus.OPEN:
            if pos.side == Side.LONG:
                pos.trailing_high = max(pos.trailing_high, fill_price)
            else:
                pos.trailing_high = min(pos.trailing_high, fill_price)

        # Persist partial state to DB
        from core.db import user_db
        if pos.status == PositionStatus.OPEN:
            user_db.save_paper_position(self.chat_id, pos)
        # Compounding fix: equity = realized balance + unrealized SEMUA posisi terbuka
        _total_unrealized = sum(
            p.pnl_unrealized for p in self._positions.values()
            if p.status == PositionStatus.OPEN
        )
        user_db.save_paper_state(self.chat_id, self._balance, self._balance + _total_unrealized)

        return {**action, "pnl": partial_pnl, "position_id": pos.position_id}

    # ──────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────

    def _simulate_fill(
        self, price: float, side: Side,
        asset: str = "", notional_usd: float = 0.0
    ) -> float:
        """
        [AUDIT FIX 2026-05-13] Realistic fill simulation using live orderbook.

        Three-layer slippage model:
          1. Actual spread: from WS orderbook bid/ask (not hardcoded)
          2. Market impact: walk the book based on order notional size
          3. Execution noise: small random jitter (±0.5 bps) for realism

        Fallback: tiered spread by asset class when orderbook unavailable.
        This ensures paper trading results are CLOSER to live execution.
        """
        # ── Layer 1+2: Orderbook-based slippage ──────────────────────────
        ob_slippage_pct = self._get_orderbook_slippage(asset, side, notional_usd)

        # ── Layer 3: Execution noise (±0.5 bps random jitter) ────────────
        noise_pct = random.uniform(-0.00005, 0.00005)  # ±0.5 bps

        total_slippage_pct = ob_slippage_pct + noise_pct

        if side == Side.LONG:
            fill = price * (1 + total_slippage_pct)   # buy at ask + impact
        else:
            fill = price * (1 - total_slippage_pct)   # sell at bid - impact

        return round(fill, 8)

    def _get_orderbook_slippage(
        self, asset: str, side: Side, notional_usd: float
    ) -> float:
        """
        [AUDIT FIX 2026-05-13] Calculate realistic slippage from live orderbook.

        Uses the WS MarketDataCache to:
          1. Get the actual bid-ask spread (Layer 1)
          2. Walk the book to calculate market impact for the given notional (Layer 2)

        Returns: slippage as a fraction of mid price (e.g. 0.0012 = 0.12%)
        """
        # Try to get live orderbook from WS cache
        ob_data = None
        if self._market_cache and asset:
            ob_data = getattr(self._market_cache, 'orderbook', {}).get(asset)

        if ob_data and isinstance(ob_data, dict):
            try:
                levels = ob_data.get("levels", [[], []])
                bids_raw = levels[0] if len(levels) > 0 else []
                asks_raw = levels[1] if len(levels) > 1 else []

                # Parse orderbook levels
                def _parse_level(x):
                    if isinstance(x, dict):
                        return float(x.get("px", 0)), float(x.get("sz", 0))
                    try:
                        return float(x[0]), float(x[1])
                    except (IndexError, TypeError, ValueError):
                        return 0.0, 0.0

                bids = [(px, sz) for px, sz in (_parse_level(b) for b in bids_raw) if px > 0 and sz > 0]
                asks = [(px, sz) for px, sz in (_parse_level(a) for a in asks_raw) if px > 0 and sz > 0]

                if bids and asks:
                    best_bid = bids[0][0]
                    best_ask = asks[0][0]
                    mid = (best_bid + best_ask) / 2.0

                    if mid <= 0:
                        return self._fallback_slippage(asset)

                    # ── Layer 1: Half-spread (always paid) ───────────────
                    half_spread_pct = (best_ask - best_bid) / (2.0 * mid)

                    # ── Layer 2: Market impact (walk the book) ───────────
                    # LONG: walk ASK side (sorted ascending — best ask first)
                    # SHORT: walk BID side (sorted descending — best bid first, already correct)
                    book_side = asks if side == Side.LONG else bids
                    impact_pct = self._walk_book(book_side, mid, notional_usd)

                    total = half_spread_pct + impact_pct

                    log.debug(
                        f"[SLIP-OB] {asset} {side.value.upper()} | "
                        f"spread={half_spread_pct*10000:.1f}bps | "
                        f"impact={impact_pct*10000:.1f}bps | "
                        f"total={total*10000:.1f}bps | "
                        f"notional=${notional_usd:.0f} | "
                        f"bid={best_bid:.6f} ask={best_ask:.6f}"
                    )
                    return total

            except Exception as e:
                log.debug(f"[SLIP-OB] {asset}: orderbook parse failed ({e}), using fallback")

        # ── Fallback: tiered spread model ────────────────────────────────
        return self._fallback_slippage(asset)

    @staticmethod
    def _walk_book(
        levels: list, mid_price: float, notional_usd: float
    ) -> float:
        """
        Walk the orderbook to calculate VWAP impact for a given notional size.
        Returns impact as fraction of mid price (e.g. 0.0012 = 12 bps).

        levels: [(price, size), ...] already sorted correctly by caller:
          - LONG  → asks sorted ascending  (best ask first)
          - SHORT → bids sorted descending (best bid first)
        """
        if notional_usd <= 0 or not levels or mid_price <= 0:
            return 0.0

        remaining_usd = notional_usd
        filled_usd = 0.0
        vwap_num = 0.0  # sum of (price × fill_usd)

        for px, sz in levels:
            if remaining_usd <= 0:
                break
            if px <= 0:
                continue
            level_usd = px * sz
            fill_usd = min(remaining_usd, level_usd)
            vwap_num += px * fill_usd
            filled_usd += fill_usd
            remaining_usd -= fill_usd

        if filled_usd <= 0:
            return 0.0

        # If order larger than visible book, extrapolate from last level + 0.05% penalty
        if remaining_usd > 0 and levels:
            last_px = levels[-1][0]
            penalty_px = last_px * 1.0005
            vwap_num += penalty_px * remaining_usd
            filled_usd += remaining_usd

        vwap = vwap_num / filled_usd
        return abs(vwap - mid_price) / mid_price

    @staticmethod
    def _fallback_slippage(asset: str) -> float:
        """
        [AUDIT FIX 2026-05-13] Tiered fallback slippage when orderbook unavailable.

        Based on Hyperliquid empirical data:
          - BTC/ETH (mega-cap): 0.02-0.05% spread
          - Top altcoins (SOL, DOGE, etc): 0.05-0.15% spread
          - Mid/small altcoins: 0.10-0.30% spread
          - Micro-cap/meme coins: 0.15-0.50% spread

        These values are CONSERVATIVE — actual slippage depends on order size.
        """
        # Tier 1: mega-cap with deep liquidity
        tier1 = {"BTC", "ETH"}
        # Tier 2: large-cap alts
        tier2 = {"SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "MATIC", "DOT", "NEAR"}
        # Tier 3: mid-cap (everything else known)
        # Tier 4: small/meme — default

        asset_upper = asset.upper() if asset else ""
        if asset_upper in tier1:
            # BTC/ETH: ~2-5 bps half-spread
            return random.uniform(0.0002, 0.0005)
        elif asset_upper in tier2:
            # Large-cap alts: ~5-12 bps
            return random.uniform(0.0005, 0.0012)
        elif asset_upper:
            # Mid/small-cap: ~8-20 bps
            return random.uniform(0.0008, 0.0020)
        else:
            # Unknown: conservative 15 bps
            return 0.0015

    async def _get_exchange_max_leverage(self, asset: str) -> int:
        """
        [AUDIT FIX 2026-05-13] Fetch the actual max leverage allowed by Hyperliquid
        for a specific asset from exchange metadata.

        This ensures paper mode never simulates leverage higher than what
        Hyperliquid actually allows — preventing unrealistic ROE projections.
        """
        try:
            from data.hyperliquid_client import get_client
            client = get_client()
            if client and client._market_cache:
                universe, _ = client._market_cache
                for u in universe:
                    if isinstance(u, dict) and u.get("name") == asset:
                        max_lev = int(u.get("maxLeverage", 50))
                        log.debug(f"[PAPER-LEV] {asset}: exchange maxLeverage={max_lev}x")
                        return max_lev
        except Exception as e:
            log.debug(f"[PAPER-LEV] {asset}: could not fetch exchange leverage ({e})")

        # Conservative fallback if metadata unavailable
        return 50

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
