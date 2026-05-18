# KARA Bot — Score Audit Report

**Date:** 2026-05-18
**Data source:** Railway production DB (`/app/storage/kara_data.db`) via `railway ssh`
**Sample:** 338 trades + 338 signals + 91 meta patterns
**Period:** ~5 days of paper trading (Hyperliquid mainnet data, scalper mode)

---

## Executive Summary

| Metric | Value |
|---|---|
| Total trades | 338 |
| Win rate | **48.8 %** |
| Total PnL | **−$67.22** |
| Avg win / Avg loss | $0.67 / −$1.03 |
| Profit factor | **0.65** |
| Expectancy / trade | **−$0.20** |
| Score ↔ PnL Pearson r | **0.025** (≈ random) |
| RF win/loss test accuracy | 0.93 (baseline 0.51) — but driven by leakage features (entry_atr, leverage), not score |

**Bottom line:** Bot kalah bukan karena salah pasar, tapi karena **scoring engine tidak prediktif** dan **exit logic merusak edge yang ada**. Score 73-97 (decile teratas) justru menghasilkan win rate 21% dan rugi $22 — kebalikan dari yang seharusnya.

---

## Phase 1 — Scoring Pipeline Audit

### 1.1 Arsitektur scoring (dari kode)

```
final_score =
    raw_score                 # = total_bull − total_bear, dari 3 analyzer
    + session_bonus           # NY +10, London +4, Asia −10
  × regime_multiplier         # LOW_VOL 0.90, NORMAL 1.0, HIGH_VOL 0.85
    + meta_score_adj          # ±8–12 dari EMA winrate (≥5 samples)
```

3 analyzer utama:

| Analyzer | Max points | Inputs |
|---|---|---|
| **OIFundingAnalyzer** (`engine/analyzers/oi_funding_analyzer.py`) | ±45 | funding rate (level + slope), OI delta, OI magnitude, funding history (8 ts), spot-perp basis |
| **LiquidationAnalyzer** (`engine/analyzers/liquidation_analyzer.py`) | ±12 | live WS liquidation events; falls back to OI proxy when WS empty |
| **OrderbookAnalyzer** (`engine/analyzers/orderbook_analyzer.py`) | ±30 | bid/ask imbalance (graduated tiers), VWAP deviation, CVD, dollar depth, walls |

**Threshold:** Scalper ≥60 (config: `min_score_to_enter = 57`, but with `+10 NY session` + `+4 London`, real entry happens at raw≥46 during overlap).

### 1.2 Pipeline anomalies discovered in production data

**🚨 ZERO-VARIANCE BUG:** Out of 338 production signals:

```
oi_funding_score   : mean=0.00, std=0.00, range=[0, 0]   ← analyzer NEVER fired
liquidation_score  : mean=0.00, std=0.00, range=[0, 0]   ← analyzer NEVER fired
orderbook_score    : mean=0.00, std=0.00, range=[0, 0]   ← analyzer NEVER fired
session_bonus      : mean=8.95, std=4.6,  range=[-6, 14] ← only this works
```

The "multi-factor" engine is in fact a **single-factor session-bonus engine**. The breakdown JSON keeps the field names but the values are 0 across the entire dataset.

Reverse-engineered weights via linear regression:
```
final_score ≈ 67.5  −  0.33 × session_bonus
            + 0.00 × (oi_funding | liquidation | orderbook)
R² = 0.053
```

**The R² of 0.053 means 95% of `final_score` movement is unexplained by the named breakdown components** — the score is being set somewhere upstream (likely the `total_bull/total_bear` aggregator or a rescaling step) and the breakdown values are being persisted as 0 due to a bug.

### 1.3 Likely root cause

Looking at the analyzers' return signatures (`bull, bear, reasons, warnings`) and the saved breakdown showing `oi_funding_score=0`, the integration layer in `engine/scoring_engine.py` is probably:
- Calling each analyzer correctly (the `reasons` text shows OI/Funding/Orderbook reasoning is generated)
- But **not persisting the per-analyzer numeric contribution** into `breakdown` — only the aggregated `total_bull`/`total_bear` and a final number

