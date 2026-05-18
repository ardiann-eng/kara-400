"""
KARA Score Audit — Phase 2 Analysis
Loads production data (338 trades + 338 signals) and runs:
- Correlation analysis
- Random Forest feature importance
- Permutation importance
- Score decomposition (linear regression to recover weights)
- Decile analysis
- Saves all results to audit_score_analysis/
"""
import json
import os
import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from scipy.stats import pearsonr, spearmanr
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import train_test_split

OUT_DIR = "audit_score_analysis"
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(f"{OUT_DIR}/data", exist_ok=True)

# ─────────────────────────────────────────────────────────────────
# 1. LOAD & FLATTEN
# ─────────────────────────────────────────────────────────────────
with open("tmp/trades_prod.json", encoding="utf-8") as f:
    trades_raw = json.load(f)
with open("tmp/signals_prod.json", encoding="utf-8") as f:
    signals_raw = json.load(f)
with open("tmp/meta_prod.json", encoding="utf-8") as f:
    meta_raw = json.load(f)

print(f"Loaded: {len(trades_raw)} trades, {len(signals_raw)} signals, {len(meta_raw)} meta")

# Build trades DataFrame
trade_rows = []
for t in trades_raw:
    try:
        d = json.loads(t["data"])
    except Exception:
        d = {}
    row = {
        "trade_id":    t["trade_id"],
        "chat_id":     t["chat_id"],
        "asset":       t["asset"],
        "side":        t["side"],
        "pnl_usd":     float(t["pnl_usd"] or 0),
        "pnl_pct":     float(t["pnl_pct"] or 0),
        "created_at":  float(t["created_at"]),
        "exit_reason": d.get("reason", "unknown"),
        "entry_price": d.get("entry_price"),
        "exit_price":  d.get("exit_price"),
        "size":        d.get("size"),
        "notional":    d.get("notional"),
        "score":       d.get("score"),
        "pos_id":      d.get("pos_id"),
    }
    trade_rows.append(row)
trades = pd.DataFrame(trade_rows)
trades["outcome"] = (trades["pnl_usd"] > 0).astype(int)
trades["dt"] = pd.to_datetime(trades["created_at"], unit="s")

# Build signals DataFrame (one row per signal, with breakdown flattened)
sig_rows = []
for s in signals_raw:
    try:
        d = json.loads(s["data"])
    except Exception:
        d = {}
    bd = d.get("breakdown", {})
    # parse reasons text for additional features (count of bullish/bearish reasons)
    reasons = bd.get("reasons", [])
    reasons_txt = " | ".join(reasons).lower() if reasons else ""

    row = {
        "sig_id":             s["sig_id"],
        "asset":              s["asset"],
        "side":               s["side"],
        "score":              s["score"],
        "price":              s["price"],
        "created_at":         float(s["created_at"]),
        # breakdown components
        "oi_funding_score":   bd.get("oi_funding_score", 0),
        "liquidation_score":  bd.get("liquidation_score", 0),
        "orderbook_score":    bd.get("orderbook_score", 0),
        "session_bonus":      bd.get("session_bonus", 0),
        "regime_multiplier":  bd.get("regime_multiplier", 1.0),
        "total_bull":         bd.get("total_bull", 0),
        "total_bear":         bd.get("total_bear", 0),
        "raw_score":          bd.get("raw_score", s["score"]),
        "final_score":        bd.get("final_score", s["score"]),
        # signal envelope
        "regime":             d.get("regime", "unknown"),
        "strength":           d.get("strength", "unknown"),
        "trade_mode":         d.get("trade_mode", "unknown"),
        "realized_vol":       d.get("realized_vol", 0),
        "entry_atr":          d.get("entry_atr", 0),
        "funding_rate":       d.get("funding_rate", 0),
        "suggested_leverage": d.get("suggested_leverage", 0),
        "stop_loss":          d.get("stop_loss", 0),
        "tp1":                d.get("tp1", 0),
        "tp2":                d.get("tp2", 0),
        "n_reasons":          len(reasons),
        # extracted feature flags from reasons text
        "has_rsi_oversold":      int("rsi oversold" in reasons_txt),
        "has_rsi_overbought":    int("rsi overbought" in reasons_txt),
        "has_rsi_divergence":    int("rsi divergence" in reasons_txt),
        "has_volume_surge":      int("volume surge" in reasons_txt),
        "has_mtf_align":         int("mtf align" in reasons_txt),
        "has_ny_session":        int("ny session" in reasons_txt),
        "has_london_session":    int("london session" in reasons_txt),
        "has_ema_bullish":       int("→ bullish" in reasons_txt or "-> bullish" in reasons_txt),
        "has_ema_bearish":       int("→ bearish" in reasons_txt or "-> bearish" in reasons_txt),
        "has_strong_imbalance":  int("strong" in reasons_txt and "imbalance" in reasons_txt),
        "has_cvd_bullish":       int("cvd bullish" in reasons_txt),
        "has_cvd_bearish":       int("cvd bearish" in reasons_txt),
        "has_funding_extreme":   int("extreme" in reasons_txt and "funding" in reasons_txt),
    }
    sig_rows.append(row)
