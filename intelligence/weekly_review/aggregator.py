"""
Deterministic stat aggregator for weekly AI audit.

LLM is NOT trusted to compute stats. This module produces the numerical evidence
pack that the analyst reasons over — from every durable source we have:
  - SQLite trade_history (all users)
  - Excel trade log (EXCEL_LOG_PATH / STORAGE)
  - Optional KARA_History_*.xlsx exports
"""

from __future__ import annotations

import glob
import math
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd

import config

# ---------- Column normalization ----------
# Live schemas coexist (excel_logger vs historical exports vs SQLite JSON).
_ALIASES = {
    "score": ["Score", "Signal Score", "signal_score", "entry_score"],
    "reason": ["Reason", "Exit Reason", "exit_reason", "data_reason"],
    "asset": ["Asset", "asset", "Symbol", "symbol"],
    "side": ["Side", "side"],
    "action": ["Action", "action", "type"],
    "pnl": ["PnL ($)", "PnL", "pnl", "pnl_usd"],
    "pnl_pct": ["PnL (%)", "pnl_pct", "pnl_roe"],
    "timestamp": ["Timestamp", "timestamp", "Time", "created_at", "exit_time"],
    "position_id": ["Position ID", "position_id", "PosID", "pos_id"],
    "mode": ["Mode", "mode", "trade_mode"],
    "session": ["Session", "session"],
    "regime": ["Regime", "regime"],
    "cvd_snapshot": ["CVD Snapshot", "cvd_snapshot"],
    "rsi_snapshot": ["RSI Snapshot", "rsi_snapshot"],
    "hold_minutes": ["Hold Minutes", "hold_minutes"],
    "entry_time": ["Entry Time", "entry_time"],
    "exit_time": ["Exit Time", "exit_time"],
    "leverage": ["Leverage", "leverage", "lev"],
    "notional": ["Notional (USD)", "notional", "Notional"],
    "setup_type": ["Setup Type", "setup_type"],
    "meta_pattern_key": ["Meta Pattern", "meta_pattern_key"],
    "chat_id": ["Chat ID", "chat_id"],
    "price": ["Price", "price", "exit_price", "entry_price"],
}


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename any recognized column to canonical snake_case name."""
    rename = {}
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    for canonical, aliases in _ALIASES.items():
        if canonical in df.columns:
            continue
        for a in aliases:
            if a in df.columns:
                rename[a] = canonical
                break
            # case-insensitive match
            hit = lower_map.get(str(a).lower())
            if hit is not None and hit not in rename:
                rename[hit] = canonical
                break
    return df.rename(columns=rename)


def _label_session(hour_utc: int) -> str:
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
    if mins < 12:
        return "5-12min"
    if mins < 30:
        return "12-30min"
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


def _lev_bin(lev: float) -> str:
    if pd.isna(lev):
        return "unknown"
    if lev < 10:
        return "<10x"
    if lev < 20:
        return "10-19x"
    if lev < 30:
        return "20-29x"
    return "30x+"


def _candidate_history_files() -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()

    def _add(p: str) -> None:
        ap = os.path.abspath(p)
        if ap not in seen and os.path.exists(ap):
            seen.add(ap)
            paths.append(ap)

    _add(config.EXCEL_LOG_PATH)
    storage = getattr(config, "STORAGE_BASE", "data")
    _add(os.path.join(storage, "trade_history.xlsx"))
    root = os.path.dirname(os.path.abspath(config.__file__))
    for p in sorted(glob.glob(os.path.join(root, "KARA_History_*.xlsx")), reverse=True):
        _add(p)
    _add(os.path.join(root, "trade_history.xlsx"))
    _add(os.path.join(root, "data", "trade_history.xlsx"))
    return paths


def _df_from_sqlite(since_ts: Optional[float] = None) -> pd.DataFrame:
    """Load closed trades from SQLite (all users)."""
    try:
        from core.db import user_db

        rows = user_db.get_all_trade_history(limit=50_000, since_ts=since_ts)
    except Exception:
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()

    normalized: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        # Flatten common nested keys
        ts = item.get("timestamp") or item.get("created_at") or item.get("exit_time")
        if isinstance(ts, (int, float)):
            # unix seconds vs ms
            if ts > 1e12:
                ts = ts / 1000.0
            item["timestamp"] = datetime.fromtimestamp(ts, tz=timezone.utc)
        action = str(item.get("action") or item.get("type") or "close").lower()
        item["action"] = action
        if item.get("pnl") is None and item.get("pnl_usd") is not None:
            item["pnl"] = item.get("pnl_usd")
        if item.get("score") is None and item.get("entry_score") is not None:
            item["score"] = item.get("entry_score")
        if item.get("mode") is None and item.get("trade_mode") is not None:
            item["mode"] = item.get("trade_mode")
        if item.get("reason") is None:
            item["reason"] = item.get("exit_reason") or item.get("data_reason") or ""
        normalized.append(item)

    df = pd.DataFrame(normalized)
    df["__source__"] = "sqlite"
    return df


def _df_from_excel_files() -> pd.DataFrame:
    frames = []
    for path in _candidate_history_files():
        try:
            df = pd.read_excel(path, engine="openpyxl")
        except Exception:
            continue
        df.columns = [c.strip() if isinstance(c, str) else c for c in df.columns]
        df = _normalize_columns(df)
        if "timestamp" not in df.columns and "created_at" not in df.columns:
            continue
        df["__source__"] = os.path.basename(path)
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _coerce_timestamp_series(s: pd.Series) -> pd.Series:
    """Parse mixed timestamp formats (datetime, ISO, unix s/ms)."""
    out = pd.to_datetime(s, errors="coerce", utc=True)
    # Retry numeric unix
    mask = out.isna()
    if mask.any():
        nums = pd.to_numeric(s[mask], errors="coerce")
        # ms vs s
        as_s = nums.where(nums < 1e12, nums / 1000.0)
        out.loc[mask] = pd.to_datetime(as_s, unit="s", errors="coerce", utc=True)
    return out


def _prepare_closed(df: pd.DataFrame, cutoff: datetime, now: datetime) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = _normalize_columns(df.copy())
    if "timestamp" not in df.columns:
        return pd.DataFrame()

    df["timestamp"] = _coerce_timestamp_series(df["timestamp"])
    df = df.dropna(subset=["timestamp"])
    df = df[(df["timestamp"] >= cutoff) & (df["timestamp"] <= now)].copy()

    if "action" in df.columns:
        act = df["action"].astype(str).str.lower()
        closed = df[act.isin(["close", "fully_closed", "closed", "exit"])].copy()
        if closed.empty:
            # Some SQLite rows only have type=close already filtered
            closed = df.copy()
    else:
        closed = df.copy()

    if closed.empty:
        return closed

    # Hold minutes
    if "position_id" in df.columns and "position_id" in closed.columns:
        try:
            opens = df[df.get("action", pd.Series(dtype=str)).astype(str).str.lower() == "open"][
                ["position_id", "timestamp"]
            ].rename(columns={"timestamp": "__open_ts__"})
            if not opens.empty:
                closed = closed.merge(opens, on="position_id", how="left")
                derived_hold = (closed["timestamp"] - closed["__open_ts__"]).dt.total_seconds() / 60.0
                if "hold_minutes" in closed.columns:
                    closed["hold_minutes"] = pd.to_numeric(closed["hold_minutes"], errors="coerce")
                    closed["hold_minutes"] = closed["hold_minutes"].fillna(derived_hold)
                else:
                    closed["hold_minutes"] = derived_hold
        except Exception:
            pass

    if "hold_minutes" not in closed.columns:
        closed["hold_minutes"] = float("nan")
    else:
        closed["hold_minutes"] = pd.to_numeric(closed["hold_minutes"], errors="coerce")

    closed["hour_utc"] = closed["timestamp"].dt.hour
    if "session" not in closed.columns:
        closed["session"] = closed["hour_utc"].map(_label_session)
    else:
        closed["session"] = closed["session"].fillna(closed["hour_utc"].map(_label_session))
        blank = closed["session"].astype(str).str.strip().isin(["", "nan", "None"])
        closed.loc[blank, "session"] = closed.loc[blank, "hour_utc"].map(_label_session)

    if "score" in closed.columns:
        closed["score"] = pd.to_numeric(closed["score"], errors="coerce")
        closed["score_bin"] = closed["score"].map(_score_bin)
    else:
        closed["score_bin"] = "unknown"

    closed["hold_bin"] = closed["hold_minutes"].map(_hold_bin)

    if "leverage" in closed.columns:
        closed["leverage"] = pd.to_numeric(closed["leverage"], errors="coerce")
        closed["lev_bin"] = closed["leverage"].map(_lev_bin)
    else:
        closed["lev_bin"] = "unknown"

    if "pnl" in closed.columns:
        closed["pnl"] = pd.to_numeric(closed["pnl"], errors="coerce")
    if "pnl_pct" in closed.columns:
        closed["pnl_pct"] = pd.to_numeric(closed["pnl_pct"], errors="coerce")

    # Normalize side / mode / reason
    if "side" in closed.columns:
        closed["side"] = closed["side"].astype(str).str.upper().str.strip()
    if "mode" in closed.columns:
        closed["mode"] = closed["mode"].astype(str).str.lower().str.strip()
    if "reason" in closed.columns:
        closed["reason"] = closed["reason"].astype(str).str.strip()
    if "asset" in closed.columns:
        closed["asset"] = closed["asset"].astype(str).str.upper().str.strip()

    # Dedupe by position_id + timestamp when available
    if "position_id" in closed.columns:
        closed = closed.drop_duplicates(subset=["position_id", "timestamp"], keep="last")
    else:
        closed = closed.drop_duplicates(keep="last")

    return closed


def load_closed_trades(days: int = 7, now: Optional[datetime] = None) -> pd.DataFrame:
    """
    Load closed trades from the last `days` days from SQLite + Excel + history files.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=days)
    since_ts = cutoff.timestamp()

    frames = []
    sql_df = _df_from_sqlite(since_ts=since_ts - 86400)  # 1d padding for opens
    if not sql_df.empty:
        frames.append(sql_df)
    xls_df = _df_from_excel_files()
    if not xls_df.empty:
        frames.append(xls_df)

    if not frames:
        return pd.DataFrame()

    raw = pd.concat(frames, ignore_index=True, sort=False)
    return _prepare_closed(raw, cutoff=cutoff, now=now)


