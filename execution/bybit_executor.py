"""Bybit V5 USDT-M Perpetual Futures Executor."""

import asyncio
import logging
from typing import Dict, List, Optional

from execution.base_executor import BaseExecutor
from models.schemas import Position, TradeSignal, AccountState, Side, BotMode, PositionStatus
from risk.risk_manager import RiskManager
from utils.helpers import gen_id, utcnow
from utils.excel_logger import get_excel_logger

log = logging.getLogger('kara.bybit_exec')


class BybitExecutor(BaseExecutor):

    def __init__(self, chat_id, bybit_client, risk_manager: RiskManager, user_max_leverage: int = 20):
        self.chat_id = chat_id
        self.client = bybit_client
        self.risk = risk_manager
        self.max_leverage = user_max_leverage
        self._positions: Dict[str, Position] = {}
        self.excel = get_excel_logger()

    @property
    def open_positions(self) -> List[Position]:
        return list(self._positions.values())

    async def get_account_state(self) -> AccountState:
        info = await self.client.get_account_info()
        equity = float(info.get("totalEquity", 0))
        available = float(info.get("availableBalance", 0))
        return AccountState(
            equity=equity,
            available_balance=available,
            positions=self.open_positions,
        )

    async def open_position(self, signal: TradeSignal) -> Optional[Position]:
        symbol = f"{signal.asset}USDT"
        state = await self.get_account_state()

        # 1. Pre-trade check
        if not self.risk.pre_trade_check(state, signal, self.open_positions):
            log.info(f"[{symbol}] Risk pre-trade check failed")
            return None

        # 2. Position sizing
        sizing = self.risk.calculate_position_size(state, signal)
        if not sizing or sizing.get("qty", 0) <= 0:
            log.warning(f"[{symbol}] Position size zero")
            return None

        qty = sizing["qty"]
        leverage = min(sizing.get("leverage", 10), self.max_leverage)
        sl_price = sizing.get("sl_price")
        tp1_price = sizing.get("tp1_price")

        # 3. Set leverage
        try:
            await self.client.set_leverage(symbol=symbol, leverage=leverage)
        except Exception as e:
            log.error(f"[{symbol}] Set leverage failed: {e}")
            return None

        # 4. Place market order
        side_str = "Buy" if signal.side == Side.LONG else "Sell"
        try:
            order = await self.client.place_order(
                symbol=symbol,
                side=side_str,
                qty=qty,
                order_type="Market",
                position_idx=0,
            )
        except Exception as e:
            log.error(f"[{symbol}] Order placement failed: {e}")
            return None

        order_id = order.get("orderId")
        if not order_id:
            log.error(f"[{symbol}] No orderId returned")
            return None

        # 5. Poll for fill (max 6s)
        fill_price = None
        for _ in range(12):
            await asyncio.sleep(0.5)
            try:
                status = await self.client.get_order(symbol=symbol, order_id=order_id)
                if status.get("orderStatus") == "Filled":
                    fill_price = float(status.get("avgPrice", 0))
                    break
            except Exception:
                pass

        if not fill_price:
            log.error(f"[{symbol}] Order not filled within 6s")
            return None

        # 6. Set SL/TP on exchange
        try:
            await self.client.place_tpsl_order(
                symbol=symbol,
                stop_loss=str(sl_price),
                take_profit=str(tp1_price) if tp1_price else None,
                position_idx=0,
            )
        except Exception as e:
            log.warning(f"[{symbol}] SL/TP set failed: {e}")

        # 7. [P1-5] Set Bybit Conditional Trailing Stop
        # Aktivasi: saat harga naik +0.3% dari entry (scalper micro-move)
        # Callback: 0.2% trailing distance
        # Ini REAL-TIME di exchange, tidak tergantung bot polling interval
        try:
            if fill_price > 0:
                trail_activate_pct = 0.003   # +0.3% dari entry
                trail_callback_pct = 0.002   # 0.2% callback

                if signal.side == Side.LONG:
                    active_price = fill_price * (1 + trail_activate_pct)
                else:
                    active_price = fill_price * (1 - trail_activate_pct)

                trailing_distance = fill_price * trail_callback_pct

                await self.client.set_trailing_stop(
                    symbol=symbol,
                    trailing_stop=str(round(trailing_distance, 6)),
                    active_price=str(round(active_price, 6)),
                    position_idx=0,
                )
                log.info(
                    f"🎯 [{symbol}] Trailing stop set: activate@{active_price:.6f} "
                    f"(+{trail_activate_pct*100:.1f}%), trail={trailing_distance:.6f} "
                    f"({trail_callback_pct*100:.1f}% callback)"
                )
        except Exception as e:
            log.warning(f"[{symbol}] Trailing stop set failed (non-critical): {e}")

        # 7. Create Position object
        pos = Position(
            id=gen_id(),
            chat_id=self.chat_id,
            asset=signal.asset,
            symbol=symbol,
            side=signal.side,
            entry_price=fill_price,
            size=qty,
            leverage=leverage,
            sl_price=sl_price,
            tp1_price=tp1_price,
            tp2_price=sizing.get("tp2_price"),
            status=PositionStatus.OPEN,
            opened_at=utcnow(),
            mode=signal.mode if hasattr(signal, "mode") else BotMode.STANDARD,
            order_id=order_id,
            score=signal.score if hasattr(signal, "score") else 0,
        )
        self._positions[pos.id] = pos
        log.info(f"[{symbol}] Opened {side_str} | qty={qty} @ {fill_price} | lev={leverage}x | SL={sl_price}")
        return pos

    async def update_positions(self, prices: Dict[str, float]) -> List[Dict]:
        actions = []
        for pos in list(self._positions.values()):
            price = prices.get(pos.symbol) or prices.get(pos.asset)
            if price is None:
                continue

            action = self.risk.check_tp_trail(pos, price)
            if not action:
                continue

            result = await self._do_close(pos, price, action)
            if result:
                actions.append(result)
        return actions

    async def close_position(self, position_id: str, current_price: float, reason: str = "manual") -> Optional[Dict]:
        pos = self._positions.get(position_id)
        if not pos:
            log.warning(f"Position {position_id} not found")
            return None
        return await self._do_close(pos, current_price, {"action": "close", "reason": reason})

    async def _do_close(self, pos: Position, current_price: float, action: Dict) -> Optional[Dict]:
        symbol = pos.symbol
        close_side = "Sell" if pos.side == Side.LONG else "Buy"
        close_qty = action.get("qty", pos.size)
        reason = action.get("reason", "unknown")

        try:
            order = await self.client.place_order(
                symbol=symbol,
                side=close_side,
                qty=close_qty,
                order_type="Market",
                position_idx=0,
                reduce_only=True,
            )
        except Exception as e:
            log.error(f"[{symbol}] Close order failed: {e}")
            return None

        # PnL calculation
        if pos.side == Side.LONG:
            pnl = (current_price - pos.entry_price) / pos.entry_price * close_qty * pos.leverage
        else:
            pnl = (pos.entry_price - current_price) / pos.entry_price * close_qty * pos.leverage

        is_partial = close_qty < pos.size
        if is_partial:
            pos.size -= close_qty
        else:
            pos.status = PositionStatus.CLOSED
            pos.closed_at = utcnow()
            self._positions.pop(pos.id, None)

        result = {
            "position_id": pos.id,
            "symbol": symbol,
            "side": pos.side.value,
            "reason": reason,
            "close_price": current_price,
            "pnl": round(pnl, 4),
            "qty_closed": close_qty,
            "partial": is_partial,
            "closed_at": utcnow().isoformat(),
        }
        log.info(f"[{symbol}] {'Partial' if is_partial else 'Full'} close | reason={reason} | PnL={pnl:.4f}")
        return result

    async def sync_positions_from_chain(self) -> None:
        try:
            positions = await self.client.get_open_positions()
        except Exception as e:
            log.error(f"Sync positions failed: {e}")
            return

        self._positions.clear()
        for p in positions:
            size = float(p.get("size", 0))
            if size <= 0:
                continue

            side = Side.LONG if p.get("side") == "Buy" else Side.SHORT
            symbol = p.get("symbol", "")
            asset = symbol.replace("USDT", "")

            pos = Position(
                id=gen_id(),
                chat_id=self.chat_id,
                asset=asset,
                symbol=symbol,
                side=side,
                entry_price=float(p.get("avgPrice", 0)),
                size=size,
                leverage=int(float(p.get("leverage", 1))),
                sl_price=float(p.get("stopLoss", 0)) or None,
                tp1_price=float(p.get("takeProfit", 0)) or None,
                status=PositionStatus.OPEN,
                opened_at=utcnow(),
                mode=BotMode.STANDARD,
            )
            self._positions[pos.id] = pos

        log.info(f"Synced {len(self._positions)} positions from Bybit")
