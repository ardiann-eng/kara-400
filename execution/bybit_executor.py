"""Bybit V5 USDT-M perpetual futures executor."""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Tuple

from execution.base_executor import BaseExecutor
from models.schemas import (
    AccountState,
    BotMode,
    ExecutionMode,
    Position,
    PositionStatus,
    Side,
    TradeSignal,
)
from risk.risk_manager import RiskManager
from utils.excel_logger import get_excel_logger
from utils.helpers import gen_id, utcnow

log = logging.getLogger("kara.bybit_exec")


class BybitExecutor(BaseExecutor):
    """Live executor for Bybit linear USDT perpetuals.

    ExecutionEngine has already decided whether a signal deserves market,
    aggressive limit, or passive limit handling. This class turns that intent
    into actual Bybit orders and refuses stale passive limits instead of
    silently chasing bad price.
    """

    PASSIVE_LIMIT_TYPES = {"limit", "passive_limit", "wait_retest", "wait_reclaim"}

    def __init__(
        self,
        chat_id: str,
        bybit_client,
        risk_manager: RiskManager,
        user_max_leverage: int = 20,
    ):
        self.chat_id = chat_id
        self.client = bybit_client
        self.risk = risk_manager
        self.max_leverage = max(1, int(user_max_leverage or 20))
        self._positions: Dict[str, Position] = {}
        self.excel = get_excel_logger()

    @property
    def open_positions(self) -> List[Position]:
        return [p for p in self._positions.values() if p.status == PositionStatus.OPEN]

    async def get_account_state(self) -> AccountState:
        try:
            acct = await self.client.get_account()
        except Exception as exc:
            log.error(f"[BYBIT] get_account failed: {exc}")
            raise RuntimeError(f"Failed to fetch Bybit account: {exc}") from exc

        total_equity = float(acct.get("totalEquity") or 0.0)
        available = float(
            acct.get("totalAvailableBalance")
            or acct.get("availableBalance")
            or acct.get("availableToWithdraw")
            or 0.0
        )
        unrealized = 0.0
        wallet_balance = total_equity

        for coin in acct.get("coin", []) or []:
            if coin.get("coin") == "USDT":
                wallet_balance = float(coin.get("walletBalance") or wallet_balance or 0.0)
                unrealized = float(coin.get("unrealisedPnl") or coin.get("unrealizedPnl") or 0.0)
                if available <= 0:
                    available = float(coin.get("availableToWithdraw") or coin.get("availableBalance") or 0.0)
                break

        if total_equity <= 0:
            total_equity = wallet_balance + unrealized
        if available <= 0:
            available = max(total_equity, 0.0)

        try:
            ex_positions = await self.client.get_open_positions()
            by_symbol = {p.get("symbol", ""): p for p in ex_positions}
            for pos in self.open_positions:
                raw = by_symbol.get(f"{pos.asset}USDT")
                if raw:
                    pos.pnl_unrealized = float(raw.get("unrealisedPnl") or raw.get("unrealizedPnl") or 0.0)
        except Exception:
            pass

        peak = float(self.risk.status.get("peak_balance") or 0.0)
        if peak <= 0:
            peak = total_equity
            try:
                self.risk._peak_balance = total_equity
            except Exception:
                pass
        drawdown = (peak - total_equity) / max(peak, 1.0)

        return AccountState(
            total_equity=round(total_equity, 2),
            wallet_balance=round(wallet_balance, 2),
            available=round(available, 2),
            used_margin=0.0,
            unrealized_pnl=round(unrealized, 2),
            daily_pnl=round(float(self.risk.status.get("daily_pnl") or 0.0), 2),
            daily_pnl_pct=round(float(self.risk.status.get("daily_pnl") or 0.0) / max(total_equity, 1.0), 4),
            peak_balance=round(peak, 2),
            current_drawdown_pct=round(drawdown, 4),
            positions=list(self.open_positions),
            mode=BotMode.LIVE,
            execution_mode=ExecutionMode.SEMI_AUTO,
            is_paused=bool(self.risk.status.get("paused")),
            kill_switch_active=bool(self.risk.status.get("kill_switch")),
        )

    async def open_position(self, signal: TradeSignal) -> Optional[Position]:
        symbol = f"{signal.asset}USDT"
        account = await self.get_account_state()

        approved, reason = self.risk.pre_trade_check(signal, account, self.open_positions)
        if not approved:
            log.warning(f"[BYBIT] {signal.asset}: trade blocked: {reason}")
            return None

        if signal.suggested_leverage > self.max_leverage:
            log.info(
                f"[BYBIT] {signal.asset}: leverage capped "
                f"{signal.suggested_leverage}x -> {self.max_leverage}x"
            )
            signal.suggested_leverage = self.max_leverage

        try:
            size_usd, contracts, leverage = self.risk.calculate_position_size(signal, account.total_equity)
        except Exception as exc:
            log.error(f"[BYBIT] {signal.asset}: sizing failed: {exc}")
            return None

        leverage = min(max(1, int(leverage)), self.max_leverage)
        if size_usd <= 0 or contracts <= 0:
            log.warning(f"[BYBIT] {signal.asset}: zero size, skip")
            return None

        side_str = "Buy" if signal.side == Side.LONG else "Sell"
        qty = self._format_qty(contracts)
        if float(qty) <= 0:
            log.warning(f"[BYBIT] {signal.asset}: qty rounded to zero, skip")
            return None

        try:
            await self.client.set_leverage(
                symbol=symbol,
                buy_leverage=leverage,
                sell_leverage=leverage,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", ""))
            msg = str(getattr(exc, "msg", exc)).lower()
            already_set = code in {"110043", "110025"} or "not modified" in msg or "same leverage" in msg
            if already_set:
                log.info(f"[BYBIT] {signal.asset}: leverage already set at {leverage}x")
            else:
                log.error(f"[BYBIT] {signal.asset}: set leverage failed, abort live entry: {exc}")
                return None

        order_id, fill_price, fill_path = await self._execute_entry_order(
            symbol=symbol,
            side=side_str,
            qty=qty,
            signal=signal,
        )
        if not order_id or not fill_price:
            return None

        margin = (float(qty) * fill_price) / max(leverage, 1)
        pos = Position(
            position_id=gen_id("POS"),
            asset=signal.asset,
            side=signal.side,
            entry_price=fill_price,
            size_initial=float(qty),
            size_current=float(qty),
            leverage=leverage,
            margin_usd=margin,
            stop_loss=signal.stop_loss,
            tp1=signal.tp1,
            tp2=signal.tp2,
            tp3=getattr(signal, "tp3", 0.0),
            trailing_high=fill_price,
            trailing_stop_price=0.0,
            entry_atr=getattr(signal, "entry_atr", 0.0),
            signal_id=signal.signal_id,
            trade_mode=getattr(signal, "trade_mode", "scalper"),
            is_paper=False,
            entry_score=signal.score,
            entry_tier=getattr(signal, "v10_tier", "B"),
            entry_setup=getattr(signal, "v10_setup", "none"),
            realized_vol=getattr(signal, "realized_vol", 0.02),
            gate_expectancy_bucket=getattr(signal, "gate_expectancy_bucket", ""),
            gate_quality_flags=list(getattr(signal, "gate_quality_flags", []) or []),
            original_entry_price=fill_price,
            entry_funding_rate=getattr(signal, "funding_rate", 0.0) or 0.0,
            atr_pct=getattr(signal, "entry_atr", 0.0),
        )
        self._positions[pos.position_id] = pos

        await self._place_exchange_tpsl(symbol, pos)
        await self._place_exchange_trailing_stop(symbol, pos)

        log_data = {
            "type": "open",
            "exchange": "bybit",
            "fill_path": fill_path,
            "execution_playbook": getattr(signal, "execution_playbook", "none"),
            "execution_order_type": getattr(signal, "execution_order_type", "market"),
            "gate_expectancy_bucket": getattr(signal, "gate_expectancy_bucket", ""),
            "gate_quality_flags": ",".join(getattr(signal, "gate_quality_flags", []) or []),
            "pos_id": pos.position_id,
            "asset": signal.asset,
            "symbol": symbol,
            "side": signal.side.value,
            "entry_price": fill_price,
            "mark_price": signal.entry_price,
            "contracts": float(qty),
            "notional": float(qty) * fill_price,
            "margin": margin,
            "leverage": leverage,
            "score": signal.score,
            "timestamp": utcnow(),
        }
        self.excel.log_trade(self.chat_id, log_data)
        self.risk.record_asset_trade(signal.asset)

        log.info(
            f"[BYBIT] OPEN {signal.asset} {signal.side.value.upper()} "
            f"@ {fill_price:.8f} via {fill_path} | qty={qty} | lev={leverage}x | "
            f"exec={getattr(signal, 'execution_playbook', 'none')}/"
            f"{getattr(signal, 'execution_order_type', 'market')}"
        )
        return pos

    async def _execute_entry_order(
        self,
        symbol: str,
        side: str,
        qty: str,
        signal: TradeSignal,
    ) -> Tuple[Optional[str], Optional[float], str]:
        exec_type = (getattr(signal, "execution_order_type", "") or "market").lower()
        playbook = (getattr(signal, "execution_playbook", "") or "").lower()

        if exec_type == "market":
            return await self._place_and_wait(symbol, side, qty, "Market", "", 6.0, "market")

        limit_price = self._limit_price(signal, exec_type)
        tif = "IOC" if exec_type == "aggressive_limit" else "PostOnly"
        wait_s = 4.0 if exec_type == "aggressive_limit" else 8.0
        order_id, fill_price, path = await self._place_and_wait(
            symbol=symbol,
            side=side,
            qty=qty,
            order_type="Limit",
            price=limit_price,
            wait_s=wait_s,
            path=exec_type,
            time_in_force=tif,
        )

        if fill_price:
            return order_id, fill_price, path

        if order_id:
            await self._cancel_best_effort(symbol, order_id)

        allow_market_fallback = exec_type == "aggressive_limit" and playbook == "short_momentum"
        if allow_market_fallback:
            log.info(f"[BYBIT] {symbol}: aggressive limit missed, market fallback for short_momentum")
            return await self._place_and_wait(symbol, side, qty, "Market", "", 6.0, "market_fallback")

        log.info(f"[BYBIT] {symbol}: {exec_type} not filled; no chase for playbook={playbook}")
        return None, None, f"{exec_type}_miss"

    async def _place_and_wait(
        self,
        symbol: str,
        side: str,
        qty: str,
        order_type: str,
        price: str,
        wait_s: float,
        path: str,
        time_in_force: str = "",
    ) -> Tuple[Optional[str], Optional[float], str]:
        try:
            order = await self.client.place_order(
                symbol=symbol,
                side=side,
                qty=qty,
                order_type=order_type,
                price=price,
                time_in_force=time_in_force,
                position_idx=0,
            )
        except Exception as exc:
            log.error(f"[BYBIT] {symbol}: {path} order placement failed: {exc}")
            return None, None, f"{path}_error"

        order_id = order.get("orderId")
        if not order_id:
            log.error(f"[BYBIT] {symbol}: no orderId returned for {path}")
            return None, None, f"{path}_no_order_id"

        filled_qty, fill_price = await self._wait_for_fill(symbol, order_id, wait_s)
        if filled_qty > 0 and fill_price > 0:
            return order_id, fill_price, path
        return order_id, None, f"{path}_miss"

    async def _wait_for_fill(
        self,
        symbol: str,
        order_id: str,
        max_wait_s: float = 6.0,
    ) -> Tuple[float, float]:
        deadline = asyncio.get_event_loop().time() + max_wait_s
        last_detail: Dict = {}

        while asyncio.get_event_loop().time() < deadline:
            try:
                detail = await self.client.get_order(symbol=symbol, order_id=order_id)
                if detail:
                    last_detail = detail
                    status = (detail.get("orderStatus") or "").lower()
                    filled = float(detail.get("cumExecQty") or detail.get("qtyFilled") or 0.0)
                    avg_px = float(detail.get("avgPrice") or 0.0)
                    if status == "filled" and filled > 0 and avg_px > 0:
                        return filled, avg_px
                    if status in {"cancelled", "canceled", "partiallyfilledcanceled"} and filled > 0 and avg_px > 0:
                        return filled, avg_px
                    if status in {"rejected", "deactivated"}:
                        return 0.0, 0.0
            except Exception:
                pass
            await asyncio.sleep(0.3)

        try:
            filled = float(last_detail.get("cumExecQty") or 0.0)
            avg_px = float(last_detail.get("avgPrice") or 0.0)
            if filled > 0 and avg_px > 0:
                return filled, avg_px
        except Exception:
            pass
        return 0.0, 0.0

    async def _cancel_best_effort(self, symbol: str, order_id: str) -> None:
        try:
            await self.client.cancel_order(symbol=symbol, order_id=order_id)
        except Exception as exc:
            log.debug(f"[BYBIT] {symbol}: cancel {order_id} failed/non-critical: {exc}")

    async def _place_exchange_tpsl(self, symbol: str, pos: Position) -> None:
        try:
            await self.client.place_tpsl_order(
                symbol=symbol,
                sl_price=self._format_price(pos.stop_loss),
                tp_price=self._format_price(pos.tp1) if pos.tp1 else "",
                position_idx=0,
            )
        except Exception as exc:
            log.warning(f"[BYBIT] {symbol}: SL/TP set failed: {exc}")

    async def _place_exchange_trailing_stop(self, symbol: str, pos: Position) -> None:
        try:
            if pos.entry_price <= 0:
                return
            activate_pct = 0.003
            callback_pct = 0.002
            active_price = (
                pos.entry_price * (1 + activate_pct)
                if pos.side == Side.LONG
                else pos.entry_price * (1 - activate_pct)
            )
            trailing_distance = pos.entry_price * callback_pct
            await self.client.set_trailing_stop(
                symbol=symbol,
                trailing_stop=self._format_price(trailing_distance),
                active_price=self._format_price(active_price),
                position_idx=0,
            )
            log.info(
                f"[BYBIT] {symbol}: trailing stop set active@{active_price:.8f} "
                f"distance={trailing_distance:.8f}"
            )
        except Exception as exc:
            log.warning(f"[BYBIT] {symbol}: trailing stop set failed/non-critical: {exc}")

    def _limit_price(self, signal: TradeSignal, exec_type: str) -> str:
        price = (
            getattr(signal, "execution_intended_entry", None)
            or getattr(signal, "execution_actual_entry", None)
            or signal.entry_price
        )
        price = float(price)
        if exec_type == "aggressive_limit":
            cross_bps = 2.0
            if signal.side == Side.LONG:
                price *= 1 + cross_bps / 10000.0
            else:
                price *= 1 - cross_bps / 10000.0
        return self._format_price(price)

    @staticmethod
    def _format_qty(qty: float) -> str:
        text = f"{float(qty):.6f}".rstrip("0").rstrip(".")
        return text if text else "0"

    @staticmethod
    def _format_price(price: float) -> str:
        text = f"{float(price):.8f}".rstrip("0").rstrip(".")
        return text if text else "0"

    async def update_positions(self, prices: Dict[str, float]) -> List[Dict]:
        actions = []
        for pos in list(self.open_positions):
            price = prices.get(pos.asset) or prices.get(f"{pos.asset}USDT")
            if price is None:
                continue
            action = self.risk.check_tp_trail(pos, price)
            if action:
                result = await self._do_close(pos, price, action)
                if result:
                    actions.append(result)
        return actions

    async def close_position(
        self,
        position_id: str,
        current_price: float,
        reason: str = "manual",
    ) -> Optional[Dict]:
        pos = self._positions.get(position_id)
        if not pos or pos.status == PositionStatus.CLOSED:
            log.warning(f"[BYBIT] position {position_id} not found/open")
            return None
        return await self._do_close(pos, current_price, {"action": "close", "reason": reason})

    async def _do_close(self, pos: Position, current_price: float, action: Dict) -> Optional[Dict]:
        symbol = f"{pos.asset}USDT"
        close_side = "Sell" if pos.side == Side.LONG else "Buy"
        close_qty = float(action.get("qty", pos.size_current) or pos.size_current)
        reason = action.get("reason") or action.get("action") or "unknown"

        try:
            await self.client.place_order(
                symbol=symbol,
                side=close_side,
                qty=self._format_qty(close_qty),
                order_type="Market",
                reduce_only=True,
                position_idx=0,
            )
        except Exception as exc:
            log.error(f"[BYBIT] {symbol}: close order failed: {exc}")
            return None

        pnl = pos.floating_pct(current_price) * close_qty * pos.entry_price
        is_partial = close_qty < pos.size_current
        if is_partial:
            pos.size_current -= close_qty
            pos.pnl_realized += pnl
        else:
            pos.pnl_realized += pnl
            pos.status = PositionStatus.CLOSED
            pos.closed_at = utcnow()
            if reason == "progress_stop" or action.get("action") == "progress_stop":
                self.risk.record_progress_stop(pos)

        result = {
            "action": action.get("action", "close"),
            "position_id": pos.position_id,
            "symbol": symbol,
            "side": pos.side.value,
            "reason": reason,
            "close_price": current_price,
            "pnl": round(pnl, 4),
            "qty_closed": close_qty,
            "partial": is_partial,
            "closed_at": utcnow().isoformat(),
        }
        log.info(f"[BYBIT] {symbol}: {'partial' if is_partial else 'full'} close | {reason} | pnl={pnl:.4f}")
        return result

    async def sync_positions_from_chain(self) -> None:
        try:
            positions = await self.client.get_open_positions()
        except Exception as exc:
            log.error(f"[BYBIT] sync_positions failed: {exc}")
            return

        self._positions.clear()
        for raw in positions:
            size = float(raw.get("size") or 0.0)
            if size <= 0:
                continue
            symbol = raw.get("symbol", "")
            asset = symbol.replace("USDT", "")
            side = Side.LONG if raw.get("side") == "Buy" else Side.SHORT
            entry = float(raw.get("avgPrice") or 0.0)
            lev = int(float(raw.get("leverage") or 1))
            pos = Position(
                position_id=gen_id("POS"),
                asset=asset,
                side=side,
                entry_price=entry,
                size_initial=size,
                size_current=size,
                leverage=lev,
                margin_usd=(size * entry) / max(lev, 1),
                stop_loss=float(raw.get("stopLoss") or 0.0),
                tp1=float(raw.get("takeProfit") or 0.0),
                tp2=0.0,
                trailing_high=entry,
                signal_id=None,
                is_paper=False,
            )
            self._positions[pos.position_id] = pos

        log.info(f"[BYBIT] synced {len(self._positions)} open position(s)")
