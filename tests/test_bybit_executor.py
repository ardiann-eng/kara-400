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
from models.schemas import (
    MarketRegime, PositionStatus, ScoreBreakdown, Side, SignalStrength, TradeSignal,
)


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
        self.stop_orders = []
        self.filled_targets = {}

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

    async def add_partial_tp_sl(self, **kwargs):
        self.partial_protections = getattr(self, "partial_protections", []) + [kwargs]
        self.stop_orders.append({
            "orderId": f"tp-{len(self.stop_orders) + 1}",
            "stopOrderType": "PartialTakeProfit",
            "tpslMode": "Partial",
            "triggerPrice": str(kwargs["take_profit"]),
            "qty": str(kwargs["quantity"]),
        })

    async def get_open_orders(self, symbol, *, order_filter="StopOrder"):
        return list(self.stop_orders)

    async def get_order_by_id(self, symbol, order_id):
        return self.filled_targets.get(order_id)

    def fill_target(self, order_id, *, price, quantity, fee=0.02):
        """Simulate a venue-side conditional target execution."""
        self.stop_orders = [
            row for row in self.stop_orders if row["orderId"] != order_id
        ]
        self.filled_targets[order_id] = VenueOrder(
            order_id=order_id,
            client_order_id="",
            symbol="BTCUSDT",
            side=Side.SHORT,
            requested_qty=quantity,
            filled_qty=quantity,
            average_fill_price=price,
            fee_paid=fee,
            status=ExecutionOrderStatus.FILLED,
            reduce_only=True,
        )

    async def get_positions(self, symbol=None):
        return self.positions


@pytest.mark.asyncio
async def test_open_arms_two_native_partial_targets_after_full_hard_stop():
    client = FakeClient()
    executor = make_executor(client)

    position = await executor.open_position(make_signal())

    assert client.protections == [{
        "symbol": "BTCUSDT", "side": Side.LONG, "stop_loss": 99.2,
    }]
    # Targets carry no paired stop: the full-position SL above already protects
    # the whole size, and pairing would stack redundant stops at one trigger.
    assert client.partial_protections == [
        {"symbol": "BTCUSDT", "side": Side.LONG, "take_profit": 101.2,
         "quantity": 0.025},
        {"symbol": "BTCUSDT", "side": Side.LONG, "take_profit": 102.2,
         "quantity": 0.037},
    ]
    assert position.native_tp_state == "armed"
    assert position.native_tp1_qty == 0.025
    assert position.native_tp2_qty == 0.037
    assert position.native_tp1_order_id == "tp-1"
    assert position.native_tp2_order_id == "tp-2"
    assert position.signal_price == 100
    assert position.entry_mark_price == pytest.approx(100.1)
    assert position.entry_best_bid is None
    assert position.entry_spread_pct is None
    assert position.entry_actual_slippage_pct == pytest.approx(
        abs(position.entry_price - 100.1) / 100.1
    )
    assert position.entry_fill_latency_ms is not None
    assert position.deployment_version


@pytest.mark.asyncio
async def test_native_armed_target_blocks_duplicate_local_tp_close():
    client = FakeClient()
    executor = make_executor(client)
    position = await executor.open_position(make_signal())
    executor.risk.check_tp_trail = lambda *_: {"action": "tp1", "close_ratio": 0.25}

    actions = await executor.update_positions({"BTC": 101.2})

    assert actions == []
    assert [order for order in client.orders if order["reduce_only"]] == []

    position.native_tp_state = "reconciliation_required"
    actions = await executor.update_positions({"BTC": 101.2})

    assert actions == []
    assert [order for order in client.orders if order["reduce_only"]] == []


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

    def save_trade(self, chat_id, trade_data):
        self.trade = (chat_id, trade_data)
        self.trades = getattr(self, "trades", []) + [(chat_id, trade_data)]

    def save_execution_candidate(self, chat_id, signal, **kwargs):
        self.candidate = (chat_id, signal, kwargs)


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
    async def quote_after_fill(symbol, side, quantity):
        assert any(order.get("reduce_only") for order in client.orders)
        return ExecutionQuote(
            symbol=symbol, mark_price=101, best_bid=100.9, best_ask=101.1,
            spread_pct=0.002, estimated_fill_price=100.9,
            estimated_slippage_pct=0.001, available_quantity=10,
            received_at=datetime.now(timezone.utc),
        )
    client.get_execution_quote = quote_after_fill
    persistence = FakePersistence()
    executor = make_executor(client, persistence=persistence)
    position = await executor.open_position(make_signal())
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.1, 100.2, 10, stop_loss=99.2)
    ]

    result = await executor.close_position(position.position_id, 101, reason="manual")

    assert result["fully_closed"] is True
    assert result["exit_price"] == 101.0
    assert result["pnl"] == pytest.approx((101 - 100.2) * 0.1 - 0.02)
    assert client.orders[-1]["reduce_only"] is True
    trade = persistence.trades[-1][1]
    assert trade["qty_closed"] == pytest.approx(0.1)
    assert trade["observed_exit_price"] == 101
    assert trade["exit_fee_paid"] == pytest.approx(0.01)
    assert "entry_spread_pct" in trade
    assert trade["exit_spread_pct"] == pytest.approx(0.002)
    assert trade["exit_quote_timing"] == "post_fill"


