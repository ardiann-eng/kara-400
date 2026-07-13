import pytest

from execution.price_bridge import HyperliquidBybitPriceBridge, PriceBridgeError
from execution.symbol_registry import BybitSymbolRegistry, UnknownBybitSymbol
from models.schemas import Side


INSTRUMENTS = [
    {
        "symbol": "BTCUSDT",
        "baseCoin": "BTC",
        "settleCoin": "USDT",
        "status": "Trading",
        "contractType": "LinearPerpetual",
        "priceFilter": {"tickSize": "0.10"},
        "lotSizeFilter": {
            "qtyStep": "0.001",
            "minOrderQty": "0.001",
            "minNotionalValue": "5",
        },
        "leverageFilter": {"maxLeverage": "100"},
    },
    {
        "symbol": "1000BONKUSDT",
        "baseCoin": "1000BONK",
        "settleCoin": "USDT",
        "status": "Trading",
        "contractType": "LinearPerpetual",
        "priceFilter": {"tickSize": "0.000001"},
        "lotSizeFilter": {
            "qtyStep": "1",
            "minOrderQty": "1",
            "minNotionalValue": "5",
        },
        "leverageFilter": {"maxLeverage": "25"},
    },
]


def test_registry_resolves_metadata_and_explicit_alias():
    registry = BybitSymbolRegistry({"kBONK": "1000BONKUSDT"})
    registry.load(INSTRUMENTS)

    assert registry.resolve("BTC").symbol == "BTCUSDT"
    assert registry.resolve("kBONK").symbol == "1000BONKUSDT"


def test_registry_does_not_guess_unknown_symbol():
    registry = BybitSymbolRegistry()
    registry.load(INSTRUMENTS)

    with pytest.raises(UnknownBybitSymbol):
        registry.resolve("NOTREAL")


def test_registry_normalizes_qty_price_and_notional():
    registry = BybitSymbolRegistry()
    registry.load(INSTRUMENTS)
    spec = registry.resolve("BTC")

    qty = registry.normalize_quantity(spec, 0.0019)
    price = registry.normalize_price(spec, 100.06)

    assert qty == 0.001
    assert price == 100.1
    with pytest.raises(ValueError, match="Notional"):
        registry.validate_notional(spec, qty, price)


def test_bridge_rebases_long_levels_on_bybit_price():
    bridge = HyperliquidBybitPriceBridge(0.003)
    levels = bridge.bridge_levels(
        side=Side.LONG,
        reference_price=100.0,
        execution_price=100.1,
        stop_loss=99.2,
        tp1=100.45,
        tp2=100.75,
    )

    assert levels.stop_loss == pytest.approx(99.2992)
    assert levels.tp1 == pytest.approx(100.55045)
    assert levels.tp2 == pytest.approx(100.85075)


def test_bridge_rebases_short_levels_and_rejects_large_gap():
    bridge = HyperliquidBybitPriceBridge(0.003)
    levels = bridge.bridge_levels(
        side=Side.SHORT,
        reference_price=100.0,
        execution_price=99.9,
        stop_loss=100.8,
        tp1=99.55,
        tp2=99.25,
    )
    assert levels.stop_loss == pytest.approx(100.6992)
    assert levels.tp1 == pytest.approx(99.45045)

    with pytest.raises(PriceBridgeError, match="price gap"):
        bridge.bridge_levels(
            side=Side.LONG,
            reference_price=100.0,
            execution_price=101.0,
            stop_loss=99.0,
            tp1=101.0,
            tp2=102.0,
        )
