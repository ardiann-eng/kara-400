# KARA — Audit #16 (30 Mei 2026, ~02:00 WIB / 29 Mei 14:52 UTC cutoff)

## Context

Deploy 28 Mei 16:43 UTC berisi 3 fix dari Audit #15:
- **Fix 5:** RSI Momentum → `confirm_pts` (was `bull_setup`/`bear_setup`)
- **Fix 6:** OKX Liquidation WS Stream
- **Fix 7:** Large Order Clustering setup detector

**Data:** User 7667519263 (deduplicated, single-user audit)
**Period:** 28 Mei 05:48 → 29 Mei 14:52 UTC (~33h)
**Deploy boundary:** 28 Mei 16:43:05 UTC (file mtime di Railway)
**Pre-deploy:** 28 trades / 10.50h
**Post-deploy:** 51 trades / 21.71h

---

## 📊 HASIL — REGRESI POST-DEPLOY

| Metric | PRE | POST | Delta | Status |
|--------|-----|------|-------|--------|
| Trades/hr | 2.67 | 2.35 | −12% | ⚠️ |
| Win Rate | 46.4% | 35.3% | **−11.1pp** | 🔴 |
| Profit Factor | 1.430 | **0.678** | **−0.752** | 🔴 |
| PnL | +$5.10 | **−$11.17** | **−$16.27** | 🔴 |
| Trailing fire | 32.1% | **17.6%** | −14.5pp | 🔴 |
| Time exit | 28.6% | **54.9%** | +26.3pp | 🔴 |
| **SHORT WR** | 36.8% | **8.3%** | **−28.5pp** | 🔴 KRITIS |
| **SHORT PF** | 1.122 | **0.010** | — | 🔴 DEAD |
| Score↔PnL r | −0.399 | **−0.048** | +0.351 | ✅ Inversion fixed |

---

## ✅ Fix 5 (RSI Momentum) — WORKING