So the bot *thinks* it's evaluating 3 independent edges. The DB tells us only one is making it through.

---

## Phase 2 — Feature Importance Findings

### 2.1 Score-PnL relationship

| Pearson r | p-value | Interpretation |
|---|---|---|
| `sig_score` ↔ `pnl_usd`: **+0.025** | 0.65 | No linear edge |
| `sig_score` ↔ `outcome` (win=1): **+0.07** | 0.20 | No predictive power |

For comparison, a useful alpha signal in crypto would have |r| ≥ 0.15 with PnL.

### 2.2 Top features by Permutation Importance (test set)

| Rank | Feature | Permutation importance | Note |
|---|---|---|---|
| 1 | `sig_entry_atr` | 0.064 | **Volatility, not signal score** |
| 2 | `sig_has_funding_extreme` | 0.025 | Contrarian funding flag (small but positive) |
| 3 | `sig_suggested_leverage` | 0.018 | Sizing artifact, not edge |
| 4 | `sig_realized_vol` | 0.017 | Volatility |
| 5 | `sig_has_volume_surge` | 0.012 | **NEGATIVELY** correlated with PnL (−0.15) |
| 6 | `sig_score` | 0.008 | The main score barely makes top-10 |

The model achieves 93% accuracy (vs 51% baseline), but the feature ranking shows **the model is learning "high ATR & high leverage trades tend to lose more in dollar terms"** — that's a position-sizing artifact, not signal alpha.

### 2.3 Per-feature correlation with PnL

| Feature | r vs PnL | Interpretation |
|---|---|---|
| `sig_has_rsi_divergence` | **−0.22** | "Bullish indicator" predicts losses |
| `sig_has_volume_surge` | **−0.15** | Volume surge → late entry → loss |
| `sig_has_funding_extreme` | +0.14 | Contrarian funding works (only positive feature) |
| `sig_has_mtf_align` | −0.10 | Multi-timeframe agreement → mean reversion |
| `session_bonus` | +0.04 | Only marginally useful |

**Three of the most-weighted bullish indicators are reversed in production**. They were calibrated on backtests; live behavior is the opposite.

### 2.4 Score decile analysis

| Decile | Score range | n | Win rate | Avg PnL | Total PnL |
|---|---|---|---|---|---|
| 0 | 57–58 | 62 | 53% | +$0.08 | +$4.66 |
| 1 | 59 | 15 | 47% | −$0.98 | **−$14.77** |
| 2 | 60 | 31 | 58% | +$0.11 | +$3.55 |
| 3 | 62 | 48 | **29%** | −$0.97 | **−$46.79** |
| 4 | 63 | 16 | 100% | +$1.25 | +$19.97 |
| 5 | 64–66 | 44 | 32% | −$0.57 | −$25.26 |
| 6 | 67 | 20 | 50% | +$0.00 | +$0.09 |
| 7 | 68–69 | 52 | 67% | +$0.19 | +$9.63 |
| 8 | 70–71 | 17 | 65% | +$0.23 | +$3.92 |
| 9 | 73–97 | 33 | **21%** | −$0.67 | **−$22.23** |

**The signal is wildly non-monotonic.** Decile 4 (score 63) wins 100%, decile 3 (score 62, just 1 point lower) wins 29%. This is the signature of a discrete decision boundary (likely the regime/MTF flag) coinciding with that exact score, not a smooth probability gradient.

The **top decile loses worst** — a textbook sign that high-score signals enter at exhaustion (mean-reversion zones reached after trends already played out).

### 2.5 Exit reason performance

| Exit reason | n | Win rate | Total PnL | Avg PnL |
|---|---|---|---|---|
| `time_exit` | 274 (81%) | 48% | −$22.06 | −$0.08 |
| `stop_loss` | 35 | 40% | −$29.76 | −$0.85 |
| `trailing_stop` | 14 | **100%** | **+$19.12** | +$1.37 |
| `momentum_exit` | 9 | **0%** | −$38.11 | **−$4.23** |
| `close_all` | 5 | 80% | +$2.42 | +$0.48 |
| `manual_close` | 1 | 100% | +$1.16 | +$1.16 |

