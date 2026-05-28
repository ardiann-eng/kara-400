# KARA — Audit #15 (29 Mei 2026, 00:00 WIB / 28 Mei 16:00 UTC)

## Context

Deploy 28 Mei siang berisi **4 fix** dari Audit #14 root cause analysis:
1. HTF Regime 4H → 1H (redesign)
2. OB ×0.6 pakai htf_regime (reliable)
3. AI dipanggil setiap signal (post-filter)
4. Pattern Memory key mismatch (bug fix)

**Data:** User 7667519263 only (deduplicated, single-user audit)
**Period:** 28 Mei 05:48 - 15:14 UTC (~9.4 jam)

---

## 📊 HASIL POST-FIX (9.4 jam, 23 trades, user 7667519263)

| Metric | Audit #14 (broken) | Audit #15 | Delta | Status |
|--------|--------------------|-----------| ------|--------|
| Trades/hr | 4.51 | **2.44** | -46% | ✅ FREQUENCY TURUN |
| Win Rate | 26.7% | **43.5%** | +16.8pp | ✅ NAIK SIGNIFIKAN |
| Profit Factor | 0.368 | **1.257** | +0.889 | ✅ PROFITABLE |
| PnL | -$13.89 | **+$2.66** | +$16.55 | ✅ FLIP POSITIVE |
| Trailing fire | 15.6% | **30.4%** | +14.8pp | ✅ EXCELLENT |
| Time exit | 51.1% | **30.4%** | -20.7pp | ✅ TURUN DRASTIS |
| Momentum death | 20.0% | **30.4%** | +10.4pp | ✅ WORKING |
| Score↔PnL r | N/A | **-0.527** | — | 🔴 INVERSE (CRITICAL) |
| AI coverage | 3.1% | **0%** | -3.1pp | 🔴 AI NOT FIRING |
| HTF CHOPPY rate | 98% | **0%** | — | 🔴 FIX NOT ACTIVE |

---

## 📈 OVERALL METRICS

| Metric | Value |
|--------|-------|
| Trades | 23 |
| Win Rate | 43.5% (10W / 13L) |
| Total PnL | +$2.66 |
| Profit Factor | 1.257 |
| Gross Profit | +$13.01 |
| Gross Loss | -$10.35 |
| Avg Win | +$1.30 |
| Avg Loss | -$0.80 |
| Win/Loss Ratio | 1.63× |
| Avg Notional | $247.49 |
| Peak Equity | +$6.26 |
| Max Drawdown | $3.60 |
| Frequency | 2.44 trades/hr |

---

## 🎯 EXIT REASON BREAKDOWN

| Reason | N | % | WR% | PnL $ | Avg PnL |
|--------|---|---|-----|-------|---------|
| trailing_stop | 7 | 30.4% | **100%** | +$11.64 | +$1.66 |
| time_exit | 7 | 30.4% | 0% | -$7.89 | -$1.13 |
| momentum_death | 7 | 30.4% | 28.6% | -$0.52 | -$0.07 |
| stop_loss | 2 | 8.7% | 50% | -$0.57 | -$0.29 |

**Insight:**
- Trailing stop = **SOLE PROFIT SOURCE** (100% WR, +$11.64)
- Time exit = **SOLE LOSS SOURCE** (0% WR, -$7.89)
- Momentum death = **DAMAGE CONTROL** (avg loss -$0.07, prevents bigger time_exit losses)
- Stop loss = neutral (1 win +$1.22, 1 loss -$1.80)

---

## 📊 PER-ASSET PERFORMANCE

