from dataclasses import replace

import pytest

from core.startup_validation import BybitPreflightResult
from execution.exchange_client import (
    ExecutionOrderStatus,
    InstrumentSpec,
    VenueOrder,
    VenueAccount,
    VenuePosition,
)
from models.schemas import Side
from tools.bybit_testnet_drill import (
    DrillSafetyError,
    partial_drill_quantity,
    parse_args,
    read_drill_credentials,
    run_hold_protected,
    run_lifecycle,
    run_missing_sl_emergency_close,
    run_recover_protected,
    run_ws_reconnect_check,
    run_multi_position_close_all,
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
    demo = False
    api_key = "test-account-key"
    _api_secret = "test-account-secret"

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
            testnet=self.testnet,
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

    async def get_account(self):
        return VenueAccount(
            total_equity=100,
            wallet_balance=100,
            available_balance=100,
            used_margin=0,
            unrealized_pnl=0,
        )

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

    async def clear_stop_loss(self, symbol):
        self.calls.append(("clear_stop_loss", symbol))
        self.stop_loss = None


def test_environment_gate_refuses_missing_flag_mainnet_and_full_auto():
    with pytest.raises(DrillSafetyError, match="confirm-testnet"):
        validate_environment(environment="testnet", confirmed=False, full_auto=False)
    with pytest.raises(DrillSafetyError, match="confirm-demo"):
        validate_environment(environment="demo", confirmed=False, full_auto=False)
    with pytest.raises(DrillSafetyError, match="FULL_AUTO"):
        validate_environment(environment="demo", confirmed=True, full_auto=True)


def test_demo_drill_credentials_use_environment_pair_without_prompt():
    prompts = []
    key, secret = read_drill_credentials(
        "demo",
        environ={
            "BYBIT_DEMO_API_KEY": " demo-key ",
            "BYBIT_DEMO_API_SECRET": " demo-secret ",
        },
        prompt=prompts.append,
    )

    assert (key, secret) == ("demo-key", "demo-secret")
    assert prompts == []


def test_drill_credentials_refuse_partial_environment_pair():
    with pytest.raises(DrillSafetyError, match="Set both BYBIT_DEMO_API_KEY"):
        read_drill_credentials(
            "demo",
            environ={"BYBIT_DEMO_API_KEY": "demo-key"},
        )


def test_drill_credentials_fall_back_to_hidden_prompt_when_unset():
    responses = iter(("prompt-key", "prompt-secret"))
    assert read_drill_credentials(
        "demo", environ={}, prompt=lambda label: next(responses)
    ) == ("prompt-key", "prompt-secret")


def test_default_evidence_path_is_environment_specific():
    assert parse_args(
        ["--environment", "demo", "--confirm-demo", "--symbol", "BTC", "--side", "long"]
    ).report is None


def test_hold_and_recover_flags_are_mutually_exclusive():
    args = parse_args(
        ["--confirm-testnet", "--symbol", "BTC", "--side", "long", "--hold-protected"]
    )
    assert args.hold_protected is True
    assert args.recover_protected is False
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--confirm-testnet", "--symbol", "BTC", "--side", "long",
                "--hold-protected", "--recover-protected",
            ]
        )

    with pytest.raises(SystemExit):
        parse_args(
            [
                "--confirm-testnet", "--symbol", "BTC", "--side", "long",
                "--recover-protected", "--ws-reconnect-check",
            ]
        )

    with pytest.raises(SystemExit):
        parse_args(
            [
                "--confirm-testnet", "--symbol", "BTC", "--side", "long",
                "--recover-protected", "--simulate-missing-sl",
            ]
        )


def test_smallest_quantity_satisfies_quantity_and_notional_minimums():
    assert smallest_valid_quantity(SPEC, 60000) == 0.001
    low_price = replace(SPEC, qty_step=1, min_qty=1, min_notional=5)
    assert smallest_valid_quantity(low_price, 2) == 3


def test_partial_drill_quantity_has_two_valid_slices_and_notional_cap():
    assert partial_drill_quantity(SPEC, 60000) == 0.002
    costly = replace(SPEC, min_qty=1, qty_step=1, min_notional=5)
    with pytest.raises(DrillSafetyError, match="exceeds"):
        partial_drill_quantity(costly, 200)


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
    assert evidence.quantity == 0.004
    assert evidence.partial_filled_qty == 0.002
    assert evidence.protected_remainder_size == 0.002
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
async def test_hold_protected_leaves_verified_native_stop_open():
    client = DrillClient()

    evidence = await run_hold_protected(
        client,
        asset="BTC",
        side=Side.LONG,
        confirm=lambda prompt: "TESTNET",
        output=lambda text: None,
    )

    assert evidence.result == "held_protected"
    assert evidence.reconciliation_result == "exchange_protected"
    assert evidence.hard_sl_present is True
    assert evidence.observed_stop_loss > 0
    assert evidence.final_position_size == 0.001
    assert client.position is not None
    places = [call[1] for call in client.calls if isinstance(call, tuple) and call[0] == "place"]
    assert [item["reduce_only"] for item in places] == [False]


