"""
Deterministic stat aggregator for weekly review.

LLM is NOT trusted to compute stats. This module produces the numerical evidence
pack that the LLM analyst reasons over.
"""

from __future__ import annotations

import glob
import math
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import pandas as pd

import config


# ---------- Column normalization ----------
# Two live schemas coexist:
#   - utils/excel_logger.py writes: Score, Reason
#   - KARA_History_*.xlsx historical export uses: Signal Score, Exit Reason
# Aggregator MUST tolerate both.
_ALIASES = {
    "score": ["Score", "Signal Score", "signal_score"],
    "reason": ["Reason", "Exit Reason", "exit_reason"],
    "asset": ["Asset", "asset", "Symbol", "symbol"],
    "side": ["Side", "side"],
    "action": ["Action", "action"],
    "pnl": ["PnL ($)", "PnL", "pnl", "pnl_usd"],
    "pnl_pct": ["PnL (%)", "pnl_pct"],
    "timestamp": ["Timestamp", "timestamp", "Time"],
    "position_id": ["Position ID", "position_id", "PosID"],
    "mode": ["Mode", "mode"],
    "session": ["Session", "session"],
    "regime": ["Regime", "regime"],
    "cvd_snapshot": ["CVD Snapshot", "cvd_snapshot"],
    "rsi_snapshot": ["RSI Snapshot", "rsi_snapshot"],
    "hold_minutes": ["Hold Minutes", "hold_minutes"],
    "entry_time": ["Entry Time", "entry_time"],
    "exit_time": ["Exit Time", "exit_time"],
}


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename any recognized column to canonical snake_case name."""
    rename = {}
    for canonical, aliases in _ALIASES.items():
        for a in aliases:
            if a in df.columns and a != canonical:
                rename[a] = canonical
                break
    return df.rename(columns=rename)


# ---------- Session labeling ----------
def _label_session(hour_utc: int) -> str:
    # NY 13-21, London 8-17, Asia 22-07 (UTC), Overlap 13-17 (London+NY)
    if 13 <= hour_utc < 17:
        return "London-NY Overlap"
    if 13 <= hour_utc < 21:
        return "NY"
    if 8 <= hour_utc < 13:
        return "London"
    return "Asia"


def _hold_bin(mins: float) -> str:
    if pd.isna(mins) or mins < 0:
        return "unknown"
    if mins < 5:
        return "<5min"
    if mins < 30:
        return "5-30min"
    if mins < 120:
        return "30-120min"
    if mins < 480:
        return "2-8h"
    return ">8h"


def _score_bin(score: float) -> str:
    if pd.isna(score):
        return "unknown"
    edges = [(0, 40), (40, 60), (60, 65), (65, 70), (70, 75), (75, 80), (80, 100)]
    for lo, hi in edges:
        if lo <= score < hi:
            return f"{lo}-{hi}"
    if score >= 100:
        return "100+"
    return "unknown"


# ---------- Loader ----------
def _candidate_history_files() -> list[str]:
    paths = []
    if os.path.exists(config.EXCEL_LOG_PATH):
        paths.append(config.EXCEL_LOG_PATH)
    root = os.path.dirname(os.path.abspath(config.__file__))
    for p in sorted(glob.glob(os.path.join(root, "KARA_History_*.xlsx")), reverse=True):
        paths.append(p)
    root_th = os.path.join(root, "trade_history.xlsx")
    if os.path.exists(root_th) and root_th not in paths:
        paths.append(root_th)
    return paths


def load_closed_trades(days: int = 7, now: Optional[datetime] = None) -> pd.DataFrame:
    """
    Load closed trades from the last `days` days. Returns DataFrame with
    canonical columns + derived: session, hour_utc, hold_minutes (if
    open/close pair matched), score_bin, hold_bin.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    frames = []
    for path in _candidate_history_files():
        try:
            df = pd.read_excel(path, engine="openpyxl")
        except Exception:
            continue
        df.columns = [c.strip() if isinstance(c, str) else c for c in df.columns]
        df = _normalize_columns(df)
        if "timestamp" not in df.columns:
            continue
        df["__source__"] = os.path.basename(path)
        frames.append(df)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    df = df.dropna(subset=["timestamp"])
    # Filter to time window
    df = df[df["timestamp"] >= cutoff].copy()

    # Focus on close events
    if "action" in df.columns:
        closed = df[df["action"].astype(str).str.lower() == "close"].copy()
    else:
        closed = df.copy()

    if closed.empty:
        return closed

    # Derive hold_minutes from paired open (only if position_id present)
    if "position_id" in df.columns and "position_id" in closed.columns:
        opens = df[df.get("action", "").astype(str).str.lower() == "open"][
            ["position_id", "timestamp"]
        ].rename(columns={"timestamp": "__open_ts__"})
        closed = closed.merge(opens, on="position_id", how="left")
        derived_hold = (closed["timestamp"] - closed["__open_ts__"]).dt.total_seconds() / 60.0
        if "hold_minutes" in closed.columns:
            closed["hold_minutes"] = pd.to_numeric(closed["hold_minutes"], errors="coerce")
            closed["hold_minutes"] = closed["hold_minutes"].fillna(derived_hold)
        else:
            closed["hold_minutes"] = derived_hold

    if "hold_minutes" not in closed.columns:
        closed["hold_minutes"] = float("nan")

    closed["hour_utc"] = closed["timestamp"].dt.hour
    if "session" not in closed.columns:
        closed["session"] = closed["hour_utc"].map(_label_session)
    else:
        # Fill missing with derived
        closed["session"] = closed["session"].fillna(closed["hour_utc"].map(_label_session))

    if "score" in closed.columns:
        closed["score"] = pd.to_numeric(closed["score"], errors="coerce")
        closed["score_bin"] = closed["score"].map(_score_bin)
    else:
        closed["score_bin"] = "unknown"

    closed["hold_bin"] = closed["hold_minutes"].map(_hold_bin)
    if "pnl" in closed.columns:
        closed["pnl"] = pd.to_numeric(closed["pnl"], errors="coerce")
    if "pnl_pct" in closed.columns:
        closed["pnl_pct"] = pd.to_numeric(closed["pnl_pct"], errors="coerce")

    return closed