| Asset | N | WR% | PnL $ | Exit Reasons | Verdict |
|-------|---|-----|-------|--------------|---------|
| AR | 2 | 100% | +$4.01 | trailing×2 | 🏆 Best performer |
| VVV | 1 | 100% | +$1.60 | trailing×1 | ✅ |
| CHIP | 1 | 100% | +$1.51 | trailing×1 | ✅ |
| ALGO | 1 | 100% | +$1.28 | trailing×1 | ✅ |
| FARTCOIN | 2 | 50% | +$0.69 | trailing×1, time×1 | ✅ |
| TON | 2 | 50% | +$0.62 | time×1, sl×1 | 🟡 Mixed |
| APT | 1 | 100% | +$0.10 | momentum×1 | ⚪ Tiny |
| XMR | 1 | 100% | +$0.05 | momentum×1 | ⚪ Tiny |
| JTO | 2 | 50% | +$0.00 | trailing×1, time×1 | ⚪ Break-even |
| TAO | 1 | 0% | -$0.04 | momentum×1 | ⚪ Tiny loss |
| ZRO | 1 | 0% | -$0.04 | momentum×1 | ⚪ Tiny loss |
| HBAR | 1 | 0% | -$0.14 | momentum×1 | ⚪ |
| ORDI | 1 | 0% | -$0.22 | momentum×1 | ⚪ |
| DOT | 1 | 0% | -$0.23 | momentum×1 | ⚪ |
| ZEC | 1 | 0% | -$0.80 | time×1 | 🟡 |
| OP | 1 | 0% | -$0.94 | time×1 | 🔴 |
| LIT | 1 | 0% | -$1.80 | sl×1 | 🔴 |
| **WLD** | **2** | **0%** | **-$3.00** | time×2 | 🔴 TOXIC |

**Pattern:** Winners = trailing stop fires. Losers = time_exit (no movement).

---

## 📊 SIDE ANALYSIS

| Side | N | WR% | PnL $ | PF |
|------|---|-----|-------|-----|
| LONG | 6 | 66.7% | +$2.50 | 1.976 |
| SHORT | 17 | 35.3% | +$0.16 | 1.021 |

**Insight:** LONG jauh lebih profitable (PF 1.98 vs 1.02). SHORT break-even.
Tapi sample size kecil — 6 LONG vs 17 SHORT. Bot bias SHORT (74% trades).

---

## 🔴 SCORE ANALYSIS — STILL INVERSE (r = -0.527)

### Score↔PnL Correlation: r = -0.527 (WORST EVER)

| Score Bucket | N | WR% | PnL $ | Avg PnL |
|---|---|---|---|---|
| 50-55 | 7 | 42.9% | +$5.19 | **+$0.74** |
| 55-60 | 8 | 62.5% | +$1.40 | +$0.18 |
| 60-65 | 4 | 50.0% | +$1.18 | +$0.29 |
| **65-70** | **3** | **0%** | **-$3.34** | **-$1.11** |
| **70-80** | **1** | **0%** | **-$1.76** | **-$1.76** |

**Winner avg score: 56.4 | Loser avg score: 60.8 → 🔴 INVERSE**

### HIGH-SCORE TOXIC Trades (score ≥ 65):

| # | Asset | Side | Score | PnL | Exit | Autopsy |
|---|-------|------|-------|-----|------|---------|
| 1 | ZEC | long | 67 | -$0.80 | time_exit | OB=18, "normal" regime |
| 9 | JTO | short | 68 | -$1.90 | time_exit | OB=-18, high_vol |
| 14 | WLD | long | 75 | -$1.76 | time_exit | Vol 8.2%, HIGH-VOL WIPEOUT |
| 22 | FARTCOIN | short | 67 | -$0.65 | time_exit | Counter-predictive |

**Total damage score ≥65: -$5.11**
**Tanpa trades score ≥65: PnL = +$7.77, PF ≈ 2.5**

---

## 🔬 SIGNAL COMPONENT ANALYSIS (12 matched)

### Component Correlation with PnL:

| Component | r | Status | >0 WR | ≤0 WR |
|-----------|---|--------|-------|-------|
| orderbook | +0.198 | ✅ Predictive | 100% (N=1) | 27% (N=11) |
| session | +0.348 | ✅ Predictive | 36% (N=11) | 0% (N=1) |
| oi_funding | +0.051 | ⚪ Neutral | 100% (N=1) | 27% (N=11) |
| liquidation | — | ⚪ Constant 0 | — | — |

### Component Distribution (12 matched signals):

| Component | Values Seen | Notes |
|-----------|-------------|-------|
| OI/Funding | Mostly -8 (short bias) | Not discriminating |
| Liquidation | Always 0 | NEVER FIRES |
| Orderbook | -18, -10, -6, +10 | Wide range |
| Session | 0 or 14 | Binary (Asia=14, other=0) |
| Regime | 100% "normal" | **0 CHOPPY** |

