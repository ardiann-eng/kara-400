# KARA Bot — Dokumen Sistem Lengkap

> Ditulis dari hasil membaca seluruh kode Python. Setiap angka berasal dari kode, bukan asumsi.
> Bahasa: Indonesia, dengan istilah teknis dalam bahasa Inggris bila lebih tepat.
>
> **Last updated:** 2026-05-20 — sinkron dengan perubahan: Opportunity Scoring v2, Profit-Lock Exit System, Learning Engine (Pattern Memory + ML), Bot Brain Dashboard, migrasi Railway.

---

## RINGKASAN PERUBAHAN TERBARU (2026-05-20)

### Audit Findings (220 trades, 8 jam, 20 Mei 2026)
| Metric | Value |
|---|---|
| Win Rate | 42.3% (93W / 127L) |
| Total PnL | −$34.62 |
| Profit Factor | 0.69 |
| Avg Win | $0.84 |
| Avg Loss | −$0.89 |
| Score ↔ PnL correlation | **−0.2079** (inverse!) |
| Worst exit | time_exit: 178 trades, 32% WR, −$65.51 |
| Best exit | trailing_stop: 23 trades, 100% WR, +$30.65 |

**Root causes:**
1. **OI/Funding analyzer mati** — HL funding flat (97% asset < threshold), OI 51% zero, Liq 92% zero
2. **Score inverse predictive** — Score 65+ = −$29.09 (22% WR), Score 55-59 = +$12.46 (51% WR)
3. **Time-exit membunuh profit** — 81% trades keluar via time_exit (32% WR)
4. **SHORT tidak bisa fire** — Liq gate + displacement penalty + threshold 62 = semua SHORT di-block
5. **ML model tidak aktif** — `import time` missing di db.py → training_data selalu kosong
6. **`db.py` missing `import time`** — `save_training_data()` dan `save_pattern_memory()` selalu throw NameError

### Perubahan Implementasi (Audit Fix 2026-05-20 Session 2)

| Area | Sebelum | Sesudah | File |
|---|---|---|---|
| **Funding data source** | Hyperliquid (97% flat/zero) | **Bybit API primary, HL fallback** | `data/hyperliquid_client.py`, `data/bybit_client.py` |
| **OI data source** | HL only (51% zero) | **Bybit OI delta (dari /v5/market/tickers, 0 extra API calls)** | `data/hyperliquid_client.py`, `data/bybit_client.py` |
| **Long/Short Ratio** | Tidak ada | **NEW: Bybit L/S ratio — contrarian crowd signal (±7-12 pts)** | `data/bybit_client.py`, `engine/scoring_engine.py` |
| **RSI Divergence** | Tidak ada | **NEW: 1m vs 5m aggregated RSI divergence (±8 pts, 0 API calls)** | `engine/scoring_engine.py` |
| **early_trail_activation** | 0.15% | **0.25%** (sweet spot, less noise trigger) | `config.py` |
| **early_trail_distance** | 0.10% | **0.15%** (wider = less false trigger) | `config.py` |
| **quick_profit_threshold** | 0.35% | **0.20%** (tangkap profit kecil sebelum time-exit) | `config.py` |
| **quick_profit_retrace** | 0.12% | **0.10%** | `config.py` |
| **time_exit_early_loss_pct** | −0.4% | **−0.2%** (cut loser lebih cepat) | `config.py` |
| **time_exit_early_loss_mins** | 5m | **3m** (verdict lebih cepat) | `config.py` |
| **P0-3 Liquidation Gate** | Block score<35 tanpa liq data | **DISABLED** (liq data selalu 0) | `engine/scoring_engine.py` |
| **Displacement penalty SHORT** | Drop 0.3% = penalty ×0.80 | **Drop 0.3-1.0% = bonus ×1.05 (trend confirm). Penalty hanya >1.5%** | `engine/scoring_engine.py` |
| **min_score_short_signal** | 62 | **52** (realistis untuk bear market) | `config.py` |
| **`import time` in db.py** | Missing | **Added** — fixes ML training data persistence | `core/db.py` |
| **Bybit executor learning** | Tidak ada record_outcome() | **Added** — ML data collection untuk live mode | `execution/bybit_executor.py` |
| **Pattern memory UI** | Plain text (WR + PnL) | **Interactive cards + action badges (+12pts, −10pts, FLIP)** | `dashboard/templates/dashboard.html`, `dashboard/reasoning_logger.py` |

---

## BAGIAN 1: DATA YANG DIKUMPULKAN

### 1.1 Sumber Data — WebSocket (Real-time, Push)

KARA membuka satu koneksi WebSocket permanen ke Hyperliquid dan subscribe ke empat channel per aset:

| Data | Channel WS | Frekuensi Update | Dipakai Untuk |
|---|---|---|---|
| **Orderbook L2** | `l2Book` | Setiap perubahan bid/ask | Imbalance scoring, spread filter, CVD |
| **Trades** | `trades` | Setiap transaksi terjadi | CVD (Cumulative Volume Delta), momentum confirmation |
| **Funding Rate** | `activeAssetCtx` | Setiap perubahan (biasanya per jam) | OI+Funding scoring, funding history 96 periode terakhir |
| **Liquidations** | `liquidations` (global) | Setiap event likuidasi terjadi | Liquidation cascade scoring |

Saat startup, bot subscribe ke ~100 aset sekaligus dengan jeda 50ms per aset (total ~5 detik) untuk menghindari rate limit Hyperliquid.

Data WS disimpan di `MarketDataCache` dalam RAM:
- `cache.orderbook[asset]` — snapshot L2 terbaru
- `cache.trades[asset]` — 500 trade terakhir
- `cache.funding[asset]` — ctx funding terbaru
- `cache.funding_history[asset]` — 96 periode funding terakhir (rolling)
- `cache.liquidations` — 100 event likuidasi global terakhir

### 1.2 Sumber Data — REST API (Pull, On-demand)

| Data | Endpoint | Frekuensi | Dipakai Untuk |
|---|---|---|---|
| **Mark Price + OI + Funding** | `metaAndAssetCtxs` (batch) | Setiap scan cycle (tiap 5-60 detik) | Semua scoring; di-cache 30 detik |
| **Candle 1m (30 candles)** | `candleSnapshot` | Per aset per scan cycle (scalper) | EMA8/21, RSI, momentum 1m |
| **Candle 1m (3 candles)** | `candleSnapshot` | Per aset per scan cycle (standard) | Momentum confirmation (3-of-4 consensus) |
| **Candle 15m (32 candles)** | `candleSnapshot` | Setiap 5 menit per aset (cache) | MTF trend confirmation scalper |
| **Candle 1h (24 candles)** | `candleSnapshot` | Setiap 60 menit per aset (cache) | Volatility regime detection |
| **All Mids** | `allMids` | Setiap 10 detik (cache) | Spot-Perp basis calculation |
| **Top Volume Markets** | internal | Saat startup | Daftar 100 aset yang di-scan |

### 1.3 Data Pasar yang TIDAK Dikumpulkan

Beberapa data yang disebutkan dalam kode tapi **tidak diimplementasikan** atau tidak connected:
- Spot price dari exchange lain (hanya pakai `allMids` Hyperliquid sebagai proxy spot)
- Order book depth dari level di luar top 20
- ~~Long/Short ratio dari sumber eksternal~~ → **SEKARANG ADA dari Bybit** (2026-05-20)
- Whale wallet tracking (butuh on-chain indexer)

### 1.4 Sumber Data Bybit (BARU 2026-05-20)

KARA sekarang menggunakan Bybit sebagai **data enrichment** untuk fundamental signals:

