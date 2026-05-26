# KARA — Audit #13 (27 Mei 2026, 23:00 WIB / 16:00 UTC)

## Context

Deploy 26 Mei malam berisi **6 fix** dari Audit #12 findings:

| # | Fix | Root Cause | Expected Impact |
|---|-----|-----------|-----------------|
| 1 | EMA freshness 8/21 → 13/34 | Mismatch period: freshness check pakai 8/21 tapi cross detection pakai 13/34 → selalu "stale" -5 | Score +9 to +15 per signal → frequency naik |
| 2 | Liq OI proxy disabled | Binance forceOrder geo-blocked (0 events), proxy selalu fire + kontradiksi direction (8 trades, 7 LOSS) | Hilangkan -$7.37 drag |
| 3 | MFI bearish disabled | 1m altcoin MFI structural bias (selalu <40), enable bad SHORTs (15 trades, WR 40%, -$5.67) | Hilangkan SHORT enabler |
| 4 | XAM window 2min→5min + threshold 0.15%→0.10% | BTC jarang move 0.15%/2min di low-vol → XAM 0% fire | XAM mulai contribute |
| 5a | Block SHORT kalau OB bullish | OB excluded dari direction voting → bot SHORT into bid wall support (3 trades, ALL LOSS) | Prevent bad SHORTs |
| 5b | SHORT TP1 ×0.70 + hold +3min | SHORT avg favorable move 0.39% vs TP1 0.85% = unreachable. Winners hold 13.2min > 12min limit | More trailing fires for SHORT |

---

## Pre-Audit (27 Mei siang)

- [ ] Confirm deploy sukses (Railway logs: "Connected", no crash)
- [ ] Tunggu ~20 jam, kumpulkan 40+ trades
- [ ] Pull data dengan runbook Step 1

---

## Tier 1 — Apakah 6 Fix Bekerja?

