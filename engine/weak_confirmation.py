"""Next-candle structural confirmation for weak scalper entries."""

from __future__ import annotations

from dataclasses import dataclass

from models.schemas import Side


@dataclass(frozen=True)
class WeakCandidate:
    asset: str
    side: Side
    signal_price: float
    invalidation_price: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    score: int
    candle_time: float
    armed_at: float


@dataclass
class WeakShadowOutcome:
    event_id: str
    candidate: WeakCandidate
    highest_price: float
    lowest_price: float
    tp1_hit: bool = False
    tp2_hit: bool = False
    sl_hit: bool = False

    def observe(self, price: float) -> None:
        self.highest_price = max(self.highest_price, price)
        self.lowest_price = min(self.lowest_price, price)
        item = self.candidate
        if item.side == Side.LONG:
            self.tp1_hit = self.tp1_hit or price >= item.tp1_price
            self.tp2_hit = self.tp2_hit or price >= item.tp2_price
            self.sl_hit = self.sl_hit or price <= item.stop_price
        else:
            self.tp1_hit = self.tp1_hit or price <= item.tp1_price
            self.tp2_hit = self.tp2_hit or price <= item.tp2_price
            self.sl_hit = self.sl_hit or price >= item.stop_price

    def metrics(self, price: float) -> dict:
        item = self.candidate
        if item.side == Side.LONG:
            mfe = self.highest_price / item.signal_price - 1
            mae = self.lowest_price / item.signal_price - 1
            final_return = price / item.signal_price - 1
        else:
            mfe = item.signal_price / self.lowest_price - 1
            mae = item.signal_price / self.highest_price - 1
            final_return = item.signal_price / price - 1
        return {
            "mfe_pct": max(0.0, mfe),
            "mae_pct": min(0.0, mae),
            "final_return_pct": final_return,
            "tp1_hit": self.tp1_hit,
            "tp2_hit": self.tp2_hit,
            "sl_hit": self.sl_hit,
        }


def latest_closed_candle(candles: list, now: float) -> tuple[float, float] | None:
    if not candles:
        return None
    for candle in reversed(candles):
        if not isinstance(candle, dict):
            continue
        try:
            close = float(candle.get("c", 0))
            timestamp = float(candle.get("t", candle.get("T", 0)))
        except (TypeError, ValueError):
            continue
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        if close > 0 and timestamp > 0 and timestamp + 60 <= now:
            return timestamp, close
    return None


def evaluate_weak_confirmation(
    candidate: WeakCandidate,
    *,
    current_side: Side,
    structure: str,
    candle_time: float,
    close_price: float,
    now: float,
    timeout_seconds: float,
) -> str:
    if now - candidate.armed_at > timeout_seconds:
        return "expired"
    if current_side != candidate.side:
        return "rejected_side_flip"
    if candle_time <= candidate.candle_time:
        return "waiting_next_candle"

    expected_structure = "bull" if candidate.side == Side.LONG else "bear"
    if structure != expected_structure:
        return "rejected_structure"

    if candidate.side == Side.LONG:
        if close_price <= candidate.invalidation_price:
            return "rejected_invalidation"
        if close_price <= candidate.signal_price:
            return "waiting_follow_through"
        if close_price >= candidate.tp1_price:
            return "rejected_chase"
    else:
        if close_price >= candidate.invalidation_price:
            return "rejected_invalidation"
        if close_price >= candidate.signal_price:
            return "waiting_follow_through"
        if close_price <= candidate.tp1_price:
            return "rejected_chase"
    return "confirmed"
