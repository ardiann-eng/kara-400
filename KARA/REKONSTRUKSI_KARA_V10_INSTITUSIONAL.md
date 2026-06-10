# PROPOSAL REKONSTRUKSI BESAR-BESARAN — KARA v10 "INSTITUSIONAL"
**Tanggal:** 6 Juni 2026
**Tujuan:** Modal Rp1.000.000 → compounding profit konsisten, grade trader profesional
**Basis:** Audit #5–#20 (16 audit, ~1.200 trade) + offline gate study
**Status modal:** Rp1.000.000 ≈ $56.5 (kurs 17.700) per user

> Dokumen ini adalah **redesign total**, bukan tambal-sulam. KARA lama = scoring engine aditif yang break-even-minus-fee. KARA v10 = sistem eksekusi institusional berbasis likuiditas + compounding engine. Setiap angka punya basis dari data KARA atau prinsip futures yang teruji.

---

## 0. REALITA MATEMATIKA COMPOUNDING (kenapa desain harus berubah total)

Modal Rp1jt (~$56). Target compounding realistis untuk trader pro = **3-8%/bulan konsisten**, BUKAN 100%/bulan (itu gambling).

**Skenario compounding $56 @ berbagai edge:**

| Edge/bulan | 6 bulan | 12 bulan | 24 bulan | Realistis? |
|-----------|---------|----------|----------|------------|
| 5%/bln | $75 | $101 | $182 | ✅ Pro-grade |
| 10%/bln | $99 | $176 | $553 | ⚠️ Agresif tapi mungkin |
| 20%/bln | $167 | $498 | $4.400 | ❌ Tidak sustainable |

**Implikasi desain:** Dengan modal kecil, **fee + slippage adalah musuh utama**. KARA lama trade 20-30×/hari → fee menggerus 30%+ alpha. v10 HARUS: **lebih sedikit trade, kualitas lebih tinggi, R:R lebih besar.** Compounding datang dari konsistensi PF >1.5, bukan frekuensi.

**Math edge minimum untuk compounding:**
- PF 1.5 + 10 trade/hari + risk 1%/trade + WR 45% R:R 2:1 = expectancy +0.35%/trade = ~3.5%/hari kotor → ~2%/hari net after fee = **target tercapai**.
- KARA sekarang: PF 0.6-0.75, expectancy NEGATIF. **Tidak ada compounding, hanya decay.**

---

## 1. RINGKASAN MASALAH FATAL (yang rekonstruksi ini selesaikan)

| # | Masalah | Bukti (22 Mei–6 Jun) | v10 Solution |
|---|---------|----------------------|--------------|
| M1 | time_exit chronic | 58% trade, WR 5.8%, -$55 | Exit redesign + entry displacement |
| M2 | Score tidak prediktif | 14/16 audit r≤0.10 | Buang scoring aditif → gate system |
| M3 | Counter-trend bias | 90% LONG di market turun, -$20 | Hard regime gate (offline study: aligned WR 45.5% vs counter 25.8%) |
| M4 | Entry tanpa thesis | Exit 100% WR, entry 28% WR | Liquidity-based entry |
| M5 | SL/TP tidak vol-aware | R:R 1.71, BE-WR gap -9.1pp | ATR-anchored levels |
| M6 | Overfitting whack-a-mole | 16 deploy, sample 15-50 | Walk-forward + sample 200+ |
| M7 | No correlation control | 5 LONG altcoin serentak | Portfolio heat limit |

---

## 2. REKONSTRUKSI TOTAL KARA v10

### 2.1 PHILOSOPHY & EDGE

**3 edge yang dipertahankan (TERBUKTI di 16 audit):**
1. **Trailing stop post-TP1** — 100% WR konsisten. Jantung sistem.
2. **Momentum death** — cut trade mati, avg -$0.07.
3. **Vol-adjusted sizing**.

**Edge BARU (institusional, menggantikan indikator lagging):**

KARA v10 trade **3 setup institusional** yang punya thesis jelas, bukan skor indikator:

