# KARA — Audit #13 Report
## 27 Mei 2026, 23:00 WIB (16:00 UTC)

**Periode data:** 26 Mei 18:00 UTC → 27 Mei 15:05 UTC (21.1 jam)
**Trades:** 54 (single user, deduplicated)
**Rate:** 2.56 trades/hr
**Signals generated:** 233

---

## Executive Summary

**NET PROFITABLE: +$2.77 (PF 1.128)** — tapi belum capai target PF >1.3.

6 fix dari Audit #12 memberikan hasil **mixed**:
- ✅ Frequency naik 3× (0.84 → 2.56/hr) — FIX #1 EMA bekerja
- ✅ SHORT membaik drastis (PnL -$9.83 → +$2.79, WR 36.7% → 50%)
- ✅ LIQ proxy disabled (0% fire) — FIX #2 confirmed
- ❌ LONG REGRESI BERAT (WR 57.7% → 42.5%, trailing 53.8% → 30%)
- ❌ Time exit NAIK (55.4% → 61.1%) — berlawanan dengan target
- ❌ Overall trailing TURUN (39.3% → 31.5%)
- ⚠️ HTF regime 98.3% CHOPPY — ini ROOT CAUSE utama

**Verdict: JANGAN mulai live executor. Diagnose LONG regression dulu.**

---

## Scorecard vs Target

| Metric | Audit #12 | Audit #13 | Target | Status |
|--------|-----------|-----------|--------|--------|
| Trades/hr | 0.84 | **2.56** | 2.5-4 | ✅ |
| WR | 46.4% | 44.4% | >48% | ❌ |
| PnL | +$2.59 | +$2.77 | >$5 | ❌ |
| PF | 1.076 | 1.128 | >1.3 | ❌ |
| Trailing fire | 39.3% | **31.5%** | >45% | ❌ REGRESI |
| Time exit | 55.4% | **61.1%** | <45% | ❌ WORSE |
| LONG WR | 57.7% | **42.5%** | >55% | ❌ REGRESI |
| LONG trailing | 53.8% | **30.0%** | >50% | ❌ REGRESI |
| SHORT WR | 36.7% | **50.0%** | >40% | ✅ |
| SHORT trailing | 26.7% | **35.7%** | >35% | ✅ |
| SHORT PnL | -$9.83 | **+$2.79** | ≥$0 | ✅ |
| Score↔PnL r | -0.18 | **+0.069** | ≥0 | ✅ |

**Skor: 5/12 target tercapai.** SHORT fix sukses, tapi LONG collapse.

---

## Fix Verification (6 Fixes)

### FIX #1: EMA Freshness 8/21 → 13/34 ✅ DEPLOYED & WORKING