signals = pd.DataFrame(sig_rows)
signals["dt"] = pd.to_datetime(signals["created_at"], unit="s")

# ─────────────────────────────────────────────────────────────────
# 2. JOIN: trades ↔ signals via timestamp+asset+side proximity
# ─────────────────────────────────────────────────────────────────
# Trades have NO direct sig_id link. Trade exit_time vs signal entry_time.
# Strategy: for each trade, find the most recent signal of same (asset, side)
# whose timestamp is BEFORE the trade.created_at (=exit timestamp).
# Trade pos_id holds entry timestamp implicitly. We use exit timestamp - typical hold
# (15-30m) to estimate entry, then match nearest signal.
#
# Simpler: for each trade, take signals where asset=trade.asset, side=trade.side,
# signal.created_at < trade.created_at, and pick the closest one (largest signal time).

joined_rows = []
sig_indexed = signals.sort_values("created_at")
sig_by_key = {(a, s): grp for (a, s), grp in sig_indexed.groupby(["asset", "side"])}

for _, t in trades.iterrows():
    key = (t["asset"], t["side"])
    grp = sig_by_key.get(key)
    if grp is None or len(grp) == 0:
        continue
    # signals BEFORE this trade exit
    before = grp[grp["created_at"] < t["created_at"]]
    if len(before) == 0:
        continue
    sig = before.iloc[-1]  # most recent
    # Sanity: signal should be within last 90 min (prevents cross-day mismatch)
    if (t["created_at"] - sig["created_at"]) > 90 * 60:
        continue
    merged = {**t.to_dict(), **{f"sig_{k}": v for k, v in sig.to_dict().items() if k not in ("asset", "side")}}
    joined_rows.append(merged)

df = pd.DataFrame(joined_rows)
print(f"Joined trades: {len(df)} (from {len(trades)} trades)")
print(f"Match rate: {len(df)/len(trades)*100:.1f}%")

# Save merged dataset
df.to_csv(f"{OUT_DIR}/data/merged_trades_signals.csv", index=False)

# ─────────────────────────────────────────────────────────────────
# 3. SUMMARY STATS
# ─────────────────────────────────────────────────────────────────
total = len(df)
wins = int(df["outcome"].sum())
losses = total - wins
win_rate = wins / total * 100 if total else 0
total_pnl = df["pnl_usd"].sum()
avg_win = df[df["outcome"] == 1]["pnl_usd"].mean()
avg_loss = df[df["outcome"] == 0]["pnl_usd"].mean()
profit_factor = abs(avg_win / avg_loss) if avg_loss else 0
expectancy = (win_rate / 100) * avg_win + (1 - win_rate / 100) * avg_loss

print(f"\n=== SUMMARY ===")
print(f"Total: {total} | Wins: {wins} | Losses: {losses}")
print(f"Win Rate: {win_rate:.1f}%")
print(f"Total PnL: ${total_pnl:.2f}")
print(f"Avg Win: ${avg_win:.2f} | Avg Loss: ${avg_loss:.2f}")
print(f"Profit Factor: {profit_factor:.3f}")
print(f"Expectancy/trade: ${expectancy:.4f}")

# ─────────────────────────────────────────────────────────────────
# 4. FEATURE LIST
# ─────────────────────────────────────────────────────────────────
features = [
    "sig_score", "sig_oi_funding_score", "sig_liquidation_score",
    "sig_orderbook_score", "sig_session_bonus", "sig_regime_multiplier",
    "sig_total_bull", "sig_total_bear", "sig_raw_score",
    "sig_realized_vol", "sig_entry_atr", "sig_funding_rate",
    "sig_suggested_leverage", "sig_n_reasons",
    "sig_has_rsi_oversold", "sig_has_rsi_overbought", "sig_has_rsi_divergence",
    "sig_has_volume_surge", "sig_has_mtf_align",
    "sig_has_ny_session", "sig_has_london_session",
    "sig_has_ema_bullish", "sig_has_ema_bearish",
    "sig_has_strong_imbalance",
    "sig_has_cvd_bullish", "sig_has_cvd_bearish",
    "sig_has_funding_extreme",
]
features = [f for f in features if f in df.columns]
print(f"\nFeatures: {len(features)}")