| Data | Endpoint Bybit | Cache | Dipakai Untuk |
|---|---|---|---|
| **Funding Rate** | `/v5/market/tickers` (field `fundingRate`) | 30 detik | OI/Funding Analyzer — menggantikan HL funding yang flat (97% = 0) |
| **Open Interest** | `/v5/market/tickers` (field `openInterest`) | 30 detik | OI delta calculation (curr vs prev snapshot) |
| **Long/Short Ratio** | `/v5/market/account-ratio` | 60 detik | Contrarian crowd signal — fade the crowd (±7-12 pts) |

**Efisiensi:** Funding + OI dari **1 API call** (same response). L/S ratio = 1 call per asset (top 20, concurrent semaphore 5).

**Fallback:** Jika asset tidak ada di Bybit (HL-only coins seperti PURR, NIL, FOGO) → fallback ke HL data.

**Kenapa Bybit lebih informatif:**
- HL funding: standard rate 0.00125%/8h untuk 97% asset (flat, no signal)
- Bybit funding: varies 0.001%-0.05%/8h — 8× lebih banyak variasi
- HL OI: banyak asset tidak berubah dalam 5 menit (volume kecil)
- Bybit OI: volume 10-50× lebih besar → delta lebih signifikan

---

## BAGIAN 2: CARA KARA MENILAI SINYAL (SCORING)

### 2a. Deteksi Regime Pasar (Volatility Regime)

**Apa yang diperiksa?**
KARA mengambil 24 candle 1h terakhir, menghitung return per candle `(close - open) / open`, lalu menghitung standar deviasi dan dikalikan `√24` untuk mendapat estimasi volatilitas harian.

**Empat regime:**
| Regime | Kondisi | Multiplier Skor |
|---|---|---|
| `LOW_VOL` | Volatilitas < 1.5%/hari | ×0.90 |
| `NORMAL` | 1.5% – 4%/hari | ×1.00 |
| `HIGH_VOL` | 4% – 8%/hari | ×0.85 |
| `EXTREME` | > 8%/hari | **Skip asset sepenuhnya** |

Selain itu ada **trend multiplier** terpisah berdasarkan perubahan harga 24 jam:
- Jika trend > 1.5% atau < -1.5%: skor × 1.10 (trending market lebih dipercaya)
- Selain itu: skor × 0.95

**Final multiplier** = vol_multiplier × trend_multiplier. Contoh: `HIGH_VOL` + trending = `0.85 × 1.10 = 0.935x`

Regime di-cache 60 menit per aset dan dipersistensikan ke SQLite (`vol_cache` table) agar tidak perlu fetch ulang saat restart.

---

### 2b. Komponen Skor Utama — OPPORTUNITY SCORING v2 (2026-05-20)

Filosofi baru: **score tinggi = move BELUM terjadi tapi kondisi ripe**. Leading indicators (OI, OB wall) bobot besar. Lagging indicators (EMA, RSI) bobot kecil atau PENALIZE jika stale.

Scoring dibagi 3 layer:

#### Layer 1: SETUP (Leading Indicators — max ~70 pts)

| Komponen | Max Pts | Logika |
|---|---|---|
| **OI/Funding** | ±28 | Uang baru masuk = move ABOUT to happen |
| **Orderbook Wall** | ±18 | Pressure building, belum breakout |
| **Liquidation** | ±12 | Cascade potential = catalyst |
| **Bybit L/S Ratio** | ±12 | Crowd heavily positioned = contrarian fade (NEW 2026-05-20) |
| **RSI Divergence (1m vs 5m)** | ±8 | Price↓ + RSI recovering = reversal (NEW 2026-05-20) |

#### Layer 2: CONFIRMATION (Lagging — range −15 to +25 pts)

| Komponen | Pts | Logika |
|---|---|---|
| **EMA Cross (fresh ≤3m)** | +10 + setup boost | Baru cross = early in move |
| **EMA Cross (stale ≥8m)** | **−10** | Move sudah jalan lama = PENALTY |
| **RSI neutral (42-58)** | +5 | Price hasn't moved = opportunity |
| **RSI extreme (>75 or <25)** | **−8** | Exhaustion = PENALTY |
| **CVD divergence** (buying but price flat) | +10 | Hidden accumulation = SETUP |
| **CVD confirms** (aligned with price) | +3 | Lagging, small weight |
| **MTF 15m aligned** | +6 | Mild confirmation |
| **MTF 15m discord** | −4 | Counter-trend warning |

#### Layer 3: DISPLACEMENT MULTIPLIER (Anti-Chase)

**LONG direction:**
| Price already moved UP | Multiplier |
|---|---|
| > 0.8% | **×0.40** (very stale) |
| > 0.5% | **×0.60** (stale) |
| > 0.3% | **×0.80** (mild) |
| < −0.2% (price dropping = fresh LONG) | **×1.10** |

**SHORT direction (FIXED 2026-05-20 — was penalizing trend confirmation):**
| Price already moved DOWN | Multiplier |
|---|---|
| > 1.5% drop | **×0.50** (exhaustion, bounce risk) |
| > 1.0% drop | **×0.70** (extended) |
| 0.3-1.0% drop | **×1.05** (trend confirmed = BONUS) |
| Price rising (counter-trend) | **×0.70** (wrong direction for SHORT) |

#### Formula Akhir

```
dominant_setup = max(bull_setup, bear_setup)
raw = max(0, dominant_setup + confirm_pts)
scaled = int(raw × 1.6)
score = int(scaled × displacement_mult)
score = clamp(0, 100)

# Then in _run_scalper():
score × regime_mult (ranging=1.0, trending=0.85, late_trend=0.70, volatile=0.90)
score + session_bonus (split: 30% to score, 70% to threshold)
score + learning_engine adjustment (−20 to +12)
```

#### Score Range Examples

| Scenario | Score |
|---|---|
| MAX (OI=28, OB=18, Liq=12, fresh EMA, CVD diverge, counter-disp) | **100** |
| Good setup (OI=20, OB=10, fresh EMA, no displacement) | **70-80** |
| Typical (OI=5, OB=10, medium EMA) | **45-60** |
| Exhausted (stale EMA, RSI extreme, displacement >0.8%) | **14-20** (BLOCKED) |

#### Analyzer 1: OI + Funding (maks ±45 poin)

Mengukur kondisi pasar dari sisi modal dan biaya sewa posisi.

**Funding Rate Analysis:**

| Kondisi Funding | Poin |
|---|---|
| > 0.0006 (EXTREME positif) | +18 bull |
| > 0.0003 (HIGH positif) | +12 bull |
| > 0.00005 (moderat positif) | +6 bull |
| > 0.00001 (sedikit positif) | +3 bull |
| < -0.0006 (EXTREME negatif) | +18 bear |
| < -0.0003 (HIGH negatif) | +12 bear |
| < -0.00005 (moderat negatif) | +6 bear |
| < -0.00001 (sedikit negatif) | +3 bear |

*Contoh nyata: BTC funding rate 0.0001 (0.01%/8h) → +6 bull*

**OI Change Analysis:**

| Kondisi | Poin |
|---|---|
| OI naik > 0.8% + harga naik > 0.2% | +22 bull (strong confirmation) |
| OI naik > 0.8% + harga turun > 0.2% | +22 bear (strong confirmation) |
| OI turun > 0.8% | +5 ke sisi yang sesuai arah harga |

**OI Magnitude Bonus** (amplifier ke sisi yang menang):

| OI Market | Bonus |
|---|---|
| > $1B (BTC, ETH level) | +8 ke sisi dominan |
| > $200M (SOL, HYPE level) | +6 |
| > $50M | +4 |
| > $10M | +2 |
| < $10M | 0 |

**Spot-Perp Basis:**

| Kondisi | Poin |
|---|---|
| Perp premium > 0.15% di atas spot | +12 bull |
| Perp premium > 0.08% di atas spot | +6 bull |
| Perp diskon > 0.15% di bawah spot | +12 bear |
| Perp diskon > 0.08% di bawah spot | +6 bear |

**Funding Trend** (slope 8 periode terakhir):
- Slope positif kuat → +1 s/d +8 bull
- Slope negatif kuat → +1 s/d +8 bear