@pytest.mark.asyncio
async def test_final_live_close_persists_environment_allocation_fill_and_fee():
    client = FakeClient()
    persistence = FakePersistence()
    user = type("User", (), {
        "bybit_environment": type("Environment", (), {"value": "demo"})(),
        "capital_allocation_idr": 1_000_000,
        "capital_allocation_usd": 62.5,
    })()
    executor = make_executor(client, persistence=persistence, user=user)
    position = await executor.open_position(make_signal())
    client.positions = [VenuePosition("BTCUSDT", Side.LONG, 0.1, 100.2, 10, stop_loss=99.2)]

    await executor.close_position(position.position_id, 101, reason="manual")

    chat_id, row = persistence.trade
    assert chat_id == "1"
    assert row["execution_environment"] == "demo"
    assert row["venue_equity"] == 1000
    assert row["capital_allocation_idr"] == 1_000_000
    assert row["capital_allocation_usd"] == 62.5
    assert row["sizing_equity"] == 62.5
    assert row["actual_fill_price"] == 101
    assert row["fee"] == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_demo_risk_rejection_persists_candidate_reason_and_equity():
    class RejectRisk(FakeRisk):
        def pre_trade_check(self, signal, account, positions):
            return False, "daily_loss_limit"

    client = FakeClient()
    persistence = FakePersistence()
    user = type("User", (), {
        "bybit_environment": type("Environment", (), {"value": "demo"})(),
        "capital_allocation_idr": 1_000_000,
        "capital_allocation_usd": 62.5,
    })()
    executor = make_executor(client, persistence=persistence, user=user)
    executor.risk = RejectRisk()

    assert await executor.open_position(make_signal()) is None
    chat_id, _signal, candidate = persistence.candidate
    assert chat_id == "1"
    assert candidate["status"] == "strategy_risk_gate"
    assert candidate["reason"] == "daily_loss_limit"
    assert candidate["extra"]["sizing_equity"] == 1000


@pytest.mark.asyncio
async def test_partial_close_persists_slice_then_final_cumulative_pnl():
    client = FakeClient()
    persistence = FakePersistence()
    executor = make_executor(client, persistence=persistence)
    position = await executor.open_position(make_signal())
    client.positions = [VenuePosition("BTCUSDT", Side.LONG, 0.1, 100.2, 10, stop_loss=99.2)]

    first = await executor.close_position(position.position_id, 101, reason="tp1", close_ratio=0.5)
    client.positions = [VenuePosition("BTCUSDT", Side.LONG, 0.05, 100.2, 10, stop_loss=100.2)]
    second = await executor.close_position(position.position_id, 101, reason="manual")

    assert first["fully_closed"] is False
    assert second["fully_closed"] is True
    assert len(persistence.trades) == 2
    partial = persistence.trades[0][1]
    final = persistence.trades[1][1]
    assert partial["pos_id"].endswith(":slice:1")
    assert partial["close_slice"] is True
    assert partial["pnl"] == partial["pnl_slice"]
    assert final["fully_closed"] is True
    assert final["pnl_total"] == pytest.approx(first["pnl"] + second["pnl"])


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
async def test_reconciliation_defers_in_flight_entry_without_recovery_or_emergency_close():
    client = FakeClient()
    client.positions = [VenuePosition("BTCUSDT", Side.LONG, 0.1, 100, 5)]
    executor = make_executor(client)
    executor._entry_symbols.add("BTCUSDT")

    await executor.reconcile()

    assert executor.open_positions == []
    assert client.orders == []
    assert executor.telemetry is None


