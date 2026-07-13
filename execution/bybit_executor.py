"""Real-money Bybit executor with exchange-native hard-stop protection."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

from execution.base_executor import BaseExecutor
from data.bybit_client import BybitAmbiguousOrderError
from execution.exchange_client import (
    ExecutionClient,
    ExecutionOrderStatus,
    LivePositionStatus,
    VenueOrder,
)
from execution.price_bridge import HyperliquidBybitPriceBridge
from execution.symbol_registry import BybitSymbolRegistry
from execution.live_risk_gate import LiveRiskViolation
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
from utils.helpers import gen_id, utcnow


log = logging.getLogger("kara.bybit_exec")


class BybitExecutionError(RuntimeError):
    pass


class BybitProtectionError(BybitExecutionError):
    pass


class BybitExecutor(BaseExecutor):
    mode = BotMode.LIVE

    def __init__(
        self,
        *,
        chat_id: str,
        client: ExecutionClient,
        risk_manager: RiskManager,
        symbol_registry: BybitSymbolRegistry,
        price_bridge: HyperliquidBybitPriceBridge,
        fill_timeout_s: float = 8.0,
        poll_interval_s: float = 0.5,
        persistence=None,
        reconcile_interval_s: float = 30.0,
        failure_threshold: int = 3,
        circuit_cooldown_s: float = 60.0,
        private_ws=None,
        telemetry=None,
        alerts=None,
        live_risk_gate=None,
    ):
        self.chat_id = str(chat_id)
        self.client = client
        self.risk = risk_manager
        self.registry = symbol_registry
        self.price_bridge = price_bridge
        self.fill_timeout_s = fill_timeout_s
        self.poll_interval_s = poll_interval_s
        self.persistence = persistence
        self.reconcile_interval_s = reconcile_interval_s
        self.failure_threshold = failure_threshold
        self.circuit_cooldown_s = circuit_cooldown_s
        self.private_ws = private_ws
        self.telemetry = telemetry
        self.alerts = alerts
        self.live_risk_gate = live_risk_gate
        self._positions: Dict[str, Position] = {}
        self._position_symbols: Dict[str, str] = {}
        self._live_status: Dict[str, LivePositionStatus] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._entry_order_ids: Dict[str, str] = {}
        self._last_reconcile_at = 0.0
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    @property
    def open_positions(self) -> List[Position]:
        return [
            position
            for position in self._positions.values()
            if position.status == PositionStatus.OPEN
        ]

    def live_status(self, position_id: str) -> Optional[LivePositionStatus]:
        return self._live_status.get(position_id)

    def _symbol_lock(self, symbol: str) -> asyncio.Lock:
        return self._locks.setdefault(symbol, asyncio.Lock())

    @property
    def circuit_open(self) -> bool:
        return time.monotonic() < self._circuit_open_until

    def _record_execution_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        if self.telemetry:
            self.telemetry.circuit_open = False
            self.telemetry.circuit_remaining_s = 0.0

    def _record_execution_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._circuit_open_until = time.monotonic() + self.circuit_cooldown_s
            log.critical(
                "Bybit entry circuit opened after %s failures",
                self._consecutive_failures,
            )
            if self.telemetry:
                self.telemetry.circuit_open = True
                self.telemetry.circuit_remaining_s = self.circuit_cooldown_s
            if self.alerts:
                self.alerts.schedule(
                    "circuit_open",
                    "CRITICAL BYBIT: entry circuit breaker terbuka. Entry baru diblokir; exit dan reconciliation tetap aktif.",
                )

    def _persist(self, position_id: str) -> None:
        if not self.persistence or position_id not in self._positions:
            return
        self.persistence.save_bybit_position(
            self.chat_id,
            self._positions[position_id],
            self._position_symbols[position_id],
            self._live_status[position_id].value,
            self._entry_order_ids.get(position_id, ""),
        )

    def load_persisted_positions(self) -> None:
        if not self.persistence:
            return
        for item in self.persistence.load_bybit_positions(self.chat_id):
            position = item["position"]
            self._positions[position.position_id] = position
            self._position_symbols[position.position_id] = item["symbol"]
            try:
                status = LivePositionStatus(item["live_status"])
            except ValueError:
                status = LivePositionStatus.RECONCILIATION_REQUIRED
            self._live_status[position.position_id] = status
            if item.get("entry_order_link_id"):
                self._entry_order_ids[position.position_id] = item[
                    "entry_order_link_id"
                ]

    async def get_account_state(self) -> AccountState:
        venue = await self.client.get_account()
        peak = max(float(self.risk.status.get("peak_balance", 0) or 0), venue.total_equity)
        drawdown = (peak - venue.total_equity) / max(peak, 1)
        daily_pnl = float(self.risk.status.get("daily_pnl", 0) or 0)
        return AccountState(
            total_equity=venue.total_equity,
            wallet_balance=venue.wallet_balance,
            available=venue.available_balance,
            used_margin=venue.used_margin,
            unrealized_pnl=venue.unrealized_pnl,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl / max(venue.total_equity, 1),
            peak_balance=peak,
            current_drawdown_pct=max(drawdown, 0),
            positions=self.open_positions,
            mode=BotMode.LIVE,
            execution_mode=ExecutionMode.FULL_AUTO,
            is_paused=bool(self.risk.status.get("paused")),
            kill_switch_active=bool(self.risk.status.get("kill_switch")),
        )

    async def _wait_for_terminal_order(
        self, symbol: str, client_order_id: str
    ) -> VenueOrder:
        deadline = asyncio.get_running_loop().time() + self.fill_timeout_s
        latest = None
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            if self.private_ws and not self.private_ws.stale:
                ws_order = await self.private_ws.wait_for_order(
                    client_order_id,
                    min(self.poll_interval_s, max(remaining, 0.01)),
                )
                if ws_order:
                    latest = ws_order
                    if latest.status in (
                        ExecutionOrderStatus.FILLED,
                        ExecutionOrderStatus.CANCELLED,
                        ExecutionOrderStatus.REJECTED,
                    ):
                        return latest
            latest = await self.client.get_order(symbol, client_order_id)
            if latest.status in (
                ExecutionOrderStatus.FILLED,
                ExecutionOrderStatus.CANCELLED,
                ExecutionOrderStatus.REJECTED,
            ):
                return latest
            if not self.private_ws or self.private_ws.stale:
                await asyncio.sleep(self.poll_interval_s)
        if latest and latest.filled_qty > 0:
            return latest
        raise BybitExecutionError(f"Order fill timeout: {client_order_id}")

    async def _place_and_confirm(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: float,
        client_order_id: str,
        reduce_only: bool = False,
    ) -> VenueOrder:
        started_at = time.monotonic()
        try:
            await self.client.place_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                client_order_id=client_order_id,
                reduce_only=reduce_only,
            )
        except BybitAmbiguousOrderError:
            # Request may have reached Bybit. Never submit a replacement blindly.
            try:
                existing = await self.client.get_order(symbol, client_order_id)
            except Exception as exc:
                raise BybitExecutionError(
                    f"Ambiguous order requires reconciliation: {client_order_id}"
                ) from exc
            if existing.status in (
                ExecutionOrderStatus.CANCELLED,
                ExecutionOrderStatus.REJECTED,
            ):
                return existing
        fill = await self._wait_for_terminal_order(symbol, client_order_id)
        if self.telemetry:
            latency_ms = max(0.0, (time.monotonic() - started_at) * 1000)
            self.telemetry.fill_latency_ms = latency_ms
            self.telemetry.last_fill_fee = fill.fee_paid
        return fill

    async def open_position(self, signal: TradeSignal) -> Optional[Position]:
        entry_started_at = time.monotonic()
        if self.circuit_open:
            raise BybitExecutionError("Bybit entry circuit breaker is open")
        spec = self.registry.resolve(signal.asset)
        try:
            async with self._symbol_lock(spec.symbol):
                account = await self.get_account_state()
                approved, reason = self.risk.pre_trade_check(
                    signal, account, self.open_positions
                )
                if not approved:
                    log.warning(
                        "Bybit entry blocked for %s: %s", signal.asset, reason
                    )
                    return None

                bybit_price = await self.client.get_mark_price(spec.symbol)
                bridge = self.price_bridge.bridge_levels(
                    side=signal.side,
                    reference_price=signal.entry_price,
                    execution_price=bybit_price,
                    stop_loss=signal.stop_loss,
                    tp1=signal.tp1,
                    tp2=signal.tp2,
                )
                if self.telemetry:
                    self.telemetry.price_bridge_gap_pct = bridge.price_gap_pct
                _, contracts, leverage = self.risk.calculate_position_size(
                    signal, account.total_equity
                )
                leverage = min(leverage, spec.max_leverage)
                if self.live_risk_gate:
                    leverage = min(leverage, self.live_risk_gate.limits.max_leverage)
                quantity = self.registry.normalize_quantity(spec, contracts)
                self.registry.validate_notional(spec, quantity, bybit_price)
                if self.live_risk_gate:
                    try:
                        quote = await self.client.get_execution_quote(
                            spec.symbol, signal.side, quantity
                        )
                        bridge = self.price_bridge.bridge_levels(
                            side=signal.side,
                            reference_price=signal.entry_price,
                            execution_price=quote.mark_price,
                            stop_loss=signal.stop_loss,
                            tp1=signal.tp1,
                            tp2=signal.tp2,
                        )
                        self.live_risk_gate.validate(
                            signal=signal,
                            equity=account.total_equity,
                            quantity=quantity,
                            leverage=leverage,
                            quote=quote,
                            open_positions=self.open_positions,
                        )
                        if self.telemetry:
                            self.telemetry.price_bridge_gap_pct = bridge.price_gap_pct
                            self.telemetry.estimated_slippage_pct = (
                                quote.estimated_slippage_pct
                            )
                    except LiveRiskViolation as exc:
                        if self.telemetry:
                            self.telemetry.risk_rejection_count += 1
                            self.telemetry.last_risk_rejection_reason = exc.reason
                        log.warning("Bybit live entry rejected for %s: %s", signal.asset, exc.reason)
                        return None
                    except Exception:
                        reason = "market_guard_error"
                        if self.telemetry:
                            self.telemetry.risk_rejection_count += 1
                            self.telemetry.last_risk_rejection_reason = reason
                        log.exception("Bybit market guard failed closed for %s", signal.asset)
                        if self.alerts:
                            await self.alerts.emit(
                                f"market_guard_error:{spec.symbol}",
                                f"WARNING BYBIT: market guard gagal untuk {spec.symbol}; entry diblokir.",
                            )
                        return None
                await self.client.set_leverage(spec.symbol, leverage)

                client_order_id = gen_id("KARA-ENTRY")
                fill = await self._place_and_confirm(
                    symbol=spec.symbol,
                    side=signal.side,
                    quantity=quantity,
                    client_order_id=client_order_id,
                )
                if fill.status != ExecutionOrderStatus.FILLED or fill.filled_qty <= 0:
                    if fill.filled_qty > 0:
                        await self._emergency_close(
                            spec.symbol,
                            signal.side,
                            fill.filled_qty,
                            "partial_entry",
                        )
                    return None

                levels = self.price_bridge.bridge_levels(
                    side=signal.side,
                    reference_price=signal.entry_price,
                    execution_price=fill.average_fill_price,
                    stop_loss=signal.stop_loss,
                    tp1=signal.tp1,
                    tp2=signal.tp2,
                )
                stop_loss = self.registry.normalize_price(spec, levels.stop_loss)
                tp1 = self.registry.normalize_price(spec, levels.tp1)
                tp2 = self.registry.normalize_price(spec, levels.tp2)
                position_id = gen_id("BYBIT-POS")
                self._live_status[position_id] = LivePositionStatus.OPEN_UNPROTECTED

                try:
                    await self.client.set_protection(
                        symbol=spec.symbol,
                        side=signal.side,
                        stop_loss=stop_loss,
                    )
                except Exception as exc:
                    if self.telemetry:
                        self.telemetry.hard_sl_missing_count += 1
                    if self.alerts:
                        await self.alerts.emit(
                            f"hard_sl_failed:{spec.symbol}",
                            f"CRITICAL BYBIT: hard SL gagal dipasang untuk {spec.symbol}; emergency close dijalankan.",
                        )
                    self._live_status[position_id] = LivePositionStatus.PENDING_CLOSE
                    try:
                        await self._emergency_close(
                            spec.symbol,
                            signal.side,
                            fill.filled_qty,
                            "protection_failed",
                        )
                        self._live_status[position_id] = LivePositionStatus.CLOSED
                    except Exception as close_exc:
                        self._live_status[position_id] = (
                            LivePositionStatus.RECONCILIATION_REQUIRED
                        )
                        self._record_execution_failure()
                        if self.alerts:
                            await self.alerts.emit(
                                f"emergency_close_failed:{spec.symbol}",
                                f"CRITICAL BYBIT: emergency close gagal untuk {spec.symbol}; reconciliation wajib.",
                            )
                        raise BybitProtectionError(
                            "Hard SL failed and emergency close was not confirmed"
                        ) from close_exc
                    self._record_execution_failure()
                    raise BybitProtectionError(
                        "Hard SL failed; entry was emergency-closed"
                    ) from exc

                margin = fill.filled_qty * fill.average_fill_price / max(leverage, 1)
                position = Position(
                    position_id=position_id,
                    asset=signal.asset,
                    side=signal.side,
                    entry_price=fill.average_fill_price,
                    size_initial=fill.filled_qty,
                    size_current=fill.filled_qty,
                    leverage=leverage,
                    margin_usd=margin,
                    stop_loss=stop_loss,
                    tp1=tp1,
                    tp2=tp2,
                    trailing_high=fill.average_fill_price,
                    signal_id=signal.signal_id,
                    meta_pattern_key=signal.meta_pattern_key,
                    meta_score_delta=signal.meta_score_delta,
                    trade_mode=signal.trade_mode,
                    is_paper=False,
                    entry_score=signal.score,
                    realized_vol=signal.realized_vol,
                    trend_pct=signal.trend_pct,
                    micro_invalidation_price=signal.micro_invalidation_price,
                    entry_location_quality=signal.entry_location_quality,
                )
                self._positions[position_id] = position
                self._position_symbols[position_id] = spec.symbol
                self._entry_order_ids[position_id] = client_order_id
                self._live_status[position_id] = LivePositionStatus.OPEN_PROTECTED
                if self.telemetry:
                    self.telemetry.hard_sl_healthy_count += 1
                    self.telemetry.entry_latency_ms = max(
                        0.0, (time.monotonic() - entry_started_at) * 1000
                    )
                    self.telemetry.actual_slippage_pct = abs(
                        fill.average_fill_price - bybit_price
                    ) / max(bybit_price, 1e-12)
                    self.telemetry.hard_sl_by_symbol[spec.symbol] = True
                self._persist(position_id)
                self._record_execution_success()
                return position
        except BybitProtectionError:
            raise
        except Exception:
            self._record_execution_failure()
            raise

    async def _emergency_close(
        self, symbol: str, entry_side: Side, quantity: float, reason: str
    ) -> VenueOrder:
        if self.telemetry:
            self.telemetry.emergency_close_attempts += 1
        close_side = Side.SHORT if entry_side == Side.LONG else Side.LONG
        client_order_id = gen_id(f"KARA-EMERGENCY-{reason}")
        try:
            fill = await self._place_and_confirm(
                symbol=symbol,
                side=close_side,
                quantity=quantity,
                client_order_id=client_order_id,
                reduce_only=True,
            )
        except Exception:
            if self.telemetry:
                self.telemetry.emergency_close_failures += 1
            raise
        if fill.status != ExecutionOrderStatus.FILLED:
            if self.telemetry:
                self.telemetry.emergency_close_failures += 1
            raise BybitExecutionError("Emergency close was not fully filled")
        if self.telemetry:
            self.telemetry.emergency_close_successes += 1
        return fill

    async def close_position(
        self,
        position_id: str,
        current_price: float,
        reason: str = "manual",
        close_ratio: float = 1.0,
    ) -> Optional[Dict]:
        close_started_at = time.monotonic()
        position = self._positions.get(position_id)
        symbol = self._position_symbols.get(position_id)
        if not position or not symbol or position.status != PositionStatus.OPEN:
            return None
        if not 0 < close_ratio <= 1:
            raise ValueError("close_ratio must be within (0, 1]")

        async with self._symbol_lock(symbol):
            venue_positions = await self.client.get_positions(symbol)
            venue = next(
                (item for item in venue_positions if item.side == position.side), None
            )
            if not venue:
                position.status = PositionStatus.CLOSED
                position.size_current = 0
                position.closed_at = utcnow()
                self._live_status[position_id] = LivePositionStatus.CLOSED
                if self.persistence:
                    self.persistence.remove_bybit_position(position_id)
                return None

            spec = self.registry.resolve(position.asset)
            requested = venue.size if close_ratio >= 1 else venue.size * close_ratio
            quantity = self.registry.normalize_quantity(spec, requested)
            self._live_status[position_id] = LivePositionStatus.PENDING_CLOSE
            close_side = Side.SHORT if position.side == Side.LONG else Side.LONG
            client_order_id = gen_id("KARA-CLOSE")
            fill = await self._place_and_confirm(
                symbol=symbol,
                side=close_side,
                quantity=quantity,
                client_order_id=client_order_id,
                reduce_only=True,
            )
            if fill.filled_qty <= 0:
                self._live_status[position_id] = LivePositionStatus.OPEN_PROTECTED
                return None
            if self.telemetry:
                self.telemetry.close_latency_ms = max(
                    0.0, (time.monotonic() - close_started_at) * 1000
                )

            if position.side == Side.LONG:
                gross_pnl = (
                    fill.average_fill_price - position.entry_price
                ) * fill.filled_qty
            else:
                gross_pnl = (
                    position.entry_price - fill.average_fill_price
                ) * fill.filled_qty
            pnl = gross_pnl - fill.fee_paid
            position.pnl_realized += pnl
            position.size_current = max(0.0, venue.size - fill.filled_qty)
            fully_closed = position.size_current <= spec.qty_step / 2
            if fully_closed:
                position.size_current = 0
                position.status = PositionStatus.CLOSED
                position.closed_at = utcnow()
                self._live_status[position_id] = LivePositionStatus.CLOSED
                balance = (await self.client.get_account()).total_equity
                self.risk.record_pnl(position.pnl_realized, balance)
            else:
                self._live_status[position_id] = LivePositionStatus.OPEN_PROTECTED
            if fully_closed and self.persistence:
                self.persistence.remove_bybit_position(position_id)
            elif not fully_closed:
                self._persist(position_id)

            return {
                "action": reason,
                "reason": reason,
                "position_id": position_id,
                "asset": position.asset,
                "side": position.side.value,
                "pnl": pnl,
                "pnl_slice": pnl,
                "pnl_total": position.pnl_realized,
                "exit_price": fill.average_fill_price,
                "fee_paid": fill.fee_paid,
                "qty_closed": fill.filled_qty,
                "fully_closed": fully_closed,
            }

    async def update_positions(
        self,
        prices: Dict[str, float],
        market_states: Optional[Dict[str, Dict]] = None,
    ) -> List[Dict]:
        actions = []
        for position in list(self.open_positions):
            current = prices.get(position.asset, 0)
            if current <= 0:
                continue
            position.pnl_unrealized = position.unrealized_pnl(current)
            action = self.risk.check_tp_trail(
                position, current, (market_states or {}).get(position.position_id)
            )
            if not action:
                continue
            result = await self.close_position(
                position.position_id,
                current,
                reason=action["action"],
                close_ratio=action.get("close_ratio", 1.0),
            )
            if result:
                result["trigger_price"] = action.get("trigger_price")
                if action["action"] == "tp1":
                    position.tp1_hit = True
                    position.stop_loss = position.entry_price
                    spec = self.registry.resolve(position.asset)
                    position.stop_loss = self.registry.normalize_price(
                        spec, position.stop_loss
                    )
                    await self.client.set_protection(
                        symbol=self._position_symbols[position.position_id],
                        side=position.side,
                        stop_loss=position.stop_loss,
                    )
                elif action["action"] == "tp2":
                    position.tp2_hit = True
                if position.status == PositionStatus.OPEN:
                    self._persist(position.position_id)
                actions.append({**action, **result})
        return actions

    async def close_all_positions(self, prices: Dict[str, float]) -> List[Dict]:
        results = []
        failures = []
        for position in list(self.open_positions):
            try:
                result = await self.close_position(
                    position.position_id,
                    prices.get(position.asset, position.entry_price),
                    reason="close_all",
                )
                if result:
                    results.append(result)
                else:
                    failures.append(position.asset)
            except Exception:
                log.exception("Bybit close-all failed for %s", position.asset)
                failures.append(position.asset)

        await self.reconcile_if_due(force=True)
        remaining = [position.asset for position in self.open_positions]
        unresolved = sorted(set(failures + remaining))
        if unresolved:
            if self.alerts:
                await self.alerts.emit(
                    "close_all_incomplete:" + ",".join(unresolved),
                    "CRITICAL BYBIT: close-all belum selesai untuk "
                    + ", ".join(unresolved),
                )
            results.append({
                "action": "close_all_failed",
                "reason": "close_all_failed",
                "failed_assets": unresolved,
                "fully_closed": False,
                "pnl": 0.0,
            })
        return results

    async def audit_protection(self) -> List[str]:
        """Return assets whose exchange position has no native hard stop."""
        unprotected = []
        for venue in await self.client.get_positions():
            if not venue.stop_loss:
                unprotected.append(venue.symbol)
        return unprotected

    async def mark_price(self, asset: str) -> float:
        spec = self.registry.resolve(asset)
        return await self.client.get_mark_price(spec.symbol)

    async def reconcile_if_due(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._last_reconcile_at < self.reconcile_interval_s:
            return False
        await self.reconcile()
        self._last_reconcile_at = now
        if self.telemetry:
            self.telemetry.last_reconciliation_at = time.time()
        return True

    async def reconcile(self) -> None:
        venue_positions = await self.client.get_positions()
        mismatch_count = 0
        healthy_stops = 0
        missing_stops = 0
        hard_sl_by_symbol = {}
        seen_symbols = set()
        for venue in venue_positions:
            seen_symbols.add(venue.symbol)
            local_id = next(
                (
                    position_id
                    for position_id, symbol in self._position_symbols.items()
                    if symbol == venue.symbol
                    and self._positions[position_id].side == venue.side
                ),
                None,
            )
            if local_id:
                local = self._positions[local_id]
                if (
                    abs(local.size_current - venue.size) > 1e-12
                    or abs(local.entry_price - venue.entry_price) > 1e-12
                ):
                    mismatch_count += 1
                local.size_current = venue.size
                local.entry_price = venue.entry_price
                local.leverage = venue.leverage
                local.pnl_unrealized = venue.unrealized_pnl
                if venue.stop_loss:
                    healthy_stops += 1
                    hard_sl_by_symbol[venue.symbol] = True
                    local.stop_loss = venue.stop_loss
                    self._live_status[local_id] = LivePositionStatus.OPEN_PROTECTED
                else:
                    missing_stops += 1
                    hard_sl_by_symbol[venue.symbol] = False
                    if self.alerts:
                        await self.alerts.emit(
                            f"missing_sl:{venue.symbol}",
                            f"CRITICAL BYBIT: posisi {venue.symbol} tidak memiliki native hard SL.",
                        )
                    self._live_status[local_id] = LivePositionStatus.OPEN_UNPROTECTED
                    if self._valid_recovery_stop(local, local.stop_loss):
                        await self.client.set_protection(
                            symbol=venue.symbol,
                            side=venue.side,
                            stop_loss=local.stop_loss,
                        )
                        self._live_status[local_id] = LivePositionStatus.OPEN_PROTECTED
                        missing_stops -= 1
                        healthy_stops += 1
                        hard_sl_by_symbol[venue.symbol] = True
                        if self.alerts:
                            await self.alerts.emit(
                                f"missing_sl_reinstalled:{venue.symbol}",
                                f"CRITICAL BYBIT: hard SL hilang untuk {venue.symbol} dan berhasil dipasang ulang.",
                            )
                    else:
                        await self._emergency_close(
                            venue.symbol, venue.side, venue.size, "missing_recovery_stop"
                        )
                        local.status = PositionStatus.CLOSED
                        local.size_current = 0
                        local.closed_at = utcnow()
                        self._live_status[local_id] = LivePositionStatus.CLOSED
                        if self.persistence:
                            self.persistence.remove_bybit_position(local_id)
                        continue
                self._persist(local_id)
                continue

            spec = self.registry.resolve_symbol(venue.symbol)
            mismatch_count += 1
            if self.telemetry:
                self.telemetry.unknown_recovered_positions += 1
            if self.alerts:
                await self.alerts.emit(
                    f"unexpected_position:{venue.symbol}",
                    f"CRITICAL BYBIT: ditemukan posisi exchange tak dikenal {venue.symbol}.",
                )
            position_id = gen_id("BYBIT-REC")
            position = Position(
                position_id=position_id,
                asset=spec.asset,
                side=venue.side,
                entry_price=venue.entry_price,
                size_initial=venue.size,
                size_current=venue.size,
                leverage=venue.leverage,
                margin_usd=venue.size * venue.entry_price / max(venue.leverage, 1),
                stop_loss=venue.stop_loss or venue.entry_price,
                tp1=venue.take_profit or venue.entry_price,
                tp2=venue.take_profit or venue.entry_price,
                is_paper=False,
                pnl_unrealized=venue.unrealized_pnl,
            )
            self._positions[position_id] = position
            self._position_symbols[position_id] = venue.symbol
            self._live_status[position_id] = (
                LivePositionStatus.OPEN_PROTECTED
                if venue.stop_loss
                else LivePositionStatus.OPEN_UNPROTECTED
            )
            if not venue.stop_loss:
                missing_stops += 1
                hard_sl_by_symbol[venue.symbol] = False
                await self._emergency_close(
                    venue.symbol, venue.side, venue.size, "unknown_recovered_position"
                )
                position.status = PositionStatus.CLOSED
                position.size_current = 0
                position.closed_at = utcnow()
                self._live_status[position_id] = LivePositionStatus.CLOSED
            else:
                healthy_stops += 1
                hard_sl_by_symbol[venue.symbol] = True
                self._persist(position_id)

        for position_id, symbol in list(self._position_symbols.items()):
            if symbol not in seen_symbols:
                mismatch_count += 1
                position = self._positions[position_id]
                position.status = PositionStatus.CLOSED
                position.size_current = 0
                position.closed_at = utcnow()
                self._live_status[position_id] = LivePositionStatus.CLOSED
                if self.persistence:
                    self.persistence.remove_bybit_position(position_id)
        if self.telemetry:
            self.telemetry.last_reconciliation_at = time.time()
            self.telemetry.reconciliation_mismatch_count += mismatch_count
            self.telemetry.hard_sl_healthy_count = healthy_stops
            self.telemetry.hard_sl_missing_count = missing_stops
            self.telemetry.hard_sl_by_symbol = hard_sl_by_symbol

    @staticmethod
    def _valid_recovery_stop(position: Position, stop_loss: float) -> bool:
        if stop_loss <= 0 or position.entry_price <= 0:
            return False
        if position.side == Side.LONG:
            return stop_loss < position.entry_price
        return stop_loss > position.entry_price