**Cap maksimal:** 45 poin per sisi.

---

#### Analyzer 2: Liquidation (maks bergantung OI; praktisnya 4–12 poin per sisi)

Mengukur di mana posisi-posisi akan terpaksa ditutup (likuidasi paksa).

**Jika ada data liquidation WS:**

| Kondisi | Poin |
|---|---|
| Cluster long liq di atas harga 1.5× lebih besar dari short | +12 bear (akan ada cascade turun) |
| Cluster short liq di bawah harga 1.5× lebih besar dari long | +12 bull (akan ada cascade naik) |
| Seimbang | +4 bull dan +4 bear |
| Cascade risk > 50% + kondisi sesuai | +6 tambahan |
| Cascade risk 20-50% | +4 tambahan |

**Jika TIDAK ada data liquidation WS** (kondisi normal — WS jarang kirim event):
Menggunakan OI USD sebagai proxy:

| OI Market | Base Points |
|---|---|
| > $500M | 10 poin total |
| > $200M | 8 poin total |
| > $100M | 6 poin total |
| > $10M | 4 poin total |
| < $10M | 2 poin total |

Poin dibagi ke bull/bear berdasarkan funding direction:
- Funding positif → longs crowded → lebih banyak ke bear (short liq tilt)
- Funding negatif → shorts crowded → lebih banyak ke bull
- **Funding netral (2026-05-14 fix)** → bull=bear=`max(base_points // 3, 1)` (min 1, max ~3 per sisi). Sebelumnya 0/0 yang membuat komponen ini "diam" untuk asset dengan funding netral; sekarang memberikan **OI tension score** kecil supaya scoring engine tetap punya sinyal dari semua 3 komponen.

*Contoh: OI $300M, funding +0.00003 → base_points=8, tilt=1 → bull=3, bear=5*
*Contoh netral: OI $300M, funding ≈ 0 → base_points=8, tension=2 → bull=2, bear=2*

---

#### Analyzer 3: Orderbook (maks ±30 poin)

Mengukur tekanan beli/jual aktif di pasar.

**Bid-Ask Imbalance** (dari orderbook top 20 level):
- Imbalance = `(total_bid_USD - total_ask_USD) / (bid + ask)`

| Nilai Imbalance | Poin |
|---|---|
| > +50% (bids jauh dominan) | +14 s/d +18 bull |
| +25% s/d +50% | +8 s/d +12 bull |
| +10% s/d +25% | +3 s/d +5 bull |
| -10% s/d +10% (seimbang) | +2 bull dan +2 bear |
| -25% s/d -10% | +3 s/d +5 bear |
| -50% s/d -25% | +8 s/d +12 bear |
| < -50% (asks jauh dominan) | +14 s/d +18 bear |

**VWAP Deviation:**

| Kondisi | Poin |
|---|---|
| Harga > 0.5% di atas VWAP | +10 bear (overbought) |
| Harga 0.2–0.5% di atas VWAP | +10 bull (momentum) |
| Harga 0.05–0.2% di atas VWAP | +4 bull |
| Harga > 0.5% di bawah VWAP | +10 bull (oversold) |
| Harga 0.2–0.5% di bawah VWAP | +10 bear |
| Harga 0.05–0.2% di bawah VWAP | +4 bear |

**Dollar Depth Asymmetry** (total $ bid vs ask, top 20 levels):

| Rasio Bids | Poin |
|---|---|
| > 65% | +5 bull |
| 55–65% | +3 bull |
| 35–45% | +3 bear |
| < 35% | +5 bear |

**CVD (Cumulative Volume Delta)** dari 100 trade terakhir:
- CVD positif besar + harga flat = akumulasi → +2 s/d +8 bull
- CVD negatif besar + harga flat = distribusi → +2 s/d +8 bear
- Threshold: BTC/ETH pakai $50K, aset lain pakai $20K

**Wall Detection:**
- Ada bid wall (order > 5× rata-rata ukuran) → +3 bull
- Ada ask wall → +3 bear

**Cap maksimal:** 30 poin per sisi.

---

### 2c. Meta Score

Meta score adalah **penyesuaian berbasis win rate historis** untuk pattern trading yang sama.

**Cara kerja:**
Setiap trade di-tag dengan pattern key format: `{mode}_{asset}_{side}` (contoh: `scalper_BTC_long`).

Setelah trade tutup, win/loss dan PnL disimpan ke database dengan Exponential Moving Average (alpha = 0.20):
- `winrate_ema = 0.80 × winrate_lama + 0.20 × hasil_baru`

Saat next signal untuk pattern yang sama:

| Kondisi | Delta |
|---|---|
| `winrate_ema ≥ 62%` dan ≥ 3 samples | **+8 poin** |
| `winrate_ema ≤ 40%` dan ≥ 3 samples | **-12 poin** |
| Samples < 3 (deployment baru) | **0 poin** |

Cap maksimal: ±15 poin.

**Penting:** Meta score baru aktif setelah minimal **3 trades** untuk pattern tersebut. Pada deployment baru atau aset baru, meta selalu +0 — ini perilaku yang benar, bukan bug.

Scalper dan Standard pakai pattern key terpisah (`scalper_BTC_long` vs `standard_BTC_long`) sehingga mereka belajar secara independen.

---

### 2d. Session Bonus

Diterapkan **additif** (bukan if/else), artinya overlap London-NY mendapat keduanya:

| Session | Jam UTC | Poin |
|---|---|---|
| NY Session | 13:00 – 20:59 UTC | **+10** |
| London Session | 08:00 – 16:59 UTC | **+4** |
| London-NY overlap | 13:00 – 16:59 UTC | **+14** (keduanya) |
| Off-session | 17:00 – 21:59 UTC | 0 |
| Asia Session | 22:00 – 06:59 UTC | **-10** |

**Blocked Hours (tidak ada sinyal sama sekali):**
Jam 08:00 dan 09:00 UTC (London open) — diblok karena data historis menunjukkan WR 7.1% dan 21.4% dengan total loss -$15.66 akibat opening spike.

---

### 2e. Formula Skor Akhir

**Standard Mode:**

```
total_bull = oi_bull + liq_bull + ob_bull
total_bear = oi_bear + liq_bear + ob_bear
margin = |total_bull - total_bear|
confidence_bonus = min(margin × 1.5, 12)

if long wins:
    raw_score = total_bull + confidence_bonus
else:
    raw_score = total_bear + confidence_bonus

raw_score += structure_delta  (±6 poin dari market structure)
raw_score += session_bonus    (±10 poin)
raw_score = min(raw_score, 92)  # cap sebelum multiplier

final_score = int(raw_score × vol_multiplier × trend_multiplier)
final_score = min(final_score, 100)

final_score += meta_delta  (-12 / 0 / +8)
final_score = min(max(final_score, 0), 100)
```

**Scalper Mode:**

```
score = bull_pts atau bear_pts (dari OB imbalance, EMA, RSI, CVD, Volume, HH/HL)
score += session_bonus
score = min(score, 100)
score += meta_delta

(lalu ada MTF alignment bonus/penalty ±12/±15)
```

---

### 2f. Keputusan Arah (LONG vs SHORT)

**Standard Mode:** Arah ditentukan **setelah** 3-of-4 consensus filter lulus:

1. `oi_dir` = sisi yang menang di OI+Funding analyzer
2. `liq_dir` = sisi yang menang di Liquidation analyzer
3. `ob_dir` = sisi yang menang di Orderbook analyzer
4. `mom_dir` = arah dari 3 candle 1m terakhir (minimal 2 dari 3 green/red)

Jika 3 dari 4 arah = LONG → trade LONG, jika 3 dari 4 = SHORT → trade SHORT.
Jika tidak ada 3-of-4 consensus → signal tidak dibuat (`return None, score`).

**Scalper Mode:** Arah ditentukan dari `bull_pts ≥ bear_pts` setelah semua komponen dihitung. Tidak ada 3-of-4 filter di scalper.