# Fill missing
for f in features:
    df[f] = pd.to_numeric(df[f], errors="coerce")
X = df[features].fillna(0.0)
y_class = df["outcome"]
y_pnl = df["pnl_usd"]

# ─────────────────────────────────────────────────────────────────
# 5. CORRELATION
# ─────────────────────────────────────────────────────────────────
print("\n=== CORRELATION (feature vs pnl_usd) ===")
corr_results = []
for f in features:
    if X[f].std() == 0:
        corr_results.append({"feature": f, "pearson": 0.0, "p_value": 1.0, "spearman": 0.0})
        continue
    r, p = pearsonr(X[f], y_pnl)
    rs, _ = spearmanr(X[f], y_pnl)
    corr_results.append({"feature": f, "pearson": r, "p_value": p, "spearman": rs})
corr_df = pd.DataFrame(corr_results).sort_values("pearson", key=abs, ascending=False)
print(corr_df.head(15).to_string(index=False))
corr_df.to_csv(f"{OUT_DIR}/data/correlations.csv", index=False)

# Same for win/loss outcome
print("\n=== CORRELATION (feature vs outcome [win=1]) ===")
corr_out = []
for f in features:
    if X[f].std() == 0:
        corr_out.append({"feature": f, "pearson": 0.0, "p_value": 1.0})
        continue
    r, p = pearsonr(X[f], y_class)
    corr_out.append({"feature": f, "pearson": r, "p_value": p})
corr_out_df = pd.DataFrame(corr_out).sort_values("pearson", key=abs, ascending=False)
print(corr_out_df.head(15).to_string(index=False))
corr_out_df.to_csv(f"{OUT_DIR}/data/correlations_outcome.csv", index=False)

# ─────────────────────────────────────────────────────────────────
# 6. RANDOM FOREST IMPORTANCE
# ─────────────────────────────────────────────────────────────────
print("\n=== RANDOM FOREST IMPORTANCE ===")
X_train, X_test, y_train, y_test = train_test_split(X, y_class, test_size=0.30, random_state=42, stratify=y_class)

rf_clf = RandomForestClassifier(n_estimators=300, max_depth=8, random_state=42, n_jobs=-1)
rf_clf.fit(X_train, y_train)
print(f"RF Train acc: {rf_clf.score(X_train, y_train):.3f}")
print(f"RF Test  acc: {rf_clf.score(X_test, y_test):.3f}")

native_imp = pd.Series(rf_clf.feature_importances_, index=features).sort_values(ascending=False)
perm = permutation_importance(rf_clf, X_test, y_test, n_repeats=20, random_state=42, n_jobs=-1)
perm_imp = pd.Series(perm.importances_mean, index=features).sort_values(ascending=False)

imp_df = pd.DataFrame({
    "feature":          native_imp.index,
    "native":           native_imp.values,
    "permutation":      [perm_imp[f] for f in native_imp.index],
    "permutation_std":  [perm.importances_std[features.index(f)] for f in native_imp.index],
}).sort_values("permutation", ascending=False)
print(imp_df.head(15).to_string(index=False))
imp_df.to_csv(f"{OUT_DIR}/data/feature_importance.csv", index=False)

# Regression target — pnl magnitude
print("\n=== RANDOM FOREST REGRESSOR (pnl) ===")
rf_reg = RandomForestRegressor(n_estimators=300, max_depth=8, random_state=42, n_jobs=-1)
rf_reg.fit(X_train, y_pnl.loc[X_train.index])
r2 = rf_reg.score(X_test, y_pnl.loc[X_test.index])
print(f"R² on test: {r2:.3f}")
perm_reg = permutation_importance(rf_reg, X_test, y_pnl.loc[X_test.index], n_repeats=20, random_state=42, n_jobs=-1)
perm_reg_s = pd.Series(perm_reg.importances_mean, index=features).sort_values(ascending=False)
print(perm_reg_s.head(10).to_string())

# ─────────────────────────────────────────────────────────────────
# 7. SCORE DECOMPOSITION via LINEAR REG
# ─────────────────────────────────────────────────────────────────
print("\n=== SCORE WEIGHT REVERSE-ENGINEER ===")
component_features = ["sig_oi_funding_score", "sig_liquidation_score",
                      "sig_orderbook_score", "sig_session_bonus"]
