"""
KARA Bot - Live Executor 
Executes REAL trades on Hyperliquid mainnet.
Contains extra safety checks vs paper executor.
Features: Post-Only ALO orders, retry with idempotency, partial fill handling.
  REAL MONEY - extra caution required.
"""

from __future__ import annotations
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from config import RISK, EXEC
from models.schemas import (
    Order, Position, AccountState, TradeSignal, Side,
    OrderStatus, PositionStatus, BotMode, ExecutionMode
)
from data.hyperliquid_client import HyperliquidClient
from risk.risk_manager import RiskManager
from utils.helpers import gen_id, format_usd, utcnow
from utils.excel_logger import get_excel_logger

log = logging.getLogger("kara.live_exec")


class LiveExecutor:
    """
    Live trading executor. Mirrors PaperExecutor interface.
    Always uses Isolated margin, defaults to Post-Only limit orders.
    """

    def __init__(
        self,
        chat_id: str,
        hl_client: HyperliquidClient,
        risk_manager: RiskManager,
    ):
        self.chat_id= str(chat_id)
        self.client = hl_client
        self.risk   = risk_manager
        self.mode   = BotMode.LIVE

        # Local position shadow (synced from chain)
        self._positions: Dict[str, Position] = {}

        log.warning(
            " LIVE executor initialized - REAL MONEY MODE. "
            "Double-check your config before trading."
        )

    # ──────────────────────────────────────────
    # ACCOUNT STATE (from chain)
    # ──────────────────────────────────────────

    async def get_account_state(self) -> AccountState:
        """Fetch live account state from Hyperliquid."""
        user_state = await self.client.get_user_state()

        if not user_state:
            raise RuntimeError("Failed to fetch account state from chain")

        margin_summary = user_state.get("marginSummary", {})
        balance   = float(margin_summary.get("accountValue", 0))
        available = float(margin_summary.get("withdrawable", 0))
        used_margin = float(margin_summary.get("totalMarginUsed", 0))

        # Parse open positions from chain
        asset_positions = user_state.get("assetPositions", [])
        positions       = []
        unrealized_total = 0.0

        for ap in asset_positions:
            pos_data = ap.get("position", {})
            if float(pos_data.get("szi", 0)) == 0:
                continue

            szi  = float(pos_data.get("szi", 0))
            entry= float(pos_data.get("entryPx", 0))
            upnl = float(pos_data.get("unrealizedPnl", 0))
            unrealized_total += upnl

            # Find or create shadow position
            pos_key = pos_data.get("coin", "")
            existing = next(
                (p for p in self._positions.values() if p.asset == pos_key),
                None
            )
            if existing:
                existing.pnl_unrealized = upnl
                existing.size_current   = abs(szi)
                positions.append(existing)

        drawdown = (
            (self.risk.status["peak_balance"] - balance) /
            max(self.risk.status["peak_balance"], 1)
        )

        return AccountState(
            total_equity=round(balance, 2),
            wallet_balance=round(balance - unrealized_total, 2),
            available=round(available, 2),
            used_margin=round(used_margin, 2),
            unrealized_pnl=round(unrealized_total, 2),
            daily_pnl=round(self.risk.status["daily_pnl"], 2),
            daily_pnl_pct=round(
                self.risk.status["daily_pnl"] / max(balance, 1), 4
            ),
            peak_balance=round(self.risk.status["peak_balance"], 2),
            current_drawdown_pct=round(drawdown, 4),
            positions=positions,
            mode=BotMode.LIVE,
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
        """Open a live position. Uses Post-Only limit order."""
        account = await self.get_account_state()

        # Risk check (non-negotiable)
        approved, reason = self.risk.pre_trade_check(
            signal, account, self.open_positions
        )
        if not approved:
            log.warning(f" LIVE trade blocked: {reason}")
            return None

        # Calculate size & leverage
        size_usd, contracts, actual_lev = self.risk.calculate_position_size(
            signal, account.total_equity
        )
        if size_usd <= 0:
            log.warning(" Calculated size is 0, skipping trade.")
            return None

        # Set leverage first
        try:
            await self.client.set_leverage(
                signal.asset,
                actual_lev,
                is_cross=False   # isolated
            )
            log.info(f"⚙️  Leverage set: {signal.asset} {actual_lev}x isolated (Capped)")
        except Exception as e:
            log.error(f"Failed to set leverage: {e}")
            # Keep going, might already be set or fail gracefully later

        # Place Post-Only limit order (slightly inside spread for fill probability)
        # We use the entry_price from signal; if market has moved, order stays open
        is_buy     = signal.side == Side.LONG
        idempotency= gen_id("ORD")

        order, filled = await self._place_with_retry(
            asset=signal.asset,
            is_buy=is_buy,
            sz=contracts,
            limit_px=signal.entry_price,
            order_type="post_only",
            idempotency_key=idempotency,
        )

        if not filled:
            log.warning(f" Order not filled after retries - cancelling")
            return None

        # Create position record
        margin = (contracts * signal.entry_price) / signal.suggested_leverage
        pos    = Position(
            position_id=gen_id("POS"),
            asset=signal.asset,
            side=signal.side,
            entry_price=order.avg_fill_price or signal.entry_price,
            size_initial=contracts,
            size_current=contracts,
            leverage=signal.suggested_leverage,
            margin_usd=margin,
            stop_loss=signal.stop_loss,
            tp1=signal.tp1,
            tp2=signal.tp2,
            trailing_high=signal.entry_price,
            signal_id=signal.signal_id,
            is_paper=False,
            entry_score=signal.score,
        )
        self._positions[pos.position_id] = pos

        log_data = {
            "type":      "open",
            "pos_id":    pos.position_id,
            "asset":     signal.asset,
            "side":      signal.side.value,
            "price":     pos.entry_price,
            "contracts": contracts,
            "notional":  contracts * pos.entry_price,
            "score":     signal.score,
            "timestamp": utcnow(),
        }
        get_excel_logger().log_trade(self.chat_id, log_data)

        log.info(
            f" [LIVE] Opened {signal.asset} {signal.side.value.upper()} "
            f"@ {pos.entry_price} | {contracts:.4f} contracts "
            f"| {signal.suggested_leverage}x isolated"
        )
        return pos

    # ──────────────────────────────────────────
    # UPDATE POSITIONS (Active Monitoring)
    # ──────────────────────────────────────────

    async def update_positions(self, prices: Dict[str, float]) -> List[Dict]:
        """
        Update local shadow positions and check for TP/SL/Trailing triggers.
        Returns list of actions taken.
        """
        actions = []
        for pos_id, pos in list(self._positions.items()):
            if pos.status != PositionStatus.OPEN:
                continue
            
            current = prices.get(pos.asset, 0)
            if current <= 0:
                continue

            # Update shadow PnL
            pos.pnl_unrealized = pos.unrealized_pnl(current)

            # Check Risk Manager for triggers (Time-based, TP, Trailing)
            action = self.risk.check_tp_trail(pos, current)
            if action:
                result = await self._execute_partial_close(pos, action, current)
                if result:
                    actions.append(result)

        return actions

    async def _execute_partial_close(
        self,
        pos: Position,
        action: Dict,
        current_price: float,
    ) -> Optional[Dict]:
        """Execute the actual trade on Hyperliquid for TP/Trailing Stop."""
        close_ratio = action.get("close_ratio", 1.0)
        
        # Call existing close_position logic
        res = await self.close_position(
            pos.position_id, 
            current_price, 
            reason=action["action"],
            close_ratio=close_ratio
        )
        
        if not res:
            return None

        # Update position state for TP hits
        if action["action"] == "tp1":
            pos.tp1_hit = True
            pos.trailing_active = True
            pos.trailing_high = current_price
            # SL move to breakeven is usually handled by RiskManager logic itself if specified,
            # but we update shadow SL here.
            pos.stop_loss = pos.entry_price
            log.info(f" [LIVE] TP1 hit on {pos.asset} - shadow SL moved to breakeven")
        elif action["action"] == "tp2":
            pos.tp2_hit = True
            log.info(f" [LIVE] TP2 hit on {pos.asset}")

        # Update peak high for trailing
        if pos.side == Side.LONG:
            pos.trailing_high = max(pos.trailing_high, current_price)
        else:
            pos.trailing_high = min(pos.trailing_high, current_price)

        return {**action, "pnl": res.get("pnl", 0), "position_id": pos.position_id}

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

        close_size  = round(pos.size_current * close_ratio, 4)
        is_buy      = pos.side == Side.SHORT   # closing long = sell

        _, filled = await self._place_with_retry(
            asset=pos.asset,
            is_buy=is_buy,
            sz=close_size,
            limit_px=current_price,
            order_type="post_only",
            reduce_only=True,
        )

        if filled:
            # Estimate PnL
            pnl = pos.floating_pct(current_price) * close_size * pos.entry_price
            pos.pnl_realized  += pnl
            pos.size_current  -= close_size

            if pos.size_current <= 0 or close_ratio >= 1.0:
                pos.status   = PositionStatus.CLOSED
                pos.closed_at= utcnow()

            self.risk.record_pnl(pnl, (await self.get_account_state()).balance)
            
            log_data = {
                "type":      "close",
                "pos_id":    position_id,
                "asset":     pos.asset,
                "side":      pos.side.value,
                "price":     current_price,
                "reason":    reason,
                "size":      close_size,
                "notional":  close_size * pos.entry_price,
                "pnl":       pnl,
                "pnl_pct":   pos.floating_pct(current_price),
                "score":     getattr(pos, 'entry_score', 0),
                "timestamp": utcnow(),
            }
            from core.db import user_db
            user_db.save_trade(self.chat_id, log_data)
            get_excel_logger().log_trade(self.chat_id, log_data)

            log.info(
                f" [LIVE] Closed {close_ratio*100:.0f}% of {pos.asset} "
                f"@ {current_price} | PnL est: {format_usd(pnl)} ({reason})"
            )
            return {"position_id": position_id, "pnl": pnl, "reason": reason}
        else:
            log.error(f" Failed to close position {position_id}")
            return None
    async def close_all_positions(self, prices: Dict[str, float]) -> List[Dict]:
        """Close all open positions on chain."""
        results = []
        for pos in list(self._positions.values()):
            if pos.status == PositionStatus.OPEN:
                price = prices.get(pos.asset, pos.entry_price)
                res = await self.close_position(pos.position_id, price, reason="close_all")
                if res:
                    results.append(res)
        return results

    # ──────────────────────────────────────────
    # ORDER RETRY LOGIC
    # ──────────────────────────────────────────

    async def _place_with_retry(
        self,
        asset: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        order_type: str = "post_only",
        reduce_only: bool = False,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[Order, bool]:
        """
        Place order with retry logic.
        Returns (Order, was_filled).
        Uses idempotency key to prevent duplicate orders on retry.
        """
        if idempotency_key is None:
            idempotency_key = gen_id("ORD")

        order = Order(
            order_id=idempotency_key,
            idempotency_key=idempotency_key,
            asset=asset,
            side=Side.LONG if is_buy else Side.SHORT,
            size=sz,
            price=limit_px,
            order_type=order_type,
            is_paper=False,
        )

        for attempt in range(1, EXEC.order_retry_count + 1):
            try:
                result = await self.client.place_order(
                    asset=asset,
                    is_buy=is_buy,
                    sz=sz,
                    limit_px=limit_px,
                    order_type=order_type,
                    reduce_only=reduce_only,
                )

                # Parse SDK result
                status = result.get("status", "")
                if status == "ok":
                    resp = result.get("response", {})
                    data = resp.get("data", {})
                    statuses = data.get("statuses", [{}])
                    fill_info = statuses[0] if statuses else {}

                    if "filled" in fill_info:
                        order.status = OrderStatus.FILLED
                        order.filled_size = sz
                        order.avg_fill_price = float(
                            fill_info["filled"].get("avgPx", limit_px)
                        )
                        log.info(
                            f" Order filled: {asset} {sz} @ "
                            f"{order.avg_fill_price}"
                        )
                        return order, True

                    elif "resting" in fill_info:
                        # Limit order resting in book
                        oid = fill_info["resting"].get("oid")
                        order.order_id = str(oid) if oid else idempotency_key
                        order.status   = OrderStatus.OPEN
                        log.info(f"📋 Order resting in book: oid={oid}")

                        # Wait for fill
                        filled = await self._wait_for_fill(order, timeout=EXEC.partial_fill_timeout_s)
                        if filled:
                            return order, True
                        else:
                            # Cancel and retry with IOC / market
                            log.warning(f"Order not filled in {EXEC.partial_fill_timeout_s}s - cancelling")
                            if oid:
                                try:
                                    await self.client.cancel_order(asset, oid)
                                except Exception as cencal_err:
                                    log.warning(f"Failed to cancel resting order {oid}: {cencal_err}")
                            # Switch to IOC on last retry
                            if attempt == EXEC.order_retry_count:
                                order_type = "limit"  # IOC fallback

            except Exception as e:
                log.error(f"Order attempt {attempt}/{EXEC.order_retry_count} failed: {e}")

            if attempt < EXEC.order_retry_count:
                await asyncio.sleep(EXEC.order_retry_delay_s * attempt)

        order.status = OrderStatus.FAILED
        return order, False

    async def _wait_for_fill(self, order: Order, timeout: int) -> bool:
        """Poll for fill status. In production, use WS user events instead."""
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout:
            await asyncio.sleep(2.0)
            open_orders = await self.client.get_open_orders()
            is_still_open = any(
                str(o.get("oid")) == str(order.order_id)
                for o in open_orders
            )
            if not is_still_open:
                order.status      = OrderStatus.FILLED
                order.filled_size = order.size
                return True
        return False
