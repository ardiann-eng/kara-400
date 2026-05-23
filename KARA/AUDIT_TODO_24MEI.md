# KARA — TODO Audit 24 Mei 2026

## ✅ DONE (23 Mei Malam — Audit #9 + Fundamental Overhaul)

### Audit #9 Completed
- Data: 134 trades (single user), 32.8 jam, 4.1 trades/hr
- WR 44.8%, PnL +$1.70, PF 1.027
- Score↔PnL r = -0.11 (MASIH INVERSE)
- Frequency BUKAN masalah (conversion 100%, 98 trades/day)
- Masalah = PnL flat karena time_exit 62% drain -$42.89

### Root Cause Diagnosis
- **Fundamental:** Bot masuk SETELAH pump (lagging indicators), bukan saat pump DIMULAI
- High score = high OI/Funding = high realized_vol (0.084 vs 0.057)
- time_exit loss: score 70+ = -7.9%, score 50-59 = -1.2%
- Trailing fire rate SAMA di semua score bucket (32-34%) → score tidak predict edge
- CVD fire 81% (constant bias, CVD=0 WR 52% vs CVD=10 WR 43%)
- EMA fire 77% (1m timeframe = noise, EMA=10 WR 39.6% vs EMA≤0 WR 65%)
- OB 10+ = satu-satunya predictor (WR 50.6%, trailing 34%)
- OB=18 = best bucket (WR 55.9%, trailing 38%, PnL +$7.02)
- **Combo OB≥10 + CVD=0 = WR 80%** (10 trades, +$6.60)

### 5 Fix Deployed (23 Mei Malam)

| # | Fix | File | Alasan |
|---|---|---|---|
| 1 | OI/Funding cap ±35 → ±8 | `oi_funding_analyzer.py` | OI inflate score tanpa improve trailing rate |
| 2 | HTF CHOPPY penalty +8 → 0 | `scoring_engine.py` | Detector return CHOPPY 91.9% = broken |
| 3 | CVD threshold 25% → 40%, price 0.1% → 0.2% | `scoring_engine.py` | Fire 81% = constant bias, bukan signal |
| 4 | EMA gap 0.03% → 0.1% | `scoring_engine.py` | 1m timeframe cross terlalu sering = noise |
| 5 | **★ PUMP TIMING GATE** | `scoring_engine.py` | Fundamental fix: hanya entry saat pump BARU MULAI |

### Pump Timing Gate — Detail

```
Entry HANYA kalau:
- vol_surge ≥ 1.5× median baseline (LONG) / 2.0× (SHORT)
- price_accel ≥ 1.2× avg candle size
- total_move < 0.7% (belum terlambat)
- 3/5 candle terakhir sesuai arah trade
- avg_candle > 0.04% (coin tidak mati)
```

**Filosofi baru:** Dari "score tinggi = masuk" ke "pump sedang dimulai + score decent = masuk"

---

## 🔲 TODO — Audit 24 Mei (Audit #10)

### Deploy & Collect
- [ ] Commit + push 5 fix ke Railway
- [ ] Tunggu 8-12 jam, kumpulkan 20+ trades post-fix
- [ ] Pull data dengan runbook Step 1

### Tier 1 — Apakah Pump Gate Bekerja?
- [ ] **time_exit %** — target < 40% (dari 62%). INI METRIC UTAMA.
- [ ] **trailing_stop fire rate** — target ≥ 45% (dari 33%). Pump gate = entry saat harga gerak.
- [ ] Berapa trade di-block oleh `pump_not_starting`? (target: 40-60% of scans)
- [ ] PnL per trade — target avg > +$0.20 (dari +$0.01)

### Tier 2 — Scoring Cleanup
- [ ] Score↔PnL r — target ≥ 0 (netral). OI cap + CVD/EMA fix harusnya hilangkan inverse.
- [ ] Score distribution — harusnya cluster 40-60 (OI capped, CVD/EMA jarang fire)
- [ ] OI component mean ≤ 8 di semua signal (verify cap bekerja)
- [ ] CVD fire rate — target 30-50% (dari 81%)
- [ ] EMA fire rate — target 25-40% (dari 77%)

### Tier 3 — Performance
- [ ] PF — target > 1.5 (dari 1.027)
- [ ] Frequency — expect 2-4/hr (turun dari 4.1, tapi higher quality)
- [ ] SHORT performance — vol_surge 2.0× harusnya filter bad shorts
- [ ] Avg time_exit loss — harusnya turun (fewer trades di market diam)

### 🚨 Red Flags (Pause Trading / Rollback)
- trailing_stop rate < 20% → pump gate terlalu ketat, turunkan vol_surge ke 1.2×
- Frequency < 1/hr → gate terlalu ketat, turunkan semua threshold
- PF < 0.7 → regresi total, rollback semua fix
- 0 trades dalam 4 jam → gate broken, emergency disable

### 🔧 Tuning Kalau Terlalu Ketat
```
vol_surge: 1.5 → 1.3 (LONG), 2.0 → 1.5 (SHORT)
price_accel: 1.2 → 1.0 (disable)
max_move: 0.7% → 1.0%
direction: 3/5 → 2/5
```

### 🔧 Tuning Kalau Terlalu Longgar
```
vol_surge: 1.5 → 2.0 (LONG), 2.0 → 2.5 (SHORT)
price_accel: 1.2 → 1.5
max_move: 0.7% → 0.5%
```

---

## 📅 Timeline

| Tanggal | Action |
|---|---|
| 23 Mei malam | Deploy 5 fix (OI + CHOPPY + CVD + EMA + Pump Gate) |
| 24 Mei pagi | Monitor logs — pump gate firing? trades masuk? |
| 24 Mei siang | Audit #10 (20+ trades post-fix) |
| 24 Mei malam | Evaluate + tune parameters kalau perlu |
| 25 Mei | Jika PF > 1.3 → mulai live executor dev |
| 26-27 Mei | Live executor ready |
| 28-29 Mei | Micro-live test ($10) |
| 1 Juni | GO/NO-GO |

---

## Referensi

| Metric | Audit #8 | Audit #9 | Target #10 |
|---|---|---|---|
| Trades | 35 | 134 | 20-50 |
| WR | 40% | 44.8% | >50% |
| PnL | +$1.56 | +$1.70 | >+$10 |
| PF | 1.587 | 1.027 | >1.5 |
| Score↔PnL r | -0.18 | -0.11 | ≥0 |
| time_exit % | 57% | 62% | <40% |
| trailing fire | 40% | 33% | >45% |
| Trades/hr | 2.2 | 4.1 | 2-4 |