**SHORT tambahan filter (hanya berlaku jika SHORT diizinkan):**
- Funding rate harus ≥ **-0.0001** (block hanya saat funding sangat negatif — shorts sudah bayar longs). Sebelumnya `+0.00001` yang terlalu ketat dan membuang neutral-funding SHORTs.
- 24h trend tidak boleh > +3% ke atas
- Bull-bear gap minimal **25 poin** (naik dari 18 — fix 2026-05-14: SHORT struktural lebih lemah di HL karena positive funding bias)
- **Technical minimum gate (BARU 2026-05-14):** `OI_bear + Liq_bear + OB_bear ≥ 10`. Mencegah sinyal "session-only" lolos — contoh nyata: KAITO SHORT score=59 tapi OI=0, Liq=0, OB=0 (pure session bonus). Sekarang diblok.

**LONG tambahan filter (BARU 2026-05-14):**
- **Technical minimum gate:** `OI_bull + Liq_bull + OB_bull ≥ 5`. Lebih permisif dari SHORT karena positive funding bias HL favors LONG.

---

## BAGIAN 3: DARI SINYAL KE EKSEKUSI

### 3a. Threshold yang Harus Dilewati

| Mode | Side | Threshold Signal | Threshold Auto-Execute |
|---|---|---|---|
| Standard | LONG | Score ≥ 55 (config), tapi kode engine pakai 62 | Score ≥ 60 (user config, locked) |
| Standard | SHORT | **Score ≥ 62** (naik dari 57, fix 2026-05-14) | **Score ≥ 62** |
| Scalper | LONG | Score ≥ 45 (HARD, tidak bisa diubah user) | Score ≥ 45 |
| Scalper | SHORT | **Score ≥ 52** (turun dari 62, fix 2026-05-20 — SHORT harus bisa fire di bear market) | **Score ≥ 52** |

**Catatan penting:** Ada inkonsistensi antara `config.SIGNAL.min_score_to_signal = 55` dan kode di `_run_standard()` yang hardcode threshold 62. Yang berlaku efektif adalah **62** untuk standard mode LONG.

**Alasan SHORT lebih tinggi (2026-05-14):** Audit data menunjukkan SHORT trades WR 57.6% dengan net -$12.55 di Hyperliquid karena structural bias — positive funding/basis hampir selalu favor LONG. Threshold dinaikkan agar hanya highest-conviction SHORT yang lolos.

---

### 3b. Semua Filter Sebelum Eksekusi

Berikut urutan filter lengkap, berurutan dari scoring hingga eksekusi:

**Di dalam Scoring Engine (memblok pembuatan sinyal):**

1. **Blocked Hours** — Jam 08:00 dan 09:00 UTC. Kedua mode diblok.
2. **EXTREME Regime** — Volatilitas > 8%/hari. Asset di-skip.
3. **Mark Price = 0** — Jika API gagal return harga, asset di-skip.
4. **Spread Filter (Standard)** — Jika bid-ask spread > 0.25%, asset di-reject.
5. **Spread Filter (Scalper)** — Jika spread > 0.15%, asset di-reject.
6. **3-of-4 Consensus (Standard only)** — Jika tidak ada 3 dari 4 indikator aligned, tidak ada sinyal.
7. **Bull-Bear Gap** — Gap minimal **18 poin (LONG)** atau **25 poin (SHORT)** (naik dari 20 di fix 2026-05-14).
8. **SHORT Funding Filter** — SHORT diblok hanya jika funding < **-0.0001** (sebelumnya `< +0.00001`, terlalu ketat).
9. **SHORT Anti-Trend Filter** — SHORT diblok jika 24h trend > +3%.
10. **Technical Minimum Gate (BARU 2026-05-14)** — SHORT diblok jika `OI_bear + Liq_bear + OB_bear < 10`. LONG diblok jika `OI_bull + Liq_bull + OB_bull < 5`. Mencegah sinyal "session-only" tanpa konfirmasi fundamental/teknikal.
11. **Momentum Consensus (Scalper)** — Jika 1m candles berlawanan dengan sinyal, di-reject.
12. **MTF Alignment (Scalper)** — Jika 15m EMA berlawanan kuat dan skor < 60 setelah penalty, di-reject.
13. **Score Threshold** — Standard LONG < 62 atau SHORT < 62. Scalper LONG < 60 atau SHORT < 62.
14. **Signal Cooldown** — Standard: 15 menit per aset. Scalper: 5 menit per aset.

**Di dalam `_handle_signals()` (memblok setelah sinyal dibuat):**

14. **Score < Auto Threshold** — Jika score < 60 (user setting), sinyal diabaikan.
15. **Expected Value (EV) Gate (Standard only)** — Matematika: `EV = (win_prob × tp2×0.70) - ((1-win_prob) × sl_pct)`. Jika EV < 0.1%, trade diblok dengan log `[EV_BLOCKED]`.

**Di dalam `pre_trade_check()` (memblok eksekusi akhir):**

16. **Kill Switch** — Jika drawdown > 95% pernah tercapai, semua trading berhenti.
17. **AI Edge Filter** — Jika ML model sudah trained dan `expected_edge < 20%`, trade diblok (hanya aktif setelah 300 trades).
18. **Bot Paused** — Jika user atau sistem pause trading.
19. **Post-loss Cooldown** — Jika daily loss > 50%, cooldown 5 jam aktif.
20. **Max Concurrent Positions** — Standard: maks 10. Scalper: maks 3.
21. **Same Asset Open** — Tidak boleh buka posisi baru di aset yang sama (kecuali pyramid scalper).
22. **Daily Loss Limit** — Jika daily loss > 90%, trading pause hari ini.
23. **Max Drawdown Kill Switch** — Jika drawdown total > 95%.
24. **Insufficient Margin** — Jika margin yang diperlukan > saldo tersedia.

---

### 3c. Position Sizing

**Formula dasar:**

```
risk_pct = ditentukan dari score:
  - Score ≥ 75: risk 3.5% dari equity
  - Score ≥ 68: risk 3.0%
  - Score ≥ 60: risk 2.5%
  - Score  < 60: risk 2.0%

equity_multiplier:
  - Equity ≥ 150% dari start: × 0.80 (lindungi keuntungan)
  - Equity ≤ 80% dari start:  × 0.50 (mode rusak)
  - Lainnya:                  × 1.00

final_risk_pct = risk_pct × equity_multiplier × AI_multiplier

size_usd = (equity × final_risk_pct) / (sl_pct × leverage)
```

**Drawdown guard:** Jika saldo sudah turun ≥ 15% dari puncak, `size_usd` dipotong 50% lagi.

**Hard cap:** `size_usd` maksimal 35% dari saldo.

**AI multiplier** (dari ML model, aktif setelah 300 trades):
- `expected_edge = 0.8` → × 1.09
- `expected_edge = 0.5` → × 1.00 (netral)
- `expected_edge = 0.4` → × 0.85

**Contoh konkret:**
Saldo $1,000, score 70 (risk 3.0%), SL 2%, leverage 10x:
```
size_usd = ($1,000 × 0.030) / (0.02 × 10) = $30 / 0.2 = $150
contracts = ($150 × 10) / harga_entry
```

---

### 3d. Perhitungan SL, TP1, TP2

**Standard Mode — Vol-aware levels dari `calculate_levels()`:**

Level dihitung berdasarkan `realized_vol` dari vol_cache:

| Regime | Noise Multiplier | SL Floor | TP Multiplier |
|---|---|---|---|
| `LOW_VOL` | 0.60 | 0.8% | 2.0× SL |
| `NORMAL` | 0.80 | 1.2% | 2.2× SL |
| `HIGH_VOL` | 1.00 | 1.8% | 2.5× SL |
| EXTREME | 1.20 | 2.5% | 3.0× SL |

