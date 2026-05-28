# KARA — Audit #14 (28 Mei 2026, 23:00 WIB / 16:00 UTC)

## Context

Deploy 27 Mei malam berisi **8 fix** dari Audit #13 findings.
**HASIL: KATASTROFIK.** PF 0.368, WR 26.7%, PnL -$13.89 dalam 10 jam.

---

## 🔴 HASIL POST-DEPLOY (10 jam, 45 trades)

| Metric | Audit #13 | Post-Deploy | Delta | Status |
|--------|-----------|-------------|-------|--------|
| Trades/hr | 2.56 | **4.51** | +76% | 🔴 OVERTRADING |
| Win Rate | 44.4% | **26.7%** | -17.7pp | 🔴 ANJLOK |
| Profit Factor | 1.128 | **0.368** | -67% | 🔴 KATASTROFIK |
| PnL | +$2.77 | **-$13.89** | -$16.66 | 🔴 BLEEDING |
| Trailing fire | 31.5% | **15.6%** | -15.9pp | 🔴 SETENGAH |
| Time exit | 61.1% | 51.1% | -10pp | 🟡 Sedikit turun |
| EMA +8 rate | 57% | **21.7%** | -35pp | ✅ FIX WORKS |
| XAM fire | 10.3% | **24.2%** | +13.9pp | ✅ FIX WORKS |
| Momentum death | N/A | **20.0%** | NEW | ✅ FIX WORKS |
| OB avg | 5.2 | 2.91 | -2.29 | 🟡 PARTIAL |

---

## 🔍 ROOT CAUSE ANALYSIS

### RC1: HTF Regime Detector RUSAK (98% CHOPPY)
- 4H EMA10/20 + strength 0.30 = terlalu ketat untuk crypto
- Hasilnya: SELALU CHOPPY → threshold +3 permanen, OB ×0.6 tidak konsisten
- **Impact:** Bot tidak bisa bedakan trending vs choppy

### RC2: OB ×0.6 Fix TIDAK KONSISTEN
- 59/161 signal masih punya OB=18 (tanpa reduction)
- 30/161 signal punya OB=10 (fix jalan)
- **Root cause:** Kondisi `abs(trend_pct) < 0.035` flip-flop tiap detik
- OB=18 di choppy = TRAP signal (wall di-eat, harga balik)

### RC3: Pattern Memory TIDAK PERNAH TRIGGER (Bug)
- `evaluate()` pakai key `AR_long_ranging`
- `record_outcome()` pakai key `AR_long_scalper`
- **KEY MISMATCH** → pattern memory selalu n=0 → tidak pernah penalty
- Akibat: bot masuk AR 4×, XPL 4×, LIT 4× — semua loss, tidak di-block

### RC4: AI Intelligence Hampir Tidak Jalan (3.1%)
- Hanya 5/161 signal dapat AI evaluation
- Cost optimization gate terlalu ketat
- Cache 5 menit = AI jarang dipanggil ulang
- Timeout 10s = terlalu lama untuk scalper

---

## ✅ FIX YANG SUDAH DI-IMPLEMENT (28 Mei Siang)

### Fix 1: HTF Regime 4H → 1H (REDESIGN)
**File:** `engine/scoring_engine.py` — `_fetch_1h_regime()`

| Aspek | Sebelum (4H) | Sesudah (1H) |
|-------|-------------|-------------|
| Timeframe | 4H candles (20 candles = 3.3 hari) | 1H candles (24 candles = 24 jam) |
| EMA | EMA10 vs EMA20 | EMA8 vs EMA21 (lebih responsif) |
| EMA gap | 0.2% (×1.002) | 0.1% (×1.001) |
| Strength threshold | 0.30 | **0.15** |
| Lookback | 10 candle × 4H = 40 jam | 8 candle × 1H = 8 jam |
| Cache | 4 jam | **15 menit** |
| Expected CHOPPY rate | 98% (useless) | ~40-60% (discriminating) |

