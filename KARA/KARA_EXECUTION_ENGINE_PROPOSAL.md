# KARA Execution Engine Proposal

**Tujuan:** mengubah KARA dari signal/gate bot menjadi execution-driven futures scalper.  
**Konteks:** KARA v10/F2.1, paper mode, Hyperliquid futures, average hold target 10-20 menit.  
**Prinsip:** edge bukan cuma arah. Edge = setup + location + trigger + invalidation + execution cost.

---

## 1. Problem Statement

Audit produksi menunjukkan v10 sudah memperbaiki beberapa hal:

- Volume tidak mati: sekitar 96 trade/hari.
- SHORT mulai punya edge: PF sekitar 1.13.
- `SHORT momentum` adalah kandidat edge terbaik: PF sekitar 1.70.
- Trailing/protective exits tetap positif saat tercapai.

Tapi sistem masih rugi karena KARA terlalu sering **entry segera setelah gate pass**, bukan menunggu lokasi yang memberi R:R bagus.

Root cause:

1. Gate v10 menjawab "boleh trade atau tidak", tapi belum menjawab "di harga berapa entry layak".
2. Market entry membuat KARA masuk setelah displacement, terutama saat volatility sudah expanded.
3. `progress_stop` menjadi loss bucket karena banyak entry tidak punya immediate follow-through.
4. Setup berbeda masih dieksekusi terlalu mirip.
5. Score tidak lagi prediktif; execution harus berbasis playbook expectancy, bukan score.

Kesimpulan: KARA perlu **Execution Engine** di antara gate pass dan order placement.

---

## 2. Design Goal

Execution Engine harus:

- Tidak terlalu ketat: tetap menjaga volume minimal 30 trade/hari.
- Tidak terlalu longgar: stop market entry pada lokasi buruk.
- Membiarkan edge `SHORT momentum` tetap cepat.
- Mengubah setup pullback/sweep/high-RV menjadi pending-entry, bukan instant market.
- Mengukur trade yang batal sebagai data, bukan menganggapnya hilang.

Target setelah implementasi awal:

| Metric | Target |
|---|---:|
| True PF | > 1.05 dalam 100 trade pertama, > 1.20 setelah tuning |
| Trade volume | 35-70 trade/hari |
| `progress_stop` share | < 30% dari exit |
| Trailing/protective fire | > 8% awal, > 12% setelah tuning |
| SHORT momentum PF | tetap > 1.3 |
| Cancelled pending rate | 30-60% wajar |

Cancelled signal bukan kegagalan. Kalau pending cancel menghindari progress_stop, itu alpha.

---

## 3. Architecture

Current flow:

```text
scan asset
-> scoring/gate pass
-> immediate signal/order
-> risk manager exit
```

Proposed flow:

```text
scan asset
-> scoring/gate pass
-> classify execution playbook
-> create pending execution intent
-> wait for trigger/location
-> choose market / aggressive limit / passive limit
-> enter or cancel
-> risk manager exit
-> log execution telemetry
```

New module:

```text
execution/execution_engine.py
```

Core object:

```python
ExecutionIntent(
    signal_id,
    asset,
    side,
    setup,
    tier,
    entry_mode,          # market | aggressive_limit | passive_limit | wait_retest
    reference_level,
    invalidation_level,
    max_chase_pct,
    limit_offset_pct,
    ttl_seconds,
    required_trigger,
    cancel_reason,
)
```

---

## 4. Execution Playbooks

### 4.1 SHORT Momentum

This is KARA's current best candidate edge. Do not over-filter it.

Use market or aggressive limit when momentum is fresh.

Entry rules:

- Setup = `momentum`.
- Side = `short`.
- CVD aligned negative or not contradicting.
- Price breaks recent 3-candle low OR closes near low with sell pressure.
- Distance from breakdown is not too extended.
- Spread/cost acceptable.

Execution:

```text
If fresh break:
    use aggressive_limit at best_bid / mid - small offset
    fallback to market only if spread is thin and expected move is large
Else if already extended:
    wait for retest of breakdown level
    place limit near retest
Else:
    cancel
```

Suggested parameters:

| Parameter | Value |
|---|---:|
| TTL fresh momentum | 15-30 sec |
| Max chase from break | 0.20-0.30% |
| Retest TTL | 60 sec |
| Limit offset | 0.02-0.05% |

Professional logic:

SHORT momentum needs urgency, but not blind chasing. If price already moved too far, R:R is degraded; wait for retest.

---

### 4.2 LONG Trend

LONG in `TRENDING_UP` is currently dangerous because KARA often enters late. Do not market buy into extension.

Entry rules:

- Side = `long`.
- Do not enter immediately on gate pass.
- Wait for pullback toward EMA13/EMA21, VWAP proxy, or prior breakout level.
- Require reclaim: price touches/near level, then trades back above it.
- Invalidation must be close: below pullback low or reclaim level.