```
sl_pct = max(realized_vol × noise_mult, sl_floor)
sl_pct = min(sl_pct, 3.5%)  # hard cap

tp2_pct = sl_pct × tp_mult
tp1_pct = sl_pct × tp_mult × 0.55
```

**Adjustment berdasarkan score:**
- Score ≥ 80: TP multiplier × 1.30
- Score ≥ 70: TP multiplier × 1.15
- Score < 62: TP multiplier × 0.85

**Adjustment berdasarkan session:**
- NY session (13-21 UTC): SL × 1.20, TP × 1.15
- Asia (22-07 UTC): SL × 0.85, TP × 0.90

**Contoh aset NORMAL:**
Realized vol 2.5%, score 70, NY session:
```
sl_pct = max(0.025 × 0.80, 0.012) = 2.0%
sl_pct_ny = min(2.0% × 1.20, 3.5%) = 2.4%
tp_mult = 2.2 × 1.15 (score 70) × 1.15 (NY) = 2.909
tp2_pct = 2.4% × 2.909 = 6.98%
tp1_pct = 6.98% × 0.55 = 3.84%
RR = 6.98 / 2.4 = 2.91x
```

**Contoh aset VOLATILE (realized vol 6%):**
```
sl_pct = max(0.06 × 1.00, 0.018) = 6%  → tapi capped 3.5%
tp2_pct = 3.5% × 2.5 = 8.75%
tp1_pct = 8.75% × 0.55 = 4.8%
```

**Scalper Mode** — Fixed levels dari config, TIDAK dipengaruhi volatilitas:
- SL: 0.65%
- TP1: 0.85%
- TP2: 1.50%
- Leverage default: 25x (maks 35x)

---

### 3e. Eksekusi Order

**Tipe order:** `post_only` (maker only) untuk mendapat rebate fee.

**Slippage simulation (paper mode):** Spread 0.03% + noise acak ±0.01% ditambahkan ke fill price.

**Leverage:** Ditentukan dari signal, di-cap oleh: (1) user setting, (2) exchange limit per aset, (3) mode config.

**Liquidation price (paper):**
```
LONG: entry × (1 - 1/leverage + 0.005)
SHORT: entry × (1 + 1/leverage - 0.005)
```

**Mode saat ini:** `FULL_AUTO = True` (hardcoded). Artinya **semua sinyal yang lolos threshold otomatis dieksekusi** tanpa konfirmasi user via Telegram. Mode semi-auto (konfirmasi Telegram) sudah ada infrastrukturnya tapi tidak aktif karena `FULL_AUTO = True`.

---

## BAGIAN 4: MANAJEMEN POSISI

### 4a. Loop Monitoring

Posisi dimonitor oleh `_position_monitor_loop()` yang berjalan sebagai task independen:
- **Frekuensi:** Setiap **5 detik**
- **Data yang di-fetch:** Mark price terbaru via `get_mark_price_fast()` (tanpa semaphore/throttle)
- **Semua harga aset aktif di-fetch sekaligus**, baru diaplikasikan ke semua posisi

Pemisahan dari scan loop krusial: bahkan jika scan sedang lambat karena throttle API, position monitor tetap jalan dan TP/SL tetap tereksekusi tepat waktu.

---

### 4b. TP1 Trigger

**Kapan aktif:** Harga menyentuh level TP1 (0.85% untuk scalper, vol-aware untuk standard)

**Yang terjadi:**
1. Tutup **25%** dari posisi (standard) atau **60%** (scalper)
2. SL dipindahkan ke **breakeven + 0.1%** (entry × 1.001 untuk LONG, × 0.999 untuk SHORT)
3. `trailing_active = True`, `trailing_high = current_price`

---

### 4c. TP2 Trigger

**Kapan aktif:** Harga menyentuh level TP2 (1.50% scalper, vol-aware standard), setelah TP1 sudah hit

**Yang terjadi:**
1. Tutup **50% dari sisa posisi** (yang tersisa 75% setelah TP1)
2. Artinya di sini yang ditutup adalah 37.5% dari posisi original
3. Sisa **37.5%** dari posisi original terus berjalan dengan trailing stop

---

### 4d. Trailing Stop

**Kapan aktif:** Setelah TP1 hit, dan harga sudah melewati TP1 sebesar 0.3% tambahan

**Jarak trail:**
- Setelah TP2 hit: `max(realized_vol × 30%, 0.3%)`
- Antara TP1 dan TP2: `max(realized_vol × 50%, 0.5%)`

*Contoh: realized_vol 2.5%, sebelum TP2 → trail distance = max(2.5% × 50%, 0.5%) = 1.25%*

**Cara bergerak:**
- Untuk LONG: `trail_sl = highest_price_reached × (1 - trail_pct)`
- Jika harga turun ke bawah `trail_sl`, posisi ditutup penuh
- Untuk SHORT: kebalikannya

---

### 4d.1. Quick-Profit Exit / Rule F0 (BARU 2026-05-14)

**Tujuan:** Di Hyperliquid banyak aset di-cap max leverage 3-5×, sehingga ROE per % move kecil. Trailing stop biasa terlalu lambat — quick-profit exit mengambil profit langsung saat harga berbalik dari peak.

**Aktif kapan?**
- `floating_pnl >= threshold` (lihat tabel di bawah)
- **DAN** harga retrace dari peak `>= retrace_threshold`
- Bekerja **kapan saja**, tidak peduli TP1 sudah hit atau belum

**Leverage-aware threshold:**

| Leverage Posisi | Floating Threshold | Retrace Threshold | Trigger Behavior |
|---|---|---|---|
| ≤ 5× (low-lev assets) | **0.25%** | **0.10%** | Close FULL posisi langsung |
| > 5× | **0.35%** | **0.12%** | Close FULL posisi langsung |

**Konfigurasi (`config.py` → ScalperConfig):**
```
quick_profit_enabled:           True
quick_profit_threshold_pct:     0.0035  (0.35% — untuk leverage > 5x)
quick_profit_retrace_pct:       0.0012  (0.12% retrace)
quick_profit_low_lev_threshold: 0.0025  (0.25% — untuk leverage <= 5x)
quick_profit_low_lev_retrace:   0.0010  (0.10% retrace)
```

**Contoh trigger:**
- BLUR LONG 3×, entry $0.026 → harga naik ke $0.02614 (+0.54% > 0.5% threshold)
- Harga turun ke $0.02609 (retrace dari peak 0.02% — belum cukup)
- Harga turun lagi ke $0.02608 (retrace 0.23% > 0.2%) → **EXIT FULL**

**Catatan urutan eksekusi:** Rule F0 dijalankan **sebelum** Rule F (early trailing) dan **sebelum** time exit. Artinya kalau quick-profit threshold terpenuhi, posisi ditutup tanpa nunggu trailing stop cycle yang panjang.

---

### 4e. Stop Loss

**Kalkulasi saat entry:**
- Standard: `calculate_levels()` berbasis volatilitas (lihat 3d)
- Scalper: Fixed 0.65% dari entry

**Bisa bergerak setelah entry?** Ya, tepat sekali:
- Setelah TP1 hit: **SL dipindahkan ke breakeven+0.1%** (tidak pernah mundur dari breakeven setelah ini)

**Ketika harga menyentuh SL:**
Posisi ditutup 100% langsung. Tidak ada partial close untuk SL.

---

### 4f. Thesis Invalidation Exit (Standard Mode)

KARA menggunakan **momentum-based time exit** untuk standard mode, bukan hard time limit:

**Rule A — Momentum Reversal** (aktif setelah 30 menit):
- Jika harga sudah retrace kembali ke dalam **20% dari jarak entry ke TP1**, posisi ditutup
- Contoh: Entry $100, TP1 $103 (jarak $3). Setelah 30 menit harga turun ke $100.60 (yang 20% dari $3 = $0.60). Exit.
- Hanya berlaku jika floating PnL masih ≥ 0% (tidak dieksekusi kalau sudah loss)

