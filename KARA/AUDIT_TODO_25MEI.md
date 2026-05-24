# KARA — Audit #12 (25 Mei 2026, 23:00 WIB / 16:00 UTC)

## Context

Deploy malam ini (24 Mei ~23:45 WIB / 16:45 UTC) berisi 5 perubahan:

| # | Fix | Expected Impact |
|---|---|---|
| 1 | EMA 8/21 → **13/34**, gap 0.04% | Fire rate 93% → ~50%. Cross hanya pada real trend. |
| 2 | DVI **disabled** → diganti **MFI** | Remove noise inverse, ganti volume-weighted confirmation |
| 3 | Regime threshold 6%/12% → **10%/18%** | Mayoritas altcoin jadi NORMAL (×1.0) → frequency naik |
| 4 | **Liq Cluster** (baru) | Real Binance+HL cascade events → replace OI proxy |
| 5 | **MFI (Money Flow Index)** ±8 pts | Confirmation: money flowing in/out, 14-bar lookback |

---

## Pre-Audit (25 Mei siang)

- [ ] Confirm deploy sukses (cek Railway logs: "Connected", no crash)
- [ ] Tunggu ~20 jam, kumpulkan 30+ trades
- [ ] Pull data dengan runbook Step 1

---

## Tier 1 — Apakah Fix Bekerja?