### Frequency (FIX #1 = main driver)
- [ ] **Trades/hr** — target **2.5-4/hr** (dari 0.84/hr di Audit #12)
- [ ] Kalau masih <1.5/hr → EMA fix belum cukup, turunkan base threshold 45→40
- [ ] Kalau >8/hr → terlalu longgar, naikkan threshold +5

### EMA Score (FIX #1)
- [ ] **EMA contribution** — target: mix of +10 (fresh), +4 (medium), 0 (neutral)
- [ ] Cek di SCORE-DEBUG logs: EMA masih selalu -5? Kalau ya → fix belum deploy
- [ ] Kalau EMA selalu +10 (100% fresh) → period 13/34 terlalu lambat cross, semua "fresh"
- [ ] Target: EMA fire rate 40-60% dengan mix positif/netral

### Liq Cluster (FIX #2)
- [ ] **Liq cluster fire rate** — target **0%** (karena Binance data kosong, proxy disabled)
- [ ] Kalau masih >0% → fix belum deploy atau ada path lain yang fire liq
- [ ] Cek signal reasons: TIDAK boleh ada "💥 Liq cascade potential" lagi

### MFI (FIX #3)
- [ ] **MFI fire rate** — target **hanya LONG trades** yang punya MFI
- [ ] SHORT trades TIDAK boleh punya "💰 MFI" di reasons
- [ ] LONG + MFI bullish: target WR >50% (was 50% di Audit #12)

### XAM (FIX #4)
- [ ] **XAM fire rate** — target **>0%** (was 0% di Audit #12)
- [ ] Cek signal reasons: ada "🔗 BTC/ETH leading" message?
- [ ] Kalau masih 0% → BTC masih sideways, bukan bug. Monitor.
- [ ] Kalau >30% → threshold 0.10% terlalu rendah, naikkan ke 0.12%

### SHORT OB Block (FIX #5a)
- [ ] **SHORT dengan OB bullish** — target **0 trades** (semua di-block)
- [ ] Cek skip counters: `ob_bullish_contradiction` harus >0
- [ ] Kalau masih ada SHORT + OB positif → fix belum deploy

### SHORT Exit Params (FIX #5b)
- [ ] **SHORT trailing fire rate** — target **>35%** (dari 26.7% di Audit #12)
- [ ] **SHORT time_exit rate** — target **<55%** (dari 66.7%)
- [ ] **SHORT avg win** — target **>$1.20** (dari $1.12)

---

## Tier 2 — Quality Metrics

- [ ] **Overall WR** — target >48% (dari 46.4%)
- [ ] **Overall PF** — target **>1.3** (dari 1.076)
- [ ] **Total PnL** — target **>+$5** (dari +$2.59)
- [ ] **Score↔PnL r** — target **≥ 0** (dari -0.18). Fix #2 + #3 harusnya fix inverse.
- [ ] **Trailing stop rate (overall)** — target **>45%** (dari 39.3%)
- [ ] **Time exit rate (overall)** — target **<45%** (dari 55.4%)

### LONG Performance (should maintain/improve)
- [ ] LONG WR — target >55% (was 57.7%)
- [ ] LONG trailing fire — target >50% (was 53.8%)
- [ ] LONG PnL — target >+$10

### SHORT Performance (main focus of fixes)
- [ ] SHORT WR — target **>40%** (dari 36.7%)
- [ ] SHORT trailing fire — target **>35%** (dari 26.7%)
- [ ] SHORT PnL — target **≥ $0** (dari -$9.83)
- [ ] SHORT win/loss ratio — target **>1.1×** (dari 0.96×)

---

## Tier 3 — Component Correlation

- [ ] OB correlation — should stay r > +0.2 (was +0.289)
- [ ] OI/Funding correlation — monitor (was +0.181)
- [ ] EMA correlation — **TARGET ≥ 0** (was always -5 = noise)
- [ ] Whale correlation — should stay positive (was good: 51.4% WR)
- [ ] **Liq correlation** — should be N/A (disabled)
- [ ] **MFI correlation** — LONG only, target ≥ 0
- [ ] **XAM correlation** — any data = good (was 0% fire)

---

## 🚨 Red Flags (Rollback)

| Kondisi | Action |
|---|---|
| PF < 0.8 | Rollback semua 6 fix |
| Trailing < 25% (overall) | EMA fix mungkin bikin entry terlalu early → revert EMA |
| Frequency > 10/hr + PF < 1.0 | EMA fix terlalu longgar → revert ke stale penalty |
| Score inverse WORSE (r < -0.20) | Ada komponen baru yang rusak |
| 0 trades dalam 4+ jam | Scoring collapse — cek threshold, OB block terlalu agresif? |
| SHORT 0% WR (15+ trades) | SHORT exit fix tidak cukup → disable SHORT |
| LONG WR drop <45% | Regresi dari fix — bisect |

---

## 🔧 Tuning Matrix

| Kondisi | Action |
|---|---|
| Frequency <1.5/hr + EMA mostly +10 | Base threshold 45→40 |
| Frequency >8/hr | Base threshold 45→50 |
| EMA selalu +10 (100% fresh) | Freshness window terlalu pendek, extend to 15 candles |
| XAM fire >30% + PnL negatif | Threshold 0.10%→0.15% (revert) |
| SHORT trailing still <30% | TP1 multiplier 0.70→0.55 (even lower target) |
| SHORT OB block >50% of all SHORTs | OB threshold terlalu sensitif, only block OB>+14 |
| LONG WR drop + EMA always +10 | EMA fresh bonus terlalu besar, reduce +10→+6 |

---

## Decision Points (27 Mei)

| Kondisi | Keputusan |
|---|---|
| PF > 1.3 + trailing > 45% + frequency > 2.5/hr | ✅ **START LIVE EXECUTOR DEV** |
| PF 1.0-1.3 + frequency > 2/hr | ⚠️ Collect more data, audit 28 Mei |
| PF < 1.0 | ❌ Bisect: disable fix 4,5b dulu (paling experimental) |
| SHORT PnL still < -$5 (20+ trades) | Disable SHORT, LONG-only |
| Frequency < 1/hr | ❌ EMA fix belum deploy atau threshold masih terlalu tinggi |

---

## Timeline

| Waktu (WIB) | Action |
|---|---|
| 26 Mei malam | Push + deploy 6 fix |
| 27 Mei 00:00-23:00 | Collect trades (~23 jam) |
| **27 Mei 23:00** | **Audit #13** |
| 28 Mei | If pass → live executor dev. If fail → diagnose. |

---

## Referensi (Audit #12 Baseline)

| Metric | Audit #12 | Target #13 |
|---|---|---|
| Trades | 56 (66.6 hrs) | 60-100 |
| Trades/hr | 0.84 | 2.5-4 |
| WR | 46.4% | >48% |
| PnL | +$2.59 | >+$5 |
| PF | 1.076 | >1.3 |
| Score↔PnL r | -0.18 | ≥0 |
| Trailing fire (overall) | 39.3% | >45% |
| Time exit (overall) | 55.4% | <45% |
| EMA contribution | always -5 (stale) | mix +10/+4/0 |
| Liq cluster | 14.3% (12.5% WR) | 0% (disabled) |
| MFI | 33.9% (all sides) | LONG only |
| XAM | 0% | >0% |
| LONG WR | 57.7% | >55% |
| LONG trailing | 53.8% | >50% |
| SHORT WR | 36.7% | >40% |
| SHORT trailing | 26.7% | >35% |
| SHORT PnL | -$9.83 | ≥$0 |

---

## Bisect Order (kalau ada regresi)

Karena 6 fix sekaligus, kalau ada masalah bisect dalam urutan ini:

1. **Revert fix 5b** (SHORT exit params) — paling experimental, bisa bikin trailing fire terlalu early
2. **Revert fix 4** (XAM) — additive, worst case = no change
3. **Revert fix 5a** (OB block SHORT) — bisa terlalu agresif block valid SHORTs
4. **Revert fix 3** (MFI bearish) — unlikely cause regresi tapi cek
5. **Revert fix 2** (Liq proxy) — very unlikely cause regresi
6. **Revert fix 1** (EMA) — LAST resort, ini fix paling impactful

---

## Catatan

### Kenapa 6 Fix Sekaligus
Deadline 1 Juni = 5 hari. Semua fix ini address ROOT CAUSES yang jelas dari data:
- Fix 1-3 = **bug fixes** (code errors, not parameter tuning)
- Fix 4 = **parameter relaxation** (additive, worst case = no change)
- Fix 5a-5b = **data-driven SHORT improvements** (clear evidence)

### Expected Compound Effect
Fix 1 (EMA) + Fix 2 (Liq) + Fix 3 (MFI) together should:
- Naikkan frequency 2-3× (EMA tidak lagi penalty semua signal)
- Fix score inverse (Liq proxy + MFI bearish = 2 komponen yang aktif merusak, sekarang gone)
- Improve SHORT (MFI tidak lagi enable bad SHORTs, Liq tidak lagi kontradiksi)

Fix 5a + 5b specifically target SHORT:
- 5a = prevent entry into support (OB contradiction)
- 5b = make TP1 reachable + give more time for downmove