@pytest.mark.asyncio
async def test_unknown_recovered_position_never_generates_fabricated_tp_actions():
    client = FakeClient()
    client.positions = [VenuePosition("BTCUSDT", Side.LONG, 0.1, 100, 5, stop_loss=99)]
    executor = make_executor(client)

    await executor.reconcile()
    recovered = executor.open_positions[0]

    assert recovered.strategy_source == "exchange_recovery_unknown"
    assert await executor.update_positions({"BTC": 101}) == []
    assert recovered.tp1_hit is False
    assert recovered.tp2_hit is False
    assert client.orders == []


@pytest.mark.asyncio
async def test_emergency_close_order_link_id_stays_within_bybit_limit():
    client = FakeClient()
    executor = make_executor(client)

    await executor._emergency_close(
        "BTCUSDT", Side.LONG, 0.1, "unknown_recovered_position_with_long_reason"
    )

    assert client.orders[-1]["reduce_only"] is True
    assert len(client.orders[-1]["client_order_id"]) <= 45


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
        max_leverage=20, max_positions=3,
        max_risk_per_trade_pct=0.035, max_total_open_risk_pct=0.105,
        max_symbol_notional_pct=7, max_total_notional_pct=21,
        max_signal_age_s=30, max_quote_age_s=5, max_spread_pct=0.0015,
        max_slippage_pct=0.002, min_depth_ratio=1,
    ))
    executor.telemetry = BybitTelemetry()

    assert await executor.open_position(make_signal()) is None
    assert client.orders == []
    assert executor.telemetry.last_risk_rejection_reason == "market_guard_error"


@pytest.mark.asyncio
async def test_successful_entry_persists_exact_quote_and_fill_audit_fields():
    client = FakeClient()

    async def execution_quote(symbol, side, quantity):
        return ExecutionQuote(
            symbol=symbol, mark_price=100.1, best_bid=100.0, best_ask=100.2,
            spread_pct=0.001998, estimated_fill_price=100.2,
            estimated_slippage_pct=0.000999, available_quantity=quantity * 10,
            received_at=datetime.now(timezone.utc),
        )

    client.get_execution_quote = execution_quote
    executor = make_executor(client)
    executor.live_risk_gate = BybitLiveRiskGate(LiveRiskLimits(
        max_leverage=20, max_positions=3, max_risk_per_trade_pct=0.1,
        max_total_open_risk_pct=0.3, max_symbol_notional_pct=10,
        max_total_notional_pct=30, max_signal_age_s=30, max_quote_age_s=5,
        max_spread_pct=0.01, max_slippage_pct=0.01, min_depth_ratio=1,
    ))

    position = await executor.open_position(make_signal())

    assert position.entry_mark_price == pytest.approx(100.1)
    assert position.entry_best_bid == pytest.approx(100.0)
    assert position.entry_best_ask == pytest.approx(100.2)
    assert position.entry_spread_pct == pytest.approx(0.001998)
    assert position.entry_estimated_fill_price == pytest.approx(100.2)
    assert position.entry_estimated_slippage_pct == pytest.approx(0.000999)
    assert position.entry_actual_slippage_pct == pytest.approx(
        abs(position.entry_price - 100.1) / 100.1
    )
    assert position.entry_signal_age_ms is not None
    assert position.initial_stop_loss == position.stop_loss
    assert position.initial_tp1 == position.tp1
    assert position.initial_tp2 == position.tp2


