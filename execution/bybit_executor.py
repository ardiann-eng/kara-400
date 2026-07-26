"""Real-money Bybit executor with exchange-native hard-stop protection."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

import config
from execution.base_executor import BaseExecutor
from data.bybit_client import BybitAmbiguousOrderError
from execution.exchange_client import (
    ExecutionClient,
    ExecutionOrderStatus,
    LivePositionStatus,
    VenueOrder,
    VenuePosition,
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
        user=None,
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
        self.user = user
        self._positions: Dict[str, Position] = {}
        self._position_symbols: Dict[str, str] = {}
        self._live_status: Dict[str, LivePositionStatus] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._reconcile_lock = asyncio.Lock()
        self._entry_symbols: set[str] = set()
        self._entry_order_ids: Dict[str, str] = {}
        self._last_reconcile_at = 0.0
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        # Native target fills are discovered during reconciliation, which has no
        # notification channel. update_positions drains these so a native TP1/TP2
        # reaches the operator through the same path as a local exit.
        self._pending_target_actions: List[Dict] = []

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
        # A private WS fill may arrive before entry finishes local registration
        # and native SL installation. Reconciliation must not recover/emergency
        # close that known in-flight symbol during this narrow lifecycle gap.
        self._entry_symbols.add(spec.symbol)
        try:
            async with self._symbol_lock(spec.symbol):
                account = await self.get_account_state()
                allocation = getattr(self.user, "capital_allocation_usd", None)
                environment = getattr(
                    getattr(self.user, "bybit_environment", None), "value", None
                )
                effective_equity = (
                    min(account.total_equity, allocation)
                    if environment == "mainnet" and allocation is not None
                    else account.total_equity
                )
                if self.telemetry:
                    self.telemetry.venue_equity = account.total_equity
                    self.telemetry.sizing_equity = effective_equity
                sizing_account = account.model_copy(
                    update={"total_equity": effective_equity}
                )
                approved, reason = self.risk.pre_trade_check(
                    signal, sizing_account, self.open_positions
                )
                if not approved:
                    self._persist_rejected_candidate(signal, "strategy_risk_gate", reason, account)
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
                    signal, effective_equity
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
                            equity=effective_equity,
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
                        self._persist_rejected_candidate(
                            signal, "bybit_live_risk_gate", exc.reason, account,
                            extra={
                                "spread_pct": quote.spread_pct,
                                "estimated_slippage_pct": quote.estimated_slippage_pct,
                                "available_depth_quantity": quote.available_quantity,
                            },
                        )
                        if self.telemetry:
                            self.telemetry.risk_rejection_count += 1
                            self.telemetry.last_risk_rejection_reason = exc.reason
                        log.warning("Bybit live entry rejected for %s: %s", signal.asset, exc.reason)
                        return None
                    except Exception:
                        reason = "market_guard_error"
                        self._persist_rejected_candidate(signal, "bybit_live_risk_gate", reason, account)
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

                target_cfg = (
                    config.SCALPER
                    if (signal.trade_mode or "standard") == "scalper"
                    else config.RISK
                )
                tp1_ratio = float(getattr(target_cfg, "tp1_close_ratio", 0.25))
                tp2_ratio = float(getattr(target_cfg, "tp2_close_ratio", 0.50))
                split = self._split_native_targets(
                    spec, fill.filled_qty, tp1_ratio, tp2_ratio
                )
                tp1_quantity = tp2_quantity = 0.0
                tp1_order_id = tp2_order_id = ""
                native_tp_state = "none"
                if split is None:
                    # A TP1 slice below the venue step cannot be installed. Native
                    # targets are an enhancement over local exits, not a condition
                    # of trading: keep the position, keep the hard SL, and let local
                    # polling own TP1/TP2 as it did before.
                    log.info(
                        "Bybit native targets skipped for %s: filled %s cannot be "
                        "split at step %s; local exits remain in control",
                        spec.symbol,
                        fill.filled_qty,
                        spec.qty_step,
                    )
                else:
                    tp1_quantity, tp2_quantity = split
                    try:
                        # Targets are installed without a paired partial stop: the
                        # full SL above already covers the whole position, and
                        # pairing would stack redundant stops at the same trigger.
                        # Verified on Bybit Demo 2026-07-26 (partial_tp_solo_probe).
                        await self.client.add_partial_tp_sl(
                            symbol=spec.symbol, side=signal.side, take_profit=tp1,
                            quantity=tp1_quantity,
                        )
                        await self.client.add_partial_tp_sl(
                            symbol=spec.symbol, side=signal.side, take_profit=tp2,
                            quantity=tp2_quantity,
                        )
                        tp1_order_id, tp2_order_id = await self._read_target_order_ids(
                            spec.symbol, spec, tp1, tp2
                        )
                        native_tp_state = "armed"
                    except Exception as exc:
                        self._live_status[position_id] = LivePositionStatus.PENDING_CLOSE
                        try:
                            await self._emergency_close(
                                spec.symbol, signal.side, fill.filled_qty,
                                "native_tp_install_failed",
                            )
                            self._live_status[position_id] = LivePositionStatus.CLOSED
                        except Exception as close_exc:
                            self._live_status[position_id] = (
                                LivePositionStatus.RECONCILIATION_REQUIRED
                            )
                            raise BybitProtectionError(
                                "Native TP setup failed and emergency close was not confirmed"
                            ) from close_exc
                        raise BybitProtectionError(
                            "Native TP setup failed; entry was emergency-closed"
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
                    strategy_source=signal.strategy_source,
                    is_paper=False,
                    entry_score=signal.score,
                    realized_vol=signal.realized_vol,
                    trend_pct=signal.trend_pct,
                    micro_invalidation_price=signal.micro_invalidation_price,
                    entry_location_quality=signal.entry_location_quality,
                    execution_environment=getattr(
                        getattr(self.user, "bybit_environment", None), "value", "legacy_testnet"
                    ),
                    entry_fee_paid=fill.fee_paid,
                    native_tp_state=native_tp_state,
                    native_tp1_qty=tp1_quantity,
                    native_tp2_qty=tp2_quantity,
                    native_tp1_order_id=tp1_order_id,
                    native_tp2_order_id=tp2_order_id,
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
        finally:
            self._entry_symbols.discard(spec.symbol)

    def _persist_rejected_candidate(self, signal, status: str, reason: str, account, extra=None) -> None:
        """Record only observed rejection inputs; never fabricate quote/fill values."""
        if (
            not self.persistence
            or not hasattr(self.persistence, "save_execution_candidate")
            or getattr(getattr(self.user, "bybit_environment", None), "value", None) != "demo"
        ):
            return
        allocation_usd = getattr(self.user, "capital_allocation_usd", None)
        environment = getattr(getattr(self.user, "bybit_environment", None), "value", None)
        sizing = (
            min(account.total_equity, allocation_usd)
            if environment == "mainnet" and allocation_usd else account.total_equity
        )
        self.persistence.save_execution_candidate(
            self.chat_id,
            signal,
            status=status,
            reason=reason,
            execution_environment="demo",
            extra={
                "venue": "bybit",
                "venue_equity": account.total_equity,
                "capital_allocation_idr": getattr(self.user, "capital_allocation_idr", None),
                "capital_allocation_usd": allocation_usd,
                "sizing_equity": sizing,
                **(extra or {}),
            },
        )

    async def _emergency_close(
        self, symbol: str, entry_side: Side, quantity: float, reason: str
    ) -> VenueOrder:
        if self.telemetry:
            self.telemetry.emergency_close_attempts += 1
        close_side = Side.SHORT if entry_side == Side.LONG else Side.LONG
        # Bybit caps orderLinkId at 45 characters. Reasons are unbounded.
        client_order_id = gen_id("KARA-EMG")
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
            try:
                quantity = self.registry.normalize_quantity(spec, requested)
            except ValueError:
                # A partial slice can round below the venue step on a position only
                # a step or two wide. Skipping the partial leaves the position under
                # its hard stop; submitting the full size instead would silently
                # turn a scale-out into a full exit.
                if close_ratio >= 1:
                    raise
                log.info(
                    "Bybit partial close skipped for %s: %.4g of %s is below step %s",
                    position.asset,
                    close_ratio,
                    venue.size,
                    spec.qty_step,
                )
                return None
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
            entry_fee_slice = position.entry_fee_paid * (
                fill.filled_qty / max(position.size_initial, 1e-12)
            )
            pnl = gross_pnl - entry_fee_slice - fill.fee_paid
            position.pnl_realized += pnl
            position.exit_fee_paid += fill.fee_paid
            position.close_slices += 1
            position.size_current = max(0.0, venue.size - fill.filled_qty)
            fully_closed = position.size_current <= spec.qty_step / 2
            balance = (await self.client.get_account()).total_equity
            if fully_closed:
                position.size_current = 0
                position.status = PositionStatus.CLOSED
                position.closed_at = utcnow()
                self._live_status[position_id] = LivePositionStatus.CLOSED
                self.risk.record_pnl(position.pnl_realized, balance)
                self._record_meta_outcome(position)
            else:
                self._live_status[position_id] = LivePositionStatus.OPEN_PROTECTED
            self._persist_close_slice(
                position, fill, reason, balance, pnl, entry_fee_slice, fully_closed
            )
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
                "execution_environment": position.execution_environment,
            }

    def _persist_close_slice(
        self, position, fill, reason: str, venue_equity: float, pnl_slice: float,
        entry_fee_slice: float, fully_closed: bool,
    ) -> None:
        """Persist each actual close slice; final row retains cumulative lifecycle PnL."""
        if not self.persistence or not hasattr(self.persistence, "save_trade"):
            return
        allocation_usd = getattr(self.user, "capital_allocation_usd", None)
        allocation_idr = getattr(self.user, "capital_allocation_idr", None)
        sizing_equity = min(venue_equity, allocation_usd) if allocation_usd else venue_equity
        slice_fee = entry_fee_slice + fill.fee_paid
        entry_notional = position.size_initial * position.entry_price
        trade_id = (
            position.position_id
            if fully_closed else f"{position.position_id}:slice:{position.close_slices}"
        )
        self.persistence.save_trade(self.chat_id, {
            "pos_id": trade_id,
            "asset": position.asset,
            "side": position.side.value,
            "reason": reason,
            "entry_price": position.entry_price,
            "exit_price": fill.average_fill_price,
            "size": position.size_initial,
            "notional": entry_notional,
            "pnl": pnl_slice,
            "pnl_slice": pnl_slice,
            "pnl_total": position.pnl_realized,
            "pnl_pct": pnl_slice / max(entry_notional, 1e-12),
            "execution_environment": position.execution_environment,
            "venue": "bybit",
            "venue_equity": venue_equity,
            "capital_allocation_idr": allocation_idr,
            "capital_allocation_usd": allocation_usd,
            "sizing_equity": sizing_equity,
            "actual_fill_price": fill.average_fill_price,
            "fee": slice_fee,
            "fee_total": position.entry_fee_paid + position.exit_fee_paid,
            "close_slice": not fully_closed,
            "fully_closed": fully_closed,
            "planned_stop_loss": position.stop_loss,
            "quantity": position.size_initial,
            "leverage": position.leverage,
            "strategy_profile": position.strategy_source,
            "trade_mode": position.trade_mode,
            # Without these a closed Bybit trade cannot be joined back to the
            # signal that produced it, which is why no historical trade carried a
            # meta pattern and no backfill was possible.
            "signal_id": position.signal_id,
            "meta_pattern_key": position.meta_pattern_key,
            "meta_score_delta": position.meta_score_delta,
            "entry_score": position.entry_score,
            "opened_at": position.opened_at.isoformat(),
            "closed_at": position.closed_at.isoformat() if position.closed_at else None,
        })

    async def update_positions(
        self,
        prices: Dict[str, float],
        market_states: Optional[Dict[str, Dict]] = None,
    ) -> List[Dict]:
        # Native target fills are detected during reconciliation, which cannot
        # notify. Surface them here so TP1/TP2 reach the operator once.
        actions = []
        if self._pending_target_actions:
            actions.extend(self._pending_target_actions)
            self._pending_target_actions = []
        for position in list(self.open_positions):
            # Recovery has exchange size/side, but not original strategy levels.
            # Never fabricate TP1/TP2 from entry price or run autonomous exits
            # until reconciliation has established a safely managed lifecycle.
            if position.strategy_source == "exchange_recovery_unknown":
                log.warning(
                    "Bybit exits deferred for recovered unknown position %s", position.asset
                )
                continue
            current = prices.get(position.asset, 0)
            if current <= 0:
                continue
            position.pnl_unrealized = position.unrealized_pnl(current)
            action = self.risk.check_tp_trail(
                position, current, (market_states or {}).get(position.position_id)
            )
            if not action:
                continue
            if (
                getattr(position, "native_tp_state", "none")
                in ("armed", "reconciliation_required")
                and action["action"] in ("tp1", "tp2")
            ):
                # Native target owns this slice. REST reconciliation must confirm
                # its actual fill before lifecycle/accounting changes.
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
                    spec = self.registry.resolve(position.asset)
                    breakeven_stop = self.registry.normalize_price(
                        spec, position.entry_price
                    )
                    try:
                        await self.client.set_protection(
                            symbol=self._position_symbols[position.position_id],
                            side=position.side,
                            stop_loss=breakeven_stop,
                        )
                    except Exception:
                        # Bybit rejects a stop the mark price has already crossed.
                        # Keep the local stop equal to the one the venue still holds:
                        # claiming break-even here would desync KARA from actual
                        # protection, and raising would abort exits for every
                        # remaining open position in this loop.
                        log.exception(
                            "Bybit break-even stop rejected for %s; venue stop unchanged",
                            position.asset,
                        )
                        result["native_stop_updated"] = False
                        result["stop_moved_to_entry"] = False
                        if self.alerts:
                            await self.alerts.emit(
                                f"breakeven_stop_failed:{position.asset}",
                                f"CRITICAL BYBIT: gagal memindahkan SL ke break-even untuk {position.asset}; SL lama di bursa tetap berlaku.",
                            )
                    else:
                        position.stop_loss = breakeven_stop
                        result["native_stop_updated"] = True
                        result["stop_moved_to_entry"] = True
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
        async with self._reconcile_lock:
            await self._reconcile_locked()

    async def _reconcile_locked(self) -> None:
        venue_positions = await self.client.get_positions()
        mismatch_count = 0
        healthy_stops = 0
        missing_stops = 0
        hard_sl_by_symbol = {}
        seen_symbols = set()
        for venue in venue_positions:
            seen_symbols.add(venue.symbol)
            if venue.symbol in self._entry_symbols:
                log.info(
                    "Bybit reconciliation defers in-flight entry for %s", venue.symbol
                )
                continue
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
                # Settlement runs after the venue sync below, but the size delta it
                # needs is only knowable before local size is overwritten.
                targets_armed = (
                    getattr(local, "native_tp_state", "none") == "armed"
                    and venue.size < local.size_current - 1e-12
                )
                size_drop = local.size_current - venue.size
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
                    reference_price = await self._reference_price(venue.symbol)
                    if reference_price <= 0:
                        # A price-feed failure is not proof the stop is unusable.
                        # Retry next reconciliation rather than destroy a live position.
                        log.error(
                            "Bybit hard SL reinstall deferred for %s: no reference price",
                            venue.symbol,
                        )
                        if self.alerts:
                            await self.alerts.emit(
                                f"missing_sl_deferred:{venue.symbol}",
                                f"CRITICAL BYBIT: {venue.symbol} tanpa hard SL dan harga referensi tidak tersedia; pemasangan ulang ditunda.",
                            )
                    elif self._valid_recovery_stop(
                        local, local.stop_loss, reference_price
                    ):
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
                if targets_armed:
                    # Runs last so the break-even stop it installs is not undone by
                    # the venue stop_loss read taken at the top of this cycle.
                    await self._settle_armed_targets(local, venue, size_drop)
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
                strategy_source="exchange_recovery_unknown",
                trade_mode="recovery",
                execution_environment=getattr(
                    getattr(self.user, "bybit_environment", None), "value", "unknown"
                ),
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
                if position.status == PositionStatus.OPEN:
                    # A venue-side stop closed this. Book it before discarding the
                    # position, or the loss never reaches history or meta learning.
                    await self._settle_vanished_position(position_id, symbol)
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

    async def _settle_armed_targets(
        self, position: Position, venue: VenuePosition, size_drop: float
    ) -> None:
        """Attribute an exchange size reduction to confirmed native target fills."""
        settled = await self._settle_native_targets(position, venue.symbol)
        self._pending_target_actions.extend(settled)
        if any(item["action"] == "tp1" for item in settled):
            await self._move_stop_to_breakeven(position, venue.symbol, settled)
        booked = sum(item["qty_closed"] for item in settled)
        shortfall = size_drop - booked
        if shortfall > self.registry.resolve(position.asset).qty_step / 2:
            # Part of the reduction has no confirmed target behind it. Never guess.
            position.native_tp_state = "reconciliation_required"
            if self.alerts:
                await self.alerts.emit(
                    f"native_tp_fill_unattributed:{venue.symbol}",
                    f"WARNING BYBIT: ukuran {venue.symbol} berkurang {shortfall:g} "
                    "di luar native TP yang terkonfirmasi. Rekonsiliasi manual diperlukan.",
                )
        elif not position.native_tp1_order_id and not position.native_tp2_order_id:
            position.native_tp_state = "none"

    def _record_meta_outcome(self, position: Position) -> None:
        """Feed one closed position into meta-pattern stats, exactly once.

        Live execution moved to Bybit but this call did not move with it, so
        meta_pattern_stats stayed empty and every signal scored meta_delta=+0.
        Losses must arrive through the same path as wins: a venue-side stop that
        never produces a local fill would otherwise be invisible, and the win-rate
        EMA would drift upward on survivorship alone.
        """
        if position.meta_outcome_recorded or not position.meta_pattern_key:
            return
        if not self.persistence or not hasattr(
            self.persistence, "update_meta_pattern_outcome"
        ):
            return
        position.meta_outcome_recorded = True
        try:
            self.persistence.update_meta_pattern_outcome(
                position.meta_pattern_key, position.pnl_realized
            )
            stats = None
            if hasattr(self.persistence, "get_meta_pattern_stats"):
                stats = self.persistence.get_meta_pattern_stats(
                    position.meta_pattern_key
                )
            log.info(
                "[META] %s pnl=%+.2f samples=%s",
                position.meta_pattern_key,
                position.pnl_realized,
                stats["samples"] if stats else 1,
            )
        except Exception:
            log.exception(
                "Bybit meta pattern outcome failed for %s", position.meta_pattern_key
            )

    async def _settle_vanished_position(self, position_id: str, symbol: str) -> None:
        """Account for a position that disappeared from the exchange.

        This is the stop-out path: the venue closed the remainder, so there is no
        local fill. Realized PnL is read from Bybit's closed-PnL record rather than
        inferred from the stop price, which gap and slippage would make wrong.
        KARA holds at most one position per symbol, so rows after this position
        opened belong to it.
        """
        position = self._positions[position_id]
        rows: List[Dict] = []
        try:
            rows = await self.client.get_closed_pnl(
                symbol, start_ms=int(position.opened_at.timestamp() * 1000)
            )
        except Exception:
            log.exception("Bybit closed PnL unavailable for %s", symbol)

        realized = 0.0
        closed_qty = 0.0
        exit_price = 0.0
        for row in rows:
            try:
                realized += float(row.get("closedPnl") or 0)
                closed_qty += float(row.get("qty") or 0)
                exit_price = float(row.get("avgExitPrice") or 0) or exit_price
            except (TypeError, ValueError):
                continue
        if not rows:
            # Never invent an outcome. The position is gone either way, but an
            # unmeasured close must not teach the meta layer anything.
            log.warning(
                "Bybit vanished position %s closed without a readable PnL record",
                position.asset,
            )
            return

        position.pnl_realized += realized
        position.exit_fee_paid += 0.0
        position.close_slices += 1
        fill = VenueOrder(
            order_id="", client_order_id="", symbol=symbol, side=position.side,
            requested_qty=closed_qty, filled_qty=closed_qty,
            average_fill_price=exit_price or position.stop_loss,
            fee_paid=0.0, status=ExecutionOrderStatus.FILLED, reduce_only=True,
        )
        try:
            balance = (await self.client.get_account()).total_equity
        except Exception:
            balance = 0.0
        self._persist_close_slice(
            position, fill, "venue_stop_or_external_close", balance, realized, 0.0, True
        )
        try:
            self.risk.record_pnl(position.pnl_realized, balance)
        except Exception:
            log.exception("Bybit risk PnL update failed for %s", position.asset)
        self._record_meta_outcome(position)

    async def _move_stop_to_breakeven(
        self, position: Position, symbol: str, actions: List[Dict]
    ) -> None:
        """Lock the remainder at entry once TP1 is confirmed filled at the venue."""
        spec = self.registry.resolve(position.asset)
        breakeven = self.registry.normalize_price(spec, position.entry_price)
        try:
            await self.client.set_protection(
                symbol=symbol, side=position.side, stop_loss=breakeven
            )
        except Exception:
            log.exception(
                "Bybit break-even stop rejected for %s after native TP1", position.asset
            )
            if self.alerts:
                await self.alerts.emit(
                    f"breakeven_stop_failed:{position.asset}",
                    f"CRITICAL BYBIT: gagal memindahkan SL ke break-even untuk "
                    f"{position.asset} setelah TP1 native; SL lama di bursa tetap berlaku.",
                )
            return
        position.stop_loss = breakeven
        for action in actions:
            if action["action"] == "tp1":
                action["native_stop_updated"] = True
                action["stop_moved_to_entry"] = True

    @staticmethod
    def _clear_target_id(position: Position, name: str) -> None:
        if name == "tp1":
            position.native_tp1_order_id = ""
        else:
            position.native_tp2_order_id = ""

    def _record_native_target_fill(
        self, position: Position, name: str, order: VenueOrder, venue_equity: float
    ) -> Dict:
        """Book one native target fill at its actual venue price, exactly once."""
        if position.side == Side.LONG:
            gross = (order.average_fill_price - position.entry_price) * order.filled_qty
        else:
            gross = (position.entry_price - order.average_fill_price) * order.filled_qty
        entry_fee_slice = position.entry_fee_paid * (
            order.filled_qty / max(position.size_initial, 1e-12)
        )
        pnl = gross - entry_fee_slice - order.fee_paid
        position.pnl_realized += pnl
        position.exit_fee_paid += order.fee_paid
        position.close_slices += 1
        if name == "tp1":
            position.tp1_hit = True
        else:
            position.tp2_hit = True
        self._persist_close_slice(
            position, order, name, venue_equity, pnl, entry_fee_slice, False
        )
        return {
            "action": name,
            "reason": name,
            "position_id": position.position_id,
            "asset": position.asset,
            "side": position.side.value,
            "pnl": pnl,
            "pnl_slice": pnl,
            "pnl_total": position.pnl_realized,
            "exit_price": order.average_fill_price,
            "trigger_price": position.tp1 if name == "tp1" else position.tp2,
            "fee_paid": order.fee_paid,
            "qty_closed": order.filled_qty,
            "fully_closed": False,
            "native_target_fill": True,
            "execution_environment": position.execution_environment,
        }

    async def _settle_native_targets(
        self, position: Position, symbol: str
    ) -> List[Dict]:
        """Attribute vanished native targets to TP1/TP2 using venue order state.

        A target order id that is no longer live has either filled or been
        cancelled. Only a confirmed FILLED order books a slice; anything else
        drops the id without inventing lifecycle or PnL.
        """
        pending = [
            (name, order_id)
            for name, order_id in (
                ("tp1", position.native_tp1_order_id),
                ("tp2", position.native_tp2_order_id),
            )
            if order_id
        ]
        if not pending:
            return []
        try:
            rows = await self.client.get_open_orders(symbol, order_filter="StopOrder")
        except Exception:
            log.exception("Bybit target settlement could not list orders for %s", symbol)
            return []
        live_ids = {str(row.get("orderId") or "") for row in rows}
        settled: List[Dict] = []
        venue_equity = 0.0
        for name, order_id in pending:
            if order_id in live_ids:
                continue
            try:
                order = await self.client.get_order_by_id(symbol, order_id)
            except Exception:
                log.exception(
                    "Bybit target lookup failed for %s %s; leaving id for retry",
                    symbol,
                    name,
                )
                continue
            if order is None:
                continue
            if order.status != ExecutionOrderStatus.FILLED or order.filled_qty <= 0:
                log.info(
                    "Bybit native %s for %s ended %s without fill",
                    name,
                    symbol,
                    order.status.value,
                )
                self._clear_target_id(position, name)
                continue
            if not venue_equity:
                venue_equity = (await self.client.get_account()).total_equity
            settled.append(
                self._record_native_target_fill(position, name, order, venue_equity)
            )
            self._clear_target_id(position, name)
        return settled

    def _split_native_targets(
        self, spec, filled_qty: float, tp1_ratio: float, tp2_ratio: float
    ):
        """Return (tp1_qty, tp2_qty), or None when the fill cannot carry targets.

        normalize_quantity raises for a slice below the venue step, so the smallest
        installable position is roughly qty_step / tp1_ratio. Smaller fills are
        legitimate trades that simply keep local exits.
        """
        try:
            tp1_quantity = self.registry.normalize_quantity(
                spec, filled_qty * tp1_ratio
            )
            tp2_quantity = self.registry.normalize_quantity(
                spec, max(0.0, filled_qty - tp1_quantity) * tp2_ratio
            )
        except ValueError:
            return None
        if filled_qty - tp1_quantity - tp2_quantity < -spec.qty_step / 2:
            return None
        return tp1_quantity, tp2_quantity

    async def _read_target_order_ids(
        self, symbol: str, spec, tp1: float, tp2: float
    ) -> tuple[str, str]:
        """Read back venue order ids for the two native targets just installed.

        Bybit's trading-stop response carries no order id, so targets are matched
        by trigger price in the conditional order list. Matching allows half a tick
        rather than float equality, because the venue formats the price itself and
        an exact-equality miss would silently cost us attribution. Each row is
        consumed once so two targets can never share an id. A missing id stays
        empty rather than guessed: attribution must fail loudly instead of
        crediting a fill to the wrong slice.
        """
        try:
            rows = await self.client.get_open_orders(symbol, order_filter="StopOrder")
        except Exception:
            log.exception("Bybit native target order ids unreadable for %s", symbol)
            return "", ""
        candidates: List[tuple[float, str]] = []
        for row in rows:
            if not str(row.get("stopOrderType", "")).endswith("TakeProfit"):
                continue
            try:
                trigger = float(row.get("triggerPrice") or 0)
            except (TypeError, ValueError):
                continue
            order_id = str(row.get("orderId") or "")
            if trigger > 0 and order_id:
                candidates.append((trigger, order_id))

        tolerance = max(getattr(spec, "tick_size", 0.0), 0.0) / 2 or 1e-9

        def take(target: float) -> str:
            for index, (trigger, order_id) in enumerate(candidates):
                if abs(trigger - target) <= tolerance:
                    candidates.pop(index)
                    return order_id
            return ""

        first, second = take(tp1), take(tp2)
        if not first or not second:
            log.warning(
                "Bybit native target ids incomplete for %s: tp1=%s tp2=%s",
                symbol,
                bool(first),
                bool(second),
            )
        return first, second

    async def _reference_price(self, symbol: str) -> float:
        """Live price used to prove a stop is still installable at the venue."""
        try:
            return float(await self.client.get_mark_price(symbol) or 0)
        except Exception:
            log.exception("Bybit mark price unavailable for %s", symbol)
            return 0.0

    @staticmethod
    def _valid_recovery_stop(
        position: Position, stop_loss: float, reference_price: float
    ) -> bool:
        """A stop is installable only when it sits on the protective side of the
        live price.

        Entry price is not a valid reference. After TP1 KARA moves the stop to
        break-even, and a trailing stop can sit far beyond entry; both are
        legitimate profit-lock states. Testing against entry rejected them and
        made reconciliation emergency-close positions it could have reprotected.
        """
        if stop_loss <= 0 or reference_price <= 0:
            return False
        if position.side == Side.LONG:
            return stop_loss < reference_price
        return stop_loss > reference_price
