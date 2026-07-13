from dataclasses import replace

import pytest

from data.bybit_client import BybitAmbiguousOrderError
from execution.bybit_executor import (
    BybitExecutionError,
    BybitExecutor,
    BybitProtectionError,
)
from execution.exchange_client import (
    ExecutionOrderStatus,
    InstrumentSpec,
    VenueAccount,
    VenueOrder,
    VenuePosition,
)
from execution.price_bridge import HyperliquidBybitPriceBridge
from execution.live_risk_gate import (
    BybitLiveRiskGate,
    ExecutionQuote,
    LiveRiskLimits,
)
from core.bybit_observability import BybitTelemetry
from datetime import datetime, timezone
from execution.symbol_registry import BybitSymbolRegistry
from models.schemas import MarketRegime, ScoreBreakdown, Side, SignalStrength, TradeSignal


SPEC_RAW = {
    "symbol": "BTCUSDT",
    "baseCoin": "BTC",
    "settleCoin": "USDT",
    "status": "Trading",
    "contractType": "LinearPerpetual",
    "priceFilter": {"tickSize": "0.1"},
    "lotSizeFilter": {
        "qtyStep": "0.001",
        "minOrderQty": "0.001",
        "minNotionalValue": "5",
    },
    "leverageFilter": {"maxLeverage": "20"},
}


class FakeRisk:
    status = {"peak_balance": 1000, "daily_pnl": 0, "paused": False, "kill_switch": False}

    def pre_trade_check(self, signal, account, positions):
        return True, "ok"

    def calculate_position_size(self, signal, equity):
        return 100, 0.1, 10

    def check_tp_trail(self, position, price, market_state=None):
        return None

    def record_pnl(self, pnl, balance):
        self.recorded = (pnl, balance)


class FakeClient:
    def __init__(self, protection_error=False):
        self.protection_error = protection_error
        self.orders = []
        self.order_results = {}
        self.protections = []
        self.positions = []

    async def get_account(self):
        return VenueAccount(1000, 1000, 900, 100, 0)

    async def get_mark_price(self, symbol):
        return 100.1

    async def set_leverage(self, symbol, leverage):
        self.leverage = (symbol, leverage)

    async def place_order(self, **kwargs):
        self.orders.append(kwargs)
        oid = kwargs["client_order_id"]
        price = 100.2 if not kwargs.get("reduce_only") else 101.0
        self.order_results[oid] = VenueOrder(
            order_id="exchange",
            client_order_id=oid,
            symbol=kwargs["symbol"],
            side=kwargs["side"],
            requested_qty=kwargs["quantity"],
            filled_qty=kwargs["quantity"],
            average_fill_price=price,
            fee_paid=0.01,
            status=ExecutionOrderStatus.FILLED,
            reduce_only=kwargs.get("reduce_only", False),
        )
        return self.order_results[oid]

    async def get_order(self, symbol, client_order_id):
        return self.order_results[client_order_id]

    async def set_protection(self, **kwargs):
        if self.protection_error:
            raise RuntimeError("SL rejected")
        self.protections.append(kwargs)

    async def get_positions(self, symbol=None):
        return self.positions


class AmbiguousFakeClient(FakeClient):
    async def place_order(self, **kwargs):
        await super().place_order(**kwargs)
        raise BybitAmbiguousOrderError(kwargs["client_order_id"])


class EntryOutcomeClient(FakeClient):
    def __init__(self, status, filled_qty=0):
        super().__init__()
        self.status = status
        self.filled_qty = filled_qty

    async def place_order(self, **kwargs):
        await super().place_order(**kwargs)
        if not kwargs.get("reduce_only"):
            order = self.order_results[kwargs["client_order_id"]]
            self.order_results[kwargs["client_order_id"]] = replace(
                order, status=self.status, filled_qty=self.filled_qty
            )
        return self.order_results[kwargs["client_order_id"]]


class FailedEmergencyCloseClient(FakeClient):
    def __init__(self):
        super().__init__(protection_error=True)

    async def place_order(self, **kwargs):
        await super().place_order(**kwargs)
        if kwargs.get("reduce_only"):
            order = self.order_results[kwargs["client_order_id"]]
            self.order_results[kwargs["client_order_id"]] = replace(
                order, status=ExecutionOrderStatus.REJECTED, filled_qty=0
            )
        return self.order_results[kwargs["client_order_id"]]