Execution:

```text
Create pending intent.
If price pulls back to level and reclaims:
    place limit near reclaim/retest price
If price runs without pullback:
    cancel, no chase
If level breaks and does not reclaim:
    cancel
```

Suggested parameters:

| Parameter | Value |
|---|---:|
| Pending TTL | 60-120 sec |
| Pullback zone | within 0.05-0.15% of EMA/VWAP/level |
| Reclaim confirmation | 1 candle close or trade-through back above level |
| Max chase | 0.15% |

Professional logic:

For LONG trend, edge is not "trend is up". Edge is buying the pullback where invalidation is cheap.

---

### 4.3 Pullback Setup

Pullback must not be market-entered. If it is a pullback, let the market pull back.

Entry rules:

- Setup = `pullback`.
- Entry only near level.
- No fill = no trade.
- If price immediately runs away, cancel.

Execution:

```text
Place passive or semi-passive limit at pullback zone.
Cancel if no fill within TTL.
Cancel if price invalidates level before fill.
Cancel if price moves too far away from limit.
```

Suggested parameters:

| Parameter | Value |
|---|---:|
| TTL | 90 sec |
| Limit zone | level +/- 0.05-0.10% |
| Cancel if away | > 0.25% from limit |
| Cancel if spread wide | true |

Professional logic:

Pullback edge comes from price improvement. Chasing a pullback converts it into late momentum with worse R:R.

---

### 4.4 Sweep / Reclaim Reversal

This is not currently mature enough in telemetry, but it should be the only professional form of counter-trend trading.

Entry rules:

- Price sweeps recent high/low.
- Reclaims the swept level.
- CVD or orderflow stops accelerating against trade.
- Invalidation is outside wick/swing.

Execution:

```text
Wait for sweep.
Wait for reclaim.
Place limit on reclaim retest.
Cancel if reclaim fails.
```

Suggested parameters:

| Parameter | Value |
|---|---:|
| Sweep lookback | 10-20 candles |
| Reclaim TTL | 60 sec |
| Retest TTL | 60 sec |
| Invalidation buffer | 0.03-0.08% beyond wick |

Professional logic:

Counter-trend without sweep/reclaim is guessing. Counter-trend with sweep/reclaim has defined invalidation.

---

### 4.5 High RV / Volatility Expanded

High RV is not always "no trade". The issue is entering after volatility expansion without retest.

Entry rules:

- If RV is in 6-8% bucket, do not market enter.
- Convert to retest-only execution.
- Require volatility compression or level retest.

Execution:

```text
Gate pass with high RV
-> no immediate order
-> wait for retest / compression
-> enter only if invalidation is close
```

Suggested parameters:

| Parameter | Value |
|---|---:|
| High-RV pending TTL | 120 sec |
| Must retest level | yes |
| Max chase | 0.00%, no chase |
| Size cap if filled | 0.30-0.50x |

Professional logic:

High RV can pay if you enter after retest. It destroys expectancy if you chase expansion.

---

## 5. Order Type Policy

### Default Policy

| Setup / Context | Order Type |
|---|---|
| SHORT fresh momentum | aggressive limit, market allowed if cost OK |
| SHORT extended momentum | wait retest limit |
| LONG trend | wait pullback/reclaim limit |
| Pullback | passive/semi-passive limit |
| Sweep reclaim | reclaim retest limit |
| High RV | retest-only limit |
| Wide spread / thin book | no trade |

### Market Order Is Allowed Only When

- Setup has urgency.
- Expected move to TP1 is at least 3x estimated roundtrip cost.
- Price is not extended from trigger.
- Spread is thin.
- Orderbook depth can absorb size.

### Limit Order Is Preferred When

- Setup depends on location.
- Invalidation is defined by level.
- Price already moved.
- RV is high.
- LONG trend needs pullback.

---

## 6. Cost Model

Before entry, calculate:

```text
roundtrip_cost_pct = taker_fee + expected_slippage + spread_cost
expected_tp1_pct >= roundtrip_cost_pct * 3
stop_distance_pct <= max_allowed_stop_for_setup
expected_RR_to_TP1 >= 0.35R
```

If not satisfied, no trade.

This is mandatory for 20-minute futures scalping. A signal can be directionally right and still not tradeable after cost.

---

## 7. Trigger And Cancel Logic

Every pending intent must end in one of:

- `filled_market`
- `filled_limit`
- `cancel_no_retest`
- `cancel_chased_too_far`
- `cancel_trigger_failed`
- `cancel_spread_wide`
- `cancel_cost_bad`
- `cancel_opposite_flow`
- `expired`

Do not silently ignore cancelled setups. Cancelled setups are part of the edge study.

### Balanced TTL Defaults

