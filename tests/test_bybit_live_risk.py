from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from execution.live_risk_gate import (
    BybitLiveRiskGate,
    ExecutionQuote,
    LiveRiskLimits,
    LiveRiskViolation,
)
from models.schemas import Position, Side
from tests.test_bybit_executor import make_signal


def limits(**changes):
    base = LiveRiskLimits(
        max_leverage=20,
        max_positions=3,
        max_risk_per_trade_pct=0.035,
        max_total_open_risk_pct=0.105,
        max_symbol_notional_pct=7.0,
        max_total_notional_pct=21.0,
        max_signal_age_s=30,
        max_quote_age_s=5,
        max_spread_pct=0.0015,
        max_slippage_pct=0.002,
        min_depth_ratio=1.0,
    )
    return replace(base, **changes)


def quote(**changes):
    base = ExecutionQuote(
        symbol="BTCUSDT",
        mark_price=100,
        best_bid=99.95,
        best_ask=100.05,
        spread_pct=0.001,
        estimated_fill_price=100.05,
        estimated_slippage_pct=0.0005,
        available_quantity=100,
        received_at=datetime.now(timezone.utc),
    )
    return replace(base, **changes)


def validate(*, custom_limits=None, custom_quote=None, signal=None, positions=(), quantity=1, leverage=20):
    BybitLiveRiskGate(custom_limits or limits()).validate(
        signal=signal or make_signal(),
        equity=1000,
        quantity=quantity,
        leverage=leverage,
        quote=custom_quote or quote(),
        open_positions=list(positions),
    )


def assert_rejected(reason, **kwargs):
    with pytest.raises(LiveRiskViolation) as error:
        validate(**kwargs)
    assert error.value.reason == reason


def test_current_scalper_aligned_limits_accept_safe_btc_entry():
    validate()


def test_scanned_asset_leverage_and_position_caps():
    signal = make_signal()
    signal.asset = "ARB"
    validate(signal=signal)
    assert_rejected("leverage_cap", leverage=21)
    positions = [object(), object(), object()]
    assert_rejected("max_live_positions", positions=positions)


def test_signal_and_quote_freshness_guards():
    signal = make_signal()
    signal.timestamp = datetime.now(timezone.utc) - timedelta(seconds=31)
    assert_rejected("stale_signal_price", signal=signal)
    stale = quote(received_at=datetime.now(timezone.utc) - timedelta(seconds=6))
    assert_rejected("stale_bybit_quote", custom_quote=stale)


def test_spread_slippage_and_depth_guards():
    assert_rejected("spread_limit", custom_quote=quote(spread_pct=0.0016))
    assert_rejected(
        "slippage_limit", custom_quote=quote(estimated_slippage_pct=0.0021)
    )
    assert_rejected(
        "insufficient_orderbook_depth",
        custom_quote=quote(available_quantity=0.9),
    )


def test_per_trade_risk_and_symbol_notional_caps():
    assert_rejected(
        "per_trade_risk_cap",
        custom_limits=limits(max_risk_per_trade_pct=0.0005),
    )
    assert_rejected(
        "symbol_notional_cap",
        custom_limits=limits(max_symbol_notional_pct=0.05),
    )


def open_position(*, size=1, stop_loss=99):
    return Position(
        position_id="existing",
        asset="ETH",
        side=Side.LONG,
        entry_price=100,
        size_initial=size,
        size_current=size,
        leverage=20,
        margin_usd=size * 5,
        stop_loss=stop_loss,
        tp1=101,
        tp2=102,
        is_paper=False,
    )


def test_total_notional_and_open_risk_caps():
    assert_rejected(
        "total_notional_cap",
        custom_limits=limits(max_total_notional_pct=0.15),
        positions=[open_position()],
    )
    assert_rejected(
        "total_open_risk_cap",
        custom_limits=limits(max_total_open_risk_pct=0.0015),
        positions=[open_position()],
    )