**Expected impact:**
- Trending detected → threshold -3 (lebih mudah entry aligned) + 2 direction votes
- Choppy detected → OB dikurangi + threshold +3 (lebih ketat)
- Counter-trend → threshold +8 (block bad trades)

### Fix 2: OB ×0.6 Pakai htf_regime (RELIABLE)
**File:** `engine/scoring_engine.py` — OB scoring section

| Sebelum | Sesudah |
|---------|---------|
| `if abs(trend_pct) < 0.035` | `if htf_regime == "CHOPPY"` |
| Flip-flop tiap detik | Stable 15 menit (cache) |
| 30/89 signal ter-reduce | **100% signal di choppy ter-reduce** |

**Expected impact:** OB=18 TIDAK MUNGKIN muncul di choppy. Max OB = 10 di choppy.

### Fix 3: AI Dipanggil Setiap Signal (POST-FILTER)
**File:** `engine/scoring_engine.py` + `intelligence/ai_analyst.py`

| Aspek | Sebelum | Sesudah |
|-------|---------|---------|
| Kapan dipanggil | Sebelum threshold, hanya borderline | **Setelah SEMUA filter lolos** |
| Coverage | 3% signal | **~100% signal yang jadi trade** |
| Cache | 5 menit | **60 detik** |
| Timeout | 10 detik | **4 detik** |
| Daily limit | 200 calls | **500 calls** |
| Bisa veto? | Tidak | **Ya** — penalty bikin score < threshold → cancel |

**Expected impact:** AI evaluate setiap trade. Bisa boost (+8) atau veto (-5 → cancel).

### Fix 4: Pattern Memory Key Mismatch (BUG FIX)
**File:** `engine/scoring_engine.py` + `execution/paper_executor.py`

| Aspek | Sebelum (BUG) | Sesudah (FIXED) |
|-------|---------------|-----------------|
| evaluate() key | `AR_long_ranging` | `AR_long_ranging` |
| record_outcome() key | `AR_long_scalper` ❌ | `AR_long_ranging` ✅ |
| Pattern memory trigger | TIDAK PERNAH | **Setelah 3 trades** |

**Expected impact:**
- 3 loss berturut → WR 0% → penalty -20 → asset BLOCKED
- Recovery: 3-4 wins → EMA WR naik → penalty hilang
- Repeat losers (AR 4×, XPL 4×, LIT 4×) tidak akan terjadi lagi

---

## 📊 DATA TEMUAN TAMBAHAN

### Repeat Losers (Pattern Memory Seharusnya Block)
| Asset | Trades | Wins | PnL | Seharusnya |
|-------|--------|------|-----|------------|
| AR | 4 | 0 | -$3.00 | Blocked setelah trade ke-3 |
| XPL | 4 | 0 | -$2.54 | Blocked setelah trade ke-3 |
| LIT | 4 | 0 | -$1.95 | Blocked setelah trade ke-3 |
| **Total saved** | | | **~$4.50** | |

### Hourly Performance
| Jam UTC | Trades | WR | PnL | Notes |
|---------|--------|-----|-----|-------|
| 18:00 | 6 | 0% | -$3.75 | Death zone (post-deploy cold start) |
| 21:00 | 3 | 67% | +$0.91 | Best hour |
| 22:00 | 9 | 33% | -$2.50 | Overtrading |
| 00:00-01:00 | 13 | 15% | -$4.47 | Asia session death |

### Winners vs Losers
| | Winners (12) | Losers (33) |
|--|--|--|
| Avg score | 57.2 | 59.7 |
| Main exit | trailing_stop (7/12) | time_exit (22/33) |
| Insight | Score TIDAK prediktif — losers punya score lebih tinggi! |

### Momentum Death Performance
- Fire rate: 20% ✅ (target 10-25%)
- Avg loss: -$0.056 ✅ (target < $0.10)
- Max loss: -$0.16 ✅ (minimal)
- **Verdict: WORKING AS DESIGNED** — cut flat trades early