@pytest.mark.asyncio
async def test_missing_sl_on_breakeven_position_is_reinstalled_not_emergency_closed():
    """Regression: LDOUSDT lost its hard SL after TP1 and was emergency-closed.

    _valid_recovery_stop compared the stop against entry price, so the break-even
    profit-lock stop installed at TP1 (stop == entry) failed validation and
    reconciliation destroyed a position it could have reprotected.
    """
    client = FakeClient()
    executor = make_executor(client)
    position = await executor.open_position(make_signal())
    # TP1 fired, so live price is above entry; the break-even stop is installable.
    client.get_mark_price = lambda symbol: _async(101.5)
    position.tp1_hit = True
    position.stop_loss = position.entry_price
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.1, position.entry_price, 10, stop_loss=None)
    ]

    await executor.reconcile()

    assert client.protections[-1] == {
        "symbol": "BTCUSDT", "side": Side.LONG, "stop_loss": position.entry_price,
    }
    assert executor.live_status(position.position_id).value == "open_protected"
    assert [o for o in client.orders if o.get("reduce_only")] == []


@pytest.mark.asyncio
async def test_missing_sl_with_trailing_stop_beyond_entry_is_reinstalled():
    """A runner trailing stop legitimately sits above entry for a long."""
    client = FakeClient()
    executor = make_executor(client)
    position = await executor.open_position(make_signal())
    client.get_mark_price = lambda symbol: _async(120.0)
    position.stop_loss = 110.0  # trailed far above entry 100.2
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.1, position.entry_price, 10, stop_loss=None)
    ]

    await executor.reconcile()

    assert client.protections[-1]["stop_loss"] == 110.0
    assert [o for o in client.orders if o.get("reduce_only")] == []


@pytest.mark.asyncio
async def test_missing_sl_stop_already_crossed_by_price_still_emergency_closes():
    """Fail closed: a long stop above live price cannot protect and must close."""
    client = FakeClient()
    executor = make_executor(client)
    position = await executor.open_position(make_signal())
    client.get_mark_price = lambda symbol: _async(90.0)
    position.stop_loss = 99.2  # above the 90.0 live price for a long
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.1, position.entry_price, 10, stop_loss=None)
    ]

    await executor.reconcile()

    assert [o for o in client.orders if o.get("reduce_only")]


@pytest.mark.asyncio
async def test_missing_sl_defers_reinstall_when_reference_price_unavailable():
    """A price-feed failure must not destroy a live position."""
    client = FakeClient()
    executor = make_executor(client)
    position = await executor.open_position(make_signal())

    async def broken_price(symbol):
        raise RuntimeError("ticker down")

    client.get_mark_price = broken_price
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.1, position.entry_price, 10, stop_loss=None)
    ]

    await executor.reconcile()

    assert [o for o in client.orders if o.get("reduce_only")] == []
    assert executor.live_status(position.position_id).value == "open_unprotected"


@pytest.mark.asyncio
async def test_rejected_breakeven_stop_keeps_local_stop_and_does_not_abort_loop():
    """Regression: an unguarded set_protection after TP1 aborted every remaining exit
    and left KARA claiming break-even while the venue still held the original stop."""
    client = FakeClient()
    executor = make_executor(client)
    position = await executor.open_position(make_signal())
    original_stop = position.stop_loss
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.1, position.entry_price, 10, stop_loss=99.2)
    ]
    position.native_tp_state = "none"
    executor.risk.check_tp_trail = lambda *_, **__: {
        "action": "tp1", "close_ratio": 0.25, "trigger_price": 101.2,
    }

    async def reject_after_entry(**kwargs):
        raise RuntimeError("stop already crossed by mark price")

    client.set_protection = reject_after_entry
    actions = await executor.update_positions({"BTC": 101.2})

    assert actions and actions[0]["native_stop_updated"] is False
    assert position.stop_loss == original_stop
    assert position.tp1_hit is True


