# KARA Audit #21 - Validasi v10/F2.1 dari Railway Production

**Run date:** 2026-06-10  
**Data source:** Railway production SQLite `/app/storage/kara_data.db` via `railway ssh`  
**Service:** `rare-youthfulness`  
**Period:** 2026-06-07 16:26:11 UTC -> 2026-06-10 08:08:09 UTC  
**Dedup:** trade single-user `6843478231` sesuai runbook; signal raw masih broadcast-like.

## Executive Verdict

**NO-GO live. v10 aktif, tapi edge belum terbukti.**

True production expectancy masih negatif:

| Metric | Result | Audit #21 Target | Verdict |
|---|---:|---:|---|
| Trades | 256 | >=100 untuk validasi awal | PASS sample |
| Volume | 96.5/day | >=30/day | PASS volume |
| Win rate | 37.9% | >=45% F2.1 / >=40% v10 | FAIL |
| Gross profit | +$30.22 | - | - |
| Gross loss | -$35.88 | - | - |
| **True PF** | **0.84** | >=1.0 F2.1 / >=1.3 v10 | **FAIL** |
| PnL | -$5.67 | >= breakeven | FAIL |
| Trailing fire | 9/256 = 3.5% | >=10% F2.1 / >=30% v10 | FAIL |
| SHORT PF | 1.13 | should improve vs old | PASS |
| LONG PF | 0.64 | must not bleed | FAIL |

Important correction: legacy `analyze.py` prints PF 1.38 because it uses `avg_win / abs(avg_loss)`. Institutional PF must be `gross_profit / abs(gross_loss)` = `30.22 / 35.88 = 0.84`.

## Deploy Verification

Live logs confirm v10/F2.1 is active:

- `[V10-GATE SKIP]`: 200 hits in latest 500 log lines.
- `v10 GATE PASS`: 2 hits.
- `[RANKED]`: 2 hits.
- `tier=S`: 5 hits.
- `score_below_threshold`, `low_vote_consensus`, `low_momentum`, `low_atr`: 0 hits.
- `long_against_downtrend`, `short_against_uptrend`: 0 hits.

Deploy boundary on Railway:

- `/app/engine/scoring_engine.py`: `1780849542`, 2026-06-07 16:25:42 UTC.
- `/app/engine/gate_system.py`: `1780849542`, 2026-06-07 16:25:42 UTC.
- `/app/risk/risk_manager.py`: `1780849542`, 2026-06-07 16:25:42 UTC.

## Root Findings

### P0 - Progress Stop Is The Main Loss Engine

`progress_stop` is 43% of exits and contributes almost all loss:

| Exit | n | WR | PnL | True PF |
|---|---:|---:|---:|---:|
| progress_stop | 110 | 29.1% | **-$25.39** | 0.21 |
| momentum_death | 110 | 26.4% | -$2.92 | 0.20 |
| stop_loss | 14 | 100% | +$7.72 | inf |
| time_exit | 13 | 100% | +$8.87 | inf |
| trailing_stop | 9 | 100% | +$6.05 | inf |

Interpretation: progress stop succeeds at cutting weak trades, but entry quality before progress stop is poor. It is not a standalone edge. Current rule converts many non-developing entries into realized losses.

### P0 - Long Side Still Bleeds

| Side | n | WR | PnL | True PF |
|---|---:|---:|---:|---:|
| LONG | 143 | 35.0% | **-$7.60** | 0.64 |
| SHORT | 113 | 41.6% | +$1.93 | 1.13 |

F2.1 fixed the old 90/10 LONG bias: LONG/SHORT is now 56/44. But the profitable side is SHORT; LONG remains the leak.

### P0 - Trailing Edge Survives But Barely Fires

`trailing_stop`: 9 trades, 100% WR, +$6.05, avg +$0.67.

This remains the only clean positive edge. But fire rate is 3.5%, below F2.1 target >=10% and far below original v10 target >=30%.

### P1 - Tier And Setup Do Not Separate Quality Enough

| Segment | n | WR | PnL | True PF |
|---|---:|---:|---:|---:|
| Tier A | 210 | 37.1% | -$6.50 | 0.77 |
| Tier S | 40 | 37.5% | -$0.33 | 0.96 |
| Tier B | 6 | 66.7% | +$1.16 | 11.94 |
| Pullback | 137 | 36.5% | -$6.43 | 0.72 |
| Momentum | 119 | 39.5% | +$0.76 | 1.06 |

Tier S is not yet a premium entry. Momentum modestly outperforms pullback. B sample is too small to trust.

### P1 - Exit Telemetry Is Misleading

`stop_loss` can close profitable trades because TP/breakeven logic moves `position.stop_loss` above/below entry. Example: ZRO LONG entry 0.901757 -> exit 0.902171, PnL positive, reason `stop_loss`.

