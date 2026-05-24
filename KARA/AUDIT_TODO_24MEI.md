# KARA — Audit 24 Mei 2026

## ✅ DONE — Audit #10 (24 Mei Pagi, 04:42 UTC)

### Data
- 19 trades (single user), 8.4 jam (23 Mei 18:57 → 24 Mei 03:23 UTC), 2.3/hr

### Results

| Metric | Audit #9 | Audit #10 | Target | Status |
|---|---|---|---|---|
| WR | 44.8% | **52.6%** | >50% | ✅ |
| PnL | +$1.70 | **+$3.92** | >+$10 | ↗️ |
| PF | 1.027 | **1.430** | >1.5 | ⚠️ Mendekati |
| time_exit | 62% | **42.1%** | <40% | ⚠️ Hampir |
| trailing | 33% | **47.4%** | >45% | ✅ |
| Score↔PnL r | -0.11 | **-0.177** | ≥0 | ❌ Masih inverse |
| Trades/hr | 4.1 | **2.3** | 2-4 | ✅ |

### Root Cause Analysis: Score Inverse

Per-component correlation (n=19 matched trades):

| Komponen | r | Fire% | Verdict |
|---|---|---|---|
| EMA | +0.226 | 9% positif | ✅ Tapi hampir mati (gap 0.1% terlalu ketat) |
| OB | +0.205 | 40% | ✅ Best predictor |
| RSI | +0.191 | 79% | ✅ Working correctly |
| FUND | +0.145 | 68% | ✅ Neutral-positive |
| XAM | -0.019 | 11% | Neutral |
| **CVD** | **-0.210** | **74%** | ❌ **INVERSE = root cause** |
| LIQ | -0.350 | 11% | ⚠️ n=2 (monitor) |

**CVD detail:** CVD>0 = 14t WR 36% PnL +$0.46. CVD=0 = 5t **WR 100%** PnL +$3.46.

### Root Cause: Bot Berhenti Trade (05:00+ UTC)

Dari Railway logs — **0 signals** dalam 37+ menit. Semua coin di-skip `score_below_threshold`.

**Tiga faktor stack:**
1. EMA gap 0.1% = **0% fire** pada 1m candle (impossible threshold)
2. CVD threshold 40% = masih 74% fire tapi score sudah collapse tanpa EMA
3. Regime threshold 4% = **SEMUA altcoin** di-label "volatile" → permanent ×0.9

Score max reachable: OB(18) + FUND(8) + RSI(5) = 31 × 0.9 = 28. Threshold = 50-58. **GAP 20+ pts.**

---

## ✅ DONE — 6 Fix (24 Mei Siang)

| # | Fix | File | Root Cause | Expected Impact |
|---|---|---|---|---|
| 1 | **CVD DISABLED** | `scoring_engine.py` | r=-0.21 inverse, fire 74% = lagging noise | Remove -10 pts false signal |
| 2 | **DVI ENABLED** (side 'A' fixed) | `scoring_engine.py` | 'A' not in sell list → 0% fire | +5-10 pts confirmation (leading 2min) |
| 3 | **DVI threshold 60%→45%** | `scoring_engine.py` | 60% too strict | Fire rate 20-40% |
| 4 | **EMA gap 0.1%→0.06%** | `scoring_engine.py` | 0.1% impossible on 1m | EMA fire ~40% (restore +10 pts) |
| 5 | **Regime threshold 4%→6% / 8%→12%** | `scoring_engine.py` | All altcoins = "volatile" permanent | ×0.9 → ×1.0 for most coins |
| 6 | **Binance liq stream** | `ws_client.py` + `main.py` | HL liq sparse (8% fire) | LIQ fire rate ↑, real cascade detection |
| 7 | **Grace period high-conviction** | `risk_manager.py` | AR sc=80 direction benar tapi dump delayed | time_exit +10min kalau score≥70 + loss<0.3% |

### Verified Before Push
- [x] DVI side matching: HL sends 'A' (sell), 'B' (buy) — confirmed live from Railway
- [x] DVI trade frequency: 720+ trades/2min per altcoin — min 15 PASTI terpenuhi
- [x] DVI dollar minimum: $100 trivially met (720 trades × avg $40+)
- [x] Binance WS: **CONNECTED** from Railway (tested, REST=403 but WS=OK)
- [x] Time format: HL `time` = milliseconds = matches DVI `time.time()*1000`
- [x] Syntax check: ALL files pass `py_compile`

---

## 🔲 TODO — Audit #11 (24 Mei 23:00 WIB / 16:00 UTC)

### Pre-Audit
- [ ] Push + deploy 6 fix
- [ ] Tunggu ~10 jam (13:00→23:00 WIB), kumpulkan 20+ trades
- [ ] Pull data dengan runbook Step 1

