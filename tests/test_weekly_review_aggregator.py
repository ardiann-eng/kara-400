"""
Unit test for weekly_review aggregator. Uses a small in-memory fixture DataFrame
so no Excel file / API is required.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from intelligence.weekly_review import aggregator


def _fixture_df() -> pd.DataFrame:
    # 20 closed trades — mix of sessions, sides, exit reasons.
    rows = []
    base = pd.Timestamp("2026-07-01 00:00:00", tz="UTC")
    # Winners in NY session
    for i in range(6):
        rows.append({
            "Timestamp": base + pd.Timedelta(hours=14, minutes=i),
            "Asset": "BTC", "Side": "LONG", "Action": "close",
            "PnL ($)": 5.0 + i, "PnL (%)": 0.01, "Score": 72,
            "Reason": "tp2", "Mode": "PAPER", "Position ID": f"P{i}",
        })
    # Losers in Asia session
    for i in range(8):
        rows.append({
            "Timestamp": base + pd.Timedelta(hours=2, minutes=i),
            "Asset": "SOL", "Side": "SHORT", "Action": "close",
            "PnL ($)": -3.0, "PnL (%)": -0.02, "Score": 63,
            "Reason": "stop_loss", "Mode": "SCALPER", "Position ID": f"Q{i}",
        })
    # Mixed London session
    for i in range(6):
        rows.append({
            "Timestamp": base + pd.Timedelta(hours=10, minutes=i),
            "Asset": "ETH", "Side": "LONG", "Action": "close",
            "PnL ($)": 2.0 if i % 2 else -1.5, "PnL (%)": 0.005, "Score": 68,
            "Reason": "trailing" if i % 2 else "time_exit",
            "Mode": "PAPER", "Position ID": f"R{i}",
        })
    return pd.DataFrame(rows)


def test_session_labeling():
    assert aggregator._label_session(2) == "Asia"
    assert aggregator._label_session(10) == "London"
    assert aggregator._label_session(14) == "London-NY Overlap"
    assert aggregator._label_session(19) == "NY"
    assert aggregator._label_session(23) == "Asia"


def test_wilson_ci_symmetric():
    lo, hi = aggregator._wilson_ci(5, 10)
    assert 0 < lo < 0.5 < hi < 1


def test_wilson_ci_edge_cases():
    assert aggregator._wilson_ci(0, 0) == (0.0, 0.0)
    lo, hi = aggregator._wilson_ci(0, 10)
    assert lo == 0.0
    assert hi > 0
    lo, hi = aggregator._wilson_ci(10, 10)
    assert hi == 1.0


def test_overall_summary():
    df = _fixture_df()
    df = aggregator._normalize_columns(df)
    assert "pnl" in df.columns, "normalize should rename 'PnL ($)' -> 'pnl'"
    summary = aggregator.overall_summary(df)
    assert summary["total_trades"] == 20
    # 6 NY wins + 3 London wins = 9 winners
    assert 0.3 < summary["winrate"] < 0.6
    assert summary["profit_factor"] is not None


def test_bucket_stats_by_session():
    df = _fixture_df()
    df.columns = [c.strip() for c in df.columns]
    df = aggregator._normalize_columns(df)
    # Manually add derived columns aggregator normally derives inside load_closed_trades.
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["hour_utc"] = df["timestamp"].dt.hour
    df["session"] = df["hour_utc"].map(aggregator._label_session)
    df["score_bin"] = df["score"].map(aggregator._score_bin)
    df["hold_bin"] = "unknown"

    stats = aggregator.compute_bucket_stats(df, ["session"])
    assert len(stats) >= 2
    # Asia bucket should be worst
    asia = next(s for s in stats if s.values.get("session") == "Asia")
    assert asia.winrate == 0.0
    assert asia.n == 8


def test_evidence_pack_is_json_serializable():
    import json
    df = _fixture_df()
    df.columns = [c.strip() for c in df.columns]
    df = aggregator._normalize_columns(df)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["hour_utc"] = df["timestamp"].dt.hour
    df["session"] = df["hour_utc"].map(aggregator._label_session)
    df["score_bin"] = df["score"].map(aggregator._score_bin)
    df["hold_bin"] = "unknown"
    pack = aggregator.to_evidence_pack(df)
    # Should serialize without error
    json.dumps(pack, default=str)
    assert pack["overall"]["total_trades"] == 20
    assert "session" in pack["buckets"]