**Rule B — Flatline** (aktif setelah 30 menit):
- Jika floating PnL < ±0.15% selama 30 menit → kapital redeployed, exit

**Rule C — TP1 sudah hit → grace period 50%, bukan tanpa time exit**
- Jika posisi sudah TP1, `effective_max_hold = max_hold × 1.5` (50% extension)
- **Setelah extended deadline lewat:**
  - Jika posisi **profit** → exit segera (`time_exit` action). Jangan tahan profit lebih lama dari max_hold extended. *(2026-05-14 fix — sebelumnya selalu skip time exit kalau profit)*
  - Jika posisi **loss** + masih dalam grace + recoverable → tunggu
  - Lainnya → exit paksa

**Rule D — Hard Safety Net 6 jam:**
- Jika sudah 6 jam dan TP1 belum pernah tercapai, posisi ditutup paksa

**Tidak ada re-scoring posisi yang sedang open** — KARA tidak recalculate score saat posisi berjalan. Exit decisions berbasis harga dan waktu, bukan score ulang.

---

### 4g. Stale Position Exit (Scalper Mode)

Scalper pakai **hard time limit 12 menit** + grace period:

- Setelah **12 menit**: cek apakah floating loss < -0.15%
  - Jika ya (masih dalam loss besar): tunggu grace period
  - Jika tidak: tutup paksa
- Setelah **18 menit total** (12 + 6 grace): tutup paksa apapun kondisinya

---

## BAGIAN 5: PERBEDAAN SCALPER VS STANDARD

| Parameter | Standard Mode | Scalper Mode |
|---|---|---|
| **Aset yang di-scan** | Top 100 volume dari Hyperliquid | Top 100 + 8 aset khusus scalper (ZEC, kBONK, SPX, COMP, REZ, PYTH, MON, VVV) |
| **Score threshold masuk** | 62 (hardcoded di engine) | 60 (locked, tidak bisa diubah user) |
| **Score threshold auto-execute** | 60 (user config) | 60 (user config) |
| **Session filter** | Bonus/penalty tapi tetap trading | Jam 08:00-09:00 UTC diblok, Asia session dapat penalty -10 |
| **Risk per trade** | 2.5-3.5% dari equity | 4% baseline (lebih agresif) |
| **SL** | Vol-aware 1.2–3.5% | Fixed 0.65% |
| **TP1** | Vol-aware ~3-4.8% | Fixed 0.85% |
| **TP2** | Vol-aware ~7-9% | Fixed 1.50% |
| **Close di TP1** | 25% dari posisi | 60% dari posisi |
| **Close di TP2** | 50% dari sisa (37.5% original) | 40% dari sisa |
| **Leverage** | 10x default (maks 10x) | 25x default (maks 35x) |
| **Max posisi concurrent** | 10 | 3 |
| **Max hold time** | 6 jam (hard cap), keluar lebih awal jika flatline/reversal | 12 menit hard + 6 menit grace |
| **EV Gate** | Ya — trade diblok jika EV < 0.1% | **Tidak ada EV gate** |
| **3-of-4 Consensus** | Wajib | Tidak ada |
| **MTF Confirmation** | Tidak ada | Ya — 15m EMA alignment (bonus +12, penalti -15) |
| **Pyramid (scale-in)** | Tidak ada | Ada (disabled by default, butuh profit ≥ 0.4%) |

---

## BAGIAN 6: RISK MANAGEMENT

### 6a. Daily Limits

| Level | Threshold | Aksi |
|---|---|---|
| Warning | Daily loss > 80% dari equity hari ini | Log warning saja |
| Pause | Daily loss > **90%** dari equity hari ini | Trading pause hari ini |

*Catatan: Nilai-nilai ini sudah sangat dilonggarkan (comment di kode: "RELAXED"). Angka 90% artinya user harus kehilangan hampir seluruh akun hari ini sebelum bot pause.*

---

### 6b. Drawdown Kill Switch

**Trigger:** Total equity saat ini < **5%** dari peak equity (drawdown > 95%)

**Auto-reset:** Ada — jika setelah kill switch aktif, drawdown membaik (equity naik kembali di bawah threshold), kill switch auto-reset. Ini bisa membuat kill switch tidak efektif.

**Manual reset:** Admin bisa reset lewat perintah `reset_kill_switch(requester_id)`, tapi hanya admin (dari env `ADMIN_CHAT_ID`).

---

### 6c. Cooldown

**Trigger:** Daily loss melebihi **50%** dari session start balance

**Durasi:** **5 jam** (dari `RISK.post_loss_cooldown_hrs = 5.0`)

**Persisten:** Cooldown disimpan ke SQLite dengan timestamp ISO, sehingga restart bot tidak bypass cooldown.

---

### 6d. Compounding

Position size otomatis mengikuti equity karena formula pakai `account_balance`:
```
size_usd = (equity × risk_pct) / (sl_pct × leverage)
```

Jika equity naik 50% → size naik 50% (compounding natural).

**Proteksi saat gains besar:**
Jika equity ≥ 150% dari start → risk_pct dikali 0.80 (kurangi agresivitas saat sudah untung)

**Proteksi saat drawdown:**
- Equity ≤ 80% dari start → risk_pct dikali 0.50 (mode hati-hati)
- Drawdown ≥ 15% dari peak → size_usd dipotong 50% lagi

**Fix Paper Executor 2026-05-14 — equity tracking benar:**

Sebelumnya `save_paper_state(chat_id, balance, balance)` — equity di-save sama dengan balance tanpa memperhitungkan unrealized PnL dari posisi yang masih terbuka. Akibatnya:
- `peak_balance` di-restore dari `balance` saja saat restart → peak tidak akurat
- Drawdown calculation berbasis peak yang salah → drawdown guard salah aktif/non-aktif
- Compounding decision (≥150% / ≤80% multipliers) berbasis equity yang salah

**Sekarang:**
```python
total_unrealized = sum(p.pnl_unrealized for p in self._positions.values() if p.status == OPEN)
user_db.save_paper_state(chat_id, balance, balance + total_unrealized)
```

Equity sekarang = `realized_balance + unrealized_dari_semua_posisi_terbuka`. Berlaku saat partial close, full close, dan close_position. Saat restart, `peak_balance` dihitung dari `max(equity_tersimpan, balance)` — bukan hardcode balance saja.

---

## BAGIAN 7: NOTIFIKASI TELEGRAM

### Event yang Trigger Telegram Message

| Event | Konten Notifikasi |
|---|---|
| **Sinyal baru (auto-executed)** | Asset, arah, score, entry/SL/TP levels, leverage, RR ratio, signal ID |
| **Posisi dibuka** | Asset, side, fill price, contracts, margin, leverage |
| **TP1 hit** | Asset, floating PnL%, pesan "SL moved to breakeven" |
| **TP2 hit** | Asset, floating PnL%, konfirmasi partial close |
| **Trailing stop hit** | Asset, trail price, peak floating PnL%, pesan "kapital dikembalikan" |
| **Stop loss hit** | Asset, loss persen, harga SL |
| **Time exit (batch scalper)** | Semua posisi scalper yang expired sekaligus, total PnL |
| **Momentum reversal exit** | Asset, hold time, PnL% |
| **Flatline exit** | Asset, hold time, PnL% |
| **Hard limit exit** | Asset, durasi, PnL% |
| **Daily reset** | Laporan harian: equity, PnL hari ini, jumlah posisi, drawdown, status bot |
| **Update notification** | Saat deploy baru terdeteksi: release tag + catatan update |
| **Bot restart (live mode)** | Peringatan posisi open yang masih ada di chain |

### Format Signal Template

