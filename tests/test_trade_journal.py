from notify.telegram import collapse_trade_lifecycles


def test_trade_journal_counts_position_once_and_keeps_partial_only_unresolved():
    rows = [
        {
            "pos_id": "POS-1",
            "reason": "trailing_stop",
            "pnl": 3.0,
            "fully_closed": True,
        },
        {
            "pos_id": "POS-1:slice:2",
            "reason": "tp2",
            "pnl": 2.0,
            "fully_closed": False,
        },
        {
            "pos_id": "POS-1:slice:1",
            "reason": "tp1",
            "pnl": 1.0,
            "fully_closed": False,
        },
        {
            "pos_id": "POS-2:slice:1",
            "reason": "tp1",
            "pnl": 0.5,
            "fully_closed": False,
        },
    ]

    closed, unresolved = collapse_trade_lifecycles(rows)

    assert len(closed) == 1
    assert closed[0]["reason"] == "trailing_stop"
    assert closed[0]["pnl"] == 6.0
    assert len(unresolved) == 1
    assert unresolved[0]["pnl"] == 0.5


def test_trade_journal_treats_legacy_non_slice_row_as_closed():
    closed, unresolved = collapse_trade_lifecycles([
        {"pos_id": "LEGACY-1", "reason": "time_exit", "pnl": -1.0}
    ])

    assert len(closed) == 1
    assert closed[0]["pnl"] == -1.0
    assert unresolved == []