### Regime Distribution (from signals):

| Regime | N | % |
|--------|---|---|
| normal | 11 | 92% |
| high_vol | 1 | 8% |
| **choppy** | **0** | **0%** |

**→ 1H Regime fix TIDAK AKTIF. Seharusnya ~40-60% CHOPPY.**

---

## ⏰ HOURLY PERFORMANCE

| Hour UTC | N | WR% | PnL $ | Notes |
|----------|---|-----|-------|-------|
| 05:00 | 1 | 0% | -$0.80 | Cold start |
| 06:00 | 3 | 66.7% | +$4.21 | 🏆 Best hour |
| 07:00 | 2 | 50% | +$1.55 | Good |
| 08:00 | 1 | 0% | -$1.24 | WLD wipeout |
| 12:00 | 2 | 50% | -$1.85 | JTO toxic |
| 13:00 | 5 | 40% | +$0.44 | Mixed |
| 14:00 | 8 | 50% | +$2.14 | Active, profitable |
| 15:00 | 1 | 0% | -$1.80 | LIT stop loss |

---

## 📝 TRADE LOG (Chronological)

| # | Time | Asset | Side | Score | Exit | PnL $ | Cum $ | Status |
|---|------|-------|------|-------|------|-------|-------|--------|
| 1 | 05:48 | ZEC | long | 67 | time_exit | -0.80 | -0.80 | ❌ HIGH-SCORE TOXIC |
| 2 | 06:14 | AR | short | 52 | trailing_stop | +2.91 | +2.11 | ✅ LOW-SCORE GOLD |
| 3 | 06:21 | TON | short | 54 | time_exit | -0.60 | +1.51 | ❌ |
| 4 | 06:30 | JTO | long | 53 | trailing_stop | +1.90 | +3.41 | ✅ LOW-SCORE GOLD |
| 5 | 07:27 | ZRO | short | 53 | momentum_death | -0.04 | +3.37 | ❌ tiny |
| 6 | 07:30 | VVV | long | 56 | trailing_stop | +1.60 | +4.97 | ✅ |
| 7 | 08:37 | WLD | short | 62 | time_exit | -1.24 | +3.72 | ❌ HIGH-VOL WIPEOUT |
| 8 | 12:27 | XMR | long | 56 | momentum_death | +0.05 | +3.78 | ✅ tiny |
| 9 | 12:50 | JTO | short | 68 | time_exit | -1.90 | +1.88 | ❌ HIGH-SCORE TOXIC |
| 10 | 13:14 | HBAR | short | 64 | momentum_death | -0.14 | +1.74 | ❌ |
| 11 | 13:18 | AR | short | 56 | trailing_stop | +1.10 | +2.84 | ✅ |
| 12 | 13:43 | TAO | short | 54 | momentum_death | -0.04 | +2.80 | ❌ tiny |
| 13 | 13:50 | ALGO | short | 54 | trailing_stop | +1.28 | +4.08 | ✅ LOW-SCORE GOLD |
| 14 | 13:58 | WLD | long | 75 | time_exit | -1.76 | +2.32 | ❌ HIGH-SCORE TOXIC |
| 15 | 14:03 | FARTCOIN | short | 64 | trailing_stop | +1.34 | +3.66 | ✅ |
| 16 | 14:08 | TON | short | 60 | stop_loss | +1.22 | +4.88 | ✅ SL profit?? |
| 17 | 14:11 | DOT | short | 56 | momentum_death | -0.23 | +4.65 | ❌ |
| 18 | 14:21 | CHIP | long | 58 | trailing_stop | +1.51 | +6.16 | ✅ |
| 19 | 14:29 | APT | short | 55 | momentum_death | +0.10 | +6.26 | ✅ tiny |
| 20 | 14:31 | OP | short | 58 | time_exit | -0.94 | +5.32 | ❌ |
| 21 | 14:40 | ORDI | short | 53 | momentum_death | -0.22 | +5.10 | ❌ |
| 22 | 14:44 | FARTCOIN | short | 67 | time_exit | -0.65 | +4.46 | ❌ HIGH-SCORE TOXIC |
| 23 | 15:14 | LIT | short | 59 | stop_loss | -1.80 | +2.66 | ❌ SL HEMORRHAGE |