```
[side_emoji] SINYAL [ASSET] - [STRENGTH]
Score: XX/100
Arah: LONG/SHORT
Entry: $XX.XX
Stop Loss: $XX.XX (X.X%)
TP1: $XX.XX (+X.X%) → 40%    ← (template lama, aktualnya 25%)
TP2: $XX.XX (+X.X%) → 35%    ← (template lama, aktualnya 37.5% dari original)
Leverage: XXx isolated
R:R = X.Xx
```

---

## BAGIAN 8: TEMUAN DARI KODE

> Section ini di-update 2026-05-14. Item yang sudah di-fix dipindahkan ke "Sudah Diperbaiki" di bawah.

### Sudah Diperbaiki (2026-05-14)

| Temuan | Status | Commit/Fix |
|---|---|---|
| SHORT threshold terlalu rendah (57) → WR 57.6% net loss | ✅ Naik ke 62 | `config.py` |
| Bull-bear gap SHORT sama dengan LONG (18) | ✅ Naik ke 25 untuk SHORT | `config.py` |
| Sinyal "session-only" lolos (score 59, OI=0, Liq=0) | ✅ Technical minimum gate ditambah | `scoring_engine.py` |
| Liquidation analyzer return 0/0 saat funding netral | ✅ OI tension fallback (bull=bear=1-3) | `liquidation_analyzer.py` |
| Paper executor: equity = balance saja (no unrealized) | ✅ Equity = balance + unrealized semua posisi | `paper_executor.py` |
| Time exit nahan profit terlalu lama saat TP1 hit | ✅ Exit segera saat profit di max_hold | `risk_manager.py` |
| Low-lev (3-5×) asset profit tipis hilang karena trail lambat | ✅ Quick-profit exit Rule F0 dengan leverage-aware threshold | `risk_manager.py` |
| Scalper margin tidak konsisten antar leverage | ✅ Leverage-aware risk_pct + margin cap | commit 83fbd7a, 645e2d1 |
| Scalper risk_per_trade_pct kebesaran (12%) | ✅ Turun ke 5% | commit b5cb4f8 |
| `localize_for_user` override TP/SL merusak RR | ✅ Tidak override lagi | commit 62e8971 |

---

### Komponen yang Diimplementasikan tapi Tidak Terkoneksi

**1. Semi-auto mode (konfirmasi Telegram) — TIDAK AKTIF**
Infrastruktur lengkap ada: `_pending_signals`, tombol Confirm/Skip di Telegram, handler `_on_trade_confirmed`. Tapi `FULL_AUTO = True` hardcoded di config.py membuat kode `else: continue` selalu membuang sinyal yang tidak auto-execute. Sinyal tidak pernah dikirim ke user untuk dikonfirmasi manual.

**2. `_broadcast_heartbeat()` di dashboard — BROKEN**
Ada bug struktural: `return` ada di dalam `try` block sebelum kode broadcast yang sebenarnya:
```python
try:
    acc = await session.get_account_state()
except Exception as e:
    return  # ← return di sini selalu terpanggil karena ada indent error
    
    await broadcast({...})  # ← baris ini TIDAK PERNAH dieksekusi
```
Dashboard tidak pernah menerima update real-time. Halaman dashboard akan tampil tapi data tidak bergerak.

**3. `calculate_atr()` dan `calculate_sl_from_atr()` — Terduplikasi**
Ada dua path untuk kalkulasi SL berbasis volatilitas:
- `calculate_sl_from_atr()` di risk_manager (old path via ATR candles)
- `calculate_levels()` di risk_manager (new path via vol_cache)

Path lama (`calculate_sl_from_atr`) masih di-call di `_handle_signals()` untuk mengambil atr_value, tapi kemudian di-override oleh `calculate_levels()`. Fetching candles untuk ATR tetap terjadi tanpa guna karena hasilnya langsung digantikan.

**4. `simulate_score()` di scoring_engine — Tidak pakai OI dalam USD**
Fungsi test ini masih menggunakan `oi.open_interest` langsung (raw contracts), bukan USD. Ini adalah bug lama yang sudah di-fix di production path tapi belum di simulation path. Test calibration di startup memberikan angka yang sedikit berbeda dari scoring nyata.

**5. `paper_sl_pct`, `paper_tp1_pct`, `paper_tp2_pct` di RiskConfig — Tidak dipakai**
Ada parameter khusus paper mode di config (sl 2.0%, tp1 1.2%, tp2 2.2%) tapi tidak ada kode yang membaca parameter ini. Semua path pakai `default_sl_pct` atau `calculate_levels()`.

### Kalkulasi yang Selalu Return Default/Zero

**1. Meta Score selalu +0 di deployment baru**
Sudah benar by design — butuh minimal 3 trades per pattern key sebelum bisa aktif. Tapi pada aset yang jarang trading, ini bisa tetap +0 sangat lama.

**2. CVD di `_run_standard()` selalu 0**
Di `_run_standard()`, ada block `if recent_trades:` untuk menghitung CVD tapi nilai ini hanya di-log untuk debug, TIDAK dimasukkan ke scoring. CVD hanya di-score di `OrderbookAnalyzer.analyze()` yang terpisah. Perhitungan CVD di diagnostic log dan perhitungan CVD di scoring adalah hal berbeda.

**3. `funding_history` jarang ter-populate**
`cache.funding_history[asset]` diisi dari WS channel `activeAssetCtx`. Hyperliquid mengirim ini hanya saat ada perubahan funding, bukan per jam. Pada banyak aset kecil, history mungkin kosong → funding trend slope selalu = 0.

**4. Predicted Rate dari Funding — Selalu None**
`funding.predicted_rate` di-check di OI analyzer (bisa +3 poin) tapi `get_funding_data()` dari Hyperliquid client tidak pernah mengisi field ini. Selalu None.

### Filter yang Ada Tapi Tidak Selalu Efektif

**1. Kill switch dengan auto-reset terlalu mudah recover**
Kill switch trigger saat drawdown > 95%, tapi auto-reset jika drawdown membaik. Dengan 95% threshold, saldo sudah hampir nol saat trigger. Auto-reset logic berarti jika ada input equity kecil setelah reset, kill switch hilang.

**2. `_enforce_locked_score_thresholds()` vs kode aktual tidak sinkron**
Fungsi ini memaksakan nilai di user config (`std_min_score_to_signal = 55`), tapi scoring engine hardcode `min_score_to_signal = 62` langsung ke kodenya. User config yang sudah di-locked ke 55 tidak pernah dipakai oleh engine.

**3. `time_based_exit_hours`, `time_based_min_profit`, `time_based_max_profit` di RiskConfig — Tidak dipakai**
Ada konfigurasi "tutup setelah 8 jam jika profit 1-3%" tapi tidak ada kode yang mengimplementasikan logic ini. Logic yang aktual adalah `time_exit_hard_hours = 6.0` (berbeda nilai dan tidak ada profit condition).

---

## BAGIAN 9: LEARNING ENGINE (BARU 2026-05-20)

### 9a. Layer 1: Pattern Memory

Setiap trade di-tag dengan pattern key: `{asset}_{side}_{regime}` (contoh: `KAITO_long_trending`).

**EMA Win Rate** (alpha=0.15): `ema_wr = 0.85 × ema_lama + 0.15 × (1 jika win, 0 jika loss)`

**Aksi berdasarkan EMA WR (aktif setelah 5 trades per pattern):**

| EMA WR | Aksi |
|---|---|
| < 25% (n≥5) | Cek sisi sebaliknya: jika opposite WR>50% → **FLIP SIDE**. Jika tidak → **score −20** |
| < 40% (n≥5) | **score −10** |
| > 65% (n≥5) | **score +8** |
| > 80% (n≥8) | **score +12, size ×1.2** |

**Contoh:** KAITO_long_ranging WR=0% n=13 → cek KAITO_short_ranging. Jika short WR>50% → flip ke SHORT. Jika tidak ada data → penalty −20 (score turun, kemungkinan besar tidak lolos threshold).

**Persistensi:** Disimpan ke SQLite table `pattern_memory`. Survive restart dan deploy.

