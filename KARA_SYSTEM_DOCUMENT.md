# KARA Bot — Dokumen Sistem Lengkap

> Ditulis dari hasil membaca seluruh kode Python. Setiap angka berasal dari kode, bukan asumsi.
> Bahasa: Indonesia, dengan istilah teknis dalam bahasa Inggris bila lebih tepat.

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
- Long/Short ratio dari sumber eksternal

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

### 2b. Komponen Skor Utama

Setiap analyzer mengembalikan `bull_points` dan `bear_points` secara terpisah. Arah (LONG/SHORT) **belum ditentukan** pada tahap ini — semua diakumulasi dulu, baru diputuskan setelahnya.

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

*Contoh: OI $300M, funding +0.00003 → base_points=8, tilt=1 → bull=3, bear=5*

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
- Funding rate harus ≥ +0.00001 (longs sedang crowded, bukan shorts)
- 24h trend tidak boleh > +3% ke atas
- Bull-bear gap minimal 20 poin (vs 18 untuk LONG)

---

## BAGIAN 3: DARI SINYAL KE EKSEKUSI

### 3a. Threshold yang Harus Dilewati

| Mode | Threshold Signal | Threshold Auto-Execute |
|---|---|---|
| Standard | Score ≥ 55 (config), tapi kode engine pakai 62 | Score ≥ 60 (user config, locked) |
| Scalper | Score ≥ 60 (HARD, tidak bisa diubah user) | Score ≥ 60 |

**Catatan penting:** Ada inkonsistensi antara `config.SIGNAL.min_score_to_signal = 55` dan kode di `_run_standard()` yang hardcode threshold 62 (`getattr(config.SIGNAL, 'min_score_to_signal', 62)`). Yang berlaku efektif adalah **62** untuk standard mode.

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
7. **Bull-Bear Gap (Standard)** — Gap minimal 18 poin (LONG) atau 20 poin (SHORT).
8. **SHORT Funding Filter** — SHORT hanya valid jika funding ≥ +0.00001.
9. **SHORT Anti-Trend Filter** — SHORT diblok jika 24h trend > +3%.
10. **Momentum Consensus (Scalper)** — Jika 1m candles berlawanan dengan sinyal, di-reject.
11. **MTF Alignment (Scalper)** — Jika 15m EMA berlawanan kuat dan skor < 60 setelah penalty, di-reject.
12. **Score Threshold** — Standard < 62 atau Scalper < 60.
13. **Signal Cooldown** — Standard: 15 menit per aset. Scalper: 5 menit per aset.

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

**Rule C — TP1 sudah hit → TIDAK ada time exit**
- Jika posisi sudah TP1 dan trailing aktif, **tidak ada time exit**. Biarkan trailing stop yang handle

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

## VERDICT

Arsitektur KARA secara keseluruhan **sound dan terstruktur dengan baik**: pemisahan antara data collection (WS + REST), scoring engine (3 analyzer independen), risk management, executor, dan notifikasi sudah jelas. Pipeline dari sinyal ke eksekusi melewati banyak filter berlapis yang membuat bot sulit masuk trade sembarangan.

**Kesenjangan terbesar antara apa yang KARA seharusnya lakukan dan apa yang sebenarnya terjadi adalah: bot beroperasi dalam mode FULL_AUTO total, tapi risk management thresholds di-set sangat longgar** — daily loss limit 90% dan kill switch di drawdown 95% secara efektif berarti tidak ada pelindung kapital yang bermakna sampai akun hampir habis. Dikombinasikan dengan position sizing yang mengambil 2.5–3.5% risk per trade dengan leverage 10-25x pada mode scalper, satu losing streak 5-6 trade bisa menghapus 20-30% akun sebelum bot pause. Sementara itu, komponen yang sebenarnya bisa melindungi (AI edge filter, semi-auto confirmation) baru aktif setelah 300 trades atau tidak aktif sama sekali (FULL_AUTO bypass). Bagi user baru dengan saldo kecil, gap antara "bot ini punya 24 filter sebelum trade" dan "bot ini bisa kehilangan 90% saldo sebelum berhenti" adalah sumber risiko yang paling kritis.