@pytest.mark.asyncio
async def test_recover_protected_closes_exact_position_reduce_only():
    client = DrillClient()
    client.position = {"side": Side.SHORT, "size": 0.001}
    client.stop_loss = 60600

    evidence = await run_recover_protected(
        client,
        asset="BTC",
        side=Side.SHORT,
        confirm=lambda prompt: "TESTNET",
        output=lambda text: None,
    )

    assert evidence.result == "passed"
    assert evidence.hard_sl_present is True
    assert evidence.final_position_size == 0
    assert client.position is None
    places = [call[1] for call in client.calls if isinstance(call, tuple) and call[0] == "place"]
    assert len(places) == 1
    assert places[0]["reduce_only"] is True
    assert places[0]["side"] == Side.LONG


@pytest.mark.asyncio
async def test_recovery_refuses_unprotected_position_without_close_order():
    client = DrillClient()
    client.position = {"side": Side.LONG, "size": 0.001}

    with pytest.raises(DrillSafetyError, match="refuses unprotected"):
        await run_recover_protected(
            client,
            asset="BTC",
            side=Side.LONG,
            confirm=lambda prompt: "TESTNET",
            output=lambda text: None,
        )
    assert not any(isinstance(call, tuple) and call[0] == "place" for call in client.calls)


@pytest.mark.asyncio
async def test_missing_sl_drill_detects_removal_and_emergency_closes():
    client = DrillClient()
    client.position = {"side": Side.LONG, "size": 0.001}
    client.stop_loss = 59400

    evidence = await run_missing_sl_emergency_close(
        client,
        asset="BTC",
        side=Side.LONG,
        confirm=lambda prompt: "TESTNET",
        output=lambda text: None,
    )

    assert evidence.result == "passed"
    assert evidence.missing_sl_detected is True
    assert evidence.emergency_close_attempted is True
    assert evidence.final_position_size == 0
    assert client.position is None
    places = [call[1] for call in client.calls if isinstance(call, tuple) and call[0] == "place"]
    assert len(places) == 1
    assert places[0]["reduce_only"] is True


@pytest.mark.asyncio
async def test_missing_sl_drill_refuses_position_that_is_not_protected():
    client = DrillClient()
    client.position = {"side": Side.LONG, "size": 0.001}

    with pytest.raises(DrillSafetyError, match="requires exactly one protected"):
        await run_missing_sl_emergency_close(
            client,
            asset="BTC",
            side=Side.LONG,
            confirm=lambda prompt: "TESTNET",
            output=lambda text: None,
        )
    assert not any(isinstance(call, tuple) and call[0] == "place" for call in client.calls)


@pytest.mark.asyncio
async def test_ws_reconnect_drill_forces_rest_reconciliation_without_close(monkeypatch):
    client = DrillClient()
    client.position = {"side": Side.LONG, "size": 0.001}
    client.stop_loss = 59400
    created = []

    class FakeTransport:
        async def close(self):
            await created[0].on_reconnect()

    class FakePrivateWS:
        def __init__(self, **kwargs):
            self.on_reconnect = kwargs["on_reconnect"]
            self.connected = False
            self._ws = None
            created.append(self)

        async def start(self):
            self.connected = True
            self._ws = FakeTransport()

        async def stop(self):
            self.connected = False

    monkeypatch.setattr("tools.bybit_testnet_drill.BybitPrivateWebSocket", FakePrivateWS)
    evidence = await run_ws_reconnect_check(
        client,
        asset="BTC",
        side=Side.LONG,
        confirm=lambda prompt: "TESTNET",
        output=lambda text: None,
    )

    assert evidence.result == "held_protected"
    assert evidence.ws_state == "reconnected"
    assert evidence.ws_reconnect_count == 1
    assert evidence.forced_rest_reconciliation is True
    assert evidence.final_position_size == 0.001
    assert client.position is not None
    assert not any(isinstance(call, tuple) and call[0] == "place" for call in client.calls)