def load_all_available_closed(now: Optional[datetime] = None, max_days: int = 365) -> pd.DataFrame:
    """Load as much closed-trade history as available (capped)."""
    return load_closed_trades(days=max_days, now=now)


# ---------- Wilson score interval ----------
def _wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + (z * z) / n
    center = (p + (z * z) / (2 * n)) / denom
    margin = (z * math.sqrt((p * (1 - p) + (z * z) / (4 * n)) / n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


@dataclass
class BucketStat:
    group_key: str
    group_cols: list[str]
    values: dict
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
    significant: bool


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
    ["lev_bin"],
    ["asset", "side"],
    ["session", "side"],
    ["score_bin", "side"],
    ["score_bin", "reason"],
    ["mode", "side"],
    ["hold_bin", "side"],
    ["reason", "side"],
]


def overall_summary(df: pd.DataFrame) -> dict:
    if df.empty or "pnl" not in df.columns:
        return {"total_trades": 0}
    pnl = df["pnl"].dropna()
    n = len(pnl)
    wins = int((pnl > 0).sum())
    losses = n - wins
    wr = wins / n if n else 0.0
    win_sum = float(pnl[pnl > 0].sum())
    loss_sum = float(pnl[pnl <= 0].sum())
    pf = (win_sum / abs(loss_sum)) if loss_sum < 0 else None
    expectancy = float(pnl.mean()) if n else 0.0

    # Streaks
    signs = (pnl > 0).astype(int).tolist()
    max_win_streak = max_loss_streak = cur_w = cur_l = 0
    for s in signs:
        if s:
            cur_w += 1
            cur_l = 0
        else:
            cur_l += 1
            cur_w = 0
        max_win_streak = max(max_win_streak, cur_w)
        max_loss_streak = max(max_loss_streak, cur_l)

    # Equity curve + max DD (unit start=0 cumulative pnl)
    cum = pnl.sort_index().cumsum() if hasattr(pnl, "sort_index") else pnl.cumsum()
    # Prefer chronological if timestamp present
    if "timestamp" in df.columns:
        ordered = df.dropna(subset=["pnl"]).sort_values("timestamp")
        cum = ordered["pnl"].cumsum()
        peak = cum.cummax()
        dd = cum - peak
        max_dd = float(dd.min()) if len(dd) else 0.0
    else:
        peak = cum.cummax()
        dd = cum - peak
        max_dd = float(dd.min()) if len(dd) else 0.0

    median_hold = None
    if "hold_minutes" in df.columns:
        hm = pd.to_numeric(df["hold_minutes"], errors="coerce").dropna()
        if len(hm):
            median_hold = round(float(hm.median()), 2)

    avg_score = None
    if "score" in df.columns:
        sc = pd.to_numeric(df["score"], errors="coerce").dropna()
        if len(sc):
            avg_score = round(float(sc.mean()), 2)

    return {
        "total_trades": n,
        "wins": wins,
        "losses": losses,
        "winrate": round(wr, 4),
        "total_pnl": round(float(pnl.sum()), 4),
        "expectancy": round(expectancy, 4),
        "profit_factor": round(pf, 3) if pf else None,
        "avg_win": round(float(pnl[pnl > 0].mean()) if wins else 0.0, 4),
        "avg_loss": round(float(pnl[pnl <= 0].mean()) if losses else 0.0, 4),
        "median_pnl": round(float(pnl.median()), 4) if n else 0.0,
        "std_pnl": round(float(pnl.std(ddof=1)), 4) if n > 1 else 0.0,
        "max_win": round(float(pnl.max()), 4) if n else 0.0,
        "max_loss": round(float(pnl.min()), 4) if n else 0.0,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "max_drawdown_usd": round(max_dd, 4),
        "median_hold_minutes": median_hold,
        "avg_entry_score": avg_score,
        "wilson_winrate_ci": [round(x, 4) for x in _wilson_ci(wins, n)],
    }


def _extreme_trades(df: pd.DataFrame, n: int = 8) -> dict:
    """Best/worst closed trades for qualitative context (capped)."""
    if df.empty or "pnl" not in df.columns:
        return {"best": [], "worst": []}
    cols = [c for c in [
        "timestamp", "asset", "side", "pnl", "pnl_pct", "score", "reason",
        "mode", "hold_minutes", "session", "leverage",
    ] if c in df.columns]
    work = df.dropna(subset=["pnl"]).copy()
    best = work.nlargest(n, "pnl")[cols]
    worst = work.nsmallest(n, "pnl")[cols]
    return {
        "best": _records_safe(best),
        "worst": _records_safe(worst),
    }


def _records_safe(df: pd.DataFrame) -> list[dict]:
    rows = []
    for _, r in df.iterrows():
        item = {}
        for k, v in r.items():
            if pd.isna(v):
                item[k] = None
            elif isinstance(v, pd.Timestamp):
                item[k] = str(v)
            else:
                try:
                    item[k] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
                except Exception:
                    item[k] = str(v)
        rows.append(item)
    return rows


def _daily_pnl_series(df: pd.DataFrame) -> list[dict]:
    if df.empty or "timestamp" not in df.columns or "pnl" not in df.columns:
        return []
    work = df.dropna(subset=["timestamp", "pnl"]).copy()
    work["day"] = work["timestamp"].dt.strftime("%Y-%m-%d")
    out = []
    for day, grp in work.groupby("day"):
        pnl = grp["pnl"]
        out.append({
            "day": day,
            "n": int(len(pnl)),
            "pnl": round(float(pnl.sum()), 4),
            "winrate": round(float((pnl > 0).mean()), 4) if len(pnl) else 0.0,
        })
    return sorted(out, key=lambda x: x["day"])


def _data_quality(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"empty": True}
    n = len(df)
    def miss(col: str) -> float:
        if col not in df.columns:
            return 1.0
        return float(df[col].isna().mean())
    sources = {}
    if "__source__" in df.columns:
        sources = df["__source__"].astype(str).value_counts().to_dict()
        sources = {str(k): int(v) for k, v in sources.items()}
    return {
        "empty": False,
        "n": n,
        "sources": sources,
        "missing_rate": {
            "pnl": round(miss("pnl"), 3),
            "score": round(miss("score"), 3),
            "reason": round(miss("reason"), 3),
            "hold_minutes": round(miss("hold_minutes"), 3),
            "side": round(miss("side"), 3),
            "asset": round(miss("asset"), 3),
            "mode": round(miss("mode"), 3),
            "leverage": round(miss("leverage"), 3),
        },
        "unique_assets": int(df["asset"].nunique()) if "asset" in df.columns else 0,
        "unique_reasons": int(df["reason"].nunique()) if "reason" in df.columns else 0,
    }


def to_evidence_pack(
    df: pd.DataFrame,
    bucket_sets: Optional[list[list[str]]] = None,
    top_n_per_bucket: int = 12,
    min_bucket_n: int = 5,
    baseline_df: Optional[pd.DataFrame] = None,
    lookback_days: int = 7,
    baseline_days: int = 30,
) -> dict:
    """Serializable evidence pack for LLM. Size-capped. Includes baseline comparison."""
    bucket_sets = bucket_sets or DEFAULT_BUCKET_SETS
    pack: dict[str, Any] = {
        "window": {
            "lookback_days": lookback_days,
            "trade_count": int(len(df)),
            "first_ts": str(df["timestamp"].min()) if not df.empty and "timestamp" in df.columns else None,
            "last_ts": str(df["timestamp"].max()) if not df.empty and "timestamp" in df.columns else None,
        },
        "overall": overall_summary(df),
        "data_quality": _data_quality(df),
        "daily_pnl": _daily_pnl_series(df),
        "extreme_trades": _extreme_trades(df),
        "buckets": {},
        "runtime_context": {
            "trade_mode": getattr(config, "TRADE_MODE", None),
            "trading_mode": getattr(config, "TRADING_MODE", None),
            "force_scalper_only": getattr(config, "FORCE_SCALPER_ONLY", None),
            "std_signal_fallback": getattr(config, "STANDARD_SIGNAL_AS_SCALPER_FALLBACK", None),
            "full_auto": getattr(config, "FULL_AUTO", None),
            "data_source": getattr(config, "DATA_SOURCE", None),
            "allow_short": getattr(config, "ALLOW_SHORT", None),
            "kara_version": getattr(config, "KARA_VERSION", None),
        },
    }

    for cols in bucket_sets:
        stats = compute_bucket_stats(df, cols)
        stats = [s for s in stats if s.n >= min_bucket_n]
        half = max(1, top_n_per_bucket // 2)
        worst = stats[:half]
        best = sorted(stats, key=lambda b: -b.expectancy)[:half]
        # Keep significant buckets even if mid-ranked
        sig = [s for s in stats if s.significant][:6]
        merged = {b.group_key: asdict(b) for b in worst + best + sig}
        pack["buckets"]["+".join(cols)] = list(merged.values())

    # Baseline comparison (prior longer window excluding current window bias note)
    if baseline_df is not None and not baseline_df.empty:
        pack["baseline"] = {
            "lookback_days": baseline_days,
            "overall": overall_summary(baseline_df),
            "window": {
                "trade_count": int(len(baseline_df)),
                "first_ts": str(baseline_df["timestamp"].min()) if "timestamp" in baseline_df.columns else None,
                "last_ts": str(baseline_df["timestamp"].max()) if "timestamp" in baseline_df.columns else None,
            },
            "note": (
                "Baseline is a longer window for stability checks. "
                "Do NOT treat a 1-week flip vs baseline as causal without sample size + CI support."
            ),
        }
        # Side / reason deltas vs baseline (high-signal)
        for key_cols in (["side"], ["reason"], ["mode"]):
            w = {b.group_key: asdict(b) for b in compute_bucket_stats(df, key_cols) if b.n >= 5}
            b = {b.group_key: asdict(b) for b in compute_bucket_stats(baseline_df, key_cols) if b.n >= 10}
            deltas = []
            for gk, ws in w.items():
                bs = b.get(gk)
                if not bs:
                    continue
                deltas.append({
                    "group": gk,
                    "week_n": ws["n"],
                    "base_n": bs["n"],
                    "week_exp": ws["expectancy"],
                    "base_exp": bs["expectancy"],
                    "exp_delta": round(ws["expectancy"] - bs["expectancy"], 4),
                    "week_wr": ws["winrate"],
                    "base_wr": bs["winrate"],
                })
            deltas.sort(key=lambda x: x["exp_delta"])
            pack.setdefault("vs_baseline", {})["+".join(key_cols)] = deltas[:10]

    return pack


def build_full_evidence(
    lookback_days: int = 7,
    baseline_days: int = 30,
    now: Optional[datetime] = None,
) -> tuple[pd.DataFrame, dict]:
    """Convenience: load window + baseline and produce evidence pack."""
    now = now or datetime.now(timezone.utc)
    df = load_closed_trades(days=lookback_days, now=now)
    baseline = load_closed_trades(days=baseline_days, now=now)
    pack = to_evidence_pack(
        df,
        baseline_df=baseline,
        lookback_days=lookback_days,
        baseline_days=baseline_days,
    )
    return df, pack