class RejectedCloseClient(FakeClient):
    reject_closes = False

    async def place_order(self, **kwargs):
        await super().place_order(**kwargs)
        if self.reject_closes and kwargs.get("reduce_only"):
            order = self.order_results[kwargs["client_order_id"]]
            self.order_results[kwargs["client_order_id"]] = replace(
                order, status=ExecutionOrderStatus.REJECTED, filled_qty=0
            )
        return self.order_results[kwargs["client_order_id"]]


class FakePersistence:
    def __init__(self):
        self.rows = {}

    def save_bybit_position(
        self, chat_id, position, symbol, live_status, entry_order_link_id=""
    ):
        self.rows[position.position_id] = {
            "symbol": symbol,
            "live_status": live_status,
            "entry_order_link_id": entry_order_link_id,
            "position": position.model_copy(deep=True),
        }

    def load_bybit_positions(self, chat_id):
        return list(self.rows.values())

    def remove_bybit_position(self, position_id):
        self.rows.pop(position_id, None)


def make_signal():
    return TradeSignal(
        signal_id="signal",
        asset="BTC",
        side=Side.LONG,
        score=70,
        strength=SignalStrength.MODERATE,
        regime=MarketRegime.NORMAL,
        breakdown=ScoreBreakdown(),
        entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=102,
        suggested_leverage=10,
    )


def make_executor(client, persistence=None, **kwargs):
    registry = BybitSymbolRegistry()
    registry.load([SPEC_RAW])
    return BybitExecutor(
        chat_id="1",
        client=client,
        risk_manager=FakeRisk(),
        symbol_registry=registry,
        price_bridge=HyperliquidBybitPriceBridge(0.003),
        fill_timeout_s=0.1,
        poll_interval_s=0,
        persistence=persistence,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_open_is_recorded_only_after_fill_and_hard_stop():
    client = FakeClient()
    executor = make_executor(client)

    position = await executor.open_position(make_signal())

    assert position.entry_price == 100.2
    assert position.size_current == 0.1
    assert client.protections[0]["stop_loss"] == 99.2
    assert executor.live_status(position.position_id).value == "open_protected"


@pytest.mark.asyncio
async def test_protection_failure_emergency_closes_reduce_only():
    client = FakeClient(protection_error=True)
    executor = make_executor(client)

    with pytest.raises(BybitProtectionError, match="emergency-closed"):
        await executor.open_position(make_signal())

    assert len(executor.open_positions) == 0
    assert client.orders[-1]["reduce_only"] is True
    assert client.orders[-1]["side"] == Side.SHORT
    assert executor._consecutive_failures == 1


@pytest.mark.asyncio
async def test_close_uses_exchange_size_fill_price_and_fee():
    client = FakeClient()
    executor = make_executor(client)
    position = await executor.open_position(make_signal())
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.1, 100.2, 10, stop_loss=99.2)
    ]

    result = await executor.close_position(position.position_id, 101, reason="manual")

    assert result["fully_closed"] is True
    assert result["exit_price"] == 101.0
    assert result["pnl"] == pytest.approx((101 - 100.2) * 0.1 - 0.01)
    assert client.orders[-1]["reduce_only"] is True


@pytest.mark.asyncio
async def test_reconcile_emergency_closes_unknown_position_without_stop():
    client = FakeClient()
    client.positions = [VenuePosition("BTCUSDT", Side.SHORT, 0.02, 100, 5)]
    executor = make_executor(client)

    await executor.reconcile()

    assert executor.open_positions == []
    assert client.orders[-1]["reduce_only"] is True


@pytest.mark.asyncio
async def test_ambiguous_entry_is_looked_up_by_same_order_link_id():
    client = AmbiguousFakeClient()
    executor = make_executor(client)

    position = await executor.open_position(make_signal())

    assert position.entry_price == 100.2
    assert len(client.orders) == 1


@pytest.mark.asyncio
async def test_partial_entry_is_emergency_closed_without_duplicate_entry():
    client = EntryOutcomeClient(ExecutionOrderStatus.CANCELLED, filled_qty=0.04)
    executor = make_executor(client)

    position = await executor.open_position(make_signal())

    assert position is None
    assert len(client.orders) == 2
    assert client.orders[0]["reduce_only"] is False
    assert client.orders[1]["reduce_only"] is True
    assert client.orders[1]["quantity"] == 0.04


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [ExecutionOrderStatus.REJECTED, ExecutionOrderStatus.CANCELLED],
)
async def test_rejected_or_cancelled_entry_is_not_recorded_or_retried(status):
    client = EntryOutcomeClient(status)
    executor = make_executor(client)

    position = await executor.open_position(make_signal())

    assert position is None
    assert executor.open_positions == []
    assert len(client.orders) == 1
    assert client.protections == []