@pytest.mark.asyncio
async def test_ws_reconnect_drill_refuses_unprotected_position_without_socket(monkeypatch):
    client = DrillClient()
    client.position = {"side": Side.LONG, "size": 0.001}

    with pytest.raises(DrillSafetyError, match="WS drill requires exactly one protected"):
        await run_ws_reconnect_check(
            client,
            asset="BTC",
            side=Side.LONG,
            confirm=lambda prompt: "TESTNET",
            output=lambda text: None,
        )


@pytest.mark.asyncio
async def test_multi_close_all_closes_two_protected_symbols_reduce_only():
    class MultiRegistry(Registry):
        pass

    class MultiClient(DrillClient):
        def __init__(self):
            super().__init__()
            self.symbol_registry = MultiRegistry()
            self.positions = {}

        async def get_instrument(self, asset):
            if asset == "BTC":
                return SPEC
            if asset == "ETH":
                return replace(SPEC, asset="ETH", symbol="ETHUSDT", qty_step=0.01, min_qty=0.01)
            raise ValueError(asset)

        async def get_mark_price(self, symbol):
            return 2000 if symbol == "ETHUSDT" else 60000

        async def get_positions(self, symbol=None):
            items = []
            for venue_symbol, value in self.positions.items():
                if symbol and venue_symbol != symbol:
                    continue
                items.append(VenuePosition(
                    symbol=venue_symbol,
                    side=value["side"],
                    size=value["size"],
                    entry_price=value["price"],
                    leverage=1,
                    stop_loss=value.get("stop_loss"),
                ))
            return items

        async def place_order(self, **kwargs):
            self.calls.append(("place", kwargs))
            symbol = kwargs["symbol"]
            order = VenueOrder(
                order_id="oid",
                client_order_id=kwargs["client_order_id"],
                symbol=symbol,
                side=kwargs["side"],
                requested_qty=kwargs["quantity"],
                filled_qty=kwargs["quantity"],
                average_fill_price=2000 if symbol == "ETHUSDT" else 60000,
                fee_paid=0.01,
                status=ExecutionOrderStatus.FILLED,
                reduce_only=kwargs["reduce_only"],
            )
            if kwargs["reduce_only"]:
                self.positions[symbol]["size"] -= kwargs["quantity"]
                if self.positions[symbol]["size"] <= 0:
                    del self.positions[symbol]
            else:
                self.positions[symbol] = {
                    "side": kwargs["side"], "size": kwargs["quantity"],
                    "price": order.average_fill_price,
                }
            self.orders[kwargs["client_order_id"]] = order
            return order

        async def set_protection(self, *, symbol, stop_loss, **kwargs):
            self.positions[symbol]["stop_loss"] = stop_loss

    client = MultiClient()
    evidence = await run_multi_position_close_all(
        client, confirm=lambda prompt: "TESTNET", output=lambda text: None
    )

    assert evidence.result == "passed"
    assert evidence.hard_sl_present is True
    assert evidence.close_all_closed_symbols == ["BTCUSDT", "ETHUSDT"]
    assert evidence.final_position_size == 0
    places = [call[1] for call in client.calls if isinstance(call, tuple) and call[0] == "place"]
    assert [item["symbol"] for item in places] == ["BTCUSDT", "ETHUSDT", "BTCUSDT", "ETHUSDT"]
    assert [item["reduce_only"] for item in places] == [False, False, True, True]


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


@pytest.mark.asyncio
async def test_demo_lifecycle_requires_demo_confirmation_and_labels_evidence():
    client = DrillClient()
    client.testnet = False
    client.demo = True
    output = []

    evidence = await run_lifecycle(
        client,
        asset="BTC",
        side=Side.LONG,
        partial_close=False,
        environment="demo",
        confirm=lambda prompt: "DEMO",
        output=output.append,
    )

    assert evidence.result == "passed"
    assert evidence.environment == "BYBIT DEMO TRADING"
    assert "BYBIT DEMO TRADING" in output[0]


@pytest.mark.asyncio
async def test_zero_balance_refuses_before_operator_confirmation_or_order():
    client = DrillClient()

    async def no_funds_preflight():
        return BybitPreflightResult(
            credentials_valid=True,
            can_read_account=True,
            can_trade_contracts=True,
            withdrawal_enabled=False,
            account_type="UNIFIED",
            position_mode="one_way",
            testnet=True,
            available_usdt=0,
        )

    client.preflight = no_funds_preflight
    with pytest.raises(DrillSafetyError, match="available USDT must be positive"):
        await run_lifecycle(
            client,
            asset="BTC",
            side=Side.LONG,
            partial_close=False,
            confirm=lambda prompt: "TESTNET",
        )
    assert not any(isinstance(call, tuple) and call[0] == "place" for call in client.calls)