Telemetry should distinguish:

- `hard_stop_loss`: initial SL hit at loss.
- `protective_stop`: breakeven/profit SL hit.
- `tp1_protective_stop`: post-TP1 stop on remaining size.

Without this, audit dashboards will misclassify profit locks as losses.

### P2 - Score Is Mostly Non-Predictive Under v10

Score/PnL correlation is effectively zero: `r = +0.008`, p=0.898.

This is expected after v10 moved entry quality into gates/tier/size, but it means old score-decile analysis is no longer valid. Audit tooling must pivot to gate/tier/setup/size-mult metrics.

## Concrete Actions

### P0.1 - Quarantine LONG Entries Until PF Recovers

Set a temporary stricter LONG gate:

- For LONG only: require `setup=momentum` OR `tier=S`.
- Block `tier=A pullback LONG` until it proves PF > 1.0.
- Keep SHORT active; it is currently the only positive side.

Verification: next 100 trades should show LONG PF >=0.95 and total PF >1.0.

Trade-off: volume drops, but current volume is already 96/day, well above target. Survival > frequency.

### P0.2 - Stop Treating Progress Stop As Success

Keep `progress_stop`, but use it as entry-quality feedback:

- If an asset/setup hits `progress_stop` twice in 60 minutes, block same `asset_side_setup` for 2 hours.
- If `progress_stop` rate for a setup >40% over 50 trades, reduce that setup size multiplier by 0.5.
- For LONG pullbacks, start with size multiplier cap `0.30x` until PF >1.0.

Verification: progress_stop PnL contribution improves from -$25.39 toward less than -$8 per 250 trades.

Trade-off: may miss delayed reversals. Current data says delayed reversals are not paying enough.

### P0.3 - Increase TP1/Trail Activation Frequency, Not Raw Entry Count

Trailing is the edge. Optimize for TP1 reach, not more entries:

- Lower `partial_tp1_at_sl_multiple` from `0.35` to `0.25` only for Tier S and SHORT momentum.
- Keep current value for LONG pullback; do not reward weak LONGs.
- Emit telemetry: `reached_0.25R`, `reached_0.5R`, `reached_tp1`, `tp1_to_trail`.

Verification: trailing fire >=10% and true PF >1.0 in next 100-150 trades.

Trade-off: earlier TP1 reduces runner size. Acceptable because current runner capture is too rare.

### P1.1 - Rename Exit Reasons For Audit Integrity

Implement reason mapping:

- If action is `stop_loss` and realized PnL > 0: save `protective_stop`.
- If action is `stop_loss` and `tp1_hit=True`: save `tp1_protective_stop`.
- If action is `stop_loss` and realized PnL <= 0: save `hard_stop_loss`.

Verification: no positive-PnL rows under `hard_stop_loss`; no negative-PnL rows under `protective_stop`.

Trade-off: reports split into more buckets; worth it because current telemetry is semantically wrong.

### P1.2 - Rebuild Audit Tooling For v10

Old `analyze.py` still assumes additive score. New required panels:

- true PF, not avg win/loss ratio.
- side x setup x tier x size_mult.
- progress_stop by asset/setup.
- trailing fire rate by tier/setup.
- gate reject funnel from logs/telemetry table.
- score analysis optional only.

Verification: dashboard can answer "which gate/tier/setup has edge?" without manual scripts.

## Current Decision

Do not rollback v10 immediately: volume is healthy, SHORT side is positive, and Q4 sample improved to PF 1.41. But do not call this an edge yet. The correct state is **paper-trading diagnostic mode** with one controlled change: suppress/resize LONG pullbacks and use progress_stop outcomes as feedback.

Live trading remains blocked until:

- true PF >1.3 for 3 consecutive audits,
- trailing fire >=10% minimum, ideally >=30%,
- LONG side no longer bleeds,
- telemetry exit labels are fixed,
- at least 300 OOS paper trades after the next change.

---

## Deep Root Cause Addendum

The initial diagnosis "progress_stop is the loss engine" is incomplete. `progress_stop` is the symptom. The root cause is earlier:

> v10 Layer 2 correctly identifies bad conditions, but treats them as sizing modifiers instead of hard rejects.

That means the bot keeps trading known-bad states with smaller size. Smaller negative expectancy is still negative expectancy.

### Root Cause 1 - RV 0.3x Is Not Risk Management, It Is An Adverse Selection Tag

Gate code allows 6-8% realized volatility to pass with `size_mult=0.3x`. Production result:

