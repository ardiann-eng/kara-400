import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from engine.weak_confirmation import bull_exhaustion_short_level
import config
from models.schemas import MarketRegime, Side
from engine.scoring_engine import ScoringEngine


NOW = 1_700_000_000.0


def candles(*, retest=True, latest_closed=True):
    rows = []
    for index in range(24):
        close = 100.0 + index * 0.1
        rows.append({
            "t": (NOW - (24 - index) * 60) * 1000,
            "o": str(close - 0.05), "h": str(close + 0.10),
            "l": str(close - 0.10), "c": str(close),
        })
    # Keep three candles before latest above prior resistance unless retest sets one.
    for index in (-4, -3, -2):
        rows[index].update({"o": "102.35", "h": "102.45", "l": "102.30", "c": "102.35"})
    if retest:
        rows[-2].update({"o": "102.05", "h": "102.10", "l": "101.95", "c": "102.05"})
    rows[-1].update({"o": "102.30", "h": "102.35", "l": "101.80", "c": "101.90"})
    if not latest_closed:
        rows[-1]["t"] = (NOW - 30) * 1000
    return rows


def test_valid_bull_exhaustion_retest_records_resistance():
    assert bull_exhaustion_short_level(
        candles(), now=NOW, mtf_state="bear", retest_candles=3, tolerance=0.0015
    ) == "prior_resistance"


def test_more_than_three_candles_ago_does_not_confirm_retest():
    data = candles(retest=False)
    data[-5].update({"o": "102.05", "h": "102.10", "l": "101.95", "c": "102.05"})
    assert bull_exhaustion_short_level(
        data, now=NOW, mtf_state="bear", retest_candles=3, tolerance=0.0015
    ) is None


def test_neutral_mtf_and_open_latest_candle_fail_closed():
    assert bull_exhaustion_short_level(
        candles(), now=NOW, mtf_state="neutral", retest_candles=3, tolerance=0.0015
    ) is None
    assert bull_exhaustion_short_level(
        candles(latest_closed=False), now=NOW, mtf_state="bear", retest_candles=3, tolerance=0.0015
    ) is None
    without_timestamps = candles()
    for candle in without_timestamps:
        candle.pop("t")
    assert bull_exhaustion_short_level(
        without_timestamps, now=NOW, mtf_state="bear", retest_candles=3, tolerance=0.0015
    ) is None


def test_signal_serializes_strategy_source_and_default_is_native():
    engine = ScoringEngine.__new__(ScoringEngine)
    signal = engine._build_scalper_signal(
        "TEST", Side.SHORT, 70, 100.0, [], MarketRegime.NORMAL, 0, 0.02, 0.031,
        "bull_exhaustion_short",
    )
    assert signal.strategy_source == "bull_exhaustion_short"
    assert signal.model_dump()["strategy_source"] == "bull_exhaustion_short"


def run_scalper(trend_pct, data):
    engine = ScoringEngine.__new__(ScoringEngine)
    engine.client = SimpleNamespace(
        get_mark_price=AsyncMock(return_value=102.0),
        _call_info_endpoint=AsyncMock(return_value=(data, True)),
    )
    engine.candle_sem = asyncio.Semaphore(1)
    engine._expire_weak_candidates = lambda *args: None
    engine._observe_weak_shadows = lambda *args: None
    engine._fetch_15m_mtf_data = AsyncMock(return_value="bear")
    engine._fetch_vol_regime = AsyncMock(return_value=(MarketRegime.NORMAL, 0.02, trend_pct))
    engine._calculate_scalper_score = lambda *args: (70, Side.SHORT, ["native bear score"])
    engine._get_session_bonus = lambda: (0, [])
    engine._apply_meta_learning = lambda *args: (0, "", None)
    engine._asset_concentration_threshold_add = lambda *args: 0
    engine._weak_candidates = {}
    engine._weak_shadow_outcomes = {}
    with patch.object(config.SCALPER, "entry_location_gate_enabled", False):
        return asyncio.run(engine._run_scalper("TEST"))


def test_run_scalper_tags_valid_bull_exhaustion_short():
    with patch("engine.scoring_engine.time.time", return_value=NOW):
        signal, _ = run_scalper(0.031, candles())
    assert signal is not None
    assert signal.strategy_source == "bull_exhaustion_short"
    assert "Bull exhaustion short: rejected prior_resistance" in signal.breakdown.reasons


def test_exact_three_percent_retains_native_scalper_short():
    with patch("engine.scoring_engine.time.time", return_value=NOW):
        signal, _ = run_scalper(
            config.SIGNAL.bull_exhaustion_short_min_trend_pct,
            candles(retest=False),
        )
    assert signal is not None
    assert signal.strategy_source == "native_scalper"


def test_configured_exhaustion_threshold_controls_special_short_path():
    with patch.object(config.SIGNAL, "bull_exhaustion_short_min_trend_pct", 0.031):
        with patch("engine.scoring_engine.time.time", return_value=NOW):
            signal, _ = run_scalper(0.0305, candles(retest=False))
    assert signal is not None
    assert signal.strategy_source == "native_scalper"
