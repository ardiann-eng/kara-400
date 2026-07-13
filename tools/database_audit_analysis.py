"""Deterministic, read-only statistical audit for KARA production databases."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone


DATA_DB = "/data/kara_data.db"
ML_DB = "/data/kara_ml.db"


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def decode(raw: object) -> dict:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def finite(value: object) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def quantile(values: list[float], probability: float) -> float | None:
    values = sorted(value for value in values if math.isfinite(value))
    if not values:
        return None
    index = (len(values) - 1) * probability
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return values[lower]
    return values[lower] * (upper - index) + values[upper] * (index - lower)


def wilson(wins: int, n: int, z: float = 1.96) -> list[float] | None:
    if not n:
        return None
    p = wins / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denominator
    return [round(max(0.0, center - margin), 6), round(min(1.0, center + margin), 6)]


def bootstrap_mean_ci(values: list[float], iterations: int = 2000) -> list[float] | None:
    if len(values) < 2:
        return None
    # Deterministic linear congruential generator keeps audit reproducible.
    state = 20260713
    means = []
    n = len(values)
    for _ in range(iterations):
        total = 0.0
        for _ in range(n):
            state = (1664525 * state + 1013904223) % (2**32)
            total += values[state % n]
        means.append(total / n)
    return [round(quantile(means, 0.025) or 0.0, 6), round(quantile(means, 0.975) or 0.0, 6)]


def stats(rows: list[dict], pnl_key: str = "pnl_usd") -> dict:
    values = [finite(row.get(pnl_key)) for row in rows]
    pnl = [value for value in values if value is not None]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    result = {
        "n": len(pnl),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(pnl), 6) if pnl else None,
        "win_rate_95ci": wilson(len(wins), len(pnl)),
        "net": round(sum(pnl), 6),
        "mean": round(statistics.fmean(pnl), 6) if pnl else None,
        "mean_95ci_bootstrap": bootstrap_mean_ci(pnl),
        "median": round(statistics.median(pnl), 6) if pnl else None,
        "avg_winner": round(statistics.fmean(wins), 6) if wins else None,
        "avg_loser": round(statistics.fmean(losses), 6) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 6) if gross_loss else None,
        "payoff_ratio": round(statistics.fmean(wins) / abs(statistics.fmean(losses)), 6)
        if wins and losses and statistics.fmean(losses)
        else None,
        "min": round(min(pnl), 6) if pnl else None,
        "max": round(max(pnl), 6) if pnl else None,
    }
    return result


def grouped(rows: list[dict], key, min_n: int = 1, pnl_key: str = "pnl_usd") -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        value = key(row) if callable(key) else row.get(key)
        groups[str(value if value not in (None, "") else "missing")].append(row)
    return {
        group: stats(items, pnl_key)
        for group, items in sorted(groups.items())
        if len(items) >= min_n
    }


def max_drawdown(rows: list[dict]) -> dict:
    ordered = sorted(rows, key=lambda row: row["created_at"])
    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    peak_time = None
    trough_time = None
    for row in ordered:
        cumulative += row["pnl_usd"]
        if cumulative > peak:
            peak = cumulative
            peak_time = row["created_at"]
        drawdown = cumulative - peak
        if drawdown < worst:
            worst = drawdown
            trough_time = row["created_at"]
    return {
        "max_drawdown_usd": round(worst, 6),
        "preceding_peak_epoch": peak_time,
        "trough_epoch": trough_time,
    }


def score_bucket(row: dict) -> str:
    score = row.get("score")
    if score is None:
        return "missing"
    if score < 60:
        return "<60"
    if score <= 64:
        return "60-64"
    if score <= 71:
        return "65-71"
    return "72+"


def signed_move(row: dict) -> float | None:
    entry = finite(row.get("entry_price"))
    exit_price = finite(row.get("exit_price"))
    if not entry or exit_price is None:
        return None
    direction = 1 if str(row.get("side", "")).lower() == "long" else -1
    return direction * (exit_price / entry - 1)


data = connect(DATA_DB)
ml_conn = connect(ML_DB)

trades = []
for db_row in data.execute("SELECT * FROM trade_history ORDER BY created_at"):
    payload = decode(db_row["data"])
    trades.append(
        {
            **payload,
            "trade_id": db_row["trade_id"],
            "chat_id": str(db_row["chat_id"]),
            "asset": db_row["asset"],
            "side": str(db_row["side"]).lower(),
            "pnl_usd": float(db_row["pnl_usd"]),
            "pnl_pct": float(db_row["pnl_pct"]),
            "created_at": float(db_row["created_at"]),
        }
    )

ml_rows = {row["pos_id"]: dict(row) for row in ml_conn.execute("SELECT * FROM ml_experience")}
signals = []
for db_row in data.execute("SELECT * FROM signals_history ORDER BY created_at"):
    payload = decode(db_row["data"])
    signals.append(
        {
            **payload,
            "sig_id": db_row["sig_id"],
            "asset": db_row["asset"],
            "side": str(db_row["side"]).lower(),
            "score": db_row["score"],
            "signal_price": db_row["price"],
            "created_at": float(db_row["created_at"]),
        }
    )

joined = []
unmatched_trade_ids = []
join_mismatches = []
for trade in trades:
    ml = ml_rows.get(trade["trade_id"])
    if not ml:
        unmatched_trade_ids.append(trade["trade_id"])
        continue
    if str(ml["chat_id"]) != trade["chat_id"] or ml["asset"] != trade["asset"] or str(ml["side"]).lower() != trade["side"]:
        join_mismatches.append(trade["trade_id"])
        continue
    joined.append({**trade, **{f"ml_{key}": value for key, value in ml.items()}})

trade_ids = {trade["trade_id"] for trade in trades}
ml_without_trade = [pos_id for pos_id in ml_rows if pos_id not in trade_ids]

for row in joined:
    row["duration_min"] = finite(row.get("ml_duration_sec"))
    if row["duration_min"] is not None:
        row["duration_min"] /= 60
    row["mfe_pct"] = finite(row.get("ml_mfe_pct"))
    row["signed_exit_move"] = signed_move(row)
    risk = finite(row.get("ml_micro_risk_pct"))
    notional = finite(row.get("notional"))
    row["micro_r"] = row["pnl_usd"] / (notional * risk) if risk and notional else None

period = {
    "first_close_epoch": min(row["created_at"] for row in trades),
    "last_close_epoch": max(row["created_at"] for row in trades),
}
period["first_close_utc"] = datetime.fromtimestamp(period["first_close_epoch"], timezone.utc).isoformat()
period["last_close_utc"] = datetime.fromtimestamp(period["last_close_epoch"], timezone.utc).isoformat()
period["hours"] = round((period["last_close_epoch"] - period["first_close_epoch"]) / 3600, 3)

# Signal attribution is intentionally conservative: exact feature identity and <=30s,
# with one candidate on both sides. It remains inferred because no durable signal_id bridge exists.
signal_candidates: dict[str, list[int]] = {}
reverse_candidates: dict[int, list[str]] = defaultdict(list)
for row in joined:
    entry_ts = finite(row.get("ml_timestamp"))
    candidates = []
    if entry_ts is not None:
        for index, signal in enumerate(signals):
            if (
                signal["asset"] == row["asset"]
                and signal["side"] == row["side"]
                and signal["score"] == row.get("score")
                and 0 <= entry_ts - signal["created_at"] <= 30
            ):
                candidates.append(index)
                reverse_candidates[index].append(row["trade_id"])
    signal_candidates[row["trade_id"]] = candidates

inferred_pairs = {}
for row in joined:
    candidates = signal_candidates[row["trade_id"]]
    if len(candidates) == 1 and len(reverse_candidates[candidates[0]]) == 1:
        inferred_pairs[row["trade_id"]] = signals[candidates[0]]

inferred_rows = []
for row in joined:
    signal = inferred_pairs.get(row["trade_id"])
    if not signal:
        continue
    planned_sl_pct = abs(float(signal["stop_loss"]) / float(signal["entry_price"]) - 1)
    planned_tp1_pct = abs(float(signal["tp1"]) / float(signal["entry_price"]) - 1)
    planned_tp2_pct = abs(float(signal["tp2"]) / float(signal["entry_price"]) - 1)
    inferred_rows.append(
        {
            **row,
            "regime": signal.get("regime"),
            "planned_sl_pct": planned_sl_pct,
            "planned_tp1_pct": planned_tp1_pct,
            "planned_tp2_pct": planned_tp2_pct,
            "planned_rr1": planned_tp1_pct / planned_sl_pct if planned_sl_pct else None,
            "planned_rr2": planned_tp2_pct / planned_sl_pct if planned_sl_pct else None,
            "signal_fill_slippage_bps": (
                (float(row["entry_price"]) / float(signal["entry_price"]) - 1)
                * (1 if row["side"] == "long" else -1)
                * 10_000
            ),
        }
    )

enriched = [row for row in joined if row.get("ml_exit_reason") is not None]
legacy = [row for row in joined if row.get("ml_exit_reason") is None]
mfe_rows = [row for row in joined if row["mfe_pct"] is not None]
duration_rows = [row for row in joined if row["duration_min"] is not None]

vol_values = sorted(finite(row.get("ml_realized_vol")) for row in joined if finite(row.get("ml_realized_vol")) is not None)
vol_q1 = quantile(vol_values, 1 / 3)
vol_q2 = quantile(vol_values, 2 / 3)

def vol_bucket(row: dict) -> str:
    value = finite(row.get("ml_realized_vol"))
    if value is None or vol_q1 is None or vol_q2 is None:
        return "missing"
    if value <= vol_q1:
        return "low_tercile"
    if value <= vol_q2:
        return "mid_tercile"
    return "high_tercile"


def trend_bucket(row: dict) -> str:
    value = finite(row.get("ml_trend_pct"))
    if value is None:
        return "missing"
    value = abs(value)
    if value < 0.01:
        return "abs_<1pct"
    if value < 0.03:
        return "abs_1-3pct"
    return "abs_3pct+"


def funding_bucket(row: dict) -> str:
    value = finite(row.get("ml_funding_rate"))
    if value is None:
        return "missing"
    if value < -0.0001:
        return "negative_<-1bp"
    if value > 0.0001:
        return "positive_>1bp"
    return "near_zero"


def expected_edge_bucket(row: dict) -> str:
    value = finite(row.get("ml_expected_edge"))
    if value is None:
        return "missing"
    if value < 0.45:
        return "<0.45"
    if value <= 0.55:
        return "0.45-0.55"
    return ">0.55"


def hour_bucket(row: dict) -> str:
    return str(datetime.fromtimestamp(row["ml_timestamp"], timezone.utc).hour)


def day_bucket(row: dict) -> str:
    return datetime.fromtimestamp(row["ml_timestamp"], timezone.utc).strftime("%Y-%m-%d_%A")


def duration_summary(rows: list[dict]) -> dict:
    values = [row["duration_min"] for row in rows if row["duration_min"] is not None]
    return {
        "n": len(values),
        "mean_min": round(statistics.fmean(values), 4) if values else None,
        "median_min": round(statistics.median(values), 4) if values else None,
        "p10_min": round(quantile(values, 0.1) or 0.0, 4) if values else None,
        "p90_min": round(quantile(values, 0.9) or 0.0, 4) if values else None,
    }


def excursion_summary(rows: list[dict]) -> dict:
    values = [row["mfe_pct"] for row in rows if row["mfe_pct"] is not None]
    return {
        "n": len(values),
        "mean_pct": round(statistics.fmean(values) * 100, 6) if values else None,
        "median_pct": round(statistics.median(values) * 100, 6) if values else None,
        "p75_pct": round((quantile(values, 0.75) or 0) * 100, 6) if values else None,
        "p90_pct": round((quantile(values, 0.9) or 0) * 100, 6) if values else None,
    }


reason_side = grouped(joined, lambda row: f"{row.get('reason')}|{row.get('side')}", min_n=1)
score_reason = grouped(joined, lambda row: f"{score_bucket(row)}|{row.get('reason')}", min_n=1)

stop_rows = [row for row in joined if row.get("reason") == "stop_loss"]
time_rows = [row for row in joined if row.get("reason") == "time_exit"]
winner_to_loser = [row for row in mfe_rows if row["mfe_pct"] >= 0.0035 and row["pnl_usd"] <= 0]
winner_to_loser_early = [row for row in mfe_rows if row.get("early_profit_lock") and row["pnl_usd"] <= 0]

micro_r_values = [finite(row["micro_r"]) for row in joined if finite(row["micro_r"]) is not None]

signal_field_distribution = {
    "n": len(signals),
    "score": dict(sorted(Counter(score_bucket(signal) for signal in signals).items())),
    "regime": dict(sorted(Counter(str(signal.get("regime", "missing")) for signal in signals).items())),
    "trade_mode": dict(sorted(Counter(str(signal.get("trade_mode", "missing")) for signal in signals).items())),
    "entry_location_quality": dict(sorted(Counter(str(signal.get("entry_location_quality", "missing")) for signal in signals).items())),
    "side": dict(sorted(Counter(signal["side"] for signal in signals).items())),
}

report = {
    "scope": {
        "period": period,
        "trade_count": len(trades),
        "signal_count": len(signals),
        "ml_count": len(ml_rows),
        "exact_trade_ml_join": len(joined),
        "trade_without_ml": len(unmatched_trade_ids),
        "ml_without_trade": len(ml_without_trade),
        "identity_mismatches": len(join_mismatches),
        "legacy_rows": len(legacy),
        "enriched_rows": len(enriched),
        "mfe_rows": len(mfe_rows),
        "duration_rows": len(duration_rows),
        "signal_join_contract": "inferred_only_no_durable_signal_id_bridge",
        "unique_bidirectional_signal_matches_30s": len(inferred_rows),
        "ambiguous_or_unmatched_signal_attribution": len(joined) - len(inferred_rows),
    },
    "overall": {**stats(joined), **max_drawdown(joined)},
    "overall_roe": stats(joined, "pnl_pct"),
    "cohort_stability": {
        "legacy": stats(legacy),
        "enriched": stats(enriched),
    },
    "signal_inventory": signal_field_distribution,
    "performance": {
        "exit": grouped(joined, "reason"),
        "side": grouped(joined, "side"),
        "exit_side": reason_side,
        "asset_n3": grouped(joined, "asset", min_n=3),
        "score_bucket": grouped(joined, score_bucket),
        "score_exit": score_reason,
        "trade_mode": grouped(joined, "ml_trade_mode"),
        "entry_location": grouped(joined, "ml_entry_location_quality"),
        "volatility_tercile": grouped(joined, vol_bucket),
        "trend_strength": grouped(joined, trend_bucket),
        "funding": grouped(joined, funding_bucket),
        "expected_edge": grouped(joined, expected_edge_bucket),
        "meta_delta": grouped(joined, "ml_meta_delta"),
        "oi_score": grouped(joined, "ml_oi_score"),
        "liquidation_score": grouped(joined, "ml_liq_score"),
        "orderbook_score": grouped(joined, "ml_ob_score"),
        "session_bonus": grouped(joined, "ml_session_bonus"),
        "hour_utc": grouped(joined, hour_bucket),
        "day_utc": grouped(joined, day_bucket),
    },
    "exit_diagnostics": {
        "stop_loss": {
            **stats(stop_rows),
            "signed_exit_move_pct": stats(stop_rows, "signed_exit_move"),
            "duration": duration_summary(stop_rows),
            "mfe": excursion_summary(stop_rows),
            "had_mfe_0_35pct": sum(1 for row in stop_rows if row["mfe_pct"] is not None and row["mfe_pct"] >= 0.0035),
        },
        "time_exit": {
            **stats(time_rows),
            "duration": duration_summary(time_rows),
            "mfe": excursion_summary(time_rows),
            "trigger": grouped(time_rows, "ml_time_exit_trigger"),
            "side": grouped(time_rows, "side"),
            "entry_location": grouped(time_rows, "ml_entry_location_quality"),
        },
        "winner_to_loser": {
            "definition": "observed MFE >= 0.35% and final cumulative pnl_usd <= 0",
            "eligible_n": len(mfe_rows),
            "n": len(winner_to_loser),
            "rate": round(len(winner_to_loser) / len(mfe_rows), 6) if mfe_rows else None,
            "stats": stats(winner_to_loser),
            "by_exit": grouped(winner_to_loser, "reason"),
            "early_profit_lock_and_final_loss_n": len(winner_to_loser_early),
        },
    },
    "holding_time": {
        "overall": duration_summary(duration_rows),
        "by_exit": {
            reason: duration_summary([row for row in duration_rows if row.get("reason") == reason])
            for reason in sorted({str(row.get("reason")) for row in duration_rows})
        },
    },
    "mfe": {
        "overall": excursion_summary(mfe_rows),
        "by_exit": {
            reason: excursion_summary([row for row in mfe_rows if row.get("reason") == reason])
            for reason in sorted({str(row.get("reason")) for row in mfe_rows})
        },
        "final_winners": excursion_summary([row for row in mfe_rows if row["pnl_usd"] > 0]),
        "final_losers": excursion_summary([row for row in mfe_rows if row["pnl_usd"] <= 0]),
    },
    "risk_normalization": {
        "metric": "pnl_usd / (initial_notional * micro_risk_pct)",
        "warning": "micro_risk_pct may use micro invalidation or planned stop fallback; not audited R multiple",
        "n": len(micro_r_values),
        "mean": round(statistics.fmean(micro_r_values), 6) if micro_r_values else None,
        "median": round(statistics.median(micro_r_values), 6) if micro_r_values else None,
        "by_score": grouped([row for row in joined if row["micro_r"] is not None], score_bucket, pnl_key="micro_r"),
    },
    "inferred_signal_analysis": {
        "warning": "Exploratory only; no exact trade-to-signal foreign key",
        "n": len(inferred_rows),
        "regime": grouped(inferred_rows, "regime"),
        "planned_level_summary_pct": {
            field: {
                "mean": round(statistics.fmean(row[field] for row in inferred_rows) * 100, 6) if inferred_rows else None,
                "median": round(statistics.median(row[field] for row in inferred_rows) * 100, 6) if inferred_rows else None,
                "p90": round((quantile([row[field] for row in inferred_rows], 0.9) or 0) * 100, 6) if inferred_rows else None,
            }
            for field in ("planned_sl_pct", "planned_tp1_pct", "planned_tp2_pct")
        },
        "planned_rr1_mean": round(statistics.fmean(row["planned_rr1"] for row in inferred_rows), 6) if inferred_rows else None,
        "planned_rr2_mean": round(statistics.fmean(row["planned_rr2"] for row in inferred_rows), 6) if inferred_rows else None,
        "entry_slippage_bps": {
            "mean": round(statistics.fmean(row["signal_fill_slippage_bps"] for row in inferred_rows), 6) if inferred_rows else None,
            "median": round(statistics.median(row["signal_fill_slippage_bps"] for row in inferred_rows), 6) if inferred_rows else None,
            "p90": round(quantile([row["signal_fill_slippage_bps"] for row in inferred_rows], 0.9) or 0, 6) if inferred_rows else None,
        },
    },
    "missing_required_evidence": {
        "mae": "not stored",
        "audited_r_multiple": "planned stop and signal_id bridge not stored in closed trade",
        "leverage_by_closed_trade": "not stored",
        "strategy_source_pure_vs_fallback": "not stored",
        "trigger_to_fill_slippage": "trigger_price and fill_price not stored",
        "rejected_scans": "not stored; false-positive rate only conditional on persisted pre-trade signals",
        "open_interest_raw_at_entry": "not stored; combined oi_funding_score only",
        "volume_at_entry": "not stored",
        "true_intrabar_mfe": "polling-based MFE only",
    },
}

print(json.dumps(report, indent=2, sort_keys=True))

data.close()
ml_conn.close()