@pytest.mark.asyncio
async def test_failed_hard_stop_and_failed_emergency_close_require_reconciliation():
    client = FailedEmergencyCloseClient()
    executor = make_executor(client)

    with pytest.raises(BybitProtectionError, match="not confirmed"):
        await executor.open_position(make_signal())

    assert len(client.orders) == 2
    assert client.orders[-1]["reduce_only"] is True
    assert {status.value for status in executor._live_status.values()} == {
        "reconciliation_required"
    }


@pytest.mark.asyncio
async def test_persisted_strategy_stop_is_restored_after_restart():
    persistence = FakePersistence()
    client = FakeClient()
    first = make_executor(client, persistence=persistence)
    position = await first.open_position(make_signal())
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.1, 100.2, 10, stop_loss=None)
    ]

    restarted = make_executor(client, persistence=persistence)
    restarted.load_persisted_positions()
    await restarted.reconcile()

    assert restarted.live_status(position.position_id).value == "open_protected"
    assert client.protections[-1]["stop_loss"] == position.stop_loss


@pytest.mark.asyncio
async def test_restart_accepts_matching_exchange_hard_stop_without_reinstall():
    persistence = FakePersistence()
    client = FakeClient()
    first = make_executor(client, persistence=persistence)
    position = await first.open_position(make_signal())
    protection_count = len(client.protections)
    client.positions = [
        VenuePosition(
            "BTCUSDT",
            Side.LONG,
            0.1,
            100.2,
            10,
            stop_loss=position.stop_loss,
        )
    ]

    restarted = make_executor(client, persistence=persistence)
    restarted.load_persisted_positions()
    await restarted.reconcile()

    assert restarted.live_status(position.position_id).value == "open_protected"
    assert len(client.protections) == protection_count


@pytest.mark.asyncio
async def test_persistence_removed_after_full_close():
    persistence = FakePersistence()
    client = FakeClient()
    executor = make_executor(client, persistence=persistence)
    position = await executor.open_position(make_signal())
    assert position.position_id in persistence.rows
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.1, 100.2, 10, stop_loss=99.2)
    ]

    await executor.close_position(position.position_id, 101)

    assert position.position_id not in persistence.rows


@pytest.mark.asyncio
async def test_partial_reduce_only_close_keeps_position_open_and_persisted():
    persistence = FakePersistence()
    client = FakeClient()
    executor = make_executor(client, persistence=persistence)
    position = await executor.open_position(make_signal())
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.1, 100.2, 10, stop_loss=99.2)
    ]

    result = await executor.close_position(
        position.position_id, 101, reason="tp1", close_ratio=0.5
    )

    assert result["fully_closed"] is False
    assert result["qty_closed"] == 0.05
    assert position.size_current == pytest.approx(0.05)
    assert position.position_id in persistence.rows
    assert executor.live_status(position.position_id).value == "open_protected"


@pytest.mark.asyncio
async def test_rejected_close_keeps_protected_position_open():
    client = RejectedCloseClient()
    executor = make_executor(client)
    position = await executor.open_position(make_signal())
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.1, 100.2, 10, stop_loss=99.2)
    ]
    client.reject_closes = True

    result = await executor.close_position(position.position_id, 101)

    assert result is None
    assert position in executor.open_positions
    assert executor.live_status(position.position_id).value == "open_protected"
    assert client.orders[-1]["reduce_only"] is True


@pytest.mark.asyncio
async def test_entry_circuit_blocks_new_entries_but_not_reconciliation():
    client = FakeClient()
    executor = make_executor(
        client, failure_threshold=1, circuit_cooldown_s=60, reconcile_interval_s=30
    )
    executor._record_execution_failure()

    with pytest.raises(Exception, match="circuit breaker"):
        await executor.open_position(make_signal())

    assert await executor.reconcile_if_due(force=True) is True
    assert await executor.reconcile_if_due() is False


@pytest.mark.asyncio
async def test_close_all_reports_exchange_positions_that_remain_open():
    client = FakeClient()
    executor = make_executor(client)
    position = await executor.open_position(make_signal())
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.1, 100.2, 10, stop_loss=99.2)
    ]

    async def failed_close(*args, **kwargs):
        return None

    executor.close_position = failed_close
    results = await executor.close_all_positions({"BTC": 101})

    failure = results[-1]
    assert failure["action"] == "close_all_failed"
    assert failure["failed_assets"] == ["BTC"]
    assert position.status.value == "open"


