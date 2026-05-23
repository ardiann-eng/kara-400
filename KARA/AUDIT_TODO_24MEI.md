# KARA — TODO 23 Mei 2026

## ✅ DONE Hari Ini

### Audit #8 Completed
- Data: 35 trades, 15.7 jam, 2.2 trades/hr
- WR 40%, PnL +$1.56, PF 1.587
- Score↔PnL r = -0.18 (INVERSE) ← root cause ditemukan

### 3 Bug Fix Deployed (engine/scoring_engine.py)

1. **Score Alignment** — `max(bull, bear)` → `aligned_setup` (bull jika LONG, bear jika SHORT)
   - Root cause: score tinggi dari setup BERLAWANAN arah = inverse predictive
   - Evidence: SOL long score=71 tapi OI=-22 (bearish). Score inflated oleh bear_setup.

2. **HTF EMA Fix** — `_ema(closes[-10:], 10)` → `_ema(closes, 10)`
   - Root cause: EMA dihitung dari data terlalu pendek → EMA10≈EMA20 selalu → CHOPPY 91.6%
   - Evidence: HTF CHOPPY 91.6% = detector broken, bukan market selalu choppy

3. **Whale Min Sample** — tambah `_whale_count >= 5` + threshold 30%→50%
   - Root cause: 1 whale trade = 100% imbalance = selalu vote. Fire rate 78%.
   - Evidence: 219/299 signals = "100% imbalance" (sample size 1-2 trades)

---

## 🔲 TODO — Belum Deploy

### Setelah Deploy Fix #1-3, Monitor:

- [ ] Deploy ke Railway (commit + push)
- [ ] Tunggu 6-8 jam, kumpulkan 30+ trades
- [ ] Audit #9: cek apakah score↔PnL r membaik (target ≥ 0)
- [ ] Cek HTF regime distribution (target: CHOPPY < 70%)
- [ ] Cek whale fire rate (target: < 40%)
- [ ] Cek trades/hr (target: ≥ 4)

### Jika Audit #9 Masih Bermasalah:

- [ ] **OI weight reduction** — ±28 → ±12 (OI terlalu dominan untuk 12-min hold)
  - Evidence: OI high (16+) = WR 30%, PnL -$3.40. OI zero = WR 60%, PnL +$4.35
  - OI beroperasi di siklus 8h, bot hold 12 menit = timeframe mismatch
- [ ] **OB weight increase** — ±18 → ±22 (satu-satunya komponen predictive)
  - Evidence: OB aligned = WR 44%, PnL +$5.18. OB against = WR 0%, PnL -$2.73

---

## 📊 Key Findings Audit #8

| Finding | Data | Status |
|---|---|---|
| Score INVERSE | r=-0.18, decile 9 = 0% WR | ✅ Fixed (aligned_setup) |
| HTF always CHOPPY | 91.6% CHOPPY | ✅ Fixed (EMA calc) |
| Whale always fires | 78% fire rate | ✅ Fixed (min sample + threshold) |
| OI contrarian vs trend-following | OI aligned SHORT = WR 27% | ⚠️ Monitor post-fix |
| OB = best predictor | OB aligned = WR 44%, +$5.18 | 📝 Noted for next iteration |
| time_exit 0% WR | 20/35 trades, -$24.30 | Should improve with better scoring |
| Trade clustering | 26% within 5min, then 3h gaps | Should improve with HTF fix |

---

## 📅 Timeline Update

| Tanggal | Action |
|---|---|
| 23 Mei (sekarang) | Deploy Fix #1-3 |
| 24 Mei | Audit #9 (30+ trades post-fix) |
| 25 Mei | Evaluate. Jika OK → mulai live executor |
| 26-27 Mei | Live executor dev |
| 28-29 Mei | Micro-live test ($10) |
| 30-31 Mei | Validation |
| 1 Juni | GO/NO-GO |
