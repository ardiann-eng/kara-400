from dataclasses import replace

import pytest

from core.startup_validation import BybitPreflightResult
from execution.exchange_client import (
    ExecutionOrderStatus,
    InstrumentSpec,
    VenueOrder,
    VenuePosition,
)
from models.schemas import Side
from tools.bybit_testnet_drill import (
    DrillSafetyError,
    run_lifecycle,
    smallest_valid_quantity,
    validate_environment,
)


SPEC = InstrumentSpec(
    asset="BTC",
    symbol="BTCUSDT",
    tick_size=0.1,
    qty_step=0.001,
    min_qty=0.001,
    min_notional=5,
    max_leverage=20,
)


class Registry:
    def normalize_price(self, spec, price):
        return round(price, 1)

    def normalize_quantity(self, spec, quantity):
        steps = int(quantity / spec.qty_step)
        return steps * spec.qty_step


class DrillClient:
    testnet = True
    api_key = "test-account-key"

    def __init__(self, *, protection_error=False, cleanup_error=False, spec=SPEC):
        self.symbol_registry = Registry()
        self.spec = spec
        self.position = None
        self.stop_loss = None
        self.orders = {}
        self.calls = []
        self.protection_error = protection_error
        self.cleanup_error = cleanup_error

    async def connect(self):
        self.calls.append("connect")

    async def sync_clock(self):
        self.calls.append("sync_clock")

    async def load_instruments(self):
        return 1

    async def preflight(self):
        return BybitPreflightResult(
            credentials_valid=True,
            can_read_account=True,
            can_trade_contracts=True,
            withdrawal_enabled=False,
            account_type="UNIFIED",
            position_mode="one_way",
            testnet=True,
            available_usdt=100,
        )

    async def get_instrument(self, asset):
        if asset != "BTC":
            raise ValueError(asset)
        return self.spec

    async def get_mark_price(self, symbol):
        return 60000

    async def get_positions(self, symbol=None):
        if not self.position:
            return []
        return [VenuePosition(
            symbol="BTCUSDT",
            side=self.position["side"],
            size=self.position["size"],
            entry_price=60000,
            leverage=1,
            stop_loss=self.stop_loss,
        )]

    async def set_leverage(self, symbol, leverage):
        self.calls.append(("leverage", symbol, leverage))

    async def place_order(self, **kwargs):
        self.calls.append(("place", kwargs))
        order = VenueOrder(
            order_id="oid",
            client_order_id=kwargs["client_order_id"],
            symbol=kwargs["symbol"],
            side=kwargs["side"],
            requested_qty=kwargs["quantity"],
            filled_qty=kwargs["quantity"],
            average_fill_price=60000,
            fee_paid=0.01,
            status=ExecutionOrderStatus.FILLED,
            reduce_only=kwargs["reduce_only"],
        )
        if kwargs["reduce_only"]:
            if self.cleanup_error:
                order = replace(
                    order,
                    status=ExecutionOrderStatus.REJECTED,
                    filled_qty=0,
                )
            else:
                self.position["size"] -= kwargs["quantity"]
                if self.position["size"] <= 0:
                    self.position = None
                    self.stop_loss = None
        else:
            self.position = {"side": kwargs["side"], "size": kwargs["quantity"]}
        self.orders[kwargs["client_order_id"]] = order
        return order

    async def get_order(self, symbol, client_order_id):
        return self.orders[client_order_id]

    async def set_protection(self, **kwargs):
        if self.protection_error:
            raise RuntimeError("SL rejected")
        self.stop_loss = kwargs["stop_loss"]


def test_environment_gate_refuses_missing_flag_mainnet_and_full_auto():
    with pytest.raises(DrillSafetyError, match="confirm-testnet"):
        validate_environment(confirm_testnet=False, testnet=True, full_auto=False)
    with pytest.raises(DrillSafetyError, match="mainnet"):
        validate_environment(confirm_testnet=True, testnet=False, full_auto=False)
    with pytest.raises(DrillSafetyError, match="FULL_AUTO"):
        validate_environment(confirm_testnet=True, testnet=True, full_auto=True)


def test_smallest_quantity_satisfies_quantity_and_notional_minimums():
    assert smallest_valid_quantity(SPEC, 60000) == 0.001
    low_price = replace(SPEC, qty_step=1, min_qty=1, min_notional=5)
    assert smallest_valid_quantity(low_price, 2) == 3


@pytest.mark.asyncio
async def test_lifecycle_installs_stop_partially_closes_and_finishes_zero():
    client = DrillClient(spec=replace(SPEC, min_notional=100))
    output = []

    evidence = await run_lifecycle(
        client,
        asset="BTC",
        side=Side.LONG,
        partial_close=True,
        confirm=lambda prompt: "TESTNET",
        output=output.append,
    )

    assert evidence.result == "passed"
    assert evidence.hard_sl_present is True
    assert evidence.final_position_size == 0
    assert evidence.entry_order_link_id_masked
    assert "test-account-key" not in output[0]
    places = [call[1] for call in client.calls if isinstance(call, tuple) and call[0] == "place"]
    assert [item["reduce_only"] for item in places] == [False, True, True]


@pytest.mark.asyncio
async def test_stop_failure_still_cleans_up_position():
    client = DrillClient(protection_error=True)

    evidence = await run_lifecycle(
        client,
        asset="BTC",
        side=Side.SHORT,
        partial_close=False,
        confirm=lambda prompt: "TESTNET",
        output=lambda text: None,
    )

    assert evidence.result == "failed"
    assert evidence.final_position_size == 0
    assert "SL rejected" in evidence.error
    assert client.position is None


@pytest.mark.asyncio
async def test_cleanup_failure_reports_unresolved_position():
    client = DrillClient(protection_error=True, cleanup_error=True)

    evidence = await run_lifecycle(
        client,
        asset="BTC",
        side=Side.LONG,
        partial_close=False,
        confirm=lambda prompt: "TESTNET",
        output=lambda text: None,
    )

    assert evidence.result == "failed"
    assert evidence.reconciliation_result == "unresolved_position"
    assert evidence.final_position_size == 0.001
    assert "Cleanup close was not filled" in evidence.error


@pytest.mark.asyncio
async def test_unknown_asset_and_operator_rejection_submit_no_order():
    client = DrillClient()
    with pytest.raises(DrillSafetyError, match="BTC and ETH"):
        await run_lifecycle(
            client,
            asset="SOL",
            side=Side.LONG,
            partial_close=False,
        )
    assert not any(isinstance(call, tuple) and call[0] == "place" for call in client.calls)

    with pytest.raises(DrillSafetyError, match="confirmation rejected"):
        await run_lifecycle(
            client,
            asset="BTC",
            side=Side.LONG,
            partial_close=False,
            confirm=lambda prompt: "NO",
            output=lambda text: None,
        )
    assert not any(isinstance(call, tuple) and call[0] == "place" for call in client.calls)