| Setup | Thesis | Data HL tersedia? |
|-------|--------|-------------------|
| **A. Liquidity Sweep Reversal** | Harga sweep stop cluster di level (session/range H-L) lalu reclaim → smart money entry | ✅ OB + trades WS |
| **B. Trend Continuation Pullback** | HTF trend + pullback ke level + OB wall hold → ride trend | ✅ OB + 1H regime |
| **C. Displacement Breakout** | Breakout level dengan volume/CVD surge + retest hold | ✅ CVD + OB |

Ketiga setup ini = **cara trader prop desk sebenarnya trading**: di level likuiditas, dengan trend, dengan konfirmasi order flow. Bukan "RSI<30 + EMA cross".

### 2.2 MARKET & TIMEFRAME

- **Market:** Hyperliquid perp. Universe filter KETAT: **OI ≥ $50M DAN realized_vol ≤ 6%**. Micro-cap/high-vol dibuang (sumber wipeout WLD/GRASS).
- **TF struktur:** 1H (regime + level), 15m (setup), 1m (entry trigger).
- **Hold:** 8-25 menit (winner di-trail bisa lebih panjang).
- **Sesi:** Fokus London+NY (13-17 UTC). Asia (WR 29% historis) → threshold ketat / skip.

### 2.3 RISK MANAGEMENT SYSTEM (compounding-safe)

| Parameter | Aturan v10 | Rationale |
|-----------|-----------|-----------|
| **Risk per trade** | **1.0% equity** (fixed) | $0.56 risk pada $56. Conviction sizing dibuang (score invalid) |
| **Position sizing** | `size = (equity × 0.01) / SL_distance_pct` | SL nyata, vol-aware |
| **Vol scaling** | RV>4% → ×0.5; RV>6% → SKIP | High-vol = -$20 historis |
| **Portfolio heat** | **Max total open risk 3% equity** | Correlation control (M7) |
| **Correlation cap** | Max 2 posisi searah corr-BTC >0.7 | 5 LONG altcoin = 1 bet |
| **Max concurrent** | 3 (turun dari 5) | Fee + konsentrasi |
| **Daily loss limit** | -4% → stop hari itu | Circuit breaker |
| **Weekly loss limit** | -8% → pause 48 jam + audit | BARU |
| **Drawdown kill** | -15% peak → kill switch | Pertahankan |
| **Compounding rule** | Recalc size dari equity tiap trade | Auto-compound |
| **Profit lock** | Tiap equity +20% → withdraw 10% ke "safe" | Lindungi gain (pro discipline) |

**Compounding mechanism:** size selalu = 1% dari equity AKTUAL. Equity naik → size naik proporsional → compounding otomatis. Equity turun → size turun → proteksi drawdown. Ini Kelly-lite fractional.

### 2.4 ENTRY LOGIC (gate-based, ganti scoring) — ANTI-OVER-FILTER

**Buang 7 komponen:** EMA, MFI, RSI-momentum, DVI, L/S, liquidation, OI-mild. (Semua terbukti lagging/inverse/mati.)

**PRINSIP KRITIS (funnel study 90 sinyal):** Hanya filter MURAH yang boleh hard-reject. Filter yang membuang >30% sinyal HARUS jadi SIZING MODIFIER, bukan gate. Bukti: RV≤6% sendirian buang 77% sinyal (median RV crypto altcoin = 9.4%!) → bot mati 4 trade/hari. Threshold realistis RV = 15% (extreme-only).

**Tiga lapis keputusan (bukan 5 gate reject):**