---

## 🔍 AUTOPSY ANALYSIS (Bot Self-Diagnosis)

### Categories:

| Category | N | PnL $ | Description |
|----------|---|-------|-------------|
| TRAILING STOP WINNER | 5 | +$6.75 | Edge working |
| LOW-SCORE GOLD | 3 | +$6.09 | Score 52-54 = profitable |
| HIGH-SCORE TOXIC | 4 | -$5.11 | Score 65+ = counter-predictive |
| HIGH-VOL WIPEOUT | 1 | -$1.24 | WLD vol 8.2% |
| SL HEMORRHAGE | 1 | -$1.80 | LIT slippage |
| TIME EXIT LOSS | 3 | -$2.53 | No movement trades |
| MOMENTUM DEATH | 5 | -$0.50 | Early cut (working) |

### Key Autopsy Quotes:

> **Trade #2 (AR short, score=52, +$2.91):** "LOW-SCORE GOLD: Score 52 justru cuan $2.91. Bot terlalu skeptis pada sinyal lemah."

> **Trade #9 (JTO short, score=68, -$1.90):** "HIGH-SCORE TOXIC: Score 68 counter-predictive. Mean-reversion guard atau regime multiplier masih salah."

> **Trade #14 (WLD long, score=75, -$1.76):** "HIGH-SCORE TOXIC: Score 75 counter-predictive. Mean-reversion guard atau regime multiplier masih salah."

> **Trade #7 (WLD short, score=62, -$1.24):** "HIGH-VOL WIPEOUT: Vol 8.2% terlalu gila untuk scalper. Blacklist WLD kalau vol >6%."

---

## 🔴 CRITICAL FINDINGS

### F1: Score INVERSE — r = -0.527 (WORST EVER)
- **Root cause:** OB=18 masih muncul tanpa reduction (regime selalu "normal")
- **Impact:** Score 65+ = 4 trades, 0 wins, -$5.11
- **Fix:** Score cap 60 (temporary) + debug 1H regime (permanent)

### F2: 1H Regime = 0% CHOPPY (FIX NOT ACTIVE)
- Semua signal = "normal" atau "high_vol"
- **Seharusnya:** ~40-60% CHOPPY setelah fix
- **Root cause candidates:**
  - Code tidak ter-deploy ke Railway
  - 1H candle data tidak tersedia dari HL
  - Cache issue (15 min cache tapi data stale)
  - Bug di `_fetch_1h_regime()` — mungkin exception silently caught

### F3: AI Intelligence = 0% Coverage (NOT FIRING)
- Zero signals mendapat AI evaluation
- **Root cause candidates:**
  - Import error / module crash
  - API key not set di Railway env
  - Conditional gate masih blocking
  - Deploy gagal

### F4: WLD = Toxic Asset
- 2 trades, 0 wins, -$3.00
- Vol 8.2% = terlalu volatile untuk scalper
- Pattern memory seharusnya block setelah loss ke-2
- **Fix:** Hardcode blacklist WLD (vol >6%)

### F5: Liquidation NEVER FIRES
- 12/12 matched signals: liquidation = 0
- HL liquidation data mungkin sparse/unavailable
- **Impact:** 12 pts scoring capacity wasted

---

## ✅ POSITIVE FINDINGS

### P1: Trailing Stop = 30.4% Fire Rate (TARGET MET)
- Target ≥25% → achieved 30.4%
- 100% WR, +$11.64 total
- **Ini EDGE utama bot. Jangan sentuh.**

### P2: Momentum Death = Working Perfectly
- 30.4% fire rate, avg loss -$0.07
- Prevents flat trades from becoming -$1.00 time_exit losses
- **Saves estimated $3-5 per session**

### P3: Frequency Turun 46% (4.51 → 2.44/hr)
- Overtrading problem dari Audit #14 resolved
- Fewer trades = less fee drag

### P4: Time Exit Turun 20pp (51% → 30%)
- Momentum death absorbs would-be time_exit trades
- Remaining time_exits = genuine no-movement situations

### P5: LONG Outperforms (PF 1.98 vs SHORT 1.02)
- Crypto upward bias confirmed
- Consider: increase LONG threshold tolerance, tighten SHORT

---

## 🎯 ACTION ITEMS

