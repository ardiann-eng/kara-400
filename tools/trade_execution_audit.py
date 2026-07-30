"""Read-only per-trade join of KARA signal context and Bybit execution facts."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from typing import Any, Dict, Iterable, Optional


RSI_PATTERN = re.compile(r"RSI[^()]*\((-?\d+(?:\.\d+)?)\)", re.IGNORECASE)
IMBALANCE_PATTERNS = (
    re.compile(r"imbalance\s+(-?\d+(?:\.\d+)?)", re.IGNORECASE),
    re.compile(r"(?:bid|ask) pressure\s+\((-?\d+(?:\.\d+)?)\)", re.IGNORECASE),
)


def extract_signal_metrics(signal: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Extract existing RSI/OB numbers without inventing missing raw features."""
    breakdown = signal.get("breakdown") or {}
    reasons = [str(item) for item in breakdown.get("reasons") or []]
    text = " | ".join(reasons)
    rsi_match = RSI_PATTERN.search(text)
    imbalance = None
    for pattern in IMBALANCE_PATTERNS:
        match = pattern.search(text)
        if match:
            imbalance = float(match.group(1))
            break
    return {
        "entry_rsi": float(rsi_match.group(1)) if rsi_match else None,
        "entry_orderbook_imbalance": imbalance,
        "entry_orderbook_score": breakdown.get("orderbook_score"),
    }


def join_trade_rows(
    signals: Iterable[Dict[str, Any]], trades: Iterable[Dict[str, Any]]
) -> list[Dict[str, Any]]:
    signal_by_id = {
        str(item.get("signal_id")): item
        for item in signals
        if item.get("signal_id")
    }
    grouped = defaultdict(list)
    for trade in trades:
        pos_id = str(trade.get("pos_id") or "")
        grouped[pos_id.split(":slice:", 1)[0] or pos_id].append(trade)

    rows = []
    for position_id, slices in grouped.items():
        signal_id = next(
            (str(item.get("signal_id")) for item in slices if item.get("signal_id")),
            "",
        )
        signal = signal_by_id.get(signal_id, {})
        final = next(
            (item for item in slices if item.get("fully_closed") is True),
            slices[-1],
        )
        rows.append({
            "position_id": position_id,
            "signal_id": signal_id or None,
            "asset": final.get("asset"),
            "side": final.get("side"),
            "entry_score": final.get("entry_score", signal.get("score")),
            "entry_regime": signal.get("regime"),
            **extract_signal_metrics(signal),
            "signal_price": final.get("signal_price", signal.get("entry_price")),
            "entry_mark_price": final.get("entry_mark_price"),
            "entry_spread_pct": final.get("entry_spread_pct"),
            "entry_fill_price": final.get("entry_price"),
            "entry_fee_paid": sum(
                float(item.get("entry_fee_allocated") or 0) for item in slices
            ),
            "final_reason": final.get("reason"),
            "final_exit_price": final.get("exit_price"),
            "exit_spread_pct": final.get("exit_spread_pct"),
            "exit_fee_paid": sum(
                float(item.get("exit_fee_paid") or 0) for item in slices
            ),
            "time_exit_trigger": final.get("time_exit_trigger"),
            "duration_sec": final.get("duration_sec"),
            "pnl_total": sum(float(item.get("pnl_slice") or 0) for item in slices),
            "deployment_version": final.get("deployment_version"),
            "deployment_commit": final.get("deployment_commit"),
            "signal_joined": bool(signal),
        })
    return rows


def load_json_rows(conn: sqlite3.Connection, table: str) -> list[Dict[str, Any]]:
    rows = []
    for (raw,) in conn.execute(f"SELECT data FROM {table}"):
        try:
            value = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            value = {}
        if isinstance(value, dict):
            rows.append(value)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = join_trade_rows(
        load_json_rows(conn, "signals_history"),
        load_json_rows(conn, "trade_history"),
    )
    print(json.dumps(rows, separators=(",", ":"), default=str))


if __name__ == "__main__":
    main()