| Aspek | Hasil |
|-------|-------|
| Marker deployed | ✅ `[AUDIT #15 FIX]` di line 1648 (`confirm_pts += 8`) |
| Score↔PnL r | −0.527 (Audit#15) → −0.399 (PRE) → **−0.048 (POST)** |
| Score range | Stabil 52-79, distribusi tidak banyak shift |
| Direction integrity | RSI momentum tidak lagi flip direction (vote weight unchanged) |

**Kesimpulan:** Inversion problem dari Audit #15 hilang. Score sekarang netral. Belum +0.15 (target predictive), tapi stop merusak. Fix valid, tidak perlu rollback.

---

## ⚠️ Fix 6 (OKX Liquidation) — INERT

**Status verifikasi:**
- ✅ Class `OKXLiquidationStream` deployed (`/app/data/ws_client.py`)
- ✅ Init di `/app/main.py` line 657
- ✅ OKX WS connectivity confirmed dari Railway SSH (14 events / 60s test)
- 🔴 Liquidation score = 0 di **349/349 sinyal**

**Root cause:** `_calc_liq_cluster()` threshold (line 2113-2150):
```python
def pts_from_notional(n):
    if n >= 20_000: return 12
    if n >= 8_000: return 8
    if n >= 2_000: return 4
    return 0
```
Plus require ≥2 events same direction in 10min window. OKX events sparse per-asset (~1/min). Per-asset notional 10-min window jarang accumulate $2K.

**Impact:** Fix 6 tidak harm performance, tapi 12 pts scoring capacity wasted.

**Tidak urgent untuk audit ini** — bukan penyebab regresi. Defer ke Audit #17.

---

## ⚠️ Fix 7 (LOC) — NOT FIRING

**Status:**
- ✅ Code present line 1715-1781 (`_loc_pts`, "Large order cluster" log)
- 🔴 Tidak ada signal yang punya `LOC > 0`

**Root cause:** `large_threshold = max(median × 3, $1K)`. Median trade altcoin $20-50, 3× = $60-150. Floor $1K terlalu tinggi untuk altcoin trade size.

**Defer ke Audit #17.** Lower floor untuk non-major asset jika data baru menunjukkan still inert.

---

## 🔴 ROOT CAUSE: REGIME SHIFT + 2 STRUCTURAL BUGS

### Market Regime Shift (28 Mei → 29 Mei)

| HTF | PRE (137 sigs) | POST (212 sigs) |
|-----|---------------|-----------------|
| TRENDING_DOWN | 67.9% | 26.4% |
| TRENDING_UP | 7.3% | 40.6% |
| CHOPPY | 24.8% | 33.0% |

Crypto rally fundamentally berubah komposisi sinyal. Bot tetap signal SHORT (12 di POST) tapi sebagian besar reverse cepat.

### Bug Structural #1: SHORT V-Bottom Trap

**Pattern dari 22 SHORT @TRENDING_DOWN (PRE+POST):**
- 18 trades (82%) **price reverse setelah entry** dalam hold window
- Loser avg pre-move: **−0.439%** (sebelum entry)
- Winner avg pre-move: **−0.540%**
- Bot SHORT setelah dump kecil → entry di local low → bounce 0.3-0.9% → time_exit loss

**Code current (line 960):** `_min_momentum = 0.0025 if SHORT else 0.0015` — SHORT butuh dump 0.25% min.

**Tapi 0.25% terlalu rendah:** mini-panic dump 0.3-0.4% sering = exhaustion = langsung di-buy back. Bukan trend awal.

### Bug Structural #2: SL Distance Tidak Vol-Aware

**Data (79 trades):**
| RV bucket | Avg SL | SL/RV ratio |
|-----------|--------|-------------|
| 0-3% | 0.87% | 0.318× |
| 3-6% | 0.83% | 0.177× |
| 6-9% | 0.79% | **0.109×** |
| 9-15% | 0.86% | **0.078×** |

SL distance hampir konstan ~0.8% terlepas dari volatility. Saat RV 11%, SL = 0.078× of daily vol = **kurang dari 1 std dev di 10-min hold** = 50% probability whipsawed by normal noise.

**Code current (line 2287-2295):**
```python
raw_sl = atr_per_minute × 1.5
sl_pct = max(min(raw_sl, 0.020), 0.006)
```
Floor 0.6% kicks in untuk semua high-vol → SL terlalu sempit.

**Konsekuensi:** 5 real SL hits POST = $-10.15 damage. SL hit sebelum trend break terjadi.

---

## 📊 PER-EXIT BREAKDOWN POST

| Exit | N | % | WR% | PnL | Notes |
|------|---|---|-----|-----|-------|
| time_exit | 28 | 54.9% | 25.0% | $-15.90 | Hold doubled 6.4→11.2 min |
| trailing_stop | 9 | 17.6% | 100% | +$12.77 | Edge intact (LONG only di POST) |
| momentum_death | 9 | 17.6% | 11.1% | $-0.88 | Damage control working |
| stop_loss | 5 | 9.8% | 20.0% | $-7.16 | High-vol SL hits |

**Trailing winners semua LONG** di TRENDING_UP/CHOPPY. Tidak ada SHORT trailing fire di POST.

---

## 🎯 P0 FIXES IMPLEMENTED (29 Mei sore)

### P0-1: SHORT vs TRENDING_UP — Hard Block
**File:** `engine/scoring_engine.py` line ~810

```python
if side == Side.SHORT and htf_regime == "TRENDING_UP":
    log.info(f"[SKIP] {asset} | reason=short_against_uptrend | htf={htf_regime}")
    return None, score
```

**Why:** Existing logic hanya raise threshold +8 untuk SHORT counter-trend. Banyak score lolos. Hard block dibutuhkan untuk regime jelas-bullish.

**Retroactive impact:** 1 NEAR short ($-0.86 saved).

### P0-2: SHORT in CHOPPY — Threshold +5
**File:** line ~825

```python
if side == Side.SHORT and htf_regime == "CHOPPY":
    _choppy_short_min = config.SIGNAL.min_score_short_signal + 5  # 62→67
    if score < _choppy_short_min:
        return None, score
```

**Why:** Range market reverses fast on minor dump. SHORT mikro-setup (OB bear, OI bear) tidak survive macro range. Butuh confidence ekstra.

**Retroactive impact:** 1 AAVE short ($-0.71 saved).

### P0-3: SHORT min_momentum 0.25% → 0.50%
**File:** line ~960

```python
_min_momentum = 0.0050 if side == Side.SHORT else 0.0015  # was 0.0025
```

**Why:** 22 SHORT @TRENDING_DOWN (PRE+POST), 82% reversal. Loser pre-move avg 0.43% (just above 0.25% threshold). Winner avg 0.54%. Threshold 0.50% filter mini-panic dumps.

**Retroactive impact:** 8 SHORTs blocked, $5.30 saved (block ONDO×2, TAO×2, SUI, OP, AAVE, dan +$0.12 win TON yang juga ke-block — net $5.18).

### P0-4: Hold-Aware SL Formula (BUG FIX)
**File:** line ~2356

```python
if realized_vol > 0:
    _hold_min_est = 25 if score>=66 else 20 if score>=61 else 15 if score>=56 else 10
    SL_NOISE_MULT = 2.5
    _expected_swing = realized_vol * (_hold_min_est / (60.0 * 24.0)) ** 0.5
    _hold_sl_pct = _expected_swing * SL_NOISE_MULT
    _hold_sl_pct = max(0.005, min(_hold_sl_pct, 0.025))
    sl_pct = max(sl_pct, _hold_sl_pct)
```

**Why:** SL distance ATR-driven adalah per-minute, tidak scale ke hold window 10-25 min. Untuk RV 8%, hold 12min, expected std swing 0.7% → SL 0.6% gets whipsawed by noise. New formula: 2.5× expected swing → ~95% noise tolerance.

**Retroactive impact:** 5 real-SL trades, damage $-10.15 → $-6.51 = **$3.65 saved**.

**Side-effect:** TP1/TP2 distances scale with SL (existing RR enforcement). Trailing trigger threshold (existing 2× SL) juga ikut. Hold-aware SL menghasilkan TP1 lebih jauh di high-vol — harus monitor apakah trailing fire rate naik atau turun di high-vol.

---

## 🧮 SIMULASI RETROAKTIF (POST data, semua P0 active)

| Filter | Blocked | Saved |
|--------|---------|-------|
| short_low_momentum (P0-3) | 8 | $+5.30 |
| short_uptrend (P0-1) | 1 | $+0.86 |
| short_choppy_low (P0-2) | 1 | $+0.71 |
| Hold-aware SL (P0-4, real-SL only) | 5 (rescued) | $+3.65 |
| **Total estimated save** | **15** | **$+10.52** |

**Expected POST PnL:** $-11.17 → **−$0.65** (essentially break-even)

**Estimated PF:** 0.678 → 0.95-1.10

---

## 🔴 RESIDUAL DAMAGE (Tidak ter-fix oleh P0)

**16 high-vol time_exit losers, total $-21.09.** Pattern:
- Asset RV >6%
- Exit by time_exit (bukan SL hit)
- Adverse drift selama hold window 10-15 min

**Mengapa SL fix tidak help:** SL tidak hit. Trade exit by time. New SL formula tidak relevan untuk path ini.

**Hipotesis untuk solusi (perlu data lebih banyak):**

### Opsi A: Vol-aware position sizing
`notional × (base_vol / actual_vol)`. Asset RV 12% → notional 50% smaller → dollar loss reduction proporsional.
- Pro: Reduce dollar damage tanpa block trades
- Con: Reduce winnings juga di high-vol scenarios

### Opsi B: Vol-aware time_exit
`hold_min × (base_vol / actual_vol)^0.5`. High-vol → exit lebih cepat (5-7 min vs 10-15 min).
- Pro: Cut drift losses earlier
- Con: Miss high-vol winners yang butuh time develop

### Opsi C: Tighter momentum_death untuk high-vol
Existing −0.2%/5min. Untuk RV >6%: −0.1%/3min.
- Pro: Cut drift losses fastest
- Con: Whipsaw dari high-vol noise = false momentum_death triggers

**Decision:** Defer ke Audit #17. Sample N=16 belum cukup untuk pilih (rule #8). Setelah deploy P0 + collect 24h data, evaluate residual high-vol losses dan pilih pendekatan.

---

## 📋 PER-USER PnL (5 users, untuk konteks)

| User | N | WR% | PnL$ | PF |
|------|---|-----|------|-----|
| 7667519263 | 79 | 39.2% | -$6.07 | 0.85 |
| 1734306621 | ~79 | TBD | TBD | TBD |
| 5034879285 | 83 | TBD | TBD | TBD |
| 6843478231 | 76 | TBD | TBD | TBD |
| 7692363431 | 32 | TBD | TBD | TBD |

(Single-user audit consistent dengan Audit #15 method. Cross-user analysis defer.)

---

## 🚨 Red Flags untuk Audit #17

| Kondisi | Action |
|---|---|
| PF < 0.9 setelah P0 | P0 fix tidak cukup — investigate exit drift residual |
| Trailing fire < 20% | Hold-aware SL menggeser TP1 terlalu jauh — rollback formula |
| SHORT count = 0 dalam 24h | min_momentum 0.50% terlalu agresif — relax ke 0.40% |
| Liq score still 0% | Lower OKX threshold (Audit #17 task) |
| LOC fire still 0% | Lower large_threshold floor (Audit #17 task) |
| Real SL distance still <1% di high-vol | Hold-aware formula not deployed correctly |

---

## 📊 COMPARISON TIMELINE (Audit #14 → #15 → #16)

| Metric | Audit #14 | Audit #15 (PRE) | Audit #16 (POST) | Trend |
|--------|-----------|-----------------|------------------|-------|
| Trades/hr | 4.51 | 2.67 | 2.35 | ✅ Stable |
| WR | 26.7% | 46.4% | 35.3% | 🔴 Down |
| PF | 0.368 | 1.430 | 0.678 | 🔴 Down |
| Trailing fire | 15.6% | 32.1% | 17.6% | 🔴 Down |
| Time exit | 51.1% | 28.6% | 54.9% | 🔴 Up |
| Score↔PnL r | N/A | −0.399 | −0.048 | ✅ Up (better) |

**Pola:** Bot sehat di PRE (Audit #15 baseline), regresi di POST. Score Fix bekerja, tapi regime shift expose 2 bug structural lama (V-bottom + SL distance) yang sebelumnya tidak terlihat di TRENDING_DOWN dominant market.

---

## ⏰ DEADLINE: 1 JUNI — STATUS

| Kriteria Live-Ready | Target | Saat Ini | Gap |
|---|---|---|---|
| Paper PF > 1.3 (3 audit konsisten) | ≥1.30 | 0.678 | 0.62 |
| WR > 40% | ≥40% | 35.3% | 4.7pp |
| Score↔PnL r > +0.15 | ≥+0.15 | −0.048 | 0.20 |
| Trailing fire ≥ 25% | ≥25% | 17.6% | 7.4pp |
| Time exit < 45% | ≤45% | 54.9% | 10pp |
| Live executor tested | ✅ | ❌ | Not started |
| Slippage measured | ✅ | ❌ | Not started |

**Status:** Tidak live-ready 1 Juni. Realistic ETA dengan P0 fixes deploy 30 Mei + 48h validation = **2-3 Juni earliest**, dengan asumsi Audit #17 menunjukkan PF > 1.0 dan Audit #18 confirms PF > 1.3.

**Recommendation:** Push live launch ke 4-5 Juni. Prioritas saat ini = paper validation + start live executor dev paralel.

---

## Timeline Action

| Waktu (WIB) | Action |
|---|---|
| 30 Mei 02:00 | Audit #16 complete (this doc) |
| 30 Mei pagi | Verify P0 deploy via railway logs |
| 30 Mei 18:00 - 31 Mei 18:00 | Collect 24h post-fix data |
| **31 Mei 20:00** | **Audit #17 — verify P0 impact, decide vol-aware sizing** |
| 1-2 Juni | Audit #18 + start live executor dev |
| 3-4 Juni | Micro-live testing $10 capital |
| 5 Juni | GO/NO-GO decision |

---

## Catatan Untuk Audit #17

**Yang harus di-verify:**
1. P0-1/2/3 logs muncul: `[SKIP] ... reason=short_against_uptrend|short_choppy_low_conviction|low_momentum`
2. P0-4 hold-aware SL log muncul: `[HOLD-SL] {asset} | rv=X% hold=Ym | ...`
3. Real SL distance >= 1.0% untuk high-vol trades
4. SHORT count turun signifikan
5. PF naik ke ≥1.0
6. Trailing fire rate >=25% (tidak turun karena SL fix)

**Yang harus di-investigasi (jika P0 cukup):**
- Vol-aware sizing untuk residual high-vol time_exit losses
- LOC threshold lowering
- Liq cluster threshold lowering

**Yang harus di-investigasi (jika P0 tidak cukup):**
- SL formula tidak deploy correctly?
- Trailing trigger threshold conflict dengan SL fix?
- Market regime continued shift?

---

## Verdict

**Strategy works** (LONG side break-even di POST regime shift). Edge utama (trailing stop 100% WR di LONG) intact.

**Yang gagal:** SHORT side hancur karena V-bottom traps + SL formula yang tidak vol-aware. Bukan masalah scoring (Fix 5 working). Bukan masalah direction filter macro (HTF working). Masalah di entry timing micro (mini-dump = noise, not trend) + SL noise tolerance.

**Yang dibutuhkan:** P0 fixes yang sudah implement targeting kedua bug ini. Bukan disable, bukan blacklist asset — fix root cause di entry quality dan SL formula proper.

**Tidak ada hardcoded blacklist** karena prinsip persona: "Disable bukan solusi. Disable adalah menyerah."
