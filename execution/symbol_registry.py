"""Bybit instrument registry and exchange-safe value normalization."""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Dict, Iterable, Mapping, Optional

from execution.exchange_client import InstrumentSpec


class UnknownBybitSymbol(ValueError):
    pass


class InvalidBybitInstrument(ValueError):
    pass


def _positive_float(value, field: str) -> float:
    number = float(value or 0)
    if number <= 0:
        raise InvalidBybitInstrument(f"{field} must be positive")
    return number


class BybitSymbolRegistry:
    """Resolve Hyperliquid asset names using Bybit instrument metadata."""

    def __init__(self, aliases: Optional[Mapping[str, str]] = None):
        self._aliases = {
            str(asset).upper(): str(symbol).upper()
            for asset, symbol in (aliases or {}).items()
        }
        self._by_asset: Dict[str, InstrumentSpec] = {}
        self._by_symbol: Dict[str, InstrumentSpec] = {}

    def load(self, instruments: Iterable[dict]) -> None:
        by_symbol = {}
        for raw in instruments:
            symbol = str(raw.get("symbol", "")).upper()
            if not symbol or raw.get("status") != "Trading":
                continue
            if raw.get("contractType") not in (None, "LinearPerpetual"):
                continue
            if str(raw.get("settleCoin", "USDT")).upper() != "USDT":
                continue

            price_filter = raw.get("priceFilter") or {}
            lot_filter = raw.get("lotSizeFilter") or {}
            leverage_filter = raw.get("leverageFilter") or {}
            base_coin = str(raw.get("baseCoin", "")).upper()
            if not base_coin:
                continue

            spec = InstrumentSpec(
                asset=base_coin,
                symbol=symbol,
                tick_size=_positive_float(price_filter.get("tickSize"), "tickSize"),
                qty_step=_positive_float(lot_filter.get("qtyStep"), "qtyStep"),
                min_qty=_positive_float(lot_filter.get("minOrderQty"), "minOrderQty"),
                min_notional=_positive_float(
                    lot_filter.get("minNotionalValue", 1), "minNotionalValue"
                ),
                max_leverage=int(
                    _positive_float(leverage_filter.get("maxLeverage", 1), "maxLeverage")
                ),
            )
            by_symbol[symbol] = spec

        self._by_symbol = by_symbol
        self._by_asset = {spec.asset: spec for spec in by_symbol.values()}
        for asset, symbol in self._aliases.items():
            if symbol in by_symbol:
                self._by_asset[asset] = by_symbol[symbol]

    def resolve(self, asset: str) -> InstrumentSpec:
        key = str(asset).upper()
        spec = self._by_asset.get(key)
        if not spec:
            raise UnknownBybitSymbol(f"No active Bybit USDT perpetual for {asset}")
        return spec

    def resolve_symbol(self, symbol: str) -> InstrumentSpec:
        spec = self._by_symbol.get(str(symbol).upper())
        if not spec:
            raise UnknownBybitSymbol(f"Unknown Bybit symbol {symbol}")
        return spec

    def normalize_quantity(self, spec: InstrumentSpec, quantity: float) -> float:
        qty = Decimal(str(quantity))
        step = Decimal(str(spec.qty_step))
        normalized = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step
        value = float(normalized)
        if value < spec.min_qty:
            raise ValueError(f"Quantity {value} below Bybit minimum {spec.min_qty}")
        return value

    def normalize_price(self, spec: InstrumentSpec, price: float) -> float:
        px = Decimal(str(price))
        tick = Decimal(str(spec.tick_size))
        normalized = (px / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
        return float(normalized)

    @staticmethod
    def validate_notional(spec: InstrumentSpec, quantity: float, price: float) -> None:
        notional = quantity * price
        if notional < spec.min_notional:
            raise ValueError(
                f"Notional {notional:.8f} below Bybit minimum {spec.min_notional}"
            )