def _async(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner()


@pytest.mark.asyncio
async def test_native_tp1_fill_books_slice_pnl_once_and_moves_stop_to_breakeven():
    """Native targets execute at the venue. KARA must learn of the fill, book the
    slice at the actual fill price, and lock the remainder at entry."""
    client = FakeClient()
    persistence = FakePersistence()
    executor = make_executor(client, persistence=persistence)
    position = await executor.open_position(make_signal())
    assert position.native_tp1_order_id == "tp-1"

    client.fill_target("tp-1", price=101.2, quantity=0.025)
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.075, position.entry_price, 10, stop_loss=99.2)
    ]
    await executor.reconcile()

    assert position.tp1_hit is True
    assert position.close_slices == 1
    assert position.stop_loss == position.entry_price
    assert client.protections[-1]["stop_loss"] == position.entry_price
    expected_gross = (101.2 - position.entry_price) * 0.025
    assert position.pnl_realized == pytest.approx(
        expected_gross - position.entry_fee_paid * (0.025 / 0.1) - 0.02
    )

    actions = await executor.update_positions({"BTC": 101.2})
    tp1_actions = [item for item in actions if item["action"] == "tp1"]
    assert len(tp1_actions) == 1
    assert tp1_actions[0]["exit_price"] == 101.2
    assert tp1_actions[0]["trigger_price"] == position.tp1
    assert tp1_actions[0]["native_target_fill"] is True
    assert tp1_actions[0]["stop_moved_to_entry"] is True

    # A second reconciliation must not double-book the same target.
    await executor.reconcile()
    assert position.close_slices == 1
    assert await executor.update_positions({"BTC": 101.2}) == []


@pytest.mark.asyncio
async def test_native_target_cancelled_without_fill_books_nothing():
    client = FakeClient()
    executor = make_executor(client)
    position = await executor.open_position(make_signal())
    client.stop_orders = [
        row for row in client.stop_orders if row["orderId"] != "tp-1"
    ]
    client.filled_targets["tp-1"] = VenueOrder(
        order_id="tp-1", client_order_id="", symbol="BTCUSDT", side=Side.SHORT,
        requested_qty=0.025, filled_qty=0.0, average_fill_price=0.0, fee_paid=0.0,
        status=ExecutionOrderStatus.CANCELLED, reduce_only=True,
    )
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.09, position.entry_price, 10, stop_loss=99.2)
    ]

    await executor.reconcile()

    assert position.tp1_hit is False
    assert position.close_slices == 0
    assert position.pnl_realized == 0
    assert position.native_tp1_order_id == ""
    # Size fell without an attributable target fill, so KARA must not proceed blind.
    assert position.native_tp_state == "reconciliation_required"


@pytest.mark.asyncio
async def test_reconcile_reused_symbol_targets_current_open_position_not_stale_closed_one():
    """A closed lifecycle remains in memory. Reusing its symbol must reconcile
    exchange size and native target fills against the current open lifecycle."""
    client = FakeClient()
    executor = make_executor(client)
    stale = await executor.open_position(make_signal())
    stale.status = PositionStatus.CLOSED
    stale.size_current = 0

    current = await executor.open_position(make_signal())
    client.fill_target(current.native_tp1_order_id, price=101.2, quantity=0.025)
    client.positions = [
        VenuePosition(
            "BTCUSDT", Side.LONG, 0.075, current.entry_price, 10, stop_loss=99.2
        )
    ]

    await executor.reconcile()

    assert stale.tp1_hit is False
    assert stale.size_current == 0
    assert current.tp1_hit is True
    assert current.size_current == 0.075


@pytest.mark.asyncio
async def test_size_drop_beyond_target_fill_is_flagged_for_reconciliation():
    client = FakeClient()
    executor = make_executor(client)
    position = await executor.open_position(make_signal())
    client.fill_target("tp-1", price=101.2, quantity=0.025)
    # Exchange lost far more size than the confirmed target accounts for.
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.02, position.entry_price, 10, stop_loss=99.2)
    ]

    await executor.reconcile()

    assert position.tp1_hit is True
    assert position.native_tp_state == "reconciliation_required"


@pytest.mark.asyncio
async def test_breakeven_failure_after_native_tp1_keeps_venue_stop_truth():
    client = FakeClient()
    executor = make_executor(client)
    position = await executor.open_position(make_signal())
    original_stop = position.stop_loss
    client.fill_target("tp-1", price=101.2, quantity=0.025)
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.075, position.entry_price, 10, stop_loss=99.2)
    ]

    async def reject(**kwargs):
        raise RuntimeError("stop already crossed")

    client.set_protection = reject
    await executor.reconcile()

    assert position.tp1_hit is True
    assert position.stop_loss == original_stop
    actions = await executor.update_positions({"BTC": 101.2})
    assert actions[0].get("stop_moved_to_entry") is not True


