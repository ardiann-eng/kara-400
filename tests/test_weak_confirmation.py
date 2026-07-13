from engine.weak_confirmation import (
    WeakCandidate,
    WeakShadowOutcome,
    evaluate_weak_confirmation,
    latest_closed_candle,
)
from models.schemas import Side


def candidate(side: Side = Side.LONG) -> WeakCandidate:
    return WeakCandidate(
        asset="BTC",
        side=side,
        signal_price=100.0,
        invalidation_price=99.5 if side == Side.LONG else 100.5,
        stop_price=99.2 if side == Side.LONG else 100.8,
        tp1_price=100.45 if side == Side.LONG else 99.55,
        tp2_price=100.75 if side == Side.LONG else 99.25,
        score=64,
        candle_time=1_000.0,
        armed_at=1_000.0,
    )


def evaluate(item: WeakCandidate, **overrides) -> str:
    values = {
        "current_side": item.side,
        "structure": "bull" if item.side == Side.LONG else "bear",
        "candle_time": 1_060.0,
        "close_price": 100.2 if item.side == Side.LONG else 99.8,
        "now": 1_060.0,
        "timeout_seconds": 150.0,
    }
    values.update(overrides)
    return evaluate_weak_confirmation(item, **values)


def test_waits_for_a_new_candle():
    assert evaluate(candidate(), candle_time=1_000.0) == "waiting_next_candle"


def test_confirms_long_and_short_follow_through():
    assert evaluate(candidate(Side.LONG)) == "confirmed"
    assert evaluate(candidate(Side.SHORT)) == "confirmed"


def test_rejects_side_flip_and_structure_failure():
    assert evaluate(candidate(), current_side=Side.SHORT) == "rejected_side_flip"
    assert evaluate(candidate(), structure="neutral") == "rejected_structure"


def test_waits_when_new_candle_has_no_follow_through():
    assert evaluate(candidate(), close_price=99.9) == "waiting_follow_through"


def test_rejects_broken_invalidation_and_late_chase():
    assert evaluate(candidate(), close_price=99.4) == "rejected_invalidation"
    assert evaluate(candidate(), close_price=100.5) == "rejected_chase"


def test_expires_after_two_candle_window():
    assert evaluate(candidate(), now=1_151.0) == "expired"


def test_latest_closed_candle_skips_open_candle_and_normalizes_milliseconds():
    candles = [
        {"t": 1_700_000_000_000, "c": "100.25"},
        {"t": 1_700_000_060_000, "c": "100.50"},
    ]
    assert latest_closed_candle(candles, 1_700_000_100.0) == (
        1_700_000_000.0,
        100.25,
    )


def test_shadow_control_tracks_mfe_mae_targets_and_final_return():
    shadow = WeakShadowOutcome("EVENT", candidate(), 100.0, 100.0)
    shadow.observe(100.8)
    shadow.observe(99.1)

    metrics = shadow.metrics(99.8)

    assert round(metrics["mfe_pct"], 4) == 0.008
    assert round(metrics["mae_pct"], 4) == -0.009
    assert round(metrics["final_return_pct"], 4) == -0.002
    assert metrics["tp1_hit"] is True
    assert metrics["tp2_hit"] is True
    assert metrics["sl_hit"] is True
