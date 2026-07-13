"""Validate Hyperliquid signal prices against Bybit execution prices."""

from __future__ import annotations

from dataclasses import dataclass

from models.schemas import Side


class PriceBridgeError(ValueError):
    pass


@dataclass(frozen=True)
class BridgedLevels:
    reference_price: float
    execution_price: float
    price_gap_pct: float
    stop_loss: float
    tp1: float
    tp2: float


class HyperliquidBybitPriceBridge:
    def __init__(self, max_price_gap_pct: float):
        if not 0 < max_price_gap_pct <= 0.02:
            raise ValueError("max_price_gap_pct must be within (0, 0.02]")
        self.max_price_gap_pct = max_price_gap_pct

    def bridge_levels(
        self,
        *,
        side: Side,
        reference_price: float,
        execution_price: float,
        stop_loss: float,
        tp1: float,
        tp2: float,
    ) -> BridgedLevels:
        if min(reference_price, execution_price, stop_loss, tp1, tp2) <= 0:
            raise PriceBridgeError("All bridge prices must be positive")

        gap = abs(execution_price - reference_price) / reference_price
        if gap > self.max_price_gap_pct:
            raise PriceBridgeError(
                f"Hyperliquid/Bybit price gap {gap:.4%} exceeds "
                f"{self.max_price_gap_pct:.4%}"
            )

        if side == Side.LONG:
            if not stop_loss < reference_price < tp1 < tp2:
                raise PriceBridgeError("Invalid LONG level ordering")
        elif not tp2 < tp1 < reference_price < stop_loss:
            raise PriceBridgeError("Invalid SHORT level ordering")

        sl_distance = abs(reference_price - stop_loss) / reference_price
        tp1_distance = abs(tp1 - reference_price) / reference_price
        tp2_distance = abs(tp2 - reference_price) / reference_price

        direction = 1 if side == Side.LONG else -1
        bridged_sl = execution_price * (1 - direction * sl_distance)
        bridged_tp1 = execution_price * (1 + direction * tp1_distance)
        bridged_tp2 = execution_price * (1 + direction * tp2_distance)

        return BridgedLevels(
            reference_price=reference_price,
            execution_price=execution_price,
            price_gap_pct=gap,
            stop_loss=bridged_sl,
            tp1=bridged_tp1,
            tp2=bridged_tp2,
        )