# ---------- Wilson score interval ----------
def _wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + (z * z) / n
    center = (p + (z * z) / (2 * n)) / denom
    margin = (z * math.sqrt((p * (1 - p) + (z * z) / (4 * n)) / n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


# ---------- Bucket stats ----------
@dataclass
class BucketStat:
    group_key: str          # e.g. "asset=BTC | side=LONG"
    group_cols: list[str]
    values: dict            # {col_name: value}
    n: int
    wins: int
    winrate: float
    avg_pnl: float
    total_pnl: float
    avg_win: float
    avg_loss: float
    expectancy: float
    profit_factor: float | None
    wilson_lo: float
    wilson_hi: float
    significant: bool       # n>=30 AND (wilson_lo>0.55 OR wilson_hi<0.45)


def compute_bucket_stats(df: pd.DataFrame, group_cols: list[str]) -> list[BucketStat]:
    if df.empty or "pnl" not in df.columns:
        return []
    missing = [c for c in group_cols if c not in df.columns]
    if missing:
        return []
    out: list[BucketStat] = []
    for keys, grp in df.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        pnl = grp["pnl"].dropna()
        n = len(pnl)
        if n == 0:
            continue
        wins_series = pnl[pnl > 0]
        losses_series = pnl[pnl <= 0]
        wins = int((pnl > 0).sum())
        wr = wins / n
        avg_win = float(wins_series.mean()) if len(wins_series) else 0.0
        avg_loss = float(losses_series.mean()) if len(losses_series) else 0.0
        expectancy = wr * avg_win + (1 - wr) * avg_loss
        total_loss = float(losses_series.sum())
        total_win = float(wins_series.sum())
        pf = (total_win / abs(total_loss)) if total_loss < 0 else None
        lo, hi = _wilson_ci(wins, n)
        significant = (n >= 30) and (lo > 0.55 or hi < 0.45)
        values = {c: (str(k) if k is not None else "None") for c, k in zip(group_cols, keys)}
        group_key = " | ".join(f"{c}={v}" for c, v in values.items())
        out.append(BucketStat(
            group_key=group_key,
            group_cols=list(group_cols),
            values=values,
            n=n,
            wins=wins,
            winrate=round(wr, 4),
            avg_pnl=round(float(pnl.mean()), 4),
            total_pnl=round(float(pnl.sum()), 4),
            avg_win=round(avg_win, 4),
            avg_loss=round(avg_loss, 4),
            expectancy=round(expectancy, 4),
            profit_factor=round(pf, 3) if pf is not None else None,
            wilson_lo=round(lo, 4),
            wilson_hi=round(hi, 4),
            significant=significant,
        ))
    # Sort worst-expectancy first (most actionable for the analyst)
    out.sort(key=lambda b: b.expectancy)
    return out


DEFAULT_BUCKET_SETS: list[list[str]] = [
    ["asset"],
    ["side"],
    ["session"],
    ["hour_utc"],
    ["score_bin"],
    ["reason"],
    ["mode"],
    ["hold_bin"],
    ["asset", "side"],
    ["session", "side"],
    ["score_bin", "reason"],
]


def overall_summary(df: pd.DataFrame) -> dict:
    if df.empty or "pnl" not in df.columns:
        return {"total_trades": 0}
    pnl = df["pnl"].dropna()
    n = len(pnl)
    wins = int((pnl > 0).sum())
    wr = wins / n if n else 0.0
    win_sum = float(pnl[pnl > 0].sum())
    loss_sum = float(pnl[pnl <= 0].sum())
    pf = (win_sum / abs(loss_sum)) if loss_sum < 0 else None
    expectancy = float(pnl.mean()) if n else 0.0
    return {
        "total_trades": n,
        "winrate": round(wr, 4),
        "total_pnl": round(float(pnl.sum()), 4),
        "expectancy": round(expectancy, 4),
        "profit_factor": round(pf, 3) if pf else None,
        "avg_win": round(float(pnl[pnl > 0].mean()) if wins else 0.0, 4),
        "avg_loss": round(float(pnl[pnl <= 0].mean()) if n - wins else 0.0, 4),
    }


def to_evidence_pack(
    df: pd.DataFrame,
    bucket_sets: Optional[list[list[str]]] = None,
    top_n_per_bucket: int = 12,
    min_bucket_n: int = 5,
) -> dict:
    """Serializable evidence pack for LLM. Size-capped."""
    bucket_sets = bucket_sets or DEFAULT_BUCKET_SETS
    pack = {
        "window": {
            "trade_count": int(len(df)),
            "first_ts": str(df["timestamp"].min()) if not df.empty else None,
            "last_ts": str(df["timestamp"].max()) if not df.empty else None,
        },
        "overall": overall_summary(df),
        "buckets": {},
    }
    for cols in bucket_sets:
        stats = compute_bucket_stats(df, cols)
        stats = [s for s in stats if s.n >= min_bucket_n]
        # Take worst top_n/2 and best top_n/2 for signal
        half = max(1, top_n_per_bucket // 2)
        worst = stats[:half]
        best = sorted(stats, key=lambda b: -b.expectancy)[:half]
        merged = {b.group_key: asdict(b) for b in worst + best}
        pack["buckets"]["+".join(cols)] = list(merged.values())
    return pack