@pytest.mark.asyncio
async def test_entry_without_readable_target_ids_still_arms_but_records_empty():
    """A venue that hides target ids must not produce invented attribution."""
    client = FakeClient()

    async def no_orders(symbol, *, order_filter="StopOrder"):
        return []

    client.get_open_orders = no_orders
    executor = make_executor(client)
    position = await executor.open_position(make_signal())

    assert position.native_tp1_order_id == ""
    assert position.native_tp2_order_id == ""
    assert position.native_tp_state == "armed"


@pytest.mark.asyncio
async def test_position_too_small_to_split_keeps_trading_with_local_exits():
    """Regression: normalize_quantity raises for a sub-step slice rather than
    returning zero, so the `tp1_quantity <= 0` guard was dead code and the entry
    escaped as an open position with a stop but no targets.

    The correct behaviour is neither an orphan nor a refusal: native targets are an
    enhancement, so an unsplittable fill keeps its hard SL and its local exits.
    With TP1 at 25% the smallest splittable size is about four venue steps, which
    is above many legitimate KARA position sizes."""
    client = FakeClient()
    # Coarse step, as on real ETHUSDT: the smallest notional-valid size cannot be
    # cut into a 25% TP1 slice that still clears the step.
    coarse = dict(SPEC_RAW, lotSizeFilter={
        "qtyStep": "0.05", "minOrderQty": "0.05", "minNotionalValue": "5",
    })
    registry = BybitSymbolRegistry()
    registry.load([coarse])
    executor = BybitExecutor(
        chat_id="1",
        client=client,
        risk_manager=FakeRisk(),
        symbol_registry=registry,
        price_bridge=HyperliquidBybitPriceBridge(0.003),
        fill_timeout_s=0.1,
        poll_interval_s=0,
    )
    executor.risk.calculate_position_size = lambda signal, equity: (100, 0.05, 10)

    position = await executor.open_position(make_signal())

    assert position is not None
    assert position.native_tp_state == "none"
    assert position.native_tp1_order_id == ""
    assert client.protections[0]["stop_loss"] == 99.2
    assert getattr(client, "partial_protections", []) == []
    assert [order for order in client.orders if order.get("reduce_only")] == []
    assert executor.live_status(position.position_id).value == "open_protected"

    # A 25% slice of a one-step position rounds below the venue step. The partial
    # is skipped rather than escalated into a full exit, and the stop still holds.
    executor.risk.check_tp_trail = lambda *_, **__: {
        "action": "tp1", "close_ratio": 0.25, "trigger_price": 101.2,
    }
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.05, position.entry_price, 10, stop_loss=99.2)
    ]
    assert await executor.update_positions({"BTC": 101.2}) == []
    assert [order for order in client.orders if order.get("reduce_only")] == []
    assert position.status == PositionStatus.OPEN

    # A full exit on the same position must still work.
    executor.risk.check_tp_trail = lambda *_, **__: {
        "action": "time_exit", "close_ratio": 1.0, "trigger_price": 101.2,
    }
    actions = await executor.update_positions({"BTC": 101.2})
    assert [item["action"] for item in actions] == ["time_exit"]
    assert actions[0]["fully_closed"] is True


@pytest.mark.asyncio
async def test_target_ids_match_when_venue_reformats_trigger_price():
    """Bybit formats the trigger price itself. Exact float equality would silently
    lose attribution; matching allows half a tick."""
    client = FakeClient()

    async def reformatted(symbol, *, order_filter="StopOrder"):
        return [
            {"orderId": "tp-a", "stopOrderType": "PartialTakeProfit",
             "tpslMode": "Partial", "triggerPrice": "101.2000", "qty": "0.025"},
            {"orderId": "tp-b", "stopOrderType": "PartialTakeProfit",
             "tpslMode": "Partial", "triggerPrice": "102.2000", "qty": "0.037"},
        ]

    client.get_open_orders = reformatted
    executor = make_executor(client)
    position = await executor.open_position(make_signal())

    assert position.native_tp1_order_id == "tp-a"
    assert position.native_tp2_order_id == "tp-b"


@pytest.mark.asyncio
async def test_two_targets_never_share_one_order_id():
    """A single venue row must not be credited to both TP1 and TP2."""
    client = FakeClient()

    async def one_row(symbol, *, order_filter="StopOrder"):
        return [
            {"orderId": "tp-only", "stopOrderType": "PartialTakeProfit",
             "tpslMode": "Partial", "triggerPrice": "101.2", "qty": "0.025"},
        ]

    client.get_open_orders = one_row
    executor = make_executor(client)
    position = await executor.open_position(make_signal())

    assert position.native_tp1_order_id == "tp-only"
    assert position.native_tp2_order_id == ""


