from engine.scalper_levels import build_scalper_levels
from models.schemas import ScoreBreakdown, Side, SignalStrength, TradeSignal


def build_scalper_signal(side: Side = Side.LONG):
    stop_loss, tp1, tp2 = build_scalper_levels(
        100.0,
        side,
        0.008,
        0.0045,
        0.0075,
    )
    return TradeSignal(
        signal_id="TEST",
        asset="BTC",
        side=side,
        score=72,
        strength=SignalStrength.MODERATE,
        regime="normal",
        breakdown=ScoreBreakdown(),
        entry_price=100.0,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        suggested_leverage=25,
        realized_vol=0.02,
    )


def test_scalper_localization_preserves_native_horizon_levels():
    signal = build_scalper_signal()
    native_levels = (signal.stop_loss, signal.tp1, signal.tp2)

    signal.localize_for_user("scalper", atr_value=0.01)

    assert (signal.stop_loss, signal.tp1, signal.tp2) == native_levels
    assert signal.trade_mode == "scalper"


def test_scalper_native_levels_match_data_calibrated_horizon_ladder():
    signal = build_scalper_signal()

    sl_pct = abs(signal.stop_loss / signal.entry_price - 1)
    tp1_pct = abs(signal.tp1 / signal.entry_price - 1)
    tp2_pct = abs(signal.tp2 / signal.entry_price - 1)

    assert round(sl_pct, 6) == 0.008
    assert round(tp1_pct, 6) == 0.0045
    assert round(tp2_pct, 6) == 0.0075


def test_authoritative_scalper_levels_are_side_aware():
    long_levels = build_scalper_levels(100.0, Side.LONG, 0.008, 0.0045, 0.0075)
    short_levels = build_scalper_levels(100.0, Side.SHORT, 0.008, 0.0045, 0.0075)

    assert long_levels == (99.2, 100.45, 100.75)
    assert short_levels == (100.8, 99.55, 99.25)


def test_standard_localization_still_uses_atr_levels():
    import config

    signal = build_scalper_signal(side=Side.SHORT)

    signal.localize_for_user("standard", atr_value=0.01)

    effective_sl = max(0.01 * config.RISK.atr_multiplier, config.RISK.default_sl_pct)
    assert signal.stop_loss == round(100.0 * (1 + effective_sl), 8)
    assert signal.tp1 == round(100.0 * (1 - effective_sl * 1.5), 8)
    assert signal.tp2 == round(100.0 * (1 - effective_sl * 2.5), 8)
    assert signal.trade_mode == "standard"