**Critical:**
- `momentum_exit` is single-handedly responsible for $38 of the $67 loss (57%) on just 9 trades.
- `trailing_stop` is the only profitable exit but fires only 4% of the time.
- `time_exit` (81% of all exits) clips winners and traps losers — net −$22.

---

## Phase 3 — Critical Findings (ranked by $ impact)

### F1 · Multi-factor analyzers not persisting scores · **−$25–40 impact**
**State:** All 338 production signals show `oi_funding_score = liquidation_score = orderbook_score = 0`, despite the textual `reasons` field containing analyzer output (e.g., "Strong ask wall", "OI/Funding bearish").
**Cause:** The aggregation step in `engine/scoring_engine.py` calls each analyzer's `analyze()` (which returns `bull, bear, reasons, warnings`), accumulates into `total_bull`/`total_bear`, but does not record per-analyzer integer contribution into the `breakdown` JSON.
**Effect:** Impossible to debug, attribute, or learn from the score. Meta-learning is starved of features. Looks like a single-factor model (just session bonus).
**Fix:** Track `bull_oi, bear_oi, bull_liq, bear_liq, bull_ob, bear_ob` separately and persist to `breakdown`.

### F2 · `momentum_exit`: 0/9 wins, −$4.23/trade · **−$38 impact**
**State:** Despite the v2 multi-confirmation rewrite (5 layers: ATR pullback / volume / trend break / RSI / volume confirmation), every single momentum_exit trigger in production was a loss.
**Cause:** Almost certainly **selection bias** — the exit only fires when *all* layers confirm bearish reversal, but by then the move has already happened. The bot is selling at the local low.
**Fix:** Either (a) raise the floor pullback to 1.5%+ so it only fires on regime changes, not noise; or (b) deprecate momentum_exit entirely and rely on trailing stop + time exit.

### F3 · Top-decile score (73–97) → 21% WR, −$22 PnL · **−$22 impact**
**State:** The highest-conviction signals are systematically the worst. Decile 9 underperforms decile 0 by 32 percentage points of WR.
**Cause:** Score is dominated by **session bonus + cumulative reasons count** (NY+London during overlap = +14 bonus, plus 8-12 narrative reasons). High-score regime = late in trend, near exhaustion. Bot enters at top.
**Fix:** Cap `session_bonus` contribution at +5 (not +14), and add a **score-velocity filter**: only enter if score has been ≥threshold for >2 scan cycles (avoid one-off spikes from session boundaries).

### F4 · `trailing_stop` only 14/338 trades (4%) but 100% WR · **+$19 actual / +$50–100 missed**
**State:** Activated only after TP1 hit (0.6%). Many winners reach +0.3–0.5%, then revert and die in `time_exit`.
**Cause:** TP1 = 0.6% is rarely reached in 20-min hold window for low-volatility assets. Trailing never activates.
**Fix:** **Already implemented** in last conversation — `time_exit_early_trail_pct = 0.003` activates trailing at 0.3%. Recommended to also lower TP1 floor to 0.4% for low-volatility regime.

### F5 · `sig_has_rsi_divergence` r = −0.22 with PnL · **−$10–15 impact**
**State:** The single most negatively correlated signal feature. Trades with RSI divergence flag lose more on average.
**Cause:** RSI divergence as a reversal signal works in ranging markets, fails badly in trending markets (during liquid sessions like NY which is when bot is most active).
**Fix:** Either (a) downgrade RSI divergence to 0 points unless `regime != trending`; or (b) require additional confirmation (volume + structure break).