```
LAPIS 1 — HARD GATES (murah, regime-agnostic, ~48% lolos = ~38 trade/hari)
  G1. Regime align: LONG ∈ {TRENDING_UP, CHOPPY/RANGING}; SHORT ∈ {TRENDING_DOWN, CHOPPY/RANGING}
      (CHOPPY = dua arah boleh. Hanya counter-trend MURNI yang di-block.)
  G2. Not exhaustion: CVD 5m extreme (>0.7) = reject
  G3. Extreme junk filter: RV >15% = reject (BUKAN 6%), spread >0.15%, OI <$50M
  → Funnel: 43/90 (48%) lolos. Trade di SEMUA regime.

LAPIS 2 — SIZING TIERS (modulate size, TIDAK reject — jaga volume)
  Liquidity context (di session H-L / range extreme / OB wall) → TIER A, size ×1.0
  Tanpa liquidity context → TIER B, size ×0.6 (tetap trade!)
  RV 0-4% → ×1.0 | RV 4-6% → ×0.75 | RV 6-15% → ×0.5 (damage control via size, bukan skip)

LAPIS 3 — SETUP CLASSIFIER (label, untuk audit; tidak reject)
  A. Sweep+reclaim | B. Pullback hold | C. Breakout+retest | D. Generic momentum
  Semua di-trade; classifier untuk ukur WR per-setup nanti.
```

**Hasil:** ~38 trade/hari (vs strict 4/hari), trade di semua regime, risk dikelola lewat UKURAN posisi bukan penolakan. Compounding butuh volume — desain ini menjaganya.

### 2.5 EXIT LOGIC (riset time-exit + trailing optimal)

**Riset time_exit dari data KARA:** time_exit median hold 6 menit = trade tidak bergerak. Bukan masalah timer, masalah entry. Tapi exit tetap perlu redesign:

| Layer | Aturan v10 | Basis data |
|-------|-----------|------------|
| **SL** | Structural: di balik level/swing yang invalidasi setup. Floor 2.5× expected swing (vol-aware) | Bukan % konstan (bug #16) |
| **TP1** | 1R (= SL distance) → close 40%, SL→BE | R:R 1:1 lock |
| **TP2** | 2R → close 30% | R:R 2:1 |
| **Trailing** | Sisa 30% trail di balik swing structure (bukan %), post-TP1 | EDGE — 100% WR |
| **Time stop** | **Adaptif:** jika belum +0.5R dalam 8 menit → exit (bukan hard 20min) | Bunuh time_exit di akar |
| **Momentum death** | flat 4min + peak <0.10% → cut | Pertahankan |
| **Pre-TP1 trail** | DISABLED | Root cause #19 |

**Time-exit yang BAGUS (jawaban pertanyaanmu):** bukan timer tetap, tapi **"progress-based"**. Trade pro tidak hold trade yang tidak perform. Aturan: jika dalam 8 menit belum capai +0.5R, thesis kemungkinan salah → keluar. Ini mengubah time_exit dari "dump bucket WR 6%" jadi "early invalidation cut".

**R:R target:** SL structural biasanya 0.6-1.0%, TP2 di 2R = 1.2-2.0%. Dengan WR 45% dan R:R 2:1 → expectancy +0.35R/trade = **edge nyata untuk compounding**.

### 2.6 REGIME DETECTION & ADAPTIVE

- **1H regime** (EMA8/21, strength 0.15) — berfungsi sejak #16, pertahankan.
- **Adaptasi per regime:**
  - TRENDING → Setup B (continuation) diprioritaskan, size normal.
  - RANGING → Setup A (sweep reversal) diprioritaskan, size normal.
  - EXTREME/RV>15% → **NO TRADE** (extreme-only, bukan 6%).
- **Adaptasi sizing:** vol naik → size turun (auto, lihat Lapis 2).

**⚠️ TEMUAN FUNNEL — SHORT signal scarcity (harus difix untuk volume di market turun):**
Data 90 sinyal: **81 LONG / 9 SHORT**, padahal 40/90 regime TRENDING_DOWN. Bot kekurangan amunisi SHORT saat market turun → di TRENDING_DOWN cuma 22% sinyal tradeable. **Akar:** komponen lama bias LONG (EMA fire 94% bullish). Setelah buang EMA + tambah setup sweep/breakout dua-arah, generasi sinyal SHORT harus naik. Target: rasio LONG/SHORT mendekati rasio regime (~55/45 saat market turun). Ini WAJIB agar volume tinggi di SEGALA regime, bukan cuma bull market.

### 2.7 DATA & FEATURE ENGINEERING (institusional)

**Bangun dari data HL yang SUDAH ada (OB + trades WS):**
1. **Session levels** (Asia/London/NY H-L) — trivial dari candle.
2. **Range extremes** (prev 1H/4H high-low).
3. **Liquidity sweep detector** — tembus+reclaim dari trade tape.
4. **CVD tick asli** (side B/A per trade) — ganti proxy candle.
5. **OB wall persistence** — wall yang bertahan vs spoofing (sudah ada sebagian).
6. **Volume profile** (opsional fase 2) — POC, value area.

**Validasi WAJIB:** tiap fitur tunjuk WR-per-bucket lebih tinggi di ≥150 trade historis SEBELUM masuk gate.

### 2.8 EXECUTION RULES

- **Slippage:** WAJIB ukur micro-live dulu (kriteria #8 selalu gagal). Target <0.05%.
- **Order type:** Limit di level untuk entry (kurangi slippage, dapat maker rebate HL). Market untuk exit.
- **Funding:** skip entry 5min sebelum settlement jika funding >0.05%/8h.
- **Sesi:** London+NY overlap prioritas. Hindari 00-06 UTC (likuiditas tipis).

### 2.9 FILTER & NO-TRADE RULES

```
HARD NO-TRADE:
- EXTREME regime / RV >6%
- spread >0.15% / OI <$50M
- counter HTF trend
- tidak di level likuiditas (Gate 2 fail)
- 30min sekitar news/funding extreme
- weekly loss -8% / daily loss -4% kena
- 3 loss beruntun (cooldown 30min)
- portfolio heat >3%
```

---

## 3. PERBANDINGAN SEBELUM vs SESUDAH

| Metrik | KARA lama (#5-#20) | KARA v10 (target) | Mekanisme |
|--------|--------------------|--------------------|-----------|
| Win Rate | 28-45% (ayun) | 42-50% (stabil) | Gate institusional |
| R:R realized | 1.71:1 | 2.0-2.5:1 | Structural SL/TP |
| Profit Factor | 0.37-2.31 (random) | 1.5-1.9 (konsisten) | Entry punya thesis |
| time_exit % | 54-65% | <30% | Progress-based time stop |
| Trailing fire | 14-30% | ≥40% | Lebih banyak reach TP1 |
| Max DD | <15% | <10% | Portfolio heat + weekly limit |
| Trade/hari | 20-30 | 6-12 | Selektif, fee turun |
| Counter-trend | 90% | <5% | Hard gate |
| Expectancy/trade | NEGATIF | +0.30-0.40R | Compounding viable |
| **Compounding/bulan** | **-decay** | **+5-10%** | PF >1.5 konsisten |

---

## 4. IMPLEMENTATION ROADMAP

**Fase 0 — Quick wins (data sudah membuktikan, deploy minggu ini):**
| # | Aksi | Bukti |
|---|------|-------|
| F0.1 | Hard-block LONG counter-trend | Offline: aligned 45.5% vs counter 25.8% |
| F0.2 | Progress-based time stop (8min/+0.5R) | time_exit WR 6% |
| F0.3 | Portfolio heat 3% + max 3 posisi | M7 |

**Fase 1 — Arsitektur gate (2 minggu):**
| # | Aksi |
|---|------|
| F1.1 | Bangun session-level + range-extreme calculator |
| F1.2 | Liquidity sweep detector (offline validate dulu) |
| F1.3 | Ganti scoring aditif → 5-gate system (feature flag KARA_V10) |
| F1.4 | Buang EMA/MFI/RSI-mom/DVI/L/S/liq |

**Fase 2 — Order flow institusional (3-4 minggu):**
| # | Aksi |
|---|------|
| F2.1 | CVD tick asli (ganti proxy) |
| F2.2 | Structural trailing (swing-based, bukan %) |
| F2.3 | Setup A/B/C classifier |

**Fase 3 — Validasi & live (4-6 minggu):**
| # | Aksi |
|---|------|
| F3.1 | Walk-forward 300 trade out-of-sample |
| F3.2 | Micro-live $10-20 + slippage measurement |
| F3.3 | Scale bertahap $20→$50→full |

**Aturan besi:** SATU komponen arsitektur per deploy. Minimum 150 trade validasi. Tidak ada multi-fix (penyebab M6).

---

## 5. BACKTEST & LIVE VALIDATION PLAN

**Tahap 1 — Offline study (sebelum kode):**
- Sudah dimulai: Gate 1 TERBUKTI (aligned WR 45.5% vs 25.8%).
- Lanjut: validasi sweep/level setup pada 200+ trade. WR setup-pass harus >45%.

**Tahap 2 — Walk-forward paper:**
- Deploy v10 di flag. Kumpulkan 300 trade BARU (out-of-sample).
- Go-gate: 3 audit berturut PF >1.3, sample ≥150 each.

**Tahap 3 — Micro-live ($10-20):**
- Ukur slippage paper vs live. Gap >20% = masalah eksekusi.
- PF micro-live >1.3 selama 200 trade.

**Tahap 4 — Compounding live:**
- Mulai Rp1jt. Risk 1%/trade. Auto-compound.
- Monthly review: PF, max DD, expectancy. Withdraw 10% tiap +20% equity.

**Kriteria GO-LIVE penuh (semua wajib):**
1. PF >1.5 di 3 audit berturut (≥150 trade each)
2. Gate-pass WR > gate-fail WR (signifikan, p<0.05)
3. Trailing fire ≥40%, time_exit <30%
4. Max DD <10%
5. Slippage live <0.05%
6. Expectancy >+0.30R/trade

---

## 6. PROYEKSI COMPOUNDING (jika v10 capai target)

Asumsi: PF 1.6, WR 45%, R:R 2:1, risk 1%/trade, 8 trade/hari, 20 hari trading/bulan, expectancy +0.35R = +0.35%/trade.

| Bulan | Equity (start Rp1jt) | Net (after fee ~30%) |
|-------|---------------------|----------------------|
| 0 | Rp1.000.000 | — |
| 3 | ~Rp1.150.000 | +5%/bln realistis |
| 6 | ~Rp1.340.000 | compounding |
| 12 | ~Rp1.800.000 | +80% tahun 1 |

**Catatan brutal:** Ini HANYA tercapai jika expectancy benar-benar positif setelah fee+slippage. Data sekarang expectancy NEGATIF. v10 harus MEMBUKTIKAN edge di walk-forward dulu. Proyeksi ini adalah TARGET, bukan janji. Trader pro tahu: +5%/bulan konsisten selama 2 tahun >> +50% sebulan lalu blow-up.

---

## 7. PRINSIP YANG TIDAK BOLEH DILANGGAR

1. **Edge dulu, compounding kemudian.** Tanpa expectancy positif terbukti, compounding = memperbesar kerugian. Validasi 300 trade dulu.
2. **Sedikit trade berkualitas > banyak trade.** Modal kecil = fee mematikan. 6-12 trade/hari, bukan 30.
3. **Level + trend + order flow = thesis.** Bukan indikator aditif. Trade seperti prop desk.
4. **Single-variable deploy.** Jangan ulangi M6 (16 deploy whack-a-mole).
5. **Proteksi modal > kejar profit.** Weekly limit, portfolio heat, profit withdrawal. Survive dulu, profit kemudian.
6. **Data memutuskan.** Setiap angka di sini hipotesis sampai walk-forward membuktikan.

---

## LANGKAH PERTAMA KONKRET

Deploy **Fase 0** (3 quick win, data sudah membuktikan) sambil membangun Fase 1. F0.1 (hard-block counter-trend) sendiri membalik sub-portfolio dari -$20 jadi +$4.47 di offline study.

Paralel: jalankan offline study untuk Setup A (sweep reversal) — apakah trade di session H-L punya WR >45%? Itu memvalidasi pilar institusional sebelum tulis kode produksi.