**Evidence:**
- EMA=-5 (stale): **0%** di logs (was 100% di Audit #12) ✅
- EMA=+4 (medium): 41.2% of signals
- EMA=+10 (fresh): 57.1% of signals
- EMA=0 (neutral): 1.7%
- Frequency naik 0.84 → 2.56/hr (3× improvement)

**Assessment:** Fix bekerja sempurna. EMA tidak lagi penalty semua signal. TAPI — EMA +10 terlalu dominan (57%). Ini berarti banyak signal masuk yang seharusnya "medium" tapi dianggap "fresh". Ini bisa jadi penyebab LONG regression (entry terlalu agresif).

### FIX #2: Liq OI Proxy Disabled ✅ CONFIRMED

**Evidence:**
- LIQ=0 di 100% signals (233/233)
- 0 trades dengan liq mention di autopsy

**Assessment:** Clean disable. Tidak ada lagi drag dari proxy yang kontradiksi.

### FIX #3: MFI Bearish Disabled ✅ CONFIRMED

**Evidence:**
- MFI fire rate: 66.1% overall (hanya positif values)
- MFI di SHORT signals: perlu verifikasi lebih lanjut dari signal data
- MFI values: hanya 0 dan positif (tidak ada negatif)

**Assessment:** MFI bearish disabled. Hanya bullish MFI yang fire (LONG enabler).

### FIX #4: XAM Window 2min→5min + Threshold 0.15%→0.10% ⚠️ MINIMAL IMPACT

**Evidence:**
- XAM fire rate: **10.3%** (24/233 signals) — naik dari 0%
- XAM di logs (recent): MTF=0 di 100% sample (126/126)
- XAM correlation: insufficient data (1 matched trade)

**Assessment:** XAM mulai fire di signal level tapi sangat jarang. BTC/ETH sideways period = expected. Bukan bug, tapi juga belum contribute ke edge.

### FIX #5a: Block SHORT kalau OB Bullish ⚠️ CANNOT FULLY VERIFY

**Evidence:**
- Tidak ada `ob_bullish_contradiction` di skip reasons (logs hanya show `score_below_threshold` dan `low_atr`)
- SHORT trades: 14 total, WR 50%, PnL +$2.79

**Assessment:** Mungkin tidak banyak SHORT+OB bullish scenario terjadi, atau fix bekerja silently. SHORT performance membaik = indirect evidence fix helps.

### FIX #5b: SHORT TP1 ×0.70 + Hold +3min ✅ WORKING

**Evidence:**
- SHORT trailing fire: **35.7%** (naik dari 26.7%) ✅
- SHORT time_exit: 57.1% (turun dari 66.7%)
- SHORT PnL: +$2.79 (dari -$9.83)
- SHORT R:R: 1.56x

**Assessment:** TP1 yang lebih rendah membuat trailing lebih reachable. SHORT sekarang profitable.

---

## 🚨 Critical Finding: LONG REGRESSION

### Data
| Metric | Audit #12 | Audit #13 | Delta |
|--------|-----------|-----------|-------|
| LONG WR | 57.7% | 42.5% | **-15.2%** |
| LONG trailing | 53.8% | 30.0% | **-23.8%** |
| LONG time_exit | ~46% | 62.5% | **+16.5%** |
| LONG PnL | +$12.42 | -$0.02 | **-$12.44** |

### Root Cause Analysis

**Hipotesis 1: EMA terlalu longgar → entry premature**

Evidence:
- EMA=+10 (fresh) di 57% signals = bot menganggap banyak cross "fresh"
- Tapi trailing hanya fire 30% untuk LONG
- Artinya: bot masuk terlalu cepat, price belum benar-benar trending

Mekanisme: EMA 13/34 cross terjadi → freshness check bilang "fresh" (≤3 candles) → score naik +10 → threshold terlewati → entry → tapi price belum commit ke direction → sideways → time_exit

**Hipotesis 2: HTF Regime 98.3% CHOPPY**

Evidence dari signal data:
- HTF regime CHOPPY: 229/233 signals (98.3%)
- TRENDING_UP: hanya 4 signals (1.7%)
- Semua 30 matched trades terjadi di CHOPPY regime

Ini CRITICAL: Bot trading di market yang HTF-nya CHOPPY. Untuk trend-following scalper, ini = death zone. Trailing stop butuh trend continuation, tapi CHOPPY = mean-reverting.

**Tapi tunggu** — dari logs, regime yang dipakai untuk scoring BUKAN HTF regime. Logs menunjukkan:
- `regime=ranging` (82 entries)
- `regime=trending` (47 entries)  
- `regime=late_trend` (14 entries)
- `regime=volatile` (3 entries)

Jadi ada **2 regime system yang berbeda**:
1. **HTF regime** (4H): CHOPPY/TRENDING — dipakai untuk threshold adjustment
2. **Intraday regime**: ranging/trending/late_trend — dipakai untuk score multiplier

**Hipotesis 3: OB Score Inverse Correlation**

Component correlation data:
- OB: r = **-0.148** (INVERSE!) — OB score tinggi = loss
- EMA: r = **-0.185** (INVERSE!) — EMA score tinggi = loss
- RSI: r = **+0.228** (POSITIVE) — RSI bekerja
- FUND: r = **+0.132** (POSITIVE) — Funding bekerja

**INI KUNCI:** OB dan EMA — dua komponen dengan fire rate tertinggi (92.7% dan 98.3%) — keduanya INVERSE correlated dengan PnL!

### Root Cause Conclusion

**LONG regression disebabkan oleh kombinasi:**

1. **EMA +10 terlalu mudah didapat** → inflates score → entry premature
2. **OB score inverse** → OB bilang "ada wall support" tapi price tidak follow through
3. **Market CHOPPY** → trend-following entry di mean-reverting market = guaranteed time_exit

**Trailing winners vs Time-exit losers:**
- OB score: Trailing avg +1.7 vs TimeExit avg +5.7 (**OB tinggi = LOSS**)
- Score: Trailing avg 57.3 vs TimeExit avg 59.1 (higher score = worse outcome!)
- Momentum: Trailing 0.43% vs TimeExit 0.35% (momentum sedikit lebih baik di winners)

---

## 🚨 Critical Finding: OB INVERSE CORRELATION

Ini temuan paling penting di audit ini.

**OB Wall score di Audit #12:** r = +0.289 (POSITIVE, best predictor)
**OB Wall score di Audit #13:** r = -0.148 (INVERSE!)

**Apa yang berubah?** 

Kemungkinan besar: **market regime shift**. Di Audit #12, market trending → OB wall = support/resistance yang valid. Di Audit #13, market CHOPPY → OB wall = liquidity trap. Market maker pasang wall untuk attract orders, lalu pull wall dan reverse.

**Ini bukan bug kode. Ini regime-dependent behavior.**

OB wall sebagai signal hanya valid di trending market. Di choppy/ranging market, wall = noise atau trap.

---

## Component Health Matrix

| Component | Fire% | Correlation | Status | Action |
|-----------|-------|-------------|--------|--------|
| OB | 92.7% | **-0.148** | 🔴 INVERSE | Reduce weight atau gate by regime |
| EMA | 98.3% | **-0.185** | 🔴 INVERSE | Tighten freshness (≤2 candles = fresh) |
| RSI | 79.4% | **+0.228** | 🟢 BEST | Maintain |
| MFI | 66.1% | -0.006 | 🟡 Neutral | Maintain (LONG only) |
| FUND | 73.4% | +0.132 | 🟢 Good | Maintain |
| LIQ | 0% | N/A | ✅ Disabled | Keep disabled |
| XAM | 10.3% | N/A | 🟡 Low fire | Monitor |

---

## Score Quintile Analysis

| Quintile | Score Range | N | WR | PnL | Trail% |
|----------|-------------|---|-----|-----|--------|
| Q1 (lowest) | 52-54 | 10 | 50.0% | +$2.55 | 40.0% |
| Q2 | 54-57 | 10 | 40.0% | -$2.46 | 20.0% |
| Q3 | 57-59 | 10 | 50.0% | +$0.06 | 20.0% |
| Q4 | 59-62 | 10 | 30.0% | -$0.39 | 20.0% |
| Q5 (highest) | 62-81 | 14 | 50.0% | +$3.01 | 50.0% |

**Pattern:** Score TIDAK monoton. Q1 dan Q5 perform best, Q2-Q4 = noise zone.

Ini menunjukkan:
- **Low score (52-54):** Hanya lolos kalau momentum kuat → trailing fires
- **Mid score (54-62):** Inflated oleh EMA+OB yang inverse → false confidence → time_exit
- **High score (62+):** Genuine multi-component alignment → works

**Implikasi:** Threshold 45 terlalu rendah. Trades di range 52-57 yang lolos karena EMA+OB inflation = mostly losers.

---

## Per-Asset Highlights

**Top Performers (all trailing exits):**
- ATOM: 2 trades, 100% WR, +$3.07
- AR: 2 trades, 100% WR, +$2.50 (SHORT!)
- RENDER: 2 trades, 100% WR, +$1.29
- TIA: 2 trades, 100% WR, +$1.18

**Worst Performers (all time_exit):**
- FET: 2 trades, 0% WR, -$2.21
- SUPER: 2 trades, 0% WR, -$1.19 (SHORT)
- IO: 1 trade, 0% WR, -$1.19 (SHORT)
- BIO: 1 trade, 0% WR, -$1.17

**Pattern:** Winners = assets dengan clear trend (ATOM, AR, RENDER trending). Losers = choppy altcoins tanpa direction.

---

## Temporal Analysis

| Date | N | WR | PnL | Trail% | TimeEx% |
|------|---|-----|-----|--------|---------|
| 26 Mei (evening) | 11 | 45.5% | -$2.04 | 18.2% | 63.6% |
| 27 Mei (full day) | 43 | 44.2% | +$4.81 | 34.9% | 60.5% |

27 Mei siang lebih baik dari malam 26 Mei. Trailing naik dari 18% ke 35% = market mulai trending siang hari.

---

## 🔧 Rekomendasi (Prioritas)

### P0 — IMMEDIATE (Deploy hari ini)

#### 1. EMA Freshness Tighten: ≤2 candles = fresh (bukan ≤3)

**Root cause:** EMA +10 terlalu mudah didapat (57% signals). Banyak "fresh" cross yang sebenarnya sudah 3 candles = 3 menit = price sudah move.

**Fix:**
```python
# Sebelum (current):
if candles_since_cross <= 3:  # fresh
    pts = 10

# Sesudah:
if candles_since_cross <= 2:  # truly fresh (just crossed)
    pts = 10
elif candles_since_cross <= 5:  # medium
    pts = 4
else:  # stale
    penalty = min(candles_since_cross - 5, 10)
```

**Expected impact:** EMA +10 turun dari 57% ke ~30%. Fewer false entries.

#### 2. OB Weight Reduction di CHOPPY/Ranging Regime

**Root cause:** OB wall inverse correlated di choppy market. Wall = trap, bukan support.

**Fix:**
```python
# Di _calculate_scalper_score, setelah OB score dihitung:
if regime_cat in ('ranging', 'choppy') and abs(ob_score) > 10:
    ob_score = int(ob_score * 0.5)  # halve OB contribution in choppy
    reasons.append(f"⚠️ OB reduced (choppy regime)")
```

**Expected impact:** OB tidak lagi inflate score di wrong regime. Fewer premature entries.

#### 3. Minimum Score Threshold Naik: 45 → 50 (base)

**Root cause:** Trades di score 52-57 mostly losers (Q2 = 40% WR, -$2.46). Threshold terlalu rendah = noise trades lolos.

**Fix:** `BASE_THRESHOLD = 50` (dari 45)

**Expected impact:** ~20% fewer trades, tapi yang lolos = higher quality. Frequency turun dari 2.56 ke ~2.0/hr (masih dalam target).

### P1 — NEXT DEPLOY (28 Mei)

#### 4. Regime-Aware Entry Gate

**Observation:** 98.3% signals di CHOPPY HTF. Bot seharusnya TIDAK agresif di CHOPPY.

**Fix:** Kalau HTF = CHOPPY, tambah +5 ke threshold (effective threshold = 55 di choppy).

```python
if htf_regime == 'CHOPPY':
    threshold += 5
    reasons.append("📊 HTF choppy — threshold raised")
```

#### 5. OB Exclude dari Score di Ranging Regime

Lebih agresif dari P0#2: di ranging regime, OB score = 0 (bukan halved).

```python
if regime_cat == 'ranging':
    ob_score = 0  # OB meaningless in ranging
```

### P2 — MONITOR (Audit #14)

#### 6. Score Formula Review

Current: `aligned_setup + confirm_pts`

Problem: EMA dan OB dominate confirm_pts tapi keduanya inverse. RSI dan FUND yang positif tapi weight-nya kecil.

Consider: Reweight — RSI ×1.5, FUND ×1.2, OB ×0.5, EMA cap at +6 (bukan +10).

#### 7. Trailing Stop Activation Threshold

Current: trailing activates after TP1 hit.
Problem: TP1 = 0.85% untuk LONG. Di choppy market, 0.85% jarang tercapai.

Consider: LONG TP1 ×0.80 (= 0.68%) di choppy regime, mirip SHORT fix.

---

## Decision Matrix (dari AUDIT_TODO_27MEI)

| Kondisi | Keputusan | Status |
|---------|-----------|--------|
| PF > 1.3 + trailing > 45% + freq > 2.5/hr | START LIVE EXECUTOR | ❌ PF 1.128, trail 31.5% |
| PF 1.0-1.3 + freq > 2/hr | Collect more data | ✅ **INI KITA** |
| PF < 1.0 | Bisect fixes | ❌ Tidak perlu |
| SHORT PnL < -$5 | Disable SHORT | ❌ SHORT profitable |
| Frequency < 1/hr | EMA fix belum deploy | ❌ Freq OK |

**Keputusan: Collect more data + deploy P0 fixes untuk address LONG regression.**

---

## Bisect Analysis

Dari 6 fix yang di-deploy, mana yang menyebabkan LONG regression?

| Fix | Suspect Level | Reasoning |
|-----|---------------|-----------|
| Fix 1 (EMA) | 🔴 HIGH | EMA +10 terlalu mudah = inflated scores = premature entry |
| Fix 2 (Liq) | 🟢 LOW | Hanya disable, tidak bisa cause regression |
| Fix 3 (MFI) | 🟢 LOW | Hanya affect SHORT |
| Fix 4 (XAM) | 🟢 LOW | 10% fire, minimal impact |
| Fix 5a (OB block) | 🟡 MED | Hanya affect SHORT, tapi OB logic change? |
| Fix 5b (SHORT exit) | 🟢 LOW | Hanya affect SHORT params |

**Conclusion:** Fix 1 (EMA freshness) adalah primary suspect untuk LONG regression. Bukan karena fix-nya salah, tapi karena threshold ≤3 terlalu longgar untuk market condition saat ini.

**TAPI — jangan revert Fix 1.** Fix 1 benar secara teori (EMA 8/21 vs 13/34 mismatch = real bug). Yang perlu di-tighten adalah freshness window (≤3 → ≤2) dan/atau OB weight reduction.

---

## Comparison dengan Trade Journal Data

Trade Journal (58 closed):
- WR: 46.6% (27W/31L)
- PnL: +Rp18.630
- R:R: 1.19x
- ATR Trail: 100% WR (17x)
- Time Exit: 14% WR (35x)

Audit #13 (54 trades, 1 user):
- WR: 44.4% (24W/30L)
- PnL: +$2.77
- R:R: 1.41x
- Trailing: 100% WR (17x)
- Time Exit: 12.1% WR (33x)

**Konsisten.** Perbedaan kecil karena user berbeda dan timing sedikit beda. Core pattern sama: trailing = edge, time_exit = drag.

---

## Key Insight: Kenapa Time Exit Masih 61%?

**Ini bukan bug. Ini structural.**

Bot ini trend-following scalper. Edge-nya = catch trend continuation via trailing stop. Tapi:
- Hanya ~30-35% entries benar-benar catch trend
- Sisanya 60-65% = market tidak move cukup → time_exit

**Cara reduce time_exit BUKAN extend hold time** (sudah dibuktikan Audit #5: 91% time_exit never recover).

**Cara yang benar:**
1. **Better entry filter** → hanya masuk kalau ada genuine trend signal (bukan inflated score)
2. **Lower TP1 di choppy** → trailing activate lebih cepat
3. **Regime gate** → skip entry di CHOPPY HTF kecuali score sangat tinggi

P0 fixes di atas address semua 3 point ini.

---

## Timeline Update

| Tanggal | Action |
|---------|--------|
| 27 Mei malam | Deploy P0 fixes (EMA tighten, OB reduce, threshold +5) |
| 28 Mei | Collect 40+ trades post-fix |
| 28 Mei malam | Audit #14 — verify LONG recovery |
| 29 Mei | If PF > 1.3 → start live executor. If not → P1 fixes |
| 30-31 Mei | Live executor dev / micro-live test |
| 1 Juni | GO/NO-GO |

---

## Summary

**Yang BEKERJA:**
- ✅ Frequency fix (EMA freshness) — 3× improvement
- ✅ SHORT fix (TP1 lower + OB block) — SHORT profitable
- ✅ Score alignment (r = +0.069, no longer inverse)
- ✅ LIQ proxy disabled — clean

**Yang PERLU FIX:**
- ❌ LONG regression — EMA terlalu longgar + OB inverse di choppy
- ❌ Time exit masih 61% — entry quality issue
- ❌ Trailing fire turun — TP1 unreachable di choppy LONG

**Root Cause:** Market regime CHOPPY + EMA/OB inflation = premature LONG entries yang tidak punya trend to ride.

**Next Action:** Deploy P0 fixes (EMA ≤2, OB ×0.6 di ranging, HTF CHOPPY +3, LONG TP1 ×0.80), collect data, audit 28 Mei.

---

## Fixes Applied (4 changes to `engine/scoring_engine.py`)

### Fix A1: EMA Freshness Tighten
- Fresh: ≤2 candles (was ≤3), bonus +8 (was +10)
- Medium: 3-7 candles, +4 (unchanged)
- Stale: ≥8 candles, penalty (unchanged)
- Setup boost: +4 (was +5)
- **Rationale:** 57% signals got +10 but r=-0.185. Crypto moves fast — 3min is NOT fresh.

### Fix A2: OB Weight ×0.6 in Ranging Regime
- When `abs(trend_pct) < 0.035` (ranging), OB points multiplied by 0.6
- Strong wall: 18 → 10 pts. Moderate: 10 → 6 pts.
- **Rationale:** OB r=-0.148 in choppy. Wall = trap. ×0.6 (not ×0.5) = not too aggressive.

### Fix A3: HTF CHOPPY Threshold +3
- Was: 0 (disabled). Now: +3 to effective threshold.
- **Rationale:** 98.3% signals in CHOPPY. +3 is gentle — won't kill frequency but raises bar.

### Fix B: LONG TP1 ×0.80 in CHOPPY HTF
- When `htf_regime == "CHOPPY"` and side == LONG, TP1 reduced by 20%
- e.g. 0.6% → 0.48%, making trailing activation easier
- **Rationale:** SHORT fix (×0.70) worked brilliantly. LONG gets ×0.80 (gentler — LONG moves naturally larger in crypto).

### Expected Impact
- Frequency: ~2.56/hr → ~1.8-2.2/hr (slight reduction from threshold +3 and EMA tighten)
- LONG trailing: 30% → target 40%+ (TP1 lower = easier to reach)
- Time exit: 61% → target <50% (fewer premature entries)
- OB inverse: should improve (reduced contribution in wrong regime)
- SHORT: should maintain (no changes to SHORT logic)