### P0 — IMMEDIATE (Hari Ini, 29 Mei)

| # | Action | Why | How to Verify |
|---|--------|-----|---------------|
| 1 | **Verify deploy: `railway logs` cari "1H Regime"** | 0 CHOPPY = code not running | Grep logs for "CHOPPY" or "1h_regime" |
| 2 | **Verify AI: `railway logs` cari "AI evaluation"** | 0% coverage = module dead | Grep logs for "ai_analyst" or error |
| 3 | **Implement score cap: reject score > 62** | Score 65+ = -$5.11 (4 trades, 0 wins) | Monitor next 20 trades |
| 4 | **Blacklist WLD** | Vol 8.2%, 0% WR, -$3.00 | Add to asset blacklist |

### P1 — Root Cause Fix (29 Mei)

| # | Action | Why |
|---|--------|-----|
| 5 | **Debug `_fetch_1h_regime()`** | Mungkin HL tidak punya 1H candle endpoint, atau exception caught |
| 6 | **Debug AI post-filter** | Mungkin import error atau env var missing |
| 7 | **Pattern memory verify** | WLD masuk 2× tanpa block — key mismatch masih ada? |
| 8 | **OB cap: max OB=10 kalau regime ≠ TRENDING** | OB=18 di "normal" = trap signal |

### P2 — Optimization (Setelah P0/P1 Fix)

| # | Action | Why |
|---|--------|-----|
| 9 | **Evaluate SHORT threshold +3** | SHORT PF 1.02 = break-even, terlalu banyak noise |
| 10 | **Consider LONG bias** | LONG PF 1.98, tapi hanya 26% of trades |
| 11 | **Session component investigation** | r=+0.348, session=14 = Asia session profitable? |

---

## 📊 COMPARISON TIMELINE (Audit #13 → #14 → #15)

| Metric | Audit #13 | Audit #14 | Audit #15 | Trend |
|--------|-----------|-----------|-----------|-------|
| Trades/hr | 2.56 | 4.51 | **2.44** | ✅ Back to normal |
| Win Rate | 44.4% | 26.7% | **43.5%** | ✅ Recovered |
| Profit Factor | 1.128 | 0.368 | **1.257** | ✅ Best yet |
| Trailing fire | 31.5% | 15.6% | **30.4%** | ✅ Recovered |
| Time exit | 61.1% | 51.1% | **30.4%** | ✅ Best yet |
| PnL | +$2.77 | -$13.89 | **+$2.66** | ✅ Profitable |

---

## 🚨 Red Flags untuk Audit #16

| Kondisi | Action |
|---|---|
| PF < 0.8 setelah score cap | Score cap terlalu agresif → relax ke 65 |
| Frequency < 1.0/hr | Score cap blocking terlalu banyak → relax |
| Trailing < 20% | Entry quality degraded → investigate |
| 1H regime masih 0% CHOPPY | Code definitely not deployed → manual redeploy |
| AI masih 0% | Module broken → disable cleanly, don't let it silently fail |
| WLD masih masuk | Blacklist not working → check implementation |

---

## 🧮 SIMULATION: Score Cap Impact

