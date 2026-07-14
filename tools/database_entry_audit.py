"""Deterministic, aggregate-only entry audit for KARA SQLite databases.

Both databases are opened with SQLite URI ``mode=ro``. Queries in this module
are limited to SELECT and read-only PRAGMA statements. No row identifier or
private owner field is included in the JSON report.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote


DEFAULT_DATA_DB = "/data/kara_data.db"
DEFAULT_ML_DB = "/data/kara_ml.db"
MFE_THRESHOLD = 0.0035
MIN_INFERENCE_N = 10
BOOTSTRAP_ITERATIONS = 2_000
RANDOM_SEED = 20260714
PURGE_SECONDS = 30.0

# Entry-time fields only. Outcome, duration, MFE/MAE, exit, and PnL fields are
# intentionally absent to prevent target leakage.
MODEL_FEATURES = (
    "score",
    "meta_delta",
    "oi_score",
    "liq_score",
    "ob_score",
    "session_bonus",
    "funding_rate",
    "realized_vol",
    "trend_pct",
    "micro_risk_pct",
    "location_numeric",
    "is_scalper",
)
LEAKAGE_FIELDS = frozenset(
    {
        "actual_pnl_pct",
        "created_at",
        "duration_sec",
        "exit_price",
        "exit_reason",
        "impulse_win",
        "is_win",
        "mae_pct",
        "mfe_pct",
        "pnl_pct",
        "pnl_usd",
        "reason",
        "time_exit_trigger",
    }
)


def connect_readonly(path: str) -> sqlite3.Connection:
    """Open an existing SQLite file read-only without creating it."""
    absolute = Path(path).expanduser().resolve()
    uri = f"file:{quote(str(absolute).replace('\\\\', '/'), safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if table not in _tables(conn):
        return set()
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _decode(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _utc(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _period(rows: Iterable[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [_finite(row.get(key)) for row in rows]
    valid = [value for value in values if value is not None]
    return {
        "start_utc": _utc(min(valid)) if valid else None,
        "end_utc": _utc(max(valid)) if valid else None,
    }


def _quantile(values: Iterable[float], probability: float) -> float | None:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return None
    position = (len(ordered) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _bootstrap_ci(values: list[float]) -> list[float] | None:
    if len(values) < 2:
        return None
    rng = random.Random(RANDOM_SEED)
    means = [
        statistics.fmean(values[rng.randrange(len(values))] for _ in values)
        for _ in range(BOOTSTRAP_ITERATIONS)
    ]
    return [round(_quantile(means, 0.025) or 0.0, 8), round(_quantile(means, 0.975) or 0.0, 8)]


def _metric(values: Iterable[object], period: dict[str, Any]) -> dict[str, Any]:
    valid = [value for item in values if (value := _finite(item)) is not None]
    positives = sum(value > 0 for value in valid)
    result = {
        "n": len(valid),
        "period": period,
        "mean": round(statistics.fmean(valid), 8) if valid else None,
        "mean_95ci_bootstrap": _bootstrap_ci(valid),
        "median": round(statistics.median(valid), 8) if valid else None,
        "positive_rate": round(positives / len(valid), 8) if valid else None,
    }
    if len(valid) < MIN_INFERENCE_N:
        result["sample_status"] = "insufficient sample"
    return result


def _binary_metric(values: Iterable[object], period: dict[str, Any]) -> dict[str, Any]:
    valid = [int(value) for item in values if (value := _finite(item)) in (0.0, 1.0)]
    result = {
        "n": len(valid),
        "period": period,
        "rate": round(sum(valid) / len(valid), 8) if valid else None,
    }
    if len(valid) < MIN_INFERENCE_N:
        result["sample_status"] = "insufficient sample"
    return result


def _cohort_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    period = _period(rows, "entry_at")
    eligible = [row for row in rows if _finite(row.get("mfe_pct")) is not None]
    primary = [int(float(row["mfe_pct"]) >= MFE_THRESHOLD) for row in eligible]
    pnl_values = [row.get("pnl_usd") for row in rows]
    result = {
        "rows_n": len(rows),
        "period": period,
        "primary_observed_mfe_ge_0_35pct": _binary_metric(primary, period),
        "secondary_pnl_usd": _metric(pnl_values, period),
        "secondary_impulse_win": _binary_metric(
            [row.get("impulse_win") for row in rows], period
        ),
    }
    pnl = [value for item in pnl_values if (value := _finite(item)) is not None]
    if pnl and sum(pnl) < 0:
        result["deletion_upper_bound_usd"] = {
            "value": round(-sum(pnl), 8),
            "warning": "Descriptive upper bound only; assumes deleted entries have zero replacement cost and no selection effects.",
        }
    return result


def side_signed_move(side: object, entry_price: object, exit_price: object) -> float | None:
    """Return favorable-positive simple price move for LONG or SHORT."""
    entry, exit_value = _finite(entry_price), _finite(exit_price)
    if entry is None or entry <= 0 or exit_value is None:
        return None
    side_value = str(side).lower()
    if side_value not in {"long", "short"}:
        return None
    direction = 1.0 if side_value == "long" else -1.0
    return direction * (exit_value / entry - 1.0)


def _read_trades(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not {"trade_history"}.issubset(_tables(conn)):
        return []
    required = {"trade_id", "asset", "side", "pnl_usd", "pnl_pct", "data", "created_at"}
    if not required.issubset(_columns(conn, "trade_history")):
        return []
    rows = []
    for db_row in conn.execute(
        "SELECT trade_id, chat_id, asset, side, pnl_usd, pnl_pct, data, created_at "
        "FROM trade_history ORDER BY created_at, trade_id"
    ):
        payload = _decode(db_row["data"])
        rows.append(
            {
                **payload,
                "_trade_key": str(db_row["trade_id"]),
                "_owner_key": str(db_row["chat_id"]),
                "asset": str(db_row["asset"]),
                "side": str(db_row["side"]).lower(),
                "pnl_usd": _finite(db_row["pnl_usd"]),
                "pnl_pct": _finite(db_row["pnl_pct"]),
                "created_at": _finite(db_row["created_at"]),
            }
        )
    return rows


def _read_ml(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if "ml_experience" not in _tables(conn):
        return []
    return [dict(row) for row in conn.execute("SELECT * FROM ml_experience ORDER BY timestamp, pos_id")]


def exact_trade_ml_join(
    trades: list[dict[str, Any]], ml_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Join durable position key and verify private owner, asset, and side identity."""
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ml_rows:
        by_key[str(row.get("pos_id"))].append(row)
    joined, mismatch, duplicate = [], 0, 0
    matched_ml_keys: set[str] = set()
    for trade in trades:
        candidates = by_key.get(trade["_trade_key"], [])
        if len(candidates) > 1:
            duplicate += 1
            continue
        if not candidates:
            continue
        ml = candidates[0]
        identity_ok = (
            str(ml.get("chat_id")) == trade["_owner_key"]
            and str(ml.get("asset")) == trade["asset"]
            and str(ml.get("side", "")).lower() == trade["side"]
        )
        if not identity_ok:
            mismatch += 1
            continue
        matched_ml_keys.add(str(ml.get("pos_id")))
        joined.append(
            {
                **trade,
                **{f"ml_{key}": value for key, value in ml.items() if key not in {"chat_id", "pos_id"}},
                "entry_at": _finite(ml.get("timestamp")),
                "mfe_pct": _finite(ml.get("mfe_pct")),
                "impulse_win": _finite(ml.get("impulse_win")),
            }
        )
    return joined, {
        "joined": len(joined),
        "trade_without_exact_ml": len(trades) - len(joined) - mismatch - duplicate,
        "identity_mismatch": mismatch,
        "duplicate_ml_key": duplicate,
        "ml_without_exact_trade": sum(
            str(row.get("pos_id")) not in matched_ml_keys for row in ml_rows
        ),
    }