### Tier 1 — Apakah Scoring Collapse Fixed?
- [ ] **Trades/hr** — target 2-4/hr (dari 0/hr saat score collapse). INI METRIC UTAMA.
- [ ] **DVI fire rate** — target 20-40%. Kalau 0% = masih bug.
- [ ] **EMA fire rate** — target 30-50% (dari 0% di logs tadi). 0.06% harusnya fix.
- [ ] **Regime distribution** — target majority "ranging/normal" (bukan semua "volatile")
- [ ] Score distribution — harusnya higher, cluster 45-65

### Tier 2 — Quality Maintained?
- [ ] **time_exit %** — target tetap <45% (pump gate still working)
- [ ] **trailing_stop rate** — target tetap >40%
- [ ] **Score↔PnL r** — target ≥ 0 (CVD inverse removed)
- [ ] WR — target >50%
- [ ] PF — target >1.3
- [ ] **Grace period**: berapa trade score≥70 yang dapat grace? Apakah ada yang convert ke trailing?

### Tier 3 — New Components
- [ ] DVI correlation vs PnL — should be ≥ 0 (neutral or positive)
- [ ] Binance liq events in cache? (check log for "[BinanceLiq]" or "[WS] Liquidation event")
- [ ] LIQ fire rate — target >15% (with Binance data)
- [ ] LIQ correlation — should improve from r=-0.35

### 🚨 Red Flags (Rollback)
- DVI fire >70% → threshold 45% terlalu rendah, naikkan ke 55%
- DVI fire 0% → masih bug (check cache, window, time format)
- Frequency < 1/hr → regime fix belum invalidate cache (tunggu 1 jam)
- Frequency > 6/hr → scoring terlalu longgar (regime penalty gone + EMA always fire)
- PF < 0.8 → regresi, rollback
- Score inverse WORSE (r < -0.2) → DVI also inverse, disable

### 🔧 Tuning Matrix

| Kondisi | Action |
|---|---|
| DVI fire >70% | Threshold 45% → 55% |
| DVI fire 0% | Check logs, mungkin cache cold (tunggu 5 min warm-up) |
| EMA fire >70% | Gap 0.06% → 0.08% |
| EMA fire <10% | Gap 0.06% → 0.04% |
| Regime still all "volatile" | Vol cache belum expire — tunggu 1 jam post-deploy |
| Trades >6/hr but PF <1.0 | Score threshold terlalu rendah, naikkan base +5 |

---

## 📅 Timeline (Updated)

| Waktu (WIB) | Action |
|---|---|
| 24 Mei 13:00 | Push + deploy 6 fix |
| 24 Mei 13:00-23:00 | Collect trades (expect 20-40 trades in 10 hrs) |
| **24 Mei 23:00** | **Audit #11** — validate fixes |
| 25 Mei | Jika PF > 1.3 + trailing >40% → mulai live executor dev |
| 26-27 Mei | Live executor ready |
| 28-29 Mei | Micro-live test ($10) |
| 1 Juni | GO/NO-GO |

---

## Referensi

| Metric | Audit #8 | Audit #9 | Audit #10 | Target #11 |
|---|---|---|---|---|
| Trades | 35 | 134 | 19 | 20-40 |
| WR | 40% | 44.8% | 52.6% | >50% |
| PnL | +$1.56 | +$1.70 | +$3.92 | >+$5 |
| PF | 1.587 | 1.027 | 1.430 | >1.3 |
| Score↔PnL r | -0.18 | -0.11 | -0.177 | ≥0 |
| time_exit % | 57% | 62% | 42% | <45% |
| trailing fire | 40% | 33% | 47% | >40% |
| Trades/hr | 2.2 | 4.1 | 2.3 | 2-4 |
| EMA fire | — | 77% | ~9% | 30-50% |
| CVD/DVI fire | — | 81% | 74%/0% | DVI 20-40% |

---

## Catatan Penting

### Kenapa Deploy 6 Fix Sekaligus (Bukan 1 per 1)
Deadline 1 Juni = 7 hari. Tidak ada waktu untuk 1-variabel-per-deploy. Trade-off: kalau ada regresi, harus bisect mana fix yang salah. Tapi 4 dari 6 fix ini address ROOT CAUSE yang teridentifikasi (bukan parameter tweak), jadi confidence tinggi.

### Vol Cache Invalidation
Regime fix (threshold 4%→6%) baru efektif setelah vol_cache SQLite expire (max 1 jam). Expect 1 jam pertama post-deploy masih pakai regime lama. Monitor setelah 14:00 WIB.

### Temuan Yang BELUM Di-fix (Monitor)

| Finding | Data | Action Kalau Persist |
|---|---|---|
| SHORT net negative | 7t, PnL -$2.01, trailing 2/7 | Disable SHORT kalau -$5+ setelah 30 trades |
| Asia session weak | Dari #9: 17t WR 29% | Block Asia kalau masih negatif post-fix |
| LIQ r=-0.35 | n=2, meaningless | Monitor with Binance data |
| XAM barely fires (0.8%) | Threshold terlalu ketat | Low priority, ±12 pts tapi 0 impact |
