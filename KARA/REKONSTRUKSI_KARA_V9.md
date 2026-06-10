# PROPOSAL REKONSTRUKSI TOTAL — KARA v9
**Tanggal:** 6 Juni 2026
**Basis:** Audit #5–#20 (22 Mei – 6 Juni), 16 deploy, ~1.200 trade kumulatif
**Penyusun:** Review sistem (perspektif futures system builder)

> Prinsip dokumen ini: setiap keputusan punya angka. Tidak ada "best practice" tanpa bukti dari data KARA sendiri. Edge lama yang TERBUKTI dipertahankan; sisanya dirombak.

---

## 1. RINGKASAN MASALAH UTAMA (dari audit 22 Mei–6 Juni)

| # | Masalah | Frekuensi/Bukti | Dampak |
|---|---------|-----------------|--------|
| **M1** | **`time_exit` chronic** | 54–65% trade di SEMUA 16 audit. Terbaru: 52/90 (58%), WR 5.8% | **-$55.43** (seluruh kerugian bot). Trailing +$34.92 ditenggelamkan |
| **M2** | **Score tidak prediktif** | 14/16 audit r ≤ +0.10 (range -0.527 s/d +0.162) | Bucket 55-60 = WR 0%, -$32. Score gating tidak punya nilai |
| **M3** | **Counter-trend bias** | #20: 90% LONG (81/90) di market 40 TRENDING_DOWN | LONG WR 24.7% -$29 vs SHORT WR 55.6% +$3.4 |
| **M4** | **Entry tanpa thesis** | Trailing(exit) 100% WR vs entry WR 28% | Edge ada di EXIT, bukan ENTRY. Entry = noise |
| **M5** | **SL/TP tidak vol-aware** | SL konstan ~0.8% di RV 3–11% (#16) | R:R realized 1.71, BE-WR 36.9%, aktual 27.8% → gap -9.1pp |
| **M6** | **Whack-a-mole overfitting** | 16 deploy/15 hari, 3-8 fix/deploy, sample 15-50 | PF berayun 0.37↔2.31, bukan edge — variance |
| **M7** | **Tidak ada correlation control** | 5 posisi altcoin LONG serentak = 1 bet BTC | Risk terkonsentrasi tersembunyi |

**Diagnosis inti:** KARA punya **exit edge nyata** (trailing 100% WR konsisten 16 audit) yang **dibunuh oleh entry tanpa edge**. Semua audit menambal scoring & exit; **akar masalah — entry tidak punya thesis struktural — tidak pernah disentuh.**

---

## 2. REKONSTRUKSI TOTAL — KARA v9

### 2.1 Philosophy & Edge Baru

**Edge lama yang DIPERTAHANKAN (terbukti):**
1. **Trailing stop post-TP1** — 100% WR di 16 audit. Ini jantung sistem. Tidak disentuh.
2. **Momentum death cut** — avg loss -$0.07, memotong trade mati. Pertahankan.
3. **Vol-adjusted sizing** — sudah benar secara konsep.

**Pergeseran filosofi inti:**
> KARA lama: "Skor indikator tinggi → masuk → harap bergerak → exit."
> KARA v9: "Hanya masuk saat displacement SUDAH terbukti + searah HTF + di lokasi likuiditas. Lalu trail."

Edge baru = **displacement continuation**: bot tidak memprediksi gerakan, ia menunggang gerakan yang **sudah dimulai** searah trend besar, dari level di mana likuiditas baru saja bereaksi. Ini menyatukan edge exit lama (trailing) dengan entry yang punya alasan.

**Tiga pilar edge v9:**
- **Pilar A — Trend alignment:** tidak pernah lawan HTF (M3 fix).
- **Pilar B — Displacement proof:** tidak masuk sebelum ada gerakan nyata (M1/M4 fix).
- **Pilar C — Liquidity context:** entry di/sesudah reaksi level (sweep, session H/L) — menggantikan indikator lagging (M2 fix).

### 2.2 Market & Timeframe

- **Market:** Hyperliquid perp (eksekusi paper→live HL). Bybit/Binance hanya untuk data sekunder.
- **Universe:** Likuiditas tinggi saja. **Hard filter: OI ≥ $50M DAN realized_vol ≤ 6%.** Data #15/#19: high-vol asset (WLD 8.2%, GRASS) = wipeout. Altcoin micro-cap dibuang.
- **Signal TF:** 1m (entry trigger) + 5m (displacement confirm).
- **Regime TF:** 1H (sudah ada, berfungsi sejak #16). Tambah 15m sebagai jembatan.
- **Hold:** 8–20 menit (pertahankan — trailing fire di menit 4-14, terbukti).

### 2.3 Risk Management System

| Parameter | Aturan v9 | Basis |
|-----------|-----------|-------|
| Risk per trade | 0.5% equity (fixed), bukan conviction-weighted | Score tidak valid (M2) → conviction sizing = sizing acak |
| Position sizing | `size = (equity × 0.005) / (SL_distance_pct)` | SL nyata, bukan hardcode 0.7% (bug #16) |
| Vol scaling | RV >4% → size ×0.5; RV >6% → SKIP | High-vol = -$20 (#20) |
| **Correlation cap** | **Max 2 posisi searah berkorelasi BTC >0.7** | M7 — 5 LONG altcoin = 1 bet |
| Max concurrent | 3 (turun dari 5) | Konsentrasi + fee drag |
| Daily loss limit | -4% equity → stop hari itu | Hard, sudah ada |
| Weekly loss limit | **-8% equity → pause 48 jam, audit wajib** | BARU — circuit breaker |
| Drawdown kill | -15% peak → kill switch (admin reset) | Sudah ada, pertahankan |
| Post-loss cooldown | 3 loss beruntun → cooldown 30 menit | M (streak 6 di #20) |

### 2.4 Entry Logic (KETAT — ini kunci)

**Buang dari scoring:** EMA cross (r=-0.336, fire 94% = constant bias), MFI (noise), RSI momentum (lagging), DVI (mati), L/S (kontradiksi), liquidation (0% fire 16 audit). **7 komponen dihapus.**

**Pertahankan:** OB wall (saat tidak crowded — predictor terbaik konsisten), CVD 5m moderate (WR 47.7% #19), momentum 5m.

**Entry v9 = GATE berurutan (semua wajib, bukan skor):**

```
GATE 1 — REGIME ALIGN (hard block M3):
  LONG hanya jika HTF 1H ∈ {TRENDING_UP, RANGING}
  SHORT hanya jika HTF 1H ∈ {TRENDING_DOWN, RANGING}
  Lawan trend = REJECT (no exception)

GATE 2 — DISPLACEMENT PROOF (bunuh time_exit M1/M4):
  net move 5m searah ≥ 0.15% (LONG) / ≥ 0.20% (SHORT)
  DAN 3 dari 5 candle terakhir searah
  Belum ada displacement = REJECT (jangan masuk market flat)

GATE 3 — LIQUIDITY CONTEXT (edge baru, ganti indikator):
  Minimal SATU true:
    (a) Harga baru sweep session/range level lalu reject (reclaim)
    (b) OB wall aligned & NOT crowded (ob_dir ≥ 12)
    (c) CVD 5m moderate (0.3–0.7) searah
  Tidak ada konteks likuiditas = REJECT

GATE 4 — EXHAUSTION VETO (sudah ada dari #19):
  CVD extreme (>0.7) = REJECT (entry di flow habis)
  RSI 1m >70 (LONG) / <30 (SHORT) = REJECT

GATE 5 — QUALITY:
  spread ≤ 0.15%, RV ≤ 6%, OI ≥ $50M
  bukan EXTREME regime
```

Tidak ada "skor 45". Trade lolos = SEMUA gate hijau. Ini mengubah KARA dari "scoring engine" jadi "checklist eksekusi" — cara trader pro sebenarnya bekerja.

### 2.5 Exit Logic (pertahankan edge, kalibrasi vol-aware)

| Layer | Aturan v9 | Status |
|-------|-----------|--------|
| SL | 2.5× expected swing untuk hold window (vol-aware, #16 formula) | Fix bug konstan 0.8% |
| TP1 | 0.5× SL → close 50%, SL→BE | Pertahankan (realistis 0.4-0.5%) |
| TP2 | 1.0× SL → close 33% | Pertahankan |
| **Trailing** | **Post-TP1, ATR×2 dari peak, ratchet only** | **JANTUNG — jangan sentuh** |
| Pre-TP1 trail | **DISABLED** (sudah, root cause #19) | Jangan hidupkan lagi |
| Momentum death | flat 4min + peak <0.10% → cut | Pertahankan |
| Time limit | 20 min hard (score≥70: +grace 5min) | Pertahankan |
| Trailing target | **fire rate ≥35%** (via Gate 2 lebih banyak reach TP1) | Naik dari 18% |

### 2.6 Regime Detection & Adaptive Logic

- **1H regime (EMA8/21, strength 0.15)** — sudah berfungsi sejak #16. Pertahankan.
- **Adaptive per regime:**
  - TRENDING_UP/DOWN → Gate 1 izinkan searah, displacement threshold normal (0.15%).
  - RANGING → izinkan dua arah TAPI displacement threshold naik (0.25%) + wajib Gate 3a (sweep+reject) — di range, hanya trade reaksi level.
  - EXTREME/CHOPPY-vol-tinggi → **NO TRADE.**
- **Adaptasi sizing:** vol naik → size turun (sudah di 2.3).

### 2.7 Data & Feature Engineering

**Buang:** EMA/MFI/RSI-momentum/DVI/L/S/liq sebagai scoring (semua terbukti lagging/mati/inverse).

**Bangun (dari data HL yang SUDAH ada — OB + trades WS):**
1. **Session levels:** Asia/London/NY high-low (rolling). Trivial dari candle history.
2. **Range high/low:** prev 1H/4H extremes.
3. **Liquidity sweep detector:** harga tembus level X lalu reclaim dalam ≤2 candle = sweep. Pakai trade tape + OB yang sudah di-cache.
4. **CVD tick asli** (bukan proxy candle) dari trade WS side B/A — untuk validasi displacement nyata.

**Validasi WAJIB sebelum produksi:** setiap fitur baru harus tunjukkan WR-per-bucket lebih tinggi di ≥150 trade historis SEBELUM masuk gate. Tidak ada fitur masuk tanpa bukti.

### 2.8 Execution Rules

- **Slippage:** WAJIB diukur sebelum live (kriteria #8 selalu gagal). Micro-live $10-20, bandingkan fill paper vs HL nyata. Target slippage <0.05%.
- **Order type:** limit di level untuk entry (kurangi slippage), market untuk exit (prioritas keluar).
- **Funding:** skip entry 5 menit sebelum funding settlement kalau funding ekstrem (>0.05%/8h) — hindari bayar funding di hold pendek.
- **Session:** Fokus London+NY overlap (13-17 UTC, likuiditas tertinggi). Asia session (terbukti lemah #9, WR 29%) → threshold +5 atau skip.
- **No overnight concern:** scalp 20 menit, tidak ada overnight risk. Tapi hindari trade saat likuiditas tipis (00-06 UTC).

### 2.9 Filter & Rejection (kapan TIDAK trading)

```
HARD NO-TRADE:
- EXTREME regime / RV >6%
- spread >0.15%
- 30 menit sebelum + sesudah high-impact event (CPI/FOMC)
- weekly loss limit -8% kena
- 3 loss beruntun (cooldown 30m)
- OI <$50M (likuiditas tipis)
- lawan HTF trend
- displacement belum terbukti (Gate 2 gagal)
```

---

## 3. PERBANDINGAN SEBELUM vs SESUDAH

| Metrik | KARA lama (avg #5-#20) | KARA v9 (target realistis) | Dasar |
|--------|------------------------|---------------------------|-------|
| Win Rate | 28-45% (ayun) | 40-48% (stabil) | Gate 2 buang flat trades |
| R:R realized | 1.71:1 | 2.0–2.5:1 | Vol-aware SL/TP |
| Profit Factor | 0.37–2.31 (random) | 1.4–1.8 (konsisten) | Entry punya thesis |
| time_exit % | 54-65% | <35% | Gate 2 (displacement) |
| Trailing fire | 14-30% | ≥35% | Lebih banyak reach TP1 |
| Max DD | <15% (modal kecil) | <12% | Correlation cap + weekly limit |
| Hold time | 6-14 min | 8-20 min (winner lebih panjang) | Trailing ride trend |
| Trade/hari | 20-30 | 8-15 (lebih selektif) | Gate ketat, fee turun |
| Counter-trend trade | 90% | <10% | Gate 1 hard block |

**Catatan jujur:** target ini realistis HANYA jika Gate 2 (displacement) terbukti menaikkan WR di validasi. Kalau tidak, edge memang tidak ada dan harus pivot strategi. Angka di atas bukan janji — itu hipotesis yang harus divalidasi (lihat Bagian 5).

---

## 4. IMPLEMENTATION ROADMAP (single-variable, urutan ketat)

| Fase | Aksi | Deploy | Validasi sebelum lanjut |
|------|------|--------|-------------------------|
| **F0** | Hard-block LONG counter-trend (Gate 1, simetris dgn SHORT yang sudah ada) | Single | 100 trade, LONG/SHORT ratio ikut regime |
| **F1** | Displacement gate (Gate 2) — bunuh time_exit | Single | 100 trade, time_exit <40% |
| **F2** | Buang EMA+MFI+RSI-momentum dari scoring | Single | 100 trade, WR tidak turun (mereka noise, harusnya naik) |
| **F3** | Correlation cap + max concurrent 3 + weekly limit | Single | Monitor DD |
| **F4** | Bangun session-level + sweep detector (OFFLINE study dulu) | Setelah validasi offline | Gate 3a hanya jika studi tunjukkan edge |
| **F5** | Vol-aware SL/TP penuh + universe filter (OI≥$50M, RV≤6%) | Single | stop_loss-on-winner <15% |
| **F6** | Micro-live $10-20 + slippage measurement | — | Slippage <0.05% |

**Aturan besi:** SATU variabel per deploy. Minimum 100-150 trade per validasi. TIDAK ADA multi-fix lagi (penyebab M6).

**Prioritas #1 = F0 + F1.** Dua ini menutup 2 bocor terbesar (counter-trend M3 + time_exit M1) tanpa fitur baru, dari data yang SUDAH membuktikannya.

---

## 5. BACKTEST & LIVE VALIDATION PLAN

**Tahap 1 — Offline study (sebelum tulis kode fitur baru):**
- Ambil 200+ trade historis. Hitung: apakah trade post-sweep / di session H-L punya WR lebih tinggi dari baseline 28%? Apakah trade yang lolos Gate 1+2 punya WR >45%?
- Kalau YA → edge nyata, lanjut. Kalau TIDAK → konsep tidak transfer, pivot.

**Tahap 2 — Paper walk-forward (bukan in-sample):**
- Deploy F0-F2. Kumpulkan 300 trade BARU (out-of-sample, bukan periode yang sama yang dipakai desain).
- Gate go: 3 audit berturut PF >1.3, masing-masing sample ≥150 trade. (Sample kecil = bug detection, bukan validasi — pelajaran #8.)

**Tahap 3 — Micro-live ($10-20):**
- Ukur slippage paper vs HL nyata. Latency. Fill quality.
- Bandingkan PnL micro-live vs paper di periode sama. Gap >20% = masalah eksekusi.

**Tahap 4 — Scaling:**
- Hanya jika micro-live PF >1.3 selama 200 trade DAN slippage <0.05%.
- Naik bertahap: $20 → $50 → $100/user.

**Kriteria GO-LIVE penuh (semua wajib):**
1. PF >1.3 di 3 audit berturut (≥150 trade each)
2. Score/gate↔PnL terukur prediktif (WR gate-pass > gate-fail, signifikan)
3. Trailing fire ≥35%, time_exit <35%
4. Max DD <12%
5. Slippage live terukur <0.05%
6. Correlation cap aktif & teruji

---

## CATATAN PENUTUP (kritis)

Tiga kebenaran keras dari data 16 audit:

1. **KARA punya 1 edge nyata: trailing stop.** Semua sisanya belum terbukti. Rekonstruksi ini = memperkuat edge itu dengan entry yang akhirnya punya alasan, bukan menambah kompleksitas.

2. **Jangan ulangi M6.** 16 deploy multi-fix dalam 15 hari adalah kenapa tidak ada yang tahu apa yang bekerja. v9 HARUS single-variable, sample besar, out-of-sample. Lebih lambat, tapi satu-satunya jalan tahu kebenaran.

3. **Validasi sebelum percaya.** Setiap angka di Bagian 3 adalah hipotesis. Gate 2 (displacement) BISA gagal menaikkan WR — kalau begitu, edge displacement tidak ada di market kita dan harus pivot ke murni level-reaction. Data yang memutuskan, bukan dokumen ini.

**Rekomendasi tindakan pertama:** Jalankan Tahap 1 (offline study) untuk Gate 1+2 pada 90-200 trade terakhir. Itu menjawab "apakah rekonstruksi ini punya dasar" SEBELUM satu baris kode produksi ditulis. Tanpa risiko, tanpa deploy.