def _read_signals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if "signals_history" not in _tables(conn):
        return []
    rows = []
    for index, row in enumerate(
        conn.execute(
            "SELECT asset, side, score, price, data, created_at "
            "FROM signals_history ORDER BY created_at, sig_id"
        )
    ):
        rows.append(
            {
                **_decode(row["data"]),
                "_signal_index": index,
                "asset": str(row["asset"]),
                "side": str(row["side"]).lower(),
                "score": _finite(row["score"]),
                "signal_price": _finite(row["price"]),
                "created_at": _finite(row["created_at"]),
            }
        )
    return rows


def infer_unique_signals(
    joined: list[dict[str, Any]], signals: list[dict[str, Any]], window: float = 30.0
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Infer signal attribution only when match is unique in both directions."""
    forward: dict[int, list[int]] = defaultdict(list)
    reverse: dict[int, list[int]] = defaultdict(list)
    for trade_index, trade in enumerate(joined):
        timestamp = _finite(trade.get("entry_at"))
        if timestamp is None:
            continue
        for signal_index, signal in enumerate(signals):
            signal_at = _finite(signal.get("created_at"))
            if (
                signal_at is not None
                and signal.get("asset") == trade.get("asset")
                and signal.get("side") == trade.get("side")
                and _finite(signal.get("score")) == _finite(trade.get("ml_score"))
                and 0 <= timestamp - signal_at <= window
            ):
                forward[trade_index].append(signal_index)
                reverse[signal_index].append(trade_index)
    inferred = []
    for trade_index, trade in enumerate(joined):
        candidates = forward[trade_index]
        if len(candidates) == 1 and len(reverse[candidates[0]]) == 1:
            signal = signals[candidates[0]]
            inferred.append(
                {
                    **trade,
                    "inferred_regime": signal.get("regime"),
                    "inferred_signal_price": signal.get("signal_price"),
                    "inferred_reason_flags": sorted(_reason_flags(signal)),
                }
            )
    return inferred, {
        "unique_bidirectional_30s": len(inferred),
        "ambiguous_or_unmatched": len(joined) - len(inferred),
    }


def _completeness(rows: list[dict[str, Any]], fields: Iterable[str]) -> dict[str, Any]:
    return {
        field: {
            "present_n": sum(row.get(field) not in (None, "") for row in rows),
            "missing_n": sum(row.get(field) in (None, "") for row in rows),
        }
        for field in fields
    }


def _grouped(
    rows: list[dict[str, Any]], key: Callable[[dict[str, Any]], object]
) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        value = key(row)
        groups[str(value) if value not in (None, "") else "missing"].append(row)
    return {name: _cohort_stats(items) for name, items in sorted(groups.items())}


def _conditional_by_side(
    rows: list[dict[str, Any]], key: Callable[[dict[str, Any]], object]
) -> dict[str, Any]:
    return {
        "all": _grouped(rows, key),
        "long": _grouped([row for row in rows if row.get("side") == "long"], key),
        "short": _grouped([row for row in rows if row.get("side") == "short"], key),
    }


def _score_bucket(row: dict[str, Any]) -> str:
    value = _finite(row.get("ml_score"))
    if value is None:
        return "missing"
    if value <= 64:
        return "60-64" if value >= 60 else "<60"
    return "65-71" if value <= 71 else "72+"


def _trend_alignment(row: dict[str, Any]) -> str:
    trend = _finite(row.get("ml_trend_pct"))
    if trend is None:
        return "missing"
    aligned = trend >= 0 if row.get("side") == "long" else trend <= 0
    return "aligned" if aligned else "counter_trend"


def _fixed_vol_bucket(row: dict[str, Any]) -> str:
    value = _finite(row.get("ml_realized_vol"))
    if value is None:
        return "missing"
    if value < 0.015:
        return "low_vol"
    if value < 0.04:
        return "mid_vol"
    return "high_vol"


def _reason_flags(signal: dict[str, Any]) -> set[str]:
    raw = signal.get("reasons")
    if raw is None and isinstance(signal.get("breakdown"), dict):
        raw = signal["breakdown"].get("reasons")
    text = " | ".join(str(item) for item in raw) if isinstance(raw, list) else str(raw or "")
    lowered = text.lower()
    contracts = {
        "momentum": ("momentum",),
        "structure": ("structure", "break", "follow-through"),
        "orderbook": ("orderbook", "bid wall", "ask wall"),
        "volume": ("volume",),
        "rsi": ("rsi", "oversold", "overbought"),
        "cvd": ("cvd",),
        "mtf": ("mtf", "15m"),
        "session": ("session",),
        "meta": ("meta",),
        "liquidation": ("liquidation", "liq ", "cascade"),
        "funding_oi": ("funding", "open interest", " oi "),
    }
    return {
        flag for flag, tokens in contracts.items() if any(token in lowered for token in tokens)
    }


def _tercile_function(values: list[float], prefix: str) -> Callable[[dict[str, Any]], str]:
    q1, q2 = _quantile(values, 1 / 3), _quantile(values, 2 / 3)

    def bucket(row: dict[str, Any]) -> str:
        value = _finite(row.get(f"ml_{prefix}"))
        if value is None or q1 is None or q2 is None:
            return "missing"
        if value <= q1:
            return "low_tercile"
        return "mid_tercile" if value <= q2 else "high_tercile"

    return bucket


def _pearson(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    mx, my = statistics.fmean(x), statistics.fmean(y)
    numerator = sum((a - mx) * (b - my) for a, b in zip(x, y))
    denominator = math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))
    return numerator / denominator if denominator else None


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        rank = (start + end - 1) / 2 + 1
        for index in order[start:end]:
            ranks[index] = rank
        start = end
    return ranks


def _feature_value(row: dict[str, Any], feature: str) -> float | None:
    if feature == "location_numeric":
        return {
            "invalid": 0.0,
            "weak": 1.0,
            "weak_confirmed": 1.0,
            "valid": 2.0,
            "excellent": 3.0,
        }.get(str(row.get("ml_entry_location_quality")))
    if feature == "is_scalper":
        return 1.0 if row.get("ml_trade_mode") == "scalper" else 0.0
    return _finite(row.get(f"ml_{feature}"))


def _correlations(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for feature in MODEL_FEATURES:
        pairs = [
            (value, target)
            for row in rows
            if (value := _feature_value(row, feature)) is not None
            and (mfe := _finite(row.get("mfe_pct"))) is not None
            for target in [float(mfe >= MFE_THRESHOLD)]
        ]
        x, y = [pair[0] for pair in pairs], [pair[1] for pair in pairs]
        pearson = _pearson(x, y)
        spearman = _pearson(_ranks(x), _ranks(y)) if pairs else None
        result[feature] = {
            "n": len(pairs),
            "pearson": round(pearson, 8) if pearson is not None else None,
            "spearman": round(spearman, 8) if spearman is not None else None,
            "sample_status": "insufficient sample" if len(pairs) < MIN_INFERENCE_N else "descriptive",
        }
    return result


def _redundancy_matrix(rows: list[dict[str, Any]]) -> dict[str, Any]:
    matrix: dict[str, Any] = {}
    for left in MODEL_FEATURES:
        matrix[left] = {}
        for right in MODEL_FEATURES:
            pairs = [
                (a, b)
                for row in rows
                if (a := _feature_value(row, left)) is not None
                and (b := _feature_value(row, right)) is not None
            ]
            value = _pearson([a for a, _ in pairs], [b for _, b in pairs])
            matrix[left][right] = {
                "n": len(pairs),
                "pearson": round(value, 8) if value is not None else None,
            }
    return matrix


def build_purged_walk_forward_folds(
    rows: list[dict[str, Any]], folds: int = 4, purge_seconds: float = PURGE_SECONDS
) -> list[tuple[list[int], list[int]]]:
    """Build expanding chronological folds with closed trades purged from test start."""
    def timestamp(index: int, key: str) -> float:
        value = _finite(rows[index].get(key))
        return value if value is not None else math.inf

    ordered = sorted(
        range(len(rows)),
        key=lambda index: (timestamp(index, "entry_at"), index),
    )
    usable = [index for index in ordered if _finite(rows[index].get("entry_at")) is not None]
    block = len(usable) // (folds + 1)
    if block < 1:
        return []
    result = []
    for fold in range(1, folds + 1):
        test_start_pos = block * fold
        test_end_pos = block * (fold + 1) if fold < folds else len(usable)
        test = usable[test_start_pos:test_end_pos]
        if not test:
            continue
        test_start = float(rows[test[0]]["entry_at"])
        train = [
            index
            for index in usable[:test_start_pos]
            if timestamp(index, "created_at") < test_start - purge_seconds
        ]
        if train:
            result.append((train, test))
    return result


def validate_feature_whitelist(features: Iterable[str]) -> None:
    selected = tuple(features)
    leaked = sorted(set(selected) & LEAKAGE_FIELDS)
    unknown = sorted(set(selected) - set(MODEL_FEATURES))
    if leaked or unknown:
        raise ValueError(f"invalid model features: leaked={leaked}, unknown={unknown}")


def _model_matrix(rows: list[dict[str, Any]]) -> tuple[list[list[float]], list[int], list[int]]:
    validate_feature_whitelist(MODEL_FEATURES)
    complete, labels, source_indexes = [], [], []
    for index, row in enumerate(rows):
        values = [_feature_value(row, feature) for feature in MODEL_FEATURES]
        mfe = _finite(row.get("mfe_pct"))
        if mfe is None or any(value is None for value in values):
            continue
        complete.append([float(value) for value in values])
        labels.append(int(mfe >= MFE_THRESHOLD))
        source_indexes.append(index)
    return complete, labels, source_indexes


def _sklearn_analysis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        import numpy as np
        from sklearn.feature_selection import mutual_info_classif
        from sklearn.inspection import permutation_importance
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import balanced_accuracy_score, log_loss, roc_auc_score
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        return {"status": "skipped", "reason": f"optional dependency unavailable: {exc.name}"}

    matrix, labels, source_indexes = _model_matrix(rows)
    if len(matrix) < 100 or min(Counter(labels).values(), default=0) < 20:
        return {
            "status": "skipped",
            "reason": "insufficient sample; need n>=100 and each class>=20",
            "n": len(matrix),
            "class_counts": dict(sorted(Counter(labels).items())),
        }
    x, y = np.asarray(matrix), np.asarray(labels)
    observed_mi = mutual_info_classif(x, y, random_state=RANDOM_SEED)
    rng = np.random.default_rng(RANDOM_SEED)
    null = np.asarray(
        [mutual_info_classif(x, rng.permutation(y), random_state=RANDOM_SEED + i + 1) for i in range(100)]
    )
    mi = {
        feature: {
            "observed": round(float(observed_mi[index]), 8),
            "null_p95": round(float(np.quantile(null[:, index], 0.95)), 8),
            "permutation_p": round(float((1 + np.sum(null[:, index] >= observed_mi[index])) / 101), 8),
        }
        for index, feature in enumerate(MODEL_FEATURES)
    }

    model_rows = [rows[index] for index in source_indexes]
    folds = build_purged_walk_forward_folds(model_rows)
    fold_reports, importances = [], []
    for fold_number, (train, test) in enumerate(folds, start=1):
        y_train, y_test = y[train], y[test]
        if len(set(y_train)) < 2 or len(set(y_test)) < 2 or len(train) < 40:
            continue
        baseline_probability = float(np.mean(y_train))
        baseline = np.full(len(test), baseline_probability)
        model = make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=1_000, random_state=RANDOM_SEED),
        )
        model.fit(x[train], y_train)
        probability = model.predict_proba(x[test])[:, 1]
        report = {
            "fold": fold_number,
            "train_n": len(train),
            "test_n": len(test),
            "train_period": _period([model_rows[index] for index in train], "entry_at"),
            "test_period": _period([model_rows[index] for index in test], "entry_at"),
            "baseline_log_loss": round(float(log_loss(y_test, baseline, labels=[0, 1])), 8),
            "model_log_loss": round(float(log_loss(y_test, probability, labels=[0, 1])), 8),
            "model_roc_auc": round(float(roc_auc_score(y_test, probability)), 8),
            "model_balanced_accuracy": round(float(balanced_accuracy_score(y_test, probability >= 0.5)), 8),
        }
        fold_reports.append(report)
        importance = permutation_importance(
            model,
            x[test],
            y_test,
            scoring="neg_log_loss",
            n_repeats=20,
            random_state=RANDOM_SEED + fold_number,
        )
        importances.append(importance.importances_mean)
    if not fold_reports:
        return {"status": "skipped", "reason": "no valid purged fold with both classes", "mutual_information": mi}
    mean_importance = np.mean(np.asarray(importances), axis=0)
    return {
        "status": "descriptive_not_deployment_proof",
        "target": "observed_mfe_ge_0_35pct",
        "features": list(MODEL_FEATURES),
        "mutual_information": mi,
        "walk_forward": fold_reports,
        "permutation_importance_mean_neg_log_loss": {
            feature: round(float(mean_importance[index]), 8)
            for index, feature in enumerate(MODEL_FEATURES)
        },
    }


def _weak_candidate_analysis(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _tables(conn)
    if "weak_confirmation_events" not in tables:
        return {"status": "unavailable", "reason": "weak_confirmation_events table missing"}
    events = [
        dict(row)
        for row in conn.execute(
            "SELECT event_id, asset, side, status, signal_price, observed_price, score, armed_at, decided_at "
            "FROM weak_confirmation_events ORDER BY armed_at, event_id"
        )
    ]
    outcomes = []
    if "weak_confirmation_outcomes" in tables:
        outcomes = [
            dict(row)
            for row in conn.execute(
                "SELECT event_id, asset, side, signal_price, observed_price, mfe_pct, mae_pct, "
                "final_return_pct, tp1_hit, tp2_hit, sl_hit, completed_at "
                "FROM weak_confirmation_outcomes ORDER BY completed_at, event_id"
            )
        ]
    outcome_by_event = {str(row["event_id"]): row for row in outcomes}
    candidate_rows = [
        {
            **event,
            **{key: value for key, value in outcome_by_event.get(str(event["event_id"]), {}).items() if key != "event_id"},
            "entry_at": _finite(event.get("armed_at")),
            "pnl_usd": None,
            "impulse_win": None,
        }
        for event in events
    ]
    period = _period(candidate_rows, "entry_at")

    def shadow_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
        complete = [row for row in items if _finite(row.get("final_return_pct")) is not None]
        return {
            "candidate_n": len(items),
            "outcome_n": len(complete),
            "period": _period(items, "entry_at"),
            "mfe_ge_0_35pct": _binary_metric(
                [int(float(row["mfe_pct"]) >= MFE_THRESHOLD) for row in complete if _finite(row.get("mfe_pct")) is not None],
                _period(items, "entry_at"),
            ),
            "final_return_pct": _metric([row.get("final_return_pct") for row in complete], _period(items, "entry_at")),
            "tp1_hit": _binary_metric([row.get("tp1_hit") for row in complete], _period(items, "entry_at")),
            "tp2_hit": _binary_metric([row.get("tp2_hit") for row in complete], _period(items, "entry_at")),
            "sl_hit": _binary_metric([row.get("sl_hit") for row in complete], _period(items, "entry_at")),
        }

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidate_rows:
        groups[str(row.get("status") or "missing")].append(row)
    return {
        "status": "candidate_level",
        "candidate_n": len(events),
        "outcome_n": len(outcomes),
        "outcome_join_n": sum(str(row["event_id"]) in outcome_by_event for row in events),
        "orphan_outcome_n": sum(str(row["event_id"]) not in {str(event["event_id"]) for event in events} for row in outcomes),
        "period": period,
        "overall": shadow_stats(candidate_rows),
        "by_status": {name: shadow_stats(items) for name, items in sorted(groups.items())},
        "by_side": {
            side: shadow_stats([row for row in candidate_rows if str(row.get("side", "")).lower() == side])
            for side in ("long", "short")
        },
        "warning": "Shadow outcomes describe immediate-entry control per armed candidate, not confirmed-trade treatment PnL.",
    }


def analyze(data_db: str = DEFAULT_DATA_DB, ml_db: str = DEFAULT_ML_DB) -> dict[str, Any]:
    data_conn = connect_readonly(data_db)
    ml_conn = connect_readonly(ml_db)
    try:
        data_integrity = str(data_conn.execute("PRAGMA integrity_check").fetchone()[0])
        ml_integrity = str(ml_conn.execute("PRAGMA integrity_check").fetchone()[0])
        trades, ml_rows, signals = _read_trades(data_conn), _read_ml(ml_conn), _read_signals(data_conn)
        joined, join_counts = exact_trade_ml_join(trades, ml_rows)
        inferred, inference_counts = infer_unique_signals(joined, signals)

        vol_values = [value for row in joined if (value := _finite(row.get("ml_realized_vol"))) is not None]
        trend_strength_values = [abs(value) for row in joined if (value := _finite(row.get("ml_trend_pct"))) is not None]
        vol_tercile = _tercile_function(vol_values, "realized_vol")
        strength_tercile_raw = _tercile_function(trend_strength_values, "trend_strength")

        def strength_tercile(row: dict[str, Any]) -> str:
            copied = {**row, "ml_trend_strength": abs(float(row["ml_trend_pct"]))} if _finite(row.get("ml_trend_pct")) is not None else row
            return strength_tercile_raw(copied)

        deployment_boundary = None
        if "weak_confirmation_events" in _tables(data_conn):
            deployment_boundary = _finite(
                data_conn.execute("SELECT MIN(armed_at) FROM weak_confirmation_events").fetchone()[0]
            )
        pre = [row for row in joined if deployment_boundary is not None and float(row.get("entry_at") or math.inf) < deployment_boundary]
        post = [row for row in joined if deployment_boundary is not None and float(row.get("entry_at") or -math.inf) >= deployment_boundary]

        report = {
            "contract": {
                "read_only": True,
                "allowed_sql": ["SELECT", "PRAGMA integrity_check", "PRAGMA table_info"],
                "privacy": "aggregate_only; raw trade, signal, event, owner, and chat IDs omitted",
                "primary_entry_label": "observed polling-based MFE >= 0.35%; not guaranteed intrabar MFE",
                "secondary_labels": ["economic pnl_usd", "stored impulse_win"],
                "minimum_inference_n": MIN_INFERENCE_N,
                "random_seed": RANDOM_SEED,
            },
            "snapshot": {
                "integrity": {"data_db": data_integrity, "ml_db": ml_integrity},
                "counts": {
                    "trade_history": len(trades),
                    "signals_history": len(signals),
                    "ml_experience": len(ml_rows),
                    **join_counts,
                },
                "trade_close_period": _period(trades, "created_at"),
                "ml_entry_period": _period([{"entry_at": row.get("timestamp")} for row in ml_rows], "entry_at"),
                "completeness": _completeness(
                    joined,
                    [
                        "entry_at", "mfe_pct", "impulse_win", "pnl_usd", "ml_score",
                        "ml_entry_location_quality", "ml_meta_delta", "ml_session_bonus",
                        "ml_realized_vol", "ml_trend_pct",
                    ],
                ),
            },
            "enriched_mfe_cohort": {
                "overall": _cohort_stats(joined),
                "side": _grouped(joined, lambda row: row.get("side")),
                "score": _conditional_by_side(joined, _score_bucket),
                "location": _conditional_by_side(joined, lambda row: row.get("ml_entry_location_quality")),
                "meta_delta": _conditional_by_side(joined, lambda row: row.get("ml_meta_delta")),
                "session_bonus": _conditional_by_side(joined, lambda row: row.get("ml_session_bonus")),
                "realized_vol_tercile": _conditional_by_side(joined, vol_tercile),
                "fixed_realized_vol": {
                    "threshold_contract": "low <1.5%, mid 1.5%-<4%, high >=4% daily realized volatility",
                    **_conditional_by_side(joined, _fixed_vol_bucket),
                },
                "trend_alignment": _conditional_by_side(joined, _trend_alignment),
                "trend_strength_tercile": _conditional_by_side(joined, strength_tercile),
            },
            "inferred_signal": {
                "warning": "Regime attribution inferred; no durable signal foreign key.",
                **inference_counts,
                "regime": _conditional_by_side(inferred, lambda row: row.get("inferred_regime")),
                "reason_flags": {
                    flag: {
                        "present": _cohort_stats(
                            [row for row in inferred if flag in row.get("inferred_reason_flags", [])]
                        ),
                        "absent": _cohort_stats(
                            [row for row in inferred if flag not in row.get("inferred_reason_flags", [])]
                        ),
                    }
                    for flag in sorted(
                        {flag for row in inferred for flag in row.get("inferred_reason_flags", [])}
                    )
                },
            },
            "deployment_cohort": {
                "boundary_source": "inferred MIN(weak_confirmation_events.armed_at)",
                "boundary_epoch": deployment_boundary,
                "boundary_utc": _utc(deployment_boundary),
                "status": "unavailable" if deployment_boundary is None else "inferred",
                "pre": _cohort_stats(pre),
                "post": _cohort_stats(post),
            },
            "weak_confirmation": _weak_candidate_analysis(data_conn),
            "feature_association": {
                "target": "observed_mfe_ge_0_35pct",
                "pearson_spearman": _correlations(joined),
                "feature_redundancy_pearson": _redundancy_matrix(joined),
                "sklearn": _sklearn_analysis(joined),
            },
            "limitations": [
                "Association and deletion upper bounds are descriptive, not causal execution recommendations.",
                "Signal regime is reported only for unique bidirectional <=30 second inferred matches.",
                "Walk-forward uses entry-time feature whitelist and purges trades not closed before test boundary.",
                "No strategy behavior, database schema, or production row is changed.",
            ],
        }
        return report
    finally:
        data_conn.close()
        ml_conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-db", default=DEFAULT_DATA_DB)
    parser.add_argument("--ml-db", default=DEFAULT_ML_DB)
    parser.add_argument("--indent", type=int, default=2)
    args = parser.parse_args(argv)
    print(json.dumps(analyze(args.data_db, args.ml_db), indent=args.indent, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