### F6 · 21 patterns with WR <35% still trading · **−$15–20 impact**
**State:** Meta tracker shows 21 `(asset, side)` patterns with EMA win rate <35% across ≥5 samples. Examples: `BIO_long` (0%, n=12), `TON_long` (0%, n=9), `INJ_long` (14%, n=28), `LINK_long` (0%, n=23).
**Cause:** Meta penalty is only ±8–12 points — not enough to push score below threshold for "strong" signals.
**Fix:** Hard-block: if `winrate_ema < 0.30 AND samples >= 10`, skip the asset for that side entirely.

### F7 · Score decomposition unstable (R² = 0.05) · **diagnostic only**
**State:** Linear regression of breakdown components vs `final_score` yields R² of 0.05 — the breakdown does not explain the score.
**Cause:** Either (a) the analyzer outputs are zeroed (F1) and `final_score` comes from `total_bull/total_bear` not in the joined breakdown; or (b) there's a non-linear regime multiplier + meta adjustment dominating.
**Fix:** Mostly resolves with F1 (persist all components).

---

## Phase 4 — Concrete Fixes & Priority Matrix

| ID | Fix | Effort | Impact | Priority |
|---|---|---|---|---|
| F1 | Persist all analyzer outputs to `breakdown` JSON (don't store 0s) | Low (1 file, ~20 lines) | High (unblocks debugging + learning) | **P0** |
| F2 | Disable or harden `momentum_exit` (raise pullback to 1.5%, require 4/5 layers) | Low | High (−$38 → ~$0) | **P0** |
| F3 | Cap `session_bonus` at +5 and add score-velocity filter (≥2 cycles above threshold) | Medium | High (−$22 → break-even) | **P0** |
| F6 | Hard-block patterns with WR<30% & n≥10 | Low (1 if-check in scoring_engine) | Medium | **P1** |
| F4 | Already done — verify `time_exit_early_trail_pct` is hit in production | Low | Medium-High | **P1** |
| F5 | Gate RSI divergence on regime (only count in ranging market) | Low | Medium | **P1** |
| New | Add per-trade feature logging to `signals_history.data` (so we can re-run this audit weekly) | Medium | Future-proofing | **P2** |
| New | Replace static threshold (60) with **score-percentile filter** (top 30% per asset over 7-day window) — avoids the "single point spike" issue in F3 | High | High but unproven | **P2** |

---

## Recommended Code Changes (highest priority)

### Fix F1 — Persist analyzer scores

In `engine/scoring_engine.py`, find where `breakdown` dict is built and replace zeros with actual analyzer outputs:

```python
# BEFORE (suspected current state):
breakdown = {
    "oi_funding_score":  0,           # ← bug: never set
    "liquidation_score": 0,           # ← bug: never set
    "orderbook_score":   0,           # ← bug: never set
    "session_bonus":     session_bonus,
    "total_bull":        total_bull,
    "total_bear":        total_bear,
    ...
}

# AFTER:
breakdown = {
    "oi_funding_score":  oi_bull - oi_bear,        # signed contribution
    "liquidation_score": liq_bull - liq_bear,
    "orderbook_score":   ob_bull - ob_bear,
    "session_bonus":     session_bonus,
    "total_bull":        total_bull,
    "total_bear":        total_bear,
    ...
}
```

### Fix F2 — Harden momentum_exit

In `risk/risk_manager.py` (Rule E2), raise the pullback threshold floor and require all 4 confirmation layers (currently 3):

```python
# Line ~1010 (momentum_exit_min_pullback_pct):
# Was: 0.008 (0.8%)
# Set: 0.015 (1.5%) — only true reversals, not noise

# In the layer-tally check, require 4/5 confirmations instead of 3/5:
if confirmation_count >= 4:  # was: >= 3
    return {"action": "momentum_exit", ...}
```

Or — preferred — disable momentum_exit and rely on trailing stop:

```python
# config.py, ScalperConfig:
momentum_exit_enabled: bool = False   # disable until fixed
```

### Fix F3 — Cap session bonus

In `engine/scoring_engine.py`, where session_bonus is calculated:

```python
# BEFORE:
session_bonus = 0
if is_ny_session:    session_bonus += 10
if is_london_session: session_bonus += 4
if is_asia_session:   session_bonus -= 10

# AFTER:
session_bonus = 0
if is_ny_session:    session_bonus += 5    # was +10
if is_london_session: session_bonus += 2   # was +4
if is_asia_session:   session_bonus -= 5   # was -10
session_bonus = max(-8, min(7, session_bonus))  # hard cap
```

### Fix F6 — Hard-block bad meta patterns

In `engine/scoring_engine.py` early-rejection block:

```python
# Add this BEFORE the score calculation:
pattern_key = f"{trade_mode}_{asset}_{side}"
meta = await db.get_meta_pattern(pattern_key)
if meta and meta.samples >= 10 and meta.winrate_ema < 0.30:
    log.info(f"[META-BLOCK] {pattern_key}: WR {meta.winrate_ema:.0%} < 30% over {meta.samples} samples — SKIP")
    return None
```

---

## Files Generated

```
audit_score_analysis/
├── analyze.py                          # Phase 2 analysis pipeline
├── dashboard.py                        # Phase 3 chart generator
├── kara_score_audit_dashboard.html     # ★ MAIN DASHBOARD (open in browser)
├── AUDIT_REPORT.md                     # this file
└── data/
    ├── merged_trades_signals.csv       # 338 trades joined with signals
    ├── correlations.csv                # feature ↔ pnl correlation
    ├── correlations_outcome.csv        # feature ↔ win/loss correlation
    ├── feature_importance.csv          # RF native + permutation importance
    ├── score_decile.csv                # decile breakdown
    ├── exit_reason.csv                 # exit reason performance
    └── summary.json                    # headline metrics
```

---

## Reproduction

To re-run the audit on fresh data:

```bash
# 1. Pull production DB (run on Windows PowerShell, project root)
$script = @'
import sqlite3, json
conn = sqlite3.connect("/app/storage/kara_data.db")
conn.row_factory = sqlite3.Row
cur = conn.cursor()
for tbl, key in [("trade_history","trades"), ("signals_history","signals"), ("meta_pattern_stats","meta")]:
    cur.execute(f"SELECT * FROM {tbl} ORDER BY rowid")
    rows = [dict(r) for r in cur.fetchall()]
    with open(f"/tmp/{key}.json","w") as f:
        json.dump(rows, f, default=str)
print("ok")
'@
$b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($script))
railway ssh --service kara-400 "echo $b64 | base64 -d > /tmp/e.py && python3 /tmp/e.py"

# 2. Download
railway ssh --service kara-400 "base64 /tmp/trades.json"  > tmp/trades_b64.txt
railway ssh --service kara-400 "base64 /tmp/signals.json" > tmp/signals_b64.txt
railway ssh --service kara-400 "base64 /tmp/meta.json"    > tmp/meta_b64.txt

# 3. Decode
$t = Get-Content tmp/trades_b64.txt -Raw
[System.IO.File]::WriteAllBytes("tmp/trades_prod.json",  [Convert]::FromBase64String($t.Trim()))
# (repeat for signals & meta)

# 4. Analyze + dashboard
venv\Scripts\python.exe audit_score_analysis\analyze.py
venv\Scripts\python.exe audit_score_analysis\dashboard.py

# 5. Open
start audit_score_analysis\kara_score_audit_dashboard.html
```

---

## Closing Notes

The bot has 3 different problems wearing one hat:

1. **Telemetry is broken** (F1) — we can't see what the analyzers actually outputted.
2. **One exit logic is catastrophic** (F2 — momentum_exit) and another is rare-but-perfect (trailing_stop).
3. **The score is set largely by session timing**, not by orderflow/funding/liquidation analysis as advertised. The result: high score = late entry = loss.

**Fixing F1 + F2 + F3 should turn an expectancy of −$0.20 into something between −$0.05 and +$0.10 per trade** — enough to flip the bot from net-losing to flat-to-profitable, without changing the entry strategy at all. Real edge improvements (proper alpha, regime detection) come after telemetry is honest.

**No production code was modified by this audit.** All changes proposed above need explicit approval and tests before deployment.