component_features = [f for f in component_features if f in df.columns]
lr = LinearRegression()
lr.fit(df[component_features].fillna(0), df["sig_final_score"].fillna(df["sig_score"]))
print("Components -> final_score weights:")
for feat, w in zip(component_features, lr.coef_):
    print(f"  {feat:30s}: {w:+.3f}")
print(f"  intercept: {lr.intercept_:+.3f}")
print(f"  R²: {lr.score(df[component_features].fillna(0), df['sig_final_score'].fillna(df['sig_score'])):.3f}")

# Component vs PnL
print("\n=== COMPONENT vs PnL ===")
for f in component_features:
    if df[f].std() == 0:
        print(f"  {f}: zero variance")
        continue
    r, p = pearsonr(df[f].fillna(0), df["pnl_usd"])
    mean_v = df[f].mean()
    print(f"  {f:30s} mean={mean_v:+.2f}  pearson={r:+.3f}  p={p:.3f}")

# ─────────────────────────────────────────────────────────────────
# 8. SCORE DECILE ANALYSIS
# ─────────────────────────────────────────────────────────────────
print("\n=== SCORE DECILE ===")
df["score_decile"] = pd.qcut(df["sig_score"], 10, labels=False, duplicates="drop")
decile_perf = df.groupby("score_decile").agg(
    n=("trade_id", "count"),
    win_rate=("outcome", "mean"),
    avg_pnl=("pnl_usd", "mean"),
    total_pnl=("pnl_usd", "sum"),
    score_min=("sig_score", "min"),
    score_max=("sig_score", "max"),
).reset_index()
print(decile_perf.to_string(index=False))
decile_perf.to_csv(f"{OUT_DIR}/data/score_decile.csv", index=False)

# ─────────────────────────────────────────────────────────────────
# 9. EXIT REASON BREAKDOWN
# ─────────────────────────────────────────────────────────────────
print("\n=== EXIT REASON BREAKDOWN ===")
exit_perf = df.groupby("exit_reason").agg(
    n=("trade_id", "count"),
    win_rate=("outcome", "mean"),
    total_pnl=("pnl_usd", "sum"),
    avg_pnl=("pnl_usd", "mean"),
).sort_values("n", ascending=False).reset_index()
print(exit_perf.to_string(index=False))
exit_perf.to_csv(f"{OUT_DIR}/data/exit_reason.csv", index=False)

# ─────────────────────────────────────────────────────────────────
# 10. SAVE CLEAN DATAFRAME for dashboard step
# ─────────────────────────────────────────────────────────────────
df.to_pickle(f"{OUT_DIR}/data/df.pkl")
imp_df.to_pickle(f"{OUT_DIR}/data/imp.pkl")
corr_df.to_pickle(f"{OUT_DIR}/data/corr.pkl")
corr_out_df.to_pickle(f"{OUT_DIR}/data/corr_out.pkl")
decile_perf.to_pickle(f"{OUT_DIR}/data/decile.pkl")
exit_perf.to_pickle(f"{OUT_DIR}/data/exit_perf.pkl")

# Save summary stats
summary = {
    "total_trades": int(total),
    "wins": int(wins),
    "losses": int(losses),
    "win_rate_pct": round(win_rate, 2),
    "total_pnl": round(total_pnl, 4),
    "avg_win": round(float(avg_win), 4),
    "avg_loss": round(float(avg_loss), 4),
    "profit_factor": round(profit_factor, 3),
    "expectancy_per_trade": round(expectancy, 4),
    "score_pnl_correlation": round(float(df["sig_score"].corr(df["pnl_usd"])), 4),
    "rf_test_accuracy": round(float(rf_clf.score(X_test, y_test)), 4),
    "rf_baseline": round(max(y_class.mean(), 1 - y_class.mean()), 4),
    "rf_regressor_r2": round(float(r2), 4),
    "n_features": len(features),
    "lr_score_R2": round(float(lr.score(df[component_features].fillna(0), df["sig_final_score"].fillna(df["sig_score"]))), 4),
}
with open(f"{OUT_DIR}/data/summary.json", "w") as f:
    json.dump(summary, f, indent=2)

print("\n=== SAVED ===")
print(f"  {OUT_DIR}/data/merged_trades_signals.csv")
print(f"  {OUT_DIR}/data/correlations.csv")
print(f"  {OUT_DIR}/data/feature_importance.csv")
print(f"  {OUT_DIR}/data/score_decile.csv")
print(f"  {OUT_DIR}/data/exit_reason.csv")
print(f"  {OUT_DIR}/data/summary.json")
print("Done.")