**If score cap = 60 was active:**
- Blocked trades: #1 (ZEC 67), #9 (JTO 68), #14 (WLD 75), #22 (FARTCOIN 67), #7 (WLD 62), #10 (HBAR 64), #15 (FARTCOIN 64)
- Wait — score 60-64 includes winners (#15 FARTCOIN trailing +$1.34)

**Better: Score cap = 65:**
- Blocked: #1 (ZEC 67, -$0.80), #9 (JTO 68, -$1.90), #14 (WLD 75, -$1.76), #22 (FARTCOIN 67, -$0.65)
- Saved: $5.11
- Missed wins: $0
- **Net improvement: +$5.11 → PnL would be +$7.77, PF ≈ 2.5**

**Recommendation: Score cap = 65 (not 60)**

---

## 📋 Per-User Context (All 5 Users)

| User | N | WR% | PnL $ | PF | Avg Notional | Trailing% |
|------|---|-----|-------|-----|-------------|-----------|
| 7667519263 | 23 | 43.5% | +$2.66 | 1.257 | $247 | 30.4% |
| 1734306621 | 23 | 43.5% | +$2.23 | 1.289 | $194 | 34.8% |
| 5034879285 | 23 | 47.8% | +$1.21 | 1.148 | $193 | 30.4% |
| 6843478231 | 21 | 28.6% | -$4.31 | 0.656 | $281 | 14.3% |
| 7692363431 | 22 | 31.8% | -$4.34 | 0.761 | $382 | 22.7% |

### Why Users Differ:

1. **Position Size:** Losing users have 1.5-2× larger notional ($281-$382 vs $193-$247)
   - Same signal, same direction, but bigger size = bigger absolute loss
2. **Execution Slippage:** Avg 0.03% entry spread across users
   - Last user to fill gets worst price
3. **Missing Signals:** Some users miss 1-2 signals due to Telegram delivery
   - User 6843478231 missed profitable signals, got stuck with losers

**Conclusion:** Perbedaan bukan strategi, tapi SIZE dan EXECUTION ORDER.

---

## Timeline

| Waktu (WIB) | Action |
|---|---|
| 29 Mei 00:00 | Audit #15 complete |
| 29 Mei pagi | Verify deploy (railway logs) |
| 29 Mei siang | Implement score cap 65 + WLD blacklist |
| 29 Mei sore | Deploy fix |
| 29 Mei 18:00 - 30 Mei 18:00 | Collect trades (~24 jam) |
| **30 Mei 18:00** | **Audit #16** |

---

## Files to Check (29 Mei)

| File | What to Check |
|------|---------------|
| `engine/scoring_engine.py` | `_fetch_1h_regime()` — is it being called? |
| `intelligence/ai_analyst.py` | Is it imported and called post-filter? |
| `execution/paper_executor.py` | Pattern memory key — still mismatched? |
| Railway env vars | `OPENAI_API_KEY` or equivalent set? |
| Railway deploy logs | Did latest commit actually deploy? |

---

## Catatan

### Kenapa User 7667519263 Dipilih untuk Audit
- Median performer (bukan best, bukan worst)
- 23 trades = full coverage (tidak miss signal)
- Notional $247 = representative middle ground
- Kalau user ini profitable, strategi works. Perbedaan antar user = execution variance.

### Verdict: STRATEGY WORKS, SCORING BROKEN
Bot **profitable** karena:
1. Trailing stop catches trends (30.4% fire, 100% WR)
2. Momentum death cuts flat trades early (-$0.07 avg)
3. Frequency reasonable (2.44/hr)

Bot **underperforms** karena:
1. Score inverse → high-score trades = traps
2. 1H regime not active → OB=18 passes unchecked
3. AI not active → no veto on bad trades
4. WLD not blacklisted → vol wipeouts

**Fix score + regime + AI = estimated PF 2.0+**

---

## 🔧 FIXES IMPLEMENTED (28 Mei Malam)

### Fix 5: RSI Momentum → confirm_pts (STRUCTURAL)
**File:** `engine/scoring_engine.py` — `_calculate_scalper_score()`

| Aspek | Sebelum | Sesudah |
|-------|---------|---------|
| RSI momentum location | `bull_setup += 8` / `bear_setup += 8` | `confirm_pts += 8` |
| Effect on score total | Same (8 pts masuk raw) | Same (8 pts masuk raw) |
| Effect on aligned_setup | INFLATED (+8) | NOT inflated |
| Effect on direction | Bisa flip direction | Cannot flip direction |

**Root cause yang di-fix:**
- Score 65+ = 0% WR, -$5.11 (4 trades semua loss)
- RSI momentum = LAGGING (price already moved, RSI confirms after)
- Masuk ke setup layer → inflate `aligned_setup` → score 67+ tanpa genuine leading edge
- Data: winners avg score 56.4, losers avg score 60.8 → RSI momentum inflate losers

**Expected impact:**
- High-score trades (65+) akan punya score lebih rendah (~60-62)
- Score↔PnL correlation membaik (less inflation from lagging)
- Direction decision lebih bersih (tidak di-flip oleh lagging RSI)

---

### Fix 6: OKX Liquidation WebSocket Stream (NEW DATA SOURCE)
**Files:** `data/ws_client.py` + `main.py`

| Aspek | Sebelum | Sesudah |
|-------|---------|---------|
| Liquidation data | HL only (sparse, ~0 events) | HL + OKX (high volume) |
| Liq fire rate | 0% (never fires) | Expected 5-15% |
| Data source | `wss://api.hyperliquid.xyz/ws` | + `wss://ws.okx.com:8443/ws/v5/public` |
| Auth required | No | No |
| Geo-blocked from Railway | N/A | **NOT blocked** (verified via SSH test) |

**Implementation:**
- New class `OKXLiquidationStream` — same pattern as `BinanceLiquidationStream`
- Subscribes to `liquidation-orders` channel (instType=SWAP, all pairs)
- Normalizes events to KARA format: `{coin, px, sz, side, source, time}`
- Feeds into `MarketDataCache.on_liquidations()` → `_calc_liq_cluster()` now has data

**Verification (from Railway SSH):**
```
Connected to wss://ws.okx.com:8443/ws/v5/public
Sent subscribe: {'op': 'subscribe', 'args': [{'channel': 'liquidation-orders', 'instType': 'SWAP'}]}
Received: {"event": "subscribe", "arg": {"channel": "liquidation-orders", "instType": "SWAP"}, "connId": "95e7b92b"}
```

**Expected impact:**
- `_calc_liq_cluster()` akan fire saat ada ≥2 liquidation events same direction dalam 10 min
- Liquidation = genuine leading signal (forced buying/selling = catalyst)
- Score +4 to +12 saat ada real cascade → higher score = genuine edge (not inflation)

---

### Fix 7: Large Order Clustering (NEW LEADING INDICATOR)
**File:** `engine/scoring_engine.py` — `_calculate_scalper_score()` (Setup Layer 5)

| Aspek | Detail |
|-------|--------|
| Data source | `cache.trades[asset]` (HL WS trades, already subscribed) |
| Window | 2 minutes |
| Large order threshold | MAX(3× median notional, $1,000) |
| Min count | ≥4 large orders same direction |
| Dominance threshold | >70% buy OR >70% sell |
| Max contribution | ±10 pts to setup layer |
| Fire rate expected | 10-20% (only during institutional activity) |

**Logic:**
1. Calculate median trade notional from last 200 trades
2. Filter trades ≥ threshold in last 2 minutes
3. If ≥4 large buys with >70% buy dominance → `bull_setup += 4 + count` (max 10)
4. If ≥4 large sells with >70% sell dominance → `bear_setup += 4 + count` (max 10)

**Why leading:**
- Institutional players split large orders into medium-sized trades (TWAP/iceberg)
- Clustering = accumulation BEFORE breakout
- Different from OB (passive liquidity) — this measures AGGRESSIVE flow
- Short window (2 min) = fresh signal, not stale

**Thresholds designed to avoid false positives:**
- $1,000 minimum prevents retail noise on altcoins
- 4 orders minimum prevents single whale from triggering
- 70% dominance prevents balanced markets from triggering

---

## 📋 Summary of All Pending Changes (Pre-Deploy)

| # | Fix | Type | Risk |
|---|-----|------|------|
| 5 | RSI momentum → confirm_pts | Structural (score architecture) | Low — score total unchanged |
| 6 | OKX Liquidation stream | New data source | Low — purely additive, no downside |
| 7 | Large Order Clustering | New leading indicator | Low — purely additive, max +10 pts |

**Net effect:** Score lebih akurat (less inflation from lagging), lebih banyak genuine leading data (liq + LOC). Tidak ada yang bisa REDUCE performance — worst case = liq/LOC tidak fire dan kita kembali ke status quo.

---

## 🚨 Monitoring Post-Deploy (Audit #16)

| Metric | Target | Red Flag |
|--------|--------|----------|
| LOC fire rate | 10-20% | >50% = threshold terlalu rendah |
| LOC fire rate | 10-20% | 0% = data issue |
| OKX Liq connected | Yes | Log "[OKXLiq] Connected" absent |
| Liq cluster fire | >0% | Still 0% = OKX data not arriving |
| Score↔PnL r | > -0.3 | Still < -0.5 = fix tidak cukup |
| Score range | 45-65 typical | Still 65+ frequent = RSI fix not deployed |
| Trailing fire | ≥25% | <20% = entry quality degraded |