| Playbook | TTL |
---|---:|
| SHORT fresh momentum | 15-30 sec |
| Momentum retest | 60 sec |
| Pullback | 90 sec |
| LONG reclaim | 90-120 sec |
| Sweep reclaim | 120 sec |
| High RV retest | 120 sec |

Too tight means missed winners. Too loose means stale fills. These TTLs are intentionally moderate.

---

## 8. Risk And Exit Adjustments

Do not overhaul exits until execution data is cleaner.

Keep:

- progress stop
- momentum death
- TP1/protective stop
- trailing after TP1

But add execution-aware exit telemetry:

```text
time_to_0_25R
time_to_0_5R
time_to_TP1
MFE_before_exit
MAE_before_exit
entry_slippage_bps
entry_type
pending_wait_seconds
trigger_type
```

Interpretation:

- If many trades never reach +0.25R: entry trigger is weak.
- If many trades reach +0.5R then exit negative: trailing/protection is late.
- If limit fills are mostly losers: limit is too passive and catching toxic fills.
- If market fills have high progress_stop: market entries are chasing.

---

## 9. Telemetry Schema

Add fields to `signals_history.data` or a new `execution_history` table:

```json
{
  "execution_playbook": "short_momentum",
  "entry_order_type": "aggressive_limit",
  "intent_created_at": 1780000000.0,
  "intent_ttl_sec": 30,
  "trigger_type": "break_3bar_low",
  "reference_level": 0.12345,
  "invalidation_level": 0.12410,
  "intended_entry": 0.12320,
  "actual_entry": 0.12322,
  "slippage_bps": 1.6,
  "spread_bps": 2.1,
  "roundtrip_cost_est_bps": 7.0,
  "distance_from_vwap_pct": 0.0018,
  "distance_from_ema13_pct": 0.0009,
  "chase_pct": 0.0012,
  "cancel_reason": null
}
```

Exit telemetry:

```json
{
  "time_to_0_25r_sec": 95,
  "time_to_0_5r_sec": null,
  "time_to_tp1_sec": null,
  "mfe_r": 0.31,
  "mae_r": -0.42,
  "final_r": -0.28,
  "exit_reason_normalized": "progress_stop"
}
```

---

## 10. Implementation Plan

### Phase 1 - Observe Without Changing Orders

Add Execution Engine in shadow mode.

For every gate pass:

- classify playbook,
- compute intended order type,
- compute reference/invalidation,
- log whether it would enter or cancel,
- still execute current behavior.

Duration: 100 trades.

Goal: verify that proposed cancel/limit logic would have reduced bad entries without killing all volume.

### Phase 2 - Enable Pending Execution For Pullback/LONG/High-RV

Keep SHORT fresh momentum mostly unchanged.

Enable pending execution for:

- LONG trend,
- pullback,
- high RV,
- counter-trend/reversal.

Duration: 150 trades.

Expected:

- trade count drops 25-45%,
- progress_stop share drops,
- PF moves above 1.0.

### Phase 3 - Enable Full Hybrid Execution

Enable:

- aggressive limit for SHORT momentum,
- retest-only mode for extended momentum,
- sweep/reclaim playbook,
- cost model.

Duration: 300 OOS trades.

Pass criteria:

- PF > 1.20,
- drawdown lower than current sample,
- SHORT momentum remains profitable,
- LONG no longer materially bleeds.

---

## 11. Concrete Config Proposal

Add config section:

```python
@dataclass
class ExecutionConfig:
    enabled: bool = False
    shadow_mode: bool = True

    market_allowed_for_short_momentum: bool = True
    market_max_spread_bps: float = 4.0
    market_max_chase_pct: float = 0.0030
    min_tp1_to_cost_multiple: float = 3.0

    aggressive_limit_offset_bps: float = 2.0
    passive_limit_offset_bps: float = 5.0

    short_momentum_ttl_sec: int = 30
    retest_ttl_sec: int = 60
    pullback_ttl_sec: int = 90
    long_reclaim_ttl_sec: int = 120
    high_rv_ttl_sec: int = 120

    long_max_chase_pct: float = 0.0015
    pullback_max_away_pct: float = 0.0025
    high_rv_market_entry_allowed: bool = False

    require_reclaim_for_countertrend: bool = True
```

Start with:

```python
EXECUTION.enabled = True
EXECUTION.shadow_mode = True
```

Then move selected playbooks live.

---

## 12. Professional Trading Rationale

A 20-minute futures bot should not optimize for "more signals". It should optimize for:

```text
Can this trade move enough, fast enough, from this exact entry price,
before cost and time decay destroy expectancy?
```

Market orders are not bad. Limit orders are not good by default. The order type is correct only when it matches the setup:

- Momentum needs urgency.
- Pullback needs price improvement.
- Reversal needs reclaim.
- High volatility needs retest.
- Trend needs location, not chase.

KARA's current weakness is not lack of intelligence. It is lack of execution discipline between signal and fill.

This proposal makes execution part of the strategy, not an afterthought.