### Frequency (PALING PENTING)
- [ ] **Trades/hr** — target **2.5-4/hr** (dari 1.6/hr di Audit #11). Regime fix = main driver.
- [ ] Kalau masih <2/hr → regime fix belum efektif (vol_cache belum rotate? cek coin regime labels)
- [ ] Kalau >6/hr → scoring terlalu longgar, naikkan threshold +5

### EMA 13/34
- [ ] **EMA fire rate** — target **40-60%** (dari 93% di Audit #11)
- [ ] Kalau >70% → gap 0.04% masih terlalu kecil, naikkan ke 0.06%
- [ ] Kalau <20% → period 13/34 terlalu ketat, turunkan ke 10/26 ATAU naikkan window fetch

### Regime
- [ ] **Regime distribution** — target majority **NORMAL** (bukan 100% volatile/CHOPPY)
- [ ] Cek per-coin: realized_vol vs threshold. Coin <10% vol seharusnya NORMAL.
- [ ] Kalau masih semua volatile → cek apakah vol_cache SQLite belum expire

### Liq Cluster
- [ ] **Liq cluster fire rate** — target **10-25%**
- [ ] Kalau 0% → events terlalu sparse untuk altcoin, atau Binance stream disconnect
- [ ] Cek di signal reasons: apakah ada "💥 Liq cluster:" message?
- [ ] Kalau >40% → threshold $2k terlalu rendah, naikkan ke $5k

### MFI (Money Flow Index)
- [ ] **MFI fire rate** — target **30-60%** (>60 bullish atau <40 bearish)
- [ ] Kalau >80% → threshold 60/40 terlalu longgar, ketatkan ke 65/35
- [ ] Kalau <20% → threshold 60/40 terlalu ketat untuk 1m, longgarkan ke 55/45
- [ ] Cek di signal reasons: apakah ada "💰 MFI" message?

---

## Tier 2 — Quality Tetap Terjaga?

- [ ] **trailing_stop rate** — target tetap **>40%** (jangan turun dari 47%)
- [ ] **time_exit %** — target **<50%**
- [ ] **Score↔PnL r** — target **≥ 0** (sudah -0.012, harusnya improve tanpa DVI noise)
- [ ] **WR** — target >45%
- [ ] **PF** — target **>1.3** (3 audit berturut = live-ready criteria #2)
- [ ] **LONG vs SHORT** — SHORT masih 0% WR? Kalau ya, pertimbangkan disable SHORT

---

## Tier 3 — Correlation Per-Komponen

- [ ] OB correlation — should stay r > +0.2
- [ ] RSI correlation — should stay r > +0.3
- [ ] EMA correlation — **TARGET ≥ 0** (was -0.33 saat over-fire)
- [ ] FUND correlation — should stay netral (±0.1)
- [ ] **Liq Cluster correlation** — target ≥ 0 (baru, any data = good)
- [ ] **MFI correlation** — target ≥ 0 (baru, should be positive if money flow = predictive)

---

## 🚨 Red Flags (Rollback)

| Kondisi | Action |
|---|---|
| PF < 0.8 | Rollback semua 5 fix |
| Trailing < 25% | Rollback — edge hilang |
| Frequency > 8/hr + PF < 1.0 | Regime terlalu longgar → revert ke 8%/15% |
| Score inverse WORSE (r < -0.15) | Ada komponen baru yang rusak |
| 0 trades dalam 4+ jam | Scoring collapse — cek threshold, EMA, regime |
| Liq cluster fire >50% + PnL negatif | Cluster picking wrong side, threshold too low |
| MFI fire >80% + r < -0.1 | MFI jadi noise seperti DVI, disable |

---

## 🔧 Tuning Matrix

| Kondisi | Action |
|---|---|
| EMA fire >70% | Gap 0.04% → 0.06% |
| EMA fire <20% | Gap 0.04% → 0.02% ATAU period 13/34 → 10/26 |
| Liq cluster fire 0% | Monitor 24h lagi, altcoin sparse. Bukan bug. |
| Liq cluster fire >40% | Notional threshold $2k → $5k |
| MFI fire >80% | Threshold 60/40 → 65/35 |
| MFI fire <20% | Threshold 60/40 → 55/45 |
| Regime still all volatile | Vol window terlalu panjang? Cek 1h candle count. |
| SHORT masih 0% WR (20+ trades total) | DISABLE SHORT |
| Frequency <1.5/hr + regime NORMAL | Score threshold terlalu tinggi, turunkan base -3 |

---

## Decision Points (25 Mei)

| Kondisi | Keputusan |
|---|---|
| PF > 1.3 + trailing > 40% + frequency > 2/hr | ✅ **START LIVE EXECUTOR DEV** (26-27 Mei) |
| PF 1.0-1.3 + trailing > 40% | ⚠️ Collect more data, audit lagi 26 Mei |
| PF < 1.0 | ❌ Diagnose regresi, bisect mana fix yang salah |
| SHORT total rugi > $5 | Disable SHORT, LONG-only mode |

---

## Timeline

| Waktu (WIB) | Action |
|---|---|
| 24 Mei 23:45 | Push + deploy 4 fix |
| 25 Mei 00:00-23:00 | Collect trades (~23 jam) |
| **25 Mei 23:00** | **Audit #12** |
| 26 Mei | If pass → live executor dev. If fail → diagnose. |

---

## Referensi (Audit #11 Baseline)

| Metric | Audit #11 (post-deploy) | Target #12 |
|---|---|---|
| Trades | 15 | 30-50 |
| Trades/hr | 1.6 | 2.5-4 |
| WR | 46.7% | >45% |
| PnL | +$3.98 | >+$5 |
| PF | 1.697 | >1.3 |
| Score↔PnL r | -0.012 | ≥0 |
| trailing fire | 47% | >40% |
| time_exit | 53% | <50% |
| EMA fire | 93% | 40-60% |
| DVI fire | 60% (disabled) | N/A |
| MFI fire | N/A (baru) | 30-60% |
| Regime | 100% volatile | majority NORMAL |
| Liq cluster | N/A (baru) | 10-25% |
| LONG WR | 60% | >50% |
| SHORT WR | 0% (5t) | >25% atau disable |

---

## Catatan

### Kenapa Deploy 5 Fix Sekaligus (Lagi)
Deadline 1 Juni = 7 hari. Trade-off sama: speed > attribution. Tapi kali ini lebih aman karena:
- Fix 1,3 = **parameter tuning** (bukan logic change) → mudah revert
- Fix 2,5 = **swap** (DVI off, MFI on) → slot confirmation tetap 1, bukan nambah
- Fix 4 = **additive** (liq cluster) dengan fallback ke proxy lama → worst case = no change, bukan regresi
- Kalau ada regresi, bisect: disable MFI dulu (paling baru), lalu liq cluster, lalu cek EMA, lalu regime

### Vol Cache
Regime fix baru efektif setelah vol_cache expire (max 1 jam). Expect jam pertama post-deploy masih pakai data lama.

### SHORT Decision
Total data SHORT: 12+ trades, 0% trailing fire, selalu time_exit loss. Kalau 25 Mei masih sama → hard disable. Crypto short-term = upward bias, scalper 12-min hold terlalu singkat untuk SHORT yang butuh momentum turun lebih lama.
