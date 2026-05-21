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
from execution.base_executor import BaseExecutor
from utils.helpers import gen_id, format_usd, utcnow
from utils.excel_logger import get_excel_logger

log = logging.getLogger("kara.live_exec")


class LiveExecutor(BaseExecutor):
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

        # Local position shadow (synced from chain on startup)
        self._positions: Dict[str, Position] = {}
        self._chain_synced: bool = False

        # SL on-chain order tracking: position_id -> sl_order_id
        # Dipakai untuk membatalkan SL order saat posisi ditutup manual/TP.
        self._sl_order_ids: Dict[str, Optional[int]] = {}

        log.warning(
            " LIVE executor initialized - REAL MONEY MODE. "
            "Double-check your config before trading."
        )

    async def load_from_chain(self):
        """
        Sync open positions from Hyperliquid on startup/restart.
        Prevents orphaned positions after bot crash — critical for live mode.
        """
        try:
            user_state = await self.client.get_user_state()
            if not user_state:
                log.error("[LIVE] load_from_chain: could not fetch user state")
                return

            asset_positions = user_state.get("assetPositions", [])
            recovered = 0
            for ap in asset_positions:
                pos_data = ap.get("position", {})
                szi = float(pos_data.get("szi", 0))
                if szi == 0:
                    continue

                asset     = pos_data.get("coin", "")
                entry_px  = float(pos_data.get("entryPx") or 0)
                upnl      = float(pos_data.get("unrealizedPnl") or 0)
                side      = Side.LONG if szi > 0 else Side.SHORT
                size_abs  = abs(szi)

                # Check if we already have this position in shadow (avoid duplicates)
                existing = next(
                    (p for p in self._positions.values()
                     if p.asset == asset and p.status == PositionStatus.OPEN),
                    None
                )
                if existing:
                    # Refresh live data on existing shadow
                    existing.size_current  = size_abs
                    existing.pnl_unrealized = upnl
                    continue

                # Reconstruct minimal position from chain data
                # SL/TP unknown after restart — set conservative defaults
                sl_pct = 0.03  # 3% fallback SL
                stop_loss = (
                    entry_px * (1 - sl_pct) if side == Side.LONG
                    else entry_px * (1 + sl_pct)
                )
                margin = (size_abs * entry_px) / max(
                    float(pos_data.get("leverage", {}).get("value", 1) if isinstance(pos_data.get("leverage"), dict) else pos_data.get("leverage", 1)),
                    1
                )

                pos = Position(
                    position_id=gen_id("REC"),  # REC = recovered
                    asset=asset,
                    side=side,
                    entry_price=entry_px,
                    size_initial=size_abs,
                    size_current=size_abs,
                    leverage=int(pos_data.get("leverage", {}).get("value", 1) if isinstance(pos_data.get("leverage"), dict) else pos_data.get("leverage", 1)),
                    margin_usd=margin,
                    stop_loss=stop_loss,
                    tp1=entry_px * (1.04 if side == Side.LONG else 0.96),
                    tp2=entry_px * (1.08 if side == Side.LONG else 0.92),
                    trailing_high=entry_px,
                    is_paper=False,
                    pnl_unrealized=upnl,
                )
                self._positions[pos.position_id] = pos
                # Pasang on-chain SL untuk posisi yang dipulihkan setelah restart.
                # SL default 3% ini lebih baik daripada tidak ada proteksi sama sekali.
                await self._place_onchain_sl(pos)
                recovered += 1
                log.warning(
                    f"[LIVE] Recovered position from chain: {asset} {side.value.upper()} "
                    f"size={size_abs} entry={entry_px} (SL/TP default — on-chain SL @ {stop_loss:.4f} dipasang)"
                )

            self._chain_synced = True
            if recovered > 0:
                log.warning(
                    f"[LIVE] Chain sync complete: {recovered} position(s) recovered. "
                    f"SL/TP levels are fallback defaults. Monitor closely."
                )
            else:
                log.info("[LIVE] Chain sync complete: no open positions found.")
        except Exception as e:
            log.error(f"[LIVE] load_from_chain failed: {e}", exc_info=True)

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

        # Set leverage first — abort trade if this fails to avoid wrong leverage
        try:
            await self.client.set_leverage(
                signal.asset,
                actual_lev,
                is_cross=False   # isolated
            )
            log.info(f"⚙️  Leverage set: {signal.asset} {actual_lev}x isolated")
        except Exception as e:
            err_str = str(e).lower()
            if "no change" in err_str or "same leverage" in err_str or "already" in err_str:
                log.info(f"⚙️  Leverage already set at {actual_lev}x for {signal.asset}, continuing.")
            else:
                log.error(f"❌ [LIVE] set_leverage failed for {signal.asset}: {e} — aborting trade.")
                return None

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
            tp3=getattr(signal, 'tp3', 0.0),
            trailing_high=signal.entry_price,
            trailing_stop_price=0.0,
            entry_atr=getattr(signal, 'entry_atr', 0.0),
            signal_id=signal.signal_id,
            is_paper=False,
            entry_score=signal.score,
            realized_vol=getattr(signal, 'realized_vol', 0.02),
            original_entry_price=signal.entry_price,  # [QUANT AGGRESSION] breakeven reference
            # [POST-MORTEM] Entry context for autopsy
            entry_funding_rate=getattr(signal, 'funding_rate', 0.0) or 0.0,
            atr_pct=getattr(signal, 'entry_atr', 0.0),
        )
        self._positions[pos.position_id] = pos

        # Place on-chain SL as safety net — active on exchange even if bot crashes
        await self._place_onchain_sl(pos)

        log_data = {
            "type":      "open",
            "pos_id":    pos.position_id,
            "asset":     signal.asset,
            "side":      signal.side.value,
            "entry_price": pos.entry_price,
            "contracts": contracts,
            "notional":  contracts * pos.entry_price,
            "score":     signal.score,
            "timestamp": utcnow(),
        }
        get_excel_logger().log_trade(self.chat_id, log_data)

        # Record per-asset trade for repeat guard
        self.risk.record_asset_trade(signal.asset)

        log.info(
            f" [LIVE] Opened {signal.asset} {signal.side.value.upper()} "
            f"@ {pos.entry_price} | {contracts:.4f} contracts "
            f"| {signal.suggested_leverage}x isolated"
        )
        return pos

    # ──────────────────────────────────────────
    # SL ON-CHAIN MANAGEMENT
    # ──────────────────────────────────────────

    async def _place_onchain_sl(self, pos: Position) -> None:
        """Place on-chain stop-loss trigger order. Safety net active even if bot crashes."""
        try:
            sl_price = pos.stop_loss
            if not sl_price or sl_price <= 0:
                log.warning(f"[SL-CHAIN] {pos.asset}: invalid stop_loss ({sl_price}), skip")
                return

            is_buy = pos.side == Side.SHORT
            result = await self.client.place_sl_order(
                asset=pos.asset,
                is_buy=is_buy,
                sz=pos.size_current,
                trigger_px=sl_price,
            )

            sl_oid = None
            try:
                statuses = result.get("response", {}).get("data", {}).get("statuses", [{}])
                fill_info = statuses[0] if statuses else {}
                sl_oid = fill_info.get("resting", {}).get("oid")
            except Exception:
                pass

            self._sl_order_ids[pos.position_id] = sl_oid
            log.info(
                f"🛡️  [SL-CHAIN] {pos.asset} on-chain SL @ {sl_price} "
                f"(oid={sl_oid}, side={'BUY' if is_buy else 'SELL'})"
            )
        except Exception as e:
            log.warning(f"⚠️  [SL-CHAIN] Failed to place on-chain SL for {pos.asset}: {e}. Software SL still active.")
            self._sl_order_ids[pos.position_id] = None

    async def _cancel_onchain_sl(self, pos: Position) -> None:
        """Cancel on-chain SL when position closes (TP/trailing/manual)."""
        sl_oid = self._sl_order_ids.pop(pos.position_id, None)
        if not sl_oid:
            return
        try:
            await self.client.cancel_order(pos.asset, sl_oid)
            log.info(f"✅ [SL-CHAIN] On-chain SL cancelled: {pos.asset} oid={sl_oid}")
        except Exception as e:
            log.warning(f"⚠️  [SL-CHAIN] Failed to cancel on-chain SL {pos.asset} oid={sl_oid}: {e}")

    async def update_onchain_sl(self, pos: Position, new_sl_price: float) -> None:
        """Move on-chain SL to new price (e.g. after TP1 hit → breakeven)."""
        await self._cancel_onchain_sl(pos)
        pos.stop_loss = new_sl_price
        await self._place_onchain_sl(pos)

    # ──────────────────────────────────────────
    # POSITION RECONCILIATION (startup sync)
    # ──────────────────────────────────────────

    async def sync_positions_from_chain(self):
        """Alias for load_from_chain — called by session.initialize() on startup."""
        await self.load_from_chain()

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

            # Refresh OHLCV history so check_tp_trail has data for momentum/HTF/emergency exits.
            try:
                await self.risk.refresh_position_candles(pos, self.client)
            except Exception as _refresh_err:
                log.debug(f"[LIVE] Candle refresh skipped for {pos.asset}: {_refresh_err}")

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
            # [QUANT AGGRESSION] SL → breakeven+0.1%
            be_ref = getattr(pos, 'original_entry_price', pos.entry_price) or pos.entry_price
            breakeven = be_ref * 1.001 if pos.side == Side.LONG else be_ref * 0.999
            await self.update_onchain_sl(pos, breakeven)
            if not hasattr(pos, 'partial_exits_done') or pos.partial_exits_done is None:
                pos.partial_exits_done = []
            if 'tp1' not in pos.partial_exits_done:
                pos.partial_exits_done.append('tp1')
            log.info(f" [LIVE] TP1 hit on {pos.asset} - SL (software + on-chain) moved to breakeven @ {breakeven:.4f}")
        elif action["action"] == "tp2":
            pos.tp2_hit = True
            if not hasattr(pos, 'partial_exits_done') or pos.partial_exits_done is None:
                pos.partial_exits_done = []
            if 'tp2' not in pos.partial_exits_done:
                pos.partial_exits_done.append('tp2')
            log.info(f" [LIVE] TP2 hit on {pos.asset}")
        elif action["action"] == "tp3":
            pos.tp3_hit = True
            if not hasattr(pos, 'partial_exits_done') or pos.partial_exits_done is None:
                pos.partial_exits_done = []
            if 'tp3' not in pos.partial_exits_done:
                pos.partial_exits_done.append('tp3')
            log.info(f" [LIVE] TP3 hit on {pos.asset} - ATR trail on last piece")

        # Update peak high for trailing (always, so ATR trail has correct peak)
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
                # Batalkan on-chain SL saat posisi penuh tertutup.
                await self._cancel_onchain_sl(pos)

                # [POST-MORTEM] Generate autopsy on full close
                try:
                    from memory.autopsy_engine import autopsy_engine
                    time_held = (pos.closed_at - pos.opened_at).total_seconds() / 60 if pos.closed_at and pos.opened_at else 0
                    autopsy_data = {
                        "trade_id": position_id,
                        "asset": pos.asset,
                        "side": pos.side.value,
                        "score": getattr(pos, 'entry_score', 0),
                        "entry_price": pos.entry_price,
                        "exit_price": current_price,
                        "pnl": pos.pnl_realized,
                        "pnl_pct": pos.roe_pct(current_price),
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

            self.risk.record_pnl(pnl, (await self.get_account_state()).balance)

            log_data = {
                "type":      "close",
                "pos_id":    position_id,
                "asset":     pos.asset,
                "side":      pos.side.value,
                "entry_price": pos.entry_price,
                "exit_price": current_price,
                "reason":    reason,
                "size":      close_size,
                "notional":  close_size * pos.entry_price,
                "pnl":       pnl,
                "pnl_pct":   pos.roe_pct(current_price),
                "score":     getattr(pos, 'entry_score', 0),
                "timestamp": utcnow(),
                "autopsy":   getattr(pos, 'autopsy', ''),
            }
            from core.db import user_db
            user_db.save_trade(self.chat_id, log_data)
            get_excel_logger().log_trade(self.chat_id, log_data)

            # [LEARNING ENGINE] Record outcome
            try:
                from engine.learning_engine import learning_engine
                _entry_minute = int(pos.opened_at.timestamp() // 60) if pos.opened_at else 0
                _signal_key = f"{pos.asset}_{pos.side.value.lower()}_{_entry_minute}"
                learning_engine.record_outcome(
                    asset=pos.asset, side=pos.side.value.lower(),
                    regime=getattr(pos, 'trade_mode', 'ranging'),
                    score=getattr(pos, 'entry_score', 50), pnl_usd=pnl,
                    pos_id=_signal_key,
                    features={
                        'oi_funding_score': 0, 'orderbook_score': 0, 'liquidation_score': 0,
                        'displacement_5m': getattr(pos, 'trend_pct', 0) or 0,
                        'rsi': 50, 'ema_freshness': 5,
                        'atr_pct': getattr(pos, 'atr_pct', 0) or 0,
                        'regime_code': 0,
                        'hour_utc': pos.opened_at.hour if pos.opened_at else 0,
                        'score': getattr(pos, 'entry_score', 50),
                    }
                )
            except Exception:
                pass

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

                        # Not filled — cancel the resting order first
                        log.warning(f"Order not filled in {EXEC.partial_fill_timeout_s}s - cancelling and retrying with IOC")
                        if oid:
                            try:
                                await self.client.cancel_order(asset, oid)
                            except Exception as cancel_err:
                                log.warning(f"Failed to cancel resting order {oid}: {cancel_err}")

                        # Immediately retry with IOC (market-like) instead of waiting for next loop
                        try:
                            ioc_result = await self.client.place_order(
                                asset=asset,
                                is_buy=is_buy,
                                sz=sz,
                                limit_px=limit_px,
                                order_type="limit",   # IOC tif
                                reduce_only=reduce_only,
                            )
                            ioc_status = ioc_result.get("status", "")
                            if ioc_status == "ok":
                                ioc_fill = ioc_result.get("response", {}).get("data", {}).get("statuses", [{}])[0]
                                if "filled" in ioc_fill:
                                    order.status = OrderStatus.FILLED
                                    order.filled_size = sz
                                    order.avg_fill_price = float(ioc_fill["filled"].get("avgPx", limit_px))
                                    log.info(f"✅ IOC fallback filled: {asset} @ {order.avg_fill_price}")
                                    return order, True
                        except Exception as ioc_err:
                            log.error(f"IOC fallback failed: {ioc_err}")
                        # IOC also failed — break out of retry loop
                        break

            except Exception as e:
                log.error(f"Order attempt {attempt}/{EXEC.order_retry_count} failed: {e}")

            if attempt < EXEC.order_retry_count:
                await asyncio.sleep(EXEC.order_retry_delay_s * attempt)

        order.status = OrderStatus.FAILED
        return order, False

    async def _wait_for_fill(self, order: Order, timeout: int) -> bool:
        """Poll for fill status. Verifies actual fill size from chain, not assumed."""
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout:
            await asyncio.sleep(2.0)
            open_orders = await self.client.get_open_orders()
            is_still_open = any(
                str(o.get("oid")) == str(order.order_id)
                for o in open_orders
            )
            if not is_still_open:
                # Order left the book — verify actual fill from chain position state
                try:
                    user_state = await self.client.get_user_state()
                    asset_positions = user_state.get("assetPositions", [])
                    for ap in asset_positions:
                        pos_data = ap.get("position", {})
                        if pos_data.get("coin") == order.asset:
                            actual_sz = abs(float(pos_data.get("szi", 0)))
                            if actual_sz > 0:
                                order.status        = OrderStatus.FILLED
                                order.filled_size   = actual_sz
                                order.avg_fill_price = float(
                                    pos_data.get("entryPx", order.price)
                                )
                                log.info(
                                    f"✅ Fill verified from chain: {order.asset} "
                                    f"{actual_sz} @ {order.avg_fill_price}"
                                )
                                return True
                except Exception as verify_err:
                    log.warning(f"Could not verify fill from chain: {verify_err}")

                # Fallback: order gone but chain pos not found — assume unfilled/cancelled
                log.warning(
                    f"Order {order.order_id} left book but no chain position found "
                    f"for {order.asset}. Treating as unfilled."
                )
                return False
        return False
