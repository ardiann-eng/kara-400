import pytest

from tools.trade_execution_audit import extract_signal_metrics, join_trade_rows


def test_extracts_existing_rsi_and_orderbook_imbalance_from_signal_reasons():
    metrics = extract_signal_metrics({
        "breakdown": {
            "orderbook_score": 6,
            "reasons": [
                "RSI overbought (66.2) → sell signal",
                "Strong bid wall (imbalance 0.73) → LONG",
            ],
        }
    })

    assert metrics == {
        "entry_rsi": 66.2,
        "entry_orderbook_imbalance": 0.73,
        "entry_orderbook_score": 6,
    }


def test_joins_signal_context_to_multi_exit_position_once():
    signals = [{
        "signal_id": "S1", "score": 70, "regime": "normal", "entry_price": 100,
        "breakdown": {"orderbook_score": 2, "reasons": ["RSI (55.0)"]},
    }]
    trades = [
        {
            "pos_id": "P1:slice:1", "signal_id": "S1", "asset": "BTC",
            "side": "long", "pnl_slice": 1, "entry_fee_allocated": 0.1,
            "exit_fee_paid": 0.2, "fully_closed": False,
        },
        {
            "pos_id": "P1", "signal_id": "S1", "asset": "BTC", "side": "long",
            "reason": "time_exit", "pnl_slice": -0.5, "entry_fee_allocated": 0.3,
            "exit_fee_paid": 0.4, "fully_closed": True, "entry_spread_pct": 0.0004,
        },
    ]

    rows = join_trade_rows(signals, trades)

    assert len(rows) == 1
    assert rows[0]["entry_rsi"] == 55.0
    assert rows[0]["entry_spread_pct"] == 0.0004
    assert rows[0]["entry_fee_paid"] == 0.4
    assert rows[0]["exit_fee_paid"] == pytest.approx(0.6)
    assert rows[0]["pnl_total"] == 0.5


def test_missing_orderbook_imbalance_stays_missing_not_zero():
    metrics = extract_signal_metrics({
        "breakdown": {"orderbook_score": 0, "reasons": ["RSI neutral (50.0)"]}
    })

    assert metrics["entry_rsi"] == 50.0
    assert metrics["entry_orderbook_imbalance"] is None
    assert metrics["entry_orderbook_score"] == 0