def make_meta_signal(key="scalper_BTC_long_s65_71"):
    return make_signal().model_copy(update={"meta_pattern_key": key})


class MetaPersistence(FakePersistence):
    def __init__(self):
        super().__init__()
        self.meta_calls = []
        self.stats = {}

    def update_meta_pattern_outcome(self, pattern_key, pnl_usd, alpha=0.20):
        self.meta_calls.append((pattern_key, pnl_usd))
        row = self.stats.setdefault(pattern_key, {"samples": 0})
        row["samples"] += 1

    def get_meta_pattern_stats(self, pattern_key):
        return self.stats.get(pattern_key)


@pytest.mark.asyncio
async def test_local_full_close_feeds_meta_pattern_once():
    """Regression: live execution moved to Bybit but the meta feedback call did
    not move with it, so meta_pattern_stats stayed empty forever."""
    client = FakeClient()
    persistence = MetaPersistence()
    executor = make_executor(client, persistence=persistence)
    position = await executor.open_position(make_meta_signal())
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.1, position.entry_price, 10, stop_loss=99.2)
    ]

    result = await executor.close_position(position.position_id, 101.0, reason="tp2")

    assert result["fully_closed"] is True
    assert len(persistence.meta_calls) == 1
    key, pnl = persistence.meta_calls[0]
    assert key == position.meta_pattern_key
    assert pnl == pytest.approx(position.pnl_realized)

    trade = persistence.trades[-1][1]
    assert trade["meta_pattern_key"] == position.meta_pattern_key
    assert trade["signal_id"] == position.signal_id

    # Idempotent: a later settlement attempt must not double count.
    executor._record_meta_outcome(position)
    assert len(persistence.meta_calls) == 1


@pytest.mark.asyncio
async def test_venue_stop_out_is_booked_as_a_loss_and_feeds_meta():
    """A native SL closes at the venue with no local fill. Without this the loss
    reached neither trade history nor meta learning, biasing win rate upward."""
    client = FakeClient()
    persistence = MetaPersistence()
    executor = make_executor(client, persistence=persistence)
    position = await executor.open_position(make_meta_signal())

    async def closed_pnl(symbol, *, start_ms=None, limit=50):
        return [{"closedPnl": "-4.25", "qty": "0.1", "avgExitPrice": "99.2"}]

    client.get_closed_pnl = closed_pnl
    client.positions = []  # venue stop fired; position is gone

    await executor.reconcile()

    assert position.status == PositionStatus.CLOSED
    assert position.pnl_realized == pytest.approx(-4.25)
    assert len(persistence.meta_calls) == 1
    assert persistence.meta_calls[0] == (position.meta_pattern_key, pytest.approx(-4.25))
    trade = persistence.trades[-1][1]
    assert trade["reason"] == "venue_stop_or_external_close"
    assert trade["pnl"] == pytest.approx(-4.25)
    assert trade["fully_closed"] is True
    assert trade["meta_pattern_key"] == position.meta_pattern_key


@pytest.mark.asyncio
async def test_vanished_position_without_pnl_record_teaches_meta_nothing():
    """Never invent an outcome: an unmeasurable close must not train the model."""
    client = FakeClient()
    persistence = MetaPersistence()
    executor = make_executor(client, persistence=persistence)
    position = await executor.open_position(make_meta_signal())

    async def no_rows(symbol, *, start_ms=None, limit=50):
        return []

    client.get_closed_pnl = no_rows
    client.positions = []

    await executor.reconcile()

    assert position.status == PositionStatus.CLOSED
    assert persistence.meta_calls == []
    assert position.pnl_realized == 0


@pytest.mark.asyncio
async def test_position_without_meta_key_is_never_recorded():
    client = FakeClient()
    persistence = MetaPersistence()
    executor = make_executor(client, persistence=persistence)
    position = await executor.open_position(make_meta_signal())
    position.meta_pattern_key = None

    executor._record_meta_outcome(position)

    assert persistence.meta_calls == []