### AI Performance
- Coverage: 5/161 signal (3.1%) — **INSUFFICIENT**
- All same signal: AR LONG score=58, conf=0.52 → +4pts
- Result: LOSS -$1.44 (stop_loss)
- **Verdict: Cannot evaluate — sample too small. Fix 3 will increase coverage.**

---

## 🎯 EXPECTED RESULTS AFTER FIX (Audit #15)

| Metric | Post-Deploy (broken) | Expected After Fix |
|--------|---------------------|-------------------|
| HTF CHOPPY rate | 98% | ~40-60% |
| OB=18 in choppy | 59 signals | **0 signals** |
| Pattern memory trigger | NEVER | After 3 losses |
| AI coverage | 3% | ~100% |
| Frequency | 4.51/hr | 2.0-3.0/hr |
| WR | 26.7% | >40% |
| PF | 0.368 | >1.0 |
| Trailing fire | 15.6% | >25% |

---

## 🚨 Red Flags untuk Audit #15

| Kondisi | Action |
|---|---|
| PF masih < 0.8 | Revert semua fix hari ini, kembali ke Audit #13 state |
| Frequency < 1.0/hr | 1H regime terlalu ketat → naikkan strength 0.15 → 0.20 |
| AI veto > 30% signals | AI terlalu agresif → kurangi penalty dari -5 ke -3 |
| Pattern memory block > 40% signals | Threshold n=3 terlalu kecil → naikkan ke n=5 |
| 0 TRENDING detected dalam 24 jam | EMA gap 0.1% masih terlalu ketat → 0.05% |
| Trailing masih < 20% | Entry quality masih jelek → investigate further |

---

## 🔧 Tuning Matrix (Post-Fix)

| Kondisi | Action |
|---|---|
| 1H regime 80%+ CHOPPY | Strength 0.15 → 0.12 (more sensitive) |
| 1H regime 80%+ TRENDING | Strength 0.15 → 0.20 (too sensitive) |
| OB correlation masih < 0 | Zero OB di choppy (bukan ×0.6) |
| AI confidence selalu > 0.7 | Prompt terlalu optimistic → review |
| AI confidence selalu < 0.3 | Prompt terlalu pessimistic → review |
| Pattern memory false positive | Naikkan n threshold dari 3 → 5 |

---

## Timeline

| Waktu (WIB) | Action |
|---|---|
| 28 Mei 12:00 | Diagnosis + implement 4 fix |
| 28 Mei sore | Deploy fix ke Railway |
| 28 Mei 18:00 - 29 Mei 18:00 | Collect trades (~24 jam) |
| **29 Mei 18:00** | **Audit #15** |

---

## Files Modified (28 Mei)

| File | Changes |
|------|---------|
| `engine/scoring_engine.py` | 1H regime, OB htf_regime, AI post-filter, pattern memory key |
| `intelligence/ai_analyst.py` | Cache 60s, timeout 4s, daily limit 500 |
| `execution/paper_executor.py` | `_learn_regime` from signal.regime.value |
| `notify/telegram.py` | "1H Regime" label |
| `models/schemas.py` | Comment update |
| `dashboard/reasoning_logger.py` | Comment update |

---

## Catatan

### Kenapa 4 Fix Sekaligus (Lagi)
Deadline 1 Juni = 3 hari. Semua fix address ROOT CAUSES yang teridentifikasi dari data:
- Fix 1+2 = **HTF regime + OB** (saling terkait, harus bareng)
- Fix 3 = **AI coverage** (independent, additive)
- Fix 4 = **Pattern memory bug** (independent, critical safety net)

### Bisect Order (kalau ada regresi)
1. **Revert Fix 4** (pattern memory) — mungkin terlalu agresif block
2. **Revert Fix 3** (AI post-filter) — AI veto mungkin terlalu banyak cancel
3. **Revert Fix 1** (1H regime) — kalau frequency collapse
4. **Fix 2 JANGAN revert** — OB di choppy = proven inverse, harus dikurangi
