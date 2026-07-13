"""Focused robustness checks for KARA audit; production paths, read-only."""

import json
import sqlite3
import statistics
from collections import defaultdict


def conn(path):
    value = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    value.row_factory = sqlite3.Row
    return value


def payload(raw):
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def summary(rows, value="pnl"):
    pnl = [float(row[value]) for row in rows if row.get(value) is not None]
    wins = [item for item in pnl if item > 0]
    losses = [item for item in pnl if item <= 0]
    return {
        "n": len(pnl),
        "wr": round(len(wins) / len(pnl), 4) if pnl else None,
        "net": round(sum(pnl), 4),
        "mean": round(statistics.fmean(pnl), 4) if pnl else None,
        "median": round(statistics.median(pnl), 4) if pnl else None,
        "pf": round(sum(wins) / abs(sum(losses)), 4) if losses and sum(losses) else None,
    }


def groups(rows, key, minimum=10):
    result = defaultdict(list)
    for row in rows:
        result[str(row.get(key) or "missing")].append(row)
    return {key: summary(value) for key, value in sorted(result.items()) if len(value) >= minimum}


data = conn("/data/kara_data.db")
ml = conn("/data/kara_ml.db")
ml_rows = {row["pos_id"]: dict(row) for row in ml.execute("SELECT * FROM ml_experience")}
trades = []
for row in data.execute("SELECT * FROM trade_history ORDER BY created_at"):
    item = payload(row["data"])
    feature = ml_rows.get(row["trade_id"], {})
    trades.append({
        **item,
        **feature,
        "pnl": float(row["pnl_usd"]),
        "trade_id": row["trade_id"],
        "close_ts": float(row["created_at"]),
        "side": str(row["side"]).lower(),
    })

enriched = [row for row in trades if row.get("exit_reason") is not None]

for row in enriched:
    score = int(row["score"])
    row["score_bucket"] = "60-64" if score <= 64 else "65-71" if score <= 71 else "72+"
    row["tp_path"] = (
        "tp2_hit" if row.get("tp2_hit") else "tp1_only" if row.get("tp1_hit") else "no_tp"
    )

signals = []
for row in data.execute("SELECT * FROM signals_history"):
    item = payload(row["data"])
    signals.append({
        **item,
        "asset": row["asset"],
        "side": str(row["side"]).lower(),
        "score": int(row["score"]),
        "ts": float(row["created_at"]),
    })

candidates = defaultdict(list)
reverse = defaultdict(list)
for trade in enriched:
    for index, signal in enumerate(signals):
        if (
            signal["asset"] == trade["asset"]
            and signal["side"] == trade["side"]
            and signal["score"] == trade["score"]
            and 0 <= float(trade["timestamp"]) - signal["ts"] <= 30
        ):
            candidates[trade["trade_id"]].append(index)
            reverse[index].append(trade["trade_id"])

attributed = []
for trade in enriched:
    hits = candidates[trade["trade_id"]]
    if len(hits) != 1 or len(reverse[hits[0]]) != 1:
        continue
    signal = signals[hits[0]]
    entry = float(signal["entry_price"])
    sl = abs(float(signal["stop_loss"]) / entry - 1)
    tp1 = abs(float(signal["tp1"]) / entry - 1)
    mfe = float(trade.get("mfe_pct") or 0)
    actual = (float(trade["exit_price"]) / float(trade["entry_price"]) - 1)
    if trade["side"] == "short":
        actual *= -1
    attributed.append({
        **trade,
        "planned_sl": sl,
        "planned_tp1": tp1,
        "mfe_reached_tp1": mfe >= tp1,
        "actual_move": actual,
        "stop_overshoot_bps": (-actual - sl) * 10000 if trade["reason"] == "stop_loss" else None,
    })

stop_attr = [row for row in attributed if row["reason"] == "stop_loss"]
overshoot = [row["stop_overshoot_bps"] for row in stop_attr]

result = {
    "snapshot_counts": {"trades": len(trades), "ml": len(ml_rows), "signals": len(signals)},
    "enriched": {
        "overall": summary(enriched),
        "score": groups(enriched, "score_bucket"),
        "entry_location": groups(enriched, "entry_location_quality"),
        "exit_reason": groups(enriched, "exit_reason"),
        "time_trigger": groups([row for row in enriched if row["exit_reason"] == "time_exit"], "time_exit_trigger"),
        "tp_path": groups(enriched, "tp_path"),
    },
    "attributed_enriched": {
        "n": len(attributed),
        "tp1_reachable_by_observed_mfe_n": sum(row["mfe_reached_tp1"] for row in attributed),
        "tp1_reachable_rate": round(sum(row["mfe_reached_tp1"] for row in attributed) / len(attributed), 4) if attributed else None,
        "planned_tp1_median_pct": round(statistics.median(row["planned_tp1"] for row in attributed) * 100, 4) if attributed else None,
        "mfe_median_pct": round(statistics.median(float(row["mfe_pct"]) for row in attributed) * 100, 4) if attributed else None,
        "stop_n": len(stop_attr),
        "stop_overshoot_bps_mean": round(statistics.fmean(overshoot), 4) if overshoot else None,
        "stop_overshoot_bps_median": round(statistics.median(overshoot), 4) if overshoot else None,
    },
}

print(json.dumps(result, indent=2, sort_keys=True))