### 9b. Layer 2: ML Model (HistGradientBoosting)

**Aktivasi:** Setelah 200 trades total.
**Retrain:** Setiap 50 trades baru.

**Features:**
- oi_funding_score, orderbook_score, liquidation_score
- displacement_5m, rsi, ema_freshness, atr_pct
- regime_code, hour_utc, score

**Output:** P(win) → 0.0 to 1.0

**Aksi:**
| P(win) | Aksi |
|---|---|
| < 0.35 | size × 0.5 |
| 0.35-0.65 | normal |
| > 0.65 | size × 1.3 |

### 9c. Integration Flow

```
Signal generated → learning_engine.evaluate()
  ├─ Layer 1: pattern memory lookup → score adj / flip side
  └─ Layer 2: ML predict → size multiplier
  
Trade closes → learning_engine.record_outcome()
  ├─ Update pattern EMA
  ├─ Save training data
  └─ Retrain model if needed
```

**Files:** `engine/learning_engine.py`, `core/db.py` (tables: `pattern_memory`, `training_data`)

---

## BAGIAN 10: BOT BRAIN DASHBOARD (BARU 2026-05-20)

Section "Bot Brain" di admin dashboard (`/admin/reasoning` atau sidebar → Bot Brain).

### Komponen UI:
1. **Stats Cards** — Patterns learned, flips triggered, ML predictions, ML accuracy
2. **Live Reasoning Flow** — WebSocket real-time: setiap evaluasi asset ditampilkan step-by-step (signal → learning → filters → execute/skip)
3. **Top Winners & Losers** — Pattern memory ranking
4. **Decision Log** — Tabel history keputusan (asset, side, score, decision, learning adjustment)

### API Endpoints:
| Endpoint | Fungsi |
|---|---|
| `GET /api/admin/reasoning/decisions` | Recent decision traces |
| `GET /api/admin/reasoning/live` | Live reasoning steps |
| `GET /api/admin/reasoning/active` | In-progress traces |
| `GET /api/admin/learning/stats` | ML + pattern stats |
| `GET /api/admin/learning/patterns` | All pattern memory entries |
| `WS /ws/admin/reasoning` | Real-time WebSocket feed |

### Reasoning Logger:
- Ring buffer in-memory (500 decisions, 2000 steps) — zero disk I/O during trading
- Emits structured JSON at each decision step
- WebSocket broadcast to connected dashboard clients

**Files:** `dashboard/reasoning_logger.py`, `dashboard/app.py`, `dashboard/templates/dashboard.html`

---

## BAGIAN 11: RANKED EXECUTION (BARU 2026-05-20)

### Masalah Sebelumnya

Bot execute sinyal **pertama yang lolos** dalam scan cycle. Karena scan paralel, ETH (score 57) bisa masuk duluan sementara ZRO (score 78) datang belakangan dan di-block karena slot penuh.

### Solusi: Batch + Sort + Distribute

```
Scan 100 markets (14 detik)
  → Kumpulkan SEMUA sinyal yang lolos filter ke all_signals_batch
  → Sort by score descending: ZRO=78, COMP=69, SOL=62...
  → Per user: hitung slot kosong, execute top-N
  → User A (0 posisi) → dapat ZRO, COMP, SOL
  → User B (2 posisi) → dapat ZRO saja
  → User C (3 posisi) → skip semua
```

**Tidak ada tambahan latency** — scan sudah 14 detik, ranked execution hanya mengubah urutan execute di akhir cycle.

**File:** `main.py` — `_scan_all_assets()` + `_handle_signals()`

---

## BAGIAN 12: PERUBAHAN LAIN (2026-05-20)

### 12a. Minimum Margin Floor

Setiap trade dijamin minimum margin $8 (selama balance ≥ $40):

```python
# config.py, ScalperConfig:
fixed_margin_per_position: float = 8.0  # minimum $8 margin per trade

# risk_manager.py, calculate_position_size():
min_margin = cfg.fixed_margin_per_position  # = 8.0
if min_margin > 0 and size_usd < min_margin:
    if min_margin <= account_balance * 0.20:
        size_usd = min_margin
```

Dengan leverage 13-20x: margin $8 = notional $104-160.

### 12b. Force Scalper Mode

Semua user di-force ke scalper mode saat startup via `_enforce_locked_score_thresholds()`:

```python
if getattr(cfg, 'trading_mode', 'standard') != 'scalper':
    cfg.trading_mode = 'scalper'
    dirty = True
```

Mencegah user yang tersimpan di DB dengan mode "standard" tetap trading di mode yang salah.

### 12c. P0-3 Gate Diturunkan

Filter liquidation cascade (P0-3) diturunkan dari `score < 55` ke `score < 35`:

- **Sebelum:** Semua sinyal score 35-54 di-block karena tidak ada live liquidation data (Hyperliquid jarang kirim event). Ini menyebabkan 0 trades.
- **Sesudah:** Hanya sinyal sangat lemah (score < 35) yang butuh liquidation catalyst. Sinyal score 35+ bisa lolos tanpa liq data.

### 12d. Telegram UI — KARA Theme

Close position notification diperbarui:

**Manual close:**
```
🌸 KARA SYSTEM: Position Closed

{asset} berhasil ditutup.

  • Realized PnL: +Rp8.760
  • Exit Price  : $17.264
  • Reason      : manual

Profit diamankan~
```

**Close all confirmation:**
```
🌸 KARA SYSTEM: Close All Positions

Akan menutup 2 posisi yang sedang terbuka.
Semua posisi akan ditutup di harga market saat ini.

[Ya, tutup semua]  [Batal]
```

---

## VERDICT (Update 2026-05-20 Session 2)

### Perubahan Fundamental

Audit kedua (220 trades, 8 jam) mengungkap masalah yang lebih dalam dari audit pertama:

1. **Data Source Revolution** — Hyperliquid funding/OI data ternyata flat (97% = 0). Sekarang pakai **Bybit sebagai primary data source** untuk funding rate, OI delta, dan Long/Short ratio. Satu API call Bybit = 3 data points yang sebelumnya mati.

2. **SHORT Unblocked** — Tiga filter yang secara kolektif memblok SEMUA SHORT signal diperbaiki: P0-3 liq gate disabled, displacement penalty dibalik untuk SHORT (drop = confirm bukan stale), threshold turun 62→52.

3. **RSI Divergence** — Multi-timeframe momentum detection dari data yang sudah ada (1m candles di-aggregate jadi 5m, zero API calls). Deteksi reversal lebih awal.

4. **ML Model Fixed** — Bug `import time` missing di db.py menyebabkan training data tidak pernah tersimpan selama berhari-hari. Sekarang fixed + bybit_executor juga record outcomes.

5. **Exit System Retuned** — Quick profit 0.20% (dari 0.35%), early trail 0.25%/0.15% (dari 0.15%/0.10%), early loss cut -0.2%/3m (dari -0.4%/5m).

### Expected Impact

| Metric | Sebelum (audit 2) | Target |
|---|---|---|
| OI/Funding active signals | 3% (HL) | ~40% (Bybit) |
| SHORT capability | 0 trades (all blocked) | 10-20% of trades |
| Score↔PnL correlation | −0.21 (inverse) | >0 (positive) |
| time_exit dominance | 81% of trades | <50% |
| trailing_stop fire rate | 10% | 30-50% |
| Profit factor | 0.69 | 1.2-1.5 |

### Risiko yang Tersisa

- **Bybit API dependency** — Jika Bybit down, fallback ke HL (flat). Perlu monitor.
- **SHORT overtrade risk** — Threshold 52 mungkin terlalu rendah di bull market. Monitor WR per minggu.
- **L/S ratio rate limit** — 20 assets × 1 call = 20 calls/60s. Bybit limit = 120/min. Safe tapi perlu monitor.
- **ML cold start** — Training data baru mulai collect sekarang. Model aktif setelah ~200 trades (~1 hari).