| RV bucket | n | WR | PnL | True PF |
|---|---:|---:|---:|---:|
| `rv=0.3x` | 59 | 28.8% | **-$6.42** | **0.31** |
| `rv=0.75x` | 87 | 36.8% | -$1.86 | 0.85 |
| `rv=full` | 110 | 43.6% | +$2.61 | 1.19 |

Counterfactual:

| Rule | Kept | PnL | True PF |
|---|---:|---:|---:|
| Current all trades | 100% | -$5.67 | 0.84 |
| Block `rv=0.3x` | 77.0% | +$0.75 | 1.03 |
| Only `rv=full` | 43.0% | +$2.61 | 1.19 |

Conclusion: `rv=0.3x` should be a hard reject until proven otherwise. The gate's own danger label is predictive.

### Root Cause 2 - Counter-Trend Scalp Size Modifier Is A Bad Trade Pass

F2.1 changed counter-trend from hard block to size reduction (`g1_ct=0.75`). Production says this was too permissive:

| Segment | n | WR | PnL | True PF |
|---|---:|---:|---:|---:|
| `g1_ct=0.75` | 28 | 35.7% | **-$4.21** | **0.36** |
| non-counter-trend | 228 | 38.2% | -$1.45 | 0.95 |

Counterfactual:

| Rule | Kept | PnL | True PF |
|---|---:|---:|---:|
| Block `rv=0.3x` + block `g1_ct` | 68.8% | **+$4.25** | **1.20** |

Conclusion: counter-trend scalp is not a "smaller size" case. It is a reject case unless it has independent reversal evidence, e.g. sweep reclaim + CVD reversal + level context.

### Root Cause 3 - SHORT Edge Is Realer Than LONG Edge

Side split:

| Side | n | WR | PnL | True PF |
|---|---:|---:|---:|---:|
| LONG | 143 | 35.0% | -$7.60 | 0.64 |
| SHORT | 113 | 41.6% | +$1.93 | 1.13 |

But this is not "disable LONG". The deeper split:

| Segment | n | PnL | True PF |
|---|---:|---:|---:|
| SHORT momentum | 57 | **+$3.59** | **1.70** |
| SHORT A momentum | 50 | +$3.80 | 1.90 |
| LONG S momentum | 7 | +$0.61 | 1.77 |
| LONG A momentum | 53 | **-$4.00** | 0.40 |
| LONG S pullback | 19 | -$1.87 | 0.52 |

Conclusion: "momentum" is not universally good. It is good mainly on SHORT or small-sample LONG S. LONG A momentum is a trap. Tier S is not enough when the side/setup context is wrong.

### Root Cause 4 - CHOPPY Is Better Than TRENDING_UP For This Bot

Scalp regime split:

| Scalp regime | n | PnL | True PF |
|---|---:|---:|---:|
| CHOPPY | 87 | +$0.90 | 1.12 |
| TRENDING_DOWN | 87 | -$0.85 | 0.94 |
| TRENDING_UP | 82 | **-$5.72** | 0.60 |

This is unintuitive but important. The bot likely enters LONG too late in upward scalp trends. `TRENDING_UP` is being treated as confirmation, but production shows it behaves like late-entry/exhaustion for this strategy.

Conclusion: LONG entries in `TRENDING_UP` need a late-entry guard, not a confidence boost.

### Root Cause 5 - Progress Stop Is Doing Its Job, But Too Many Bad Trades Reach It

`progress_stop` by side/setup:

| Segment | n | PnL | True PF |
|---|---:|---:|---:|
| progress_stop LONG | 60 | -$16.33 | 0.14 |
| progress_stop SHORT | 50 | -$9.07 | 0.32 |
| progress_stop pullback | 61 | -$17.26 | 0.18 |
| progress_stop momentum | 49 | -$8.14 | 0.27 |

So `progress_stop` is not the root. It is the trash collector. The problem is that the gate feeds it trash.

### Correct P0 Fix

Replace these v10 "size-only penalties" with hard rejects for the next paper window:

1. `rv=0.3x` -> reject.
2. `g1_ct=0.75` -> reject unless `setup=sweep` or explicit reclaim logic exists.
3. `LONG + A + momentum` -> reject or size cap 0.20x until PF >1.0.
4. `LONG + S + pullback` -> reject until PF >1.0.
5. Keep `SHORT + momentum`, especially `A momentum`, as the primary edge candidate.

Expected counterfactual from current sample:

| Candidate production rule | Kept | PnL | True PF |
|---|---:|---:|---:|
| Block `rv=0.3x` + `g1_ct` | 68.8% | +$4.25 | 1.20 |
| Only SHORT momentum | 22.3% | +$3.59 | 1.70 |
| Short or momentum, no low size | 53.9% | +$2.41 | 1.14 |

This is still not enough for live. But it is a real hypothesis for the next paper deployment.