@pytest.mark.asyncio
async def test_close_all_reports_mixed_success_and_failure():
    client = FakeClient()
    executor = make_executor(client)
    first = await executor.open_position(make_signal())
    second = first.model_copy(
        deep=True,
        update={"position_id": "BYBIT-POS-SECOND", "asset": "ETH"},
    )
    executor._positions[second.position_id] = second
    executor._position_symbols[second.position_id] = "ETHUSDT"
    executor._live_status[second.position_id] = executor._live_status[first.position_id]
    outcomes = {
        first.position_id: {
            "action": "close_all",
            "asset": "BTC",
            "fully_closed": True,
            "pnl": 1.0,
        },
        second.position_id: None,
    }

    async def mixed_close(position_id, *args, **kwargs):
        if position_id == first.position_id:
            first.status = first.status.__class__.CLOSED
        return outcomes[position_id]

    async def no_reconcile(force=False):
        return True

    executor.close_position = mixed_close
    executor.reconcile_if_due = no_reconcile
    results = await executor.close_all_positions({"BTC": 101, "ETH": 2000})

    assert results[0]["asset"] == "BTC"
    assert results[-1]["action"] == "close_all_failed"
    assert results[-1]["failed_assets"] == ["ETH"]


@pytest.mark.asyncio
async def test_unknown_position_failed_emergency_close_remains_reconciliation_required():
    client = FailedEmergencyCloseClient()
    client.protection_error = False
    client.positions = [VenuePosition("BTCUSDT", Side.SHORT, 0.02, 100, 5)]
    executor = make_executor(client)

    with pytest.raises(BybitExecutionError, match="Emergency close"):
        await executor.reconcile()

    assert executor.open_positions
    recovered_id = executor.open_positions[0].position_id
    assert executor.live_status(recovered_id).value == "open_unprotected"


@pytest.mark.asyncio
async def test_protection_audit_returns_only_missing_stops():
    client = FakeClient()
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.1, 100, 5, stop_loss=None),
        VenuePosition("ETHUSDT", Side.SHORT, 1, 2000, 5, stop_loss=2100),
    ]
    executor = make_executor(client)

    assert await executor.audit_protection() == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_live_risk_rejection_happens_before_leverage_or_order():
    client = FakeClient()

    async def execution_quote(symbol, side, quantity):
        return ExecutionQuote(
            symbol="BTCUSDT",
            mark_price=100.1,
            best_bid=99,
            best_ask=101,
            spread_pct=0.01,
            estimated_fill_price=101,
            estimated_slippage_pct=0.001,
            available_quantity=quantity,
            received_at=datetime.now(timezone.utc),
        )

    client.get_execution_quote = execution_quote
    executor = make_executor(client)
    executor.live_risk_gate = BybitLiveRiskGate(LiveRiskLimits(
        asset_allowlist=frozenset({"BTC", "ETH"}),
        max_leverage=20,
        max_positions=3,
        max_risk_per_trade_pct=0.035,
        max_total_open_risk_pct=0.105,
        max_symbol_notional_pct=7,
        max_total_notional_pct=21,
        max_signal_age_s=30,
        max_quote_age_s=5,
        max_spread_pct=0.0015,
        max_slippage_pct=0.002,
        min_depth_ratio=1,
    ))
    executor.telemetry = BybitTelemetry()

    position = await executor.open_position(make_signal())

    assert position is None
    assert client.orders == []
    assert not hasattr(client, "leverage")
    assert executor.telemetry.risk_rejection_count == 1
    assert executor.telemetry.last_risk_rejection_reason == "spread_limit"


@pytest.mark.asyncio
async def test_market_guard_transport_error_fails_closed_before_order():
    client = FakeClient()

    async def failed_quote(symbol, side, quantity):
        raise RuntimeError("orderbook unavailable")

    client.get_execution_quote = failed_quote
    executor = make_executor(client)
    executor.live_risk_gate = BybitLiveRiskGate(LiveRiskLimits(
        asset_allowlist=frozenset({"BTC"}), max_leverage=20, max_positions=3,
        max_risk_per_trade_pct=0.035, max_total_open_risk_pct=0.105,
        max_symbol_notional_pct=7, max_total_notional_pct=21,
        max_signal_age_s=30, max_quote_age_s=5, max_spread_pct=0.0015,
        max_slippage_pct=0.002, min_depth_ratio=1,
    ))
    executor.telemetry = BybitTelemetry()

    assert await executor.open_position(make_signal()) is None
    assert client.orders == []
    assert executor.telemetry.last_risk_rejection_reason == "market_guard_error"
