"""Bybit-only live entry limits layered above strategy risk settings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

from models.schemas import Position, Side, TradeSignal


class LiveRiskViolation(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ExecutionQuote:
    symbol: str
    mark_price: float
    best_bid: float
    best_ask: float
    spread_pct: float
    estimated_fill_price: float
    estimated_slippage_pct: float
    available_quantity: float
    received_at: datetime


@dataclass(frozen=True)
class LiveRiskLimits:
    max_leverage: int
    max_positions: int
    max_risk_per_trade_pct: float
    max_total_open_risk_pct: float
    max_symbol_notional_pct: float
    max_total_notional_pct: float
    max_signal_age_s: float
    max_quote_age_s: float
    max_spread_pct: float
    max_slippage_pct: float
    min_depth_ratio: float


class BybitLiveRiskGate:
    def __init__(self, limits: LiveRiskLimits):
        self.limits = limits

    def validate(
        self,
        *,
        signal: TradeSignal,
        equity: float,
        quantity: float,
        leverage: int,
        quote: ExecutionQuote,
        open_positions: Sequence[Position],
    ) -> None:
        limits = self.limits
        if equity <= 0:
            raise LiveRiskViolation("invalid_equity")
        if leverage > limits.max_leverage:
            raise LiveRiskViolation("leverage_cap")
        if len(open_positions) >= limits.max_positions:
            raise LiveRiskViolation("max_live_positions")

        now = datetime.now(timezone.utc)
        signal_time = self._utc(signal.timestamp)
        quote_time = self._utc(quote.received_at)
        if (now - signal_time).total_seconds() > limits.max_signal_age_s:
            raise LiveRiskViolation("stale_signal_price")
        if (now - quote_time).total_seconds() > limits.max_quote_age_s:
            raise LiveRiskViolation("stale_bybit_quote")
        if quote.spread_pct > limits.max_spread_pct:
            raise LiveRiskViolation("spread_limit")
        if quote.estimated_slippage_pct > limits.max_slippage_pct:
            raise LiveRiskViolation("slippage_limit")
        if quote.available_quantity < quantity * limits.min_depth_ratio:
            raise LiveRiskViolation("insufficient_orderbook_depth")

        entry_notional = quantity * quote.estimated_fill_price
        stop_distance_pct = abs(signal.entry_price - signal.stop_loss) / signal.entry_price
        entry_risk = entry_notional * stop_distance_pct
        if entry_risk > equity * limits.max_risk_per_trade_pct:
            raise LiveRiskViolation("per_trade_risk_cap")
        if entry_notional > equity * limits.max_symbol_notional_pct:
            raise LiveRiskViolation("symbol_notional_cap")

        total_notional = entry_notional + sum(self._notional(item) for item in open_positions)
        if total_notional > equity * limits.max_total_notional_pct:
            raise LiveRiskViolation("total_notional_cap")
        total_risk = entry_risk + sum(self._risk(item) for item in open_positions)
        if total_risk > equity * limits.max_total_open_risk_pct:
            raise LiveRiskViolation("total_open_risk_cap")

    @staticmethod
    def _notional(position: Position) -> float:
        return position.size_current * position.entry_price

    @classmethod
    def _risk(cls, position: Position) -> float:
        return cls._notional(position) * abs(
            position.entry_price - position.stop_loss
        ) / max(position.entry_price, 1e-12)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
