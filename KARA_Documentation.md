# KARA Bot — Dokumentasi Teknis Lengkap

**Versi**: 7.1.0  
**Tanggal Dokumen**: Mei 2026  
**Platform**: Hyperliquid Futures (Testnet & Mainnet)  
**Bahasa**: Python 3.10+

---

## Daftar Isi

1. [Gambaran Umum](#1-gambaran-umum)
2. [Tech Stack](#2-tech-stack)
3. [Arsitektur Sistem](#3-arsitektur-sistem)
4. [Sumber Data](#4-sumber-data)
5. [Sistem Scoring](#5-sistem-scoring)
6. [Filter Entry](#6-filter-entry)
7. [Manajemen Posisi](#7-manajemen-posisi)
8. [Manajemen Risiko](#8-manajemen-risiko)
9. [Mode Trading](#9-mode-trading)
10. [Intelligence Model (AI)](#10-intelligence-model-ai)
11. [Meta Scoring](#11-meta-scoring)
12. [Telegram Bot](#12-telegram-bot)
13. [Dashboard Web](#13-dashboard-web)
14. [Database & Persistensi](#14-database--persistensi)
15. [Deployment](#15-deployment)
16. [Keterbatasan yang Diketahui](#16-keterbatasan-yang-diketahui)
17. [Ringkasan Fitur](#17-ringkasan-fitur)
18. [Changelog](#18-changelog)

---

## 1. Gambaran Umum

KARA adalah bot trading futures otomatis yang dirancang untuk platform **Hyperliquid** — sebuah DEX (Decentralized Exchange) on-chain untuk perpetual futures. Bot ini menggunakan pendekatan **multi-factor signal scoring** untuk mengidentifikasi peluang trading, dengan sistem manajemen risiko berlapis dan dukungan AI berbasis machine learning.

### Tujuan Utama

- Menghasilkan sinyal trading berkualitas tinggi dari data on-chain secara real-time
- Mengeksekusi atau merekomendasikan entry/exit berdasarkan skor sinyal terbobot
- Melindungi modal pengguna dengan hard limit dan kill-switch otomatis
- Memberikan pengalaman trading yang transparan melalui notifikasi Telegram dan dashboard web

### Filosofi Desain

- **Scoring sebelum entry**: Setiap aset mendapat skor 0–100 sebelum ada keputusan trading
- **Arah ditentukan terakhir**: Bull vs. bear dihitung dari akumulasi bukti semua analyzer, bukan asumsi awal
- **Konsensus 3-dari-4**: Minimal 3 dari 4 sinyal (OI, Liq, OB, Momentum) harus sepakat arah
- **Multi-user**: Satu instance bot dapat melayani banyak pengguna via Telegram, masing-masing dengan sesi dan state independen

---

## 2. Tech Stack

| Komponen | Library / Framework | Versi / Catatan |
|---|---|---|
| Bahasa | Python | 3.10+ |
| Async runtime | `asyncio` | Built-in |
| Exchange SDK | `hyperliquid-python-sdk` | Resmi |
| HTTP client | `httpx` | Async |
| WebSocket | `websockets` | Built-in async |
| Bot Telegram | `python-telegram-bot` | v21+ |
| Web framework | `FastAPI` | + `uvicorn` |
| Database | `SQLite` | Via `aiosqlite` (async) |
| Machine Learning | `scikit-learn` | `HistGradientBoostingClassifier` |
| Data processing | `numpy`, `pandas` | Untuk feature engineering & backtesting |
| Image generation | `Pillow` (PIL) | PnL card & daily card PNG |
| Config | `python-dotenv` | `.env` file loading |
| Enkripsi | `cryptography` (Fernet) | Untuk wallet secret |
| Validasi model | `pydantic` | v2 |
| Deployment | `Docker` + `docker-compose` | Railway-compatible |

---

## 3. Arsitektur Sistem

### 3.1 Komponen Utama

```
┌─────────────────────────────────────────────────────────────────┐
│                         KARA Bot Core                           │
│                         (main.py)                               │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │  scan_loop   │  │ position_    │  │   ws_watchdog_loop   │  │
│  │  (60s/15s)   │  │ monitor_loop │  │   (health check)     │  │
│  └──────┬───────┘  └──────┬───────┘  └──────────────────────┘  │
│         │                 │                                      │
│  ┌──────▼───────┐  ┌──────▼───────┐                            │
│  │ScoringEngine │  │ RiskManager  │                            │
│  │(scoring_     │  │(risk_manager │                            │
│  │ engine.py)   │  │ .py)         │                            │
│  └──────┬───────┘  └──────┬───────┘                            │
│         │                 │                                      │
│  ┌──────▼─────────────────▼──────────────────────────────────┐  │
│  │              Executor Layer                                 │  │
│  │    PaperExecutor (paper_executor.py)                       │  │
│  │    LiveExecutor  (live_executor.py)                        │  │
│  └────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
         │                     │                     │
┌────────▼──────┐   ┌──────────▼──────┐   ┌─────────▼──────────┐
│  Data Layer   │   │  Intelligence   │   │  Notification      │
│               │   │  Model          │   │                    │
│ Hyperliquid   │   │ (ML + Meta)     │   │ Telegram Bot       │
│ REST + WS     │   │                 │   │ Dashboard FastAPI  │
│ hyperliquid_  │   │ intelligence_   │   │ PnL Card + Daily   │
│ client.py     │   │ model.py        │   │ Card (Pillow)      │
│ ws_client.py  │   │ experience_     │   │                    │
│               │   │ buffer.py       │   │                    │
└───────────────┘   └─────────────────┘   └────────────────────┘
```

### 3.2 Alur Kerja Utama

```
scan_loop (setiap 60 detik / 15 detik scalper)
    │
    ▼
_scan_all_assets()
    │   Iterasi semua aset yang diizinkan
    ▼
ScoringEngine.run_asset(asset, mode)
    │   Jalankan _run_standard() dan/atau _run_scalper()
    ▼
[3 Analyzer]: OI+Funding │ Liquidation │ Orderbook
    │   Akumulasi bull_pts & bear_pts
    ▼
Consensus Filter (3-of-4)
    │   + Gap filter (min gap 18 LONG / 20 SHORT)
    ▼
Score dihitung → Session bonus → Meta delta
    ▼
_handle_signals()
    │
    ├─ score < threshold → skip
    ├─ SEMI_AUTO → kirim notif ke Telegram, tunggu persetujuan
    └─ FULL_AUTO (score ≥ 60) → langsung ke executor
           │
           ▼
        pre_trade_check() → calculate_position_size()
           │
           ▼
        PaperExecutor / LiveExecutor → buka posisi
           │
           ▼
        position_monitor_loop (setiap 5 detik)
           │
           ▼
        check_tp_trail() → Rule A/B/C/D/E/F
           │
           └─ Tutup posisi → PnL card → notifikasi Telegram
```

### 3.3 Task Async

`main.py` menjalankan 3 task async secara bersamaan:

| Task | Fungsi | Interval |
|---|---|---|
| `scan_loop` | Scan semua aset, generate sinyal | 60 detik (standard), 15 detik (scalper) |
| `position_monitor_loop` | Monitor posisi terbuka, cek SL/TP | ~5 detik |
| `ws_watchdog_loop` | Monitor kesehatan WebSocket, reconnect jika perlu | Setiap iterasi |

### 3.4 Multi-User Architecture

- Setiap pengguna Telegram memiliki objek `UserSession` yang independen
- `chat_id` → kunci untuk lookup session, config, posisi, balance
- Satu instance bot melayani banyak pengguna secara bersamaan
- Data per-user disimpan terpisah di SQLite (keyed by `chat_id`)

---

## 4. Sumber Data

### 4.1 REST API (Hyperliquid)

Dikelola oleh `data/hyperliquid_client.py`. Ada dua HTTP client:
- `_http_data` — selalu ke mainnet (data publik)
- `_http_trade` — ke testnet (paper mode) atau mainnet (live mode)

**Throttle mode**:
- **Throttle ON** (untuk scoring): semaphore 8 concurrent, sleep 0.12 detik per call
- **Throttle OFF** (untuk position monitor): tidak ada throttle, akses lebih cepat

**Circuit breaker**: Jika mendapat HTTP 502, delay backoff diterapkan sebelum retry.

**Cache**: Market metadata di-cache 5 menit di memory.

Data yang diambil via REST:

| Data | Endpoint / Method | Keterangan |
|---|---|---|
| Mark price | `info.all_mids()` | Harga tengah semua aset |
| Funding rate | `info.meta_and_asset_ctxs()` | Per-aset |
| Open Interest | `info.meta_and_asset_ctxs()` | USD notional |
| Orderbook | `info.l2_snapshot(asset)` | Level 1–20 bid/ask |
| Candles (OHLCV) | `info.candles_snapshot()` | Untuk vol regime & EMA |
| Account state | `info.user_state()` | Balance, margin, equity |
| Positions | `info.user_state()` | Posisi terbuka on-chain |
| Exchange metadata | `info.meta()` | Daftar aset, tick size, dll |

**Fallback berlapis** jika SDK gagal:
1. Coba SDK call
2. Coba HTTP direct ke `https://api.hyperliquid.xyz/info`
3. Gunakan safe defaults (harga 0, OI 0, dll)

### 4.2 WebSocket (`data/ws_client.py`)

`KaraWebSocketClient` berlangganan (subscribe) ke channel Hyperliquid WS secara real-time:

| Channel | Data yang Diterima | Keterangan |
|---|---|---|
| `l2Book` | Orderbook depth | Per-aset, update real-time |
| `trades` | Recent trades | Untuk CVD (Cumulative Volume Delta) |
| `activeAssetCtx` | Funding rate live | Update funding setiap 8 jam |
| `liquidations` | Data likuidasi real-time | Jika tersedia |
| `userEvents` | Event akun pengguna | Untuk live mode |

**Reconnect**: Exponential backoff, maksimum 120 detik antar percobaan.  
**Health check**: Jika tidak ada pesan selama 60 detik, koneksi dianggap tidak sehat.  
**Subscription delay**: 50ms per aset (100 aset ≈ 5 detik total untuk mencegah rate limit).

### 4.3 Cache & State

| Cache | Lokasi | TTL |
|---|---|---|
| Vol regime (realized vol) | Memory + SQLite `vol_cache` | 60 menit |
| Market metadata | Memory (`_market_cache`) | 5 menit |
| WS market data | `MarketDataCache` object | Sepanjang sesi (no expiry) |
| OI snapshot | SQLite `oi_snapshots` | Disimpan per cycle |

---

## 5. Sistem Scoring

### 5.1 Pipeline Scoring (Standard Mode)

```
Input: asset, mark_price, funding_rate, oi_data, orderbook
         │
         ▼
[1] OI + Funding Analyzer        → bull_pts₁, bear_pts₁  (max 35 per sisi)
[2] Liquidation Analyzer         → bull_pts₂, bear_pts₂  (max ~22 per sisi)
[3] Orderbook Analyzer           → bull_pts₃, bear_pts₃  (max 30 per sisi)
[4] Momentum (EMA/Candle)        → bull_pts₄, bear_pts₄  (sinyal arah)
         │
         ▼
Total bull = Σ bull_pts,  Total bear = Σ bear_pts
         │
         ▼
3-of-4 Consensus Filter
         │  (perlu ≥ 3 dari 4 analyzer sepakat arah)
         ▼
raw_score = max(bull, bear) → skala ke 0-100
         │
         ▼
Bull-Bear Gap Filter
         │  (gap ≥ 18 untuk LONG, ≥ 20 untuk SHORT)
         ▼
Structure Bonus (candle pattern) → ±5 pts
         │
         ▼
Vol Multiplier × (0.8 – 1.2)
         │
         ▼
Trend Multiplier × (0.9 – 1.1)
         │
         ▼
Session Bonus (NY/London/Asia)   → tapered di score ≥ 62 dan ≥ 72
         │
         ▼
Meta Learning Delta              → +8 atau -12
         │
         ▼
final_score = int(raw_score × vol_mult × trend_mult) + session + meta
```

### 5.2 OI + Funding Analyzer (`engine/analyzers/oi_funding_analyzer.py`)

**Funding Rate Tiers** (bull direction):

| Funding Rate | Poin |
|---|---|
| > 0.0006 (sangat tinggi) | +18 bull |
| > 0.0003 | +12 bull |
| > 0.00005 | +6 bull |
| > 0.00001 | +3 bull |

Arah terbalik untuk bear (funding sangat negatif → bear signal).

**Open Interest Change**:

| Kondisi | Poin |
|---|---|
| Harga naik + OI naik > 0.8% | +22 bull (konfirmasi trend) |
| Harga turun + OI naik > 0.8% | +22 bear (konfirmasi distribusi) |
| Divergence (harga turun + OI naik bearish) | Signal sesuai arah OI |

**Spot-Perp Basis**:

| Basis | Poin |
|---|---|
| > 0.15% | +10 bull (dikurangi jadi +6 jika funding sudah tinggi) |
| > 0.08% | +5 bull |

**OI Magnitude Tiebreaker**:
- Maksimum +4 pts, hanya digunakan saat `bull_pts == bear_pts`

**Cap**: `bull = min(bull, 35)`, `bear = min(bear, 35)`

### 5.3 Liquidation Analyzer (`engine/analyzers/liquidation_analyzer.py`)

**Tanpa data WS real** (mode proxy berbasis OI):

| OI USD | Poin Base |
|---|---|
| > $500 juta | 10 pts |
| > $200 juta | 8 pts |
| > $100 juta | 6 pts |
| > $10 juta | 4 pts |
| Lainnya | 2 pts |

Poin didistribusikan ke arah berdasarkan funding (funding positif → lebih banyak ke bear/short squeeze risk).

**Dengan data WS real** (jika tersedia):

| Kondisi | Poin |
|---|---|
| long_liq_above > short_liq_below × 1.5 | +12 bear |
| short_liq_below > long_liq_above × 1.5 | +12 bull |

**Cascade Risk Bonus**:

| Cascade Risk | Poin |
|---|---|
| > 0.5 | +6 pts ke sisi dominan |
| > 0.2 | +4 pts ke sisi dominan |

### 5.4 Orderbook Analyzer (`engine/analyzers/orderbook_analyzer.py`)

**Imbalance (bid vs ask depth)**:

| Imbalance | Poin |
|---|---|
| > 0.50 (sangat bullish) | +14 hingga +18 bull |
| > 0.25 | +8 hingga +12 bull |
| > 0.10 | +3 hingga +5 bull |

Simetris untuk bear.

**VWAP Deviation**:

| Harga vs VWAP | Sinyal | Poin |
|---|---|---|
| > 0.5% di atas VWAP | Overbought | +10 bear |
| > 0.2% di atas VWAP | Momentum | +10 bull |
| > 0.5% di bawah VWAP | Oversold | +10 bull |
| > 0.2% di bawah VWAP | Momentum | +10 bear |

**Dollar Depth Asymmetry**:

| Kondisi | Poin |
|---|---|
| Bid depth > 65% total | +5 bull |
| Bid depth > 55% total | +3 bull |

**Wall Detection**: Bid wall atau ask wall yang signifikan = +3 pts ke arah yang relevan.

**CVD (Cumulative Volume Delta, 100 trades terakhir)**:

| Kondisi | Poin |
|---|---|
| Akumulasi kuat | +2 hingga +8 bull |
| Distribusi kuat | +2 hingga +8 bear |

**Cap**: `bull = min(bull, 30)`, `bear = min(bear, 30)`

### 5.5 Scorer Scalper (`_run_scalper()` di scoring_engine.py)

Mode scalper menggunakan pendekatan berbeda — lebih cepat dan berbasis data 1 menit:

| Sinyal | Sumber Data | Keterangan |
|---|---|---|
| OB Imbalance | Orderbook real-time | Bias instan bid/ask |
| EMA 8/21 | Candle 1m | Trend jangka pendek |
| RSI 14 | Candle 1m | Overbought/oversold |
| CVD | 100 trades terakhir | Net buying/selling pressure |
| Volume surge | Candle 1m | Lonjakan volume vs average |
| HH/HL structure | Candle 1m | Higher highs, higher lows |
| 15m MTF confirmation | Candle 15m | Multi-timeframe confirmation |

### 5.6 Vol Regime & Multiplier

Dihitung oleh `_fetch_vol_regime()`:
1. Ambil 24 candle 1h dari Hyperliquid
2. Hitung realized vol: `std_dev(returns) × sqrt(24)`
3. Cache 60 menit (memory + SQLite)

| Vol Regime | Condition | Multiplier |
|---|---|---|
| LOW_VOL | vol < threshold_low | × 1.10 |
| NORMAL | threshold_low ≤ vol < threshold_high | × 1.00 |
| HIGH_VOL | threshold_high ≤ vol < extreme_threshold | × 0.90 |
| EXTREME | vol ≥ extreme_threshold | × 0.80 |

*(Nilai threshold spesifik berasal dari `config.py` — tidak terlihat eksplisit dalam kode analyzer, disimpan di VolatilityConfig)*

### 5.7 Session Threshold Adjustment (v7.0.1)

`_get_session_bonus()` sebelumnya menambahkan poin langsung ke skor (session bonus). **Sejak v7.0.1, session tidak lagi mengubah skor — melainkan menggeser threshold entry.**

Fungsi sekarang mengembalikan **3 nilai**: `(session_bonus=0, reasons, session_threshold_delta)`.

| Sesi | Jam UTC | Threshold Delta | Efek |
|---|---|---|---|
| New York | 13:00–21:00 | −5 | Threshold lebih rendah → lebih mudah masuk |
| London | 08:00–17:00 | −2 | Threshold sedikit lebih rendah |
| Asia | 22:00–07:00 | +5 | Threshold lebih tinggi → lebih sulit masuk |

*Pendekatan ini mencegah inflasi skor: sinyal lemah tidak bisa "diselamatkan" oleh session bonus.*

### 5.7a OI-Tier Threshold Adjustment (v7.0.1)

Selain session, **ukuran OI aset** juga menggeser threshold entry secara dinamis. Ini menggantikan `oi_magnitude_bonus` lama yang menginflasi skor.

| OI USD | Threshold Delta | Rasional |
|---|---|---|
| > $1 miliar (BTC, ETH) | +3 | Pasar efisien, perlu sinyal lebih kuat |
| > $200 juta (SOL, HYPE) | +1 | Sedikit lebih efisien |
| > $50 juta | 0 | Netral |
| > $10 juta | −2 | Lebih eksplosif, threshold lebih rendah |
| < $10 juta (micro-cap) | −3 | Paling eksplosif, threshold terendah |

### 5.8 Formula Skor Final

```python
final_score = int(raw_score * vol_multiplier * trend_multiplier) + meta_delta
```

Di mana:
- `raw_score` = `max(total_bull, total_bear)` yang sudah dinormalisasi ke 0–100
- `vol_multiplier` = 0.80 – 1.20 berdasarkan volatilitas regime
- `trend_multiplier` = 0.90 – 1.10 berdasarkan trend alignment
- `meta_delta` = +8 atau -12 dari meta-learning (capped ±10 sejak v7.0.1)

**Session tidak lagi masuk ke formula skor.** Session dan OI-tier menggeser `threshold` yang dibandingkan dengan skor, bukan skor itu sendiri:

```python
effective_threshold = base_threshold + session_threshold_delta + oi_threshold_delta
# Entry hanya terjadi jika: final_score >= effective_threshold
```

---

## 6. Filter Entry

### 6.1 Tabel Filter Entry Lengkap

| # | Filter | Logika | Nilai | Keterangan |
|---|---|---|---|---|
| 1 | Skor minimum LONG (standard) | `score < min_score_to_signal` | **45** (v7.0.1, was 55) | Tidak ada notif/trade |
| 2 | Skor minimum SHORT (standard) | `score < min_score_short_signal` | **72** (NEW) | SHORT butuh skor lebih tinggi |
| 3 | Skor minimum (scalper) | `score < effective_threshold` | ~50 ± adj | Threshold dipengaruhi session+OI |
| 4 | Skor auto-trade LONG | `score < min_score_to_auto_trade` | **52** (v7.0.1, was 60) | Hanya notif, tidak eksekusi |
| 5 | Skor auto-trade SHORT | `score < min_score_short_auto` | **75** (NEW) | SHORT auto-execute threshold |
| 6 | Jam diblokir | `hour in BLOCKED_HOURS_UTC` | [8, 9] UTC | Seluruh jam tersebut diblokir |
| 7 | Konsensus 3-of-4 | `signals_agree >= 3 dari 4` | — | OI, Liq, OB, Momentum harus sepakat |
| 8 | Bull-Bear Gap (LONG) | `bull - bear >= min_bull_bear_gap` | 18 | Gap minimum untuk long |
| 9 | Bull-Bear Gap (SHORT) | `bear - bull >= min_bull_bear_gap_short` | 20 | Gap lebih ketat untuk short |
| 10 | Kill switch | `risk_state.kill_switch == True` | — | Semua entry diblokir |
| 11 | Bot di-pause | `risk_state.is_paused == True` | — | Entry diblokir |
| 12 | Post-loss cooldown | `now < cooldown_until` | 5 jam | Setelah daily loss > 50% |
| 13 | Max posisi | `open_positions >= max_concurrent` | 10 (std) / 3 (scalper) | Tidak buka posisi baru |
| 14 | Aset duplikat | `asset already in open positions` | — | Tidak boleh double entry |
| 23 | Per-asset repeat guard | `asset_trade_count >= 2 hari ini` | **2 trade/asset/hari** | Blokir aset yang sudah 2× hari ini (v7.1.0) |
| 24 | Per-asset cooldown | `setelah trade ke-2 di asset yang sama` | **2 jam** cooldown | Setelah 2 jam lewat, tetap blokir sampai besok (v7.1.0) |
| 15 | Daily loss limit | `daily_loss >= daily_loss_limit_pct` | 80% paper / 5% live | Soft pause |
| 16 | Daily loss hard | `daily_loss >= daily_loss_hard_pct` | 90% paper / **15% live** (v7.0.1) | Hard stop |
| 17 | Max drawdown | `drawdown >= max_drawdown_pct` | 95% paper / **25% live** (v7.0.1) | Kill switch otomatis |
| 18 | Margin check | Equity cukup untuk posisi baru | — | Verifikasi sebelum order |
| 19 | AI Edge filter | `edge < 45% AND model.is_ready` | 45% | Blokir jika AI prediksi kalah |
| 20 | Expected Value | `EV < 0.001` | — | EV = (winrate × TP) − (lossrate × SL) |
| 21 | Short filters | 3 filter protektif untuk short | — | Lihat detail di bawah |
| 22 | FULL_AUTO mode | `FULL_AUTO = True` | — | Hardcoded True di config |

### 6.2 Filter Short Khusus

Untuk membuka posisi SHORT, diperlukan tambahan (v7.0.1 diperketat berdasarkan data audit: SHORT WR 57.6% tapi net −$12.55):

1. `ALLOW_SHORT = True` (di `config.py` — saat ini `True`)
2. **Threshold lebih tinggi**: Skor ≥ 72 untuk notif, ≥ 75 untuk auto-execute (vs 45/52 untuk LONG)
3. Filter protektif 1: Funding rate tidak terlalu negatif (mencegah short squeeze)
4. Filter protektif 2: Bull-bear gap minimum 20 (lebih ketat dari LONG)
5. Filter protektif 3: Vol regime tidak EXTREME (short di volatilitas ekstrem diblokir)

*Alasan pengetatan: Data menunjukkan bias struktural di Hyperliquid — funding rate positif dan basis spot-perp hampir selalu menguntungkan LONG. SHORT hanya dieksekusi pada sinyal sangat high-conviction.*

---

## 7. Manajemen Posisi

### 7.1 Entry

**Paper Mode**:
- Harga fill = mark_price + 0.03% spread + random noise kecil
- Liquidation price: `entry × (1 - 1/leverage + 0.005)` untuk LONG

**Live Mode**:
- Order POST_ONLY (maker-only) — tidak sweep orderbook
- Retry 3 kali, lalu fallback ke IOC jika POST_ONLY gagal
- Leverage isolated diset SEBELUM order ditempatkan; abort jika gagal
- SL on-chain ditempatkan segera setelah entry berhasil
- **v7.0.1 — Verifikasi fill dari chain** (`_wait_for_fill`): Setelah order meninggalkan buku, bot memeriksa posisi aktual dari chain (`assetPositions`). Jika posisi tidak ditemukan di chain, order dianggap unfilled/dibatalkan — bukan diasumsikan filled 100% seperti sebelumnya.
- **v7.0.1 — Sync posisi saat startup** (`sync_positions_from_chain`): Live mode memanggil `load_from_chain()` saat inisialisasi untuk recovery posisi orphan jika bot pernah crash.

### 7.2 Stop Loss (Rule A)

| Mode | SL Pct | Keterangan |
|---|---|---|
| Standard (paper) | 2.0% | `paper_sl_pct` di config |
| Standard (live) | 3.0% | `default_sl_pct` di config |
| Scalper (Normal/Low-Vol) | **1.0%** | Floor dinaikkan dari 0.70% (v7.1.0) |
| Scalper (High-Vol/Extreme) | **1.5%** | Floor lebih tinggi saat volatile (v7.1.0) |

**Vol-aware SL (Scalper)** — ATR-based dengan regime-aware floor:
```
atr14_pct = realized_vol / sqrt(24)   # per-candle 1h ATR
sl_pct    = max(SL_FLOOR, min(atr14_pct × 1.5, 2.0%))

SL_FLOOR:
  Normal/Low-Vol  → 1.0%  (was 0.70%)
  High-Vol/Extreme → 1.5% (was 1.2%)
```
Alasan kenaikan floor: `vol_cache` TTL 60 menit — regime bisa berubah ke HIGH_VOL sementara `realized_vol` cache masih rendah, menghasilkan SL terlalu ketat.

**Vol-aware SL (Standard)** (via `calculate_levels()`):
```
sl_pct = max(realized_vol × noise_multiplier, sl_floor)
sl_pct = min(sl_pct, 0.08)  # Cap maksimum 8%
```
Pada sesi NY: SL dilebarkan ×1.20 (karena volatilitas lebih tinggi).

### 7.3 Take Profit

**Standard Mode**:

| TP | Persentase Move | Rasio Close |
|---|---|---|
| TP1 | TP1 pct (14% dari harga) dari entry | Tutup 25% posisi |
| TP2 | TP2 pct (25% dari harga) dari entry | Tutup 50% sisa posisi |

Nilai eksak dari `config.py`:
- `tp1_pct = 0.014` (1.4%)
- `tp2_pct = 0.025` (2.5%)
- `tp1_close_ratio = 0.25` (tutup 25% di TP1)
- `tp2_close_ratio = 0.50` (tutup 50% sisa di TP2)

**Scalper Mode** (v7.1.0 — disesuaikan proporsional dengan SL floor baru):

| TP | Persentase Move | Rasio Close | R:R |
|---|---|---|---|
| TP1 | **1.43%** dari entry (was 1.0%) | Tutup 55% posisi | 1.43× |
| TP2 | **2.14%** dari entry (was 1.5%) | Tutup 75% sisa | 2.14× |

Trailing: **0.50%** (was 0.35%). R:R identik dengan sebelumnya — hanya skala naik proporsional dengan SL floor 1.0%.

### 7.4 Trailing Stop (Rule D)

Trailing stop aktif setelah TP1 tercapai (posisi tidak tertutup penuh):

| Fase | Trail Distance |
|---|---|
| Sebelum TP2 tercapai | 0.5× realized vol |
| Setelah TP2 tercapai | 0.3× realized vol (lebih ketat) |

### 7.5 Time-Based Exit (Rule E & F)

**Scalper Mode (Rule E)**:
- Hold maksimum: **20 menit** (`max_hold_minutes`)
- Grace period: **+8 menit** tambahan (`max_hold_grace_minutes`)
- Setelah 28 menit total: exit paksa pada harga pasar

**Standard Mode (Rule F)** — 3 kondisi time exit:
1. **Pullback sinyal**: Setelah 30 menit, jika harga pullback >15% dari jarak ke TP1 → exit
2. **Flatline**: Jika tidak bergerak <0.15% dalam 45 menit → exit (posisi tidak produktif)
3. **Hard limit**: 6 jam maksimum hold time untuk standard mode

*Catatan: `time_based_exit_hours = 8.0` ada di config, namun Rule F menggunakan 6 jam di kode `check_tp_trail()`. Ini adalah diskrepansi antara config dan implementasi.*

### 7.6 Breakeven Protection (Rule B)

Setelah TP1 tercapai:
- SL dipindahkan ke **entry + 0.1%** (breakeven + buffer kecil)
- Untuk live mode: SL on-chain di-cancel dan ditempatkan ulang di breakeven

### 7.7 Ringkasan Rules check_tp_trail()

| Rule | Trigger | Aksi |
|---|---|---|
| Rule A | Harga melewati SL | Tutup 100% posisi |
| Rule B | TP1 tercapai | Tutup 25%, pindahkan SL ke BE+0.1% |
| Rule C | TP2 tercapai | Tutup 50% sisa |
| Rule D | Trailing stop hit | Tutup sisa posisi |
| Rule E | Max hold (scalper) | Exit paksa setelah 28 menit |
| Rule F | Time/momentum (standard) | Exit jika flatline/pullback/6h |

---

## 8. Manajemen Risiko

### 8.1 Position Sizing

Formula utama:

```python
risk_usd = equity × risk_pct × ai_multiplier
size_usd = risk_usd / (sl_pct × leverage)
size_usd = min(size_usd, equity × 0.35)  # Cap 35% equity
```

**Score-based Risk Pct** (`get_risk_pct()`):

| Skor | Risk Pct |
|---|---|
| ≥ 75 | 3.5% |
| ≥ 68 | 3.0% |
| ≥ 60 | 2.5% |
| < 60 | 2.0% |

**Equity Protection Multiplier**:

| Kondisi Equity | Multiplier |
|---|---|
| Equity ≥ 1.5× peak | × 0.8 (proteksi profit) |
| Equity ≤ 0.8× peak | × 0.5 (drawdown protection) |
| Normal | × 1.0 |

**AI Multiplier** (dari Intelligence Model):
- Formula: `1.0 + ((edge - 0.5) × 0.3)`
- Range: clamped ke [0.85, 1.15]
- Jika model belum ready: multiplier = 1.0 (netral)

### 8.2 Leverage

| Mode | Default | Maksimum |
|---|---|---|
| Standard | 10× | 10× |
| Scalper | 25× | 35× |

Triple-cap pada leverage:
1. Signal leverage (dari sinyal)
2. User leverage (dari pengaturan pengguna)
3. Exchange leverage (maksimum yang diizinkan exchange untuk aset tersebut)

Leverage final = `min(signal_lev, user_lev, exchange_max_lev)`

### 8.3 Daily Loss Limits

| Parameter | Paper | Live (v7.0.1) |
|---|---|---|
| `daily_loss_limit_pct` | 80% | 5% |
| `daily_loss_hard_pct` | 90% | **15%** (was 8%) |
| `max_drawdown_pct` | 95% | **25%** (was 20%) |

- Soft limit: Bot di-pause, user dinotifikasi
- Hard limit: Kill-switch diaktifkan, semua entry diblokir
- Cooldown: Dipicu jika daily loss > 50%, durasi **5 jam** (`post_loss_cooldown_hrs`)
- **Kill-switch live mode**: Tidak akan auto-reset — harus di-reset manual. Paper mode tetap bisa auto-reset.
- Limit live mode diambil dari environment variables (`KARA_LIVE_MAX_DRAWDOWN_PCT`, `KARA_LIVE_DAILY_LOSS_HARD_PCT`), bisa dikonfigurasi tanpa ubah kode.

### 8.4 Paper Mode Balance

```python
PAPER_BALANCE_USD = 1_000_000 / 16_000  # = $62.50
```

Saldo awal paper mode adalah **$62.50** (bukan $1 juta). Pembagian 16.000 adalah hardcoded.

### 8.5 Expected Value Filter

Sebelum membuka posisi, dihitung EV:

```python
EV = (win_prob × tp2_pct × 0.70) - (loss_prob × sl_pct)
```

- Jika `EV < 0.001` → entry diblokir
- `win_prob` berasal dari AI edge prediction (atau 0.5 jika model belum ready)

---

## 9. Mode Trading

### 9.1 Execution Mode

| Mode | Keterangan | Konfigurasi |
|---|---|---|
| `SEMI_AUTO` | Sinyal dikirim ke Telegram, user harus setujui | Default |
| `FULL_AUTO` | LONG skor ≥ **52**, SHORT skor ≥ **75** langsung dieksekusi | `FULL_AUTO = True` di config |

*Catatan: `FULL_AUTO = True` hardcoded di `config.py`. Threshold auto-execute dipisah antara LONG (52) dan SHORT (75) sejak v7.0.1.*

### 9.2 Trading Mode

| Mode | Aset Target | Scan Interval | Max Hold | Leverage | Max Posisi |
|---|---|---|---|---|---|
| Standard | Semua aset kecuali SCALPER_ASSETS | 60 detik | 6–8 jam | 10× | 10 |
| Scalper | SCALPER_ASSETS saja | 15 detik | 28 menit | 25–35× | 3 |

**SCALPER_ASSETS** (dari `config.py`):
```python
SCALPER_ASSETS = ["ZEC", "kBONK", "SPX", "COMP", "REZ", "PYTH", "MON", "VVV"]
```

### 9.3 Market Mode (Paper vs Live)

| Aspek | Paper Mode | Live Mode |
|---|---|---|
| Exchange | Hyperliquid Testnet | Hyperliquid Mainnet |
| Dana nyata | Tidak | Ya |
| SL on-chain | Tidak | Ya (selalu) |
| Recovery setelah restart | Dari SQLite | Dari chain (`load_from_chain()`) |
| Saldo awal | $62.50 (simulasi) | Saldo akun sebenarnya |

### 9.4 Perpindahan Mode

Mode dapat diganti saat runtime via Telegram tanpa restart:
- `/scalper` — beralih ke scalper mode
- `/standard` — beralih ke standard mode
- `/paper` — beralih ke paper execution
- `/live` — beralih ke live execution

Dikelola oleh `core/mode_manager.py` (singleton `ModeManager`).

---

## 10. Intelligence Model (AI)

### 10.1 Algoritma

**Model**: `HistGradientBoostingClassifier` (scikit-learn)

```python
HistGradientBoostingClassifier(
    max_iter=100,
    learning_rate=0.05,
    early_stopping=True,
    class_weight="balanced"
)
```

**Keunggulan**: Native support untuk missing values (NaN), cepat, mendukung data tidak seimbang.

### 10.2 Features (Input Model)

9 fitur dari `intelligence/feature_engine.py`:

| # | Feature | Sumber |
|---|---|---|
| 1 | `score` | Skor sinyal (0–100) |
| 2 | `meta_delta` | Output meta-learning |
| 3 | `oi_score` | Poin dari OI+Funding analyzer |
| 4 | `liq_score` | Poin dari Liquidation analyzer |
| 5 | `ob_score` | Poin dari Orderbook analyzer |
| 6 | `session_bonus` | Bonus/penalti sesi |
| 7 | `funding_rate` | Funding rate aktual |
| 8 | `realized_vol` | Volatilitas terealisasi |
| 9 | `trend_pct` | Persentase trend |

### 10.3 Training

| Parameter | Nilai |
|---|---|
| Min samples untuk training | 300 (`INTELLIGENCE_RETRAIN_MIN_SAMPLES`) |
| Interval retrain | Setiap 12 jam (`INTELLIGENCE_RETRAIN_INTERVAL_HOURS`) |
| Train/test split | 80/20 |
| Trigger retrain | Setelah close posisi jika samples ≥ 300 |

**Guard conditions** sebelum model dianggap valid:
- Kedua kelas (win/loss) harus ada di data training
- Win rate antara 10%–90% (hindari data bias)
- Test accuracy tidak boleh > 90% (deteksi overfit)

### 10.4 Status Model

- `is_ready = False` saat startup
- `is_ready = True` hanya setelah `retrain()` berhasil dalam sesi ini
- Jika `is_ready = False`: `predict_edge()` mengembalikan 0.5 (netral)
- Model disimpan ke file: `kara_intelligence.pkl`

### 10.5 Experience Buffer (`intelligence/experience_buffer.py`)

Database terpisah: `kara_ml.db`, tabel `ml_experience`

**Kolom yang disimpan**:

| Kolom | Isi | Kapan Diisi |
|---|---|---|
| `pos_id` | ID posisi | Saat entry |
| `chat_id` | ID user Telegram | Saat entry |
| `timestamp` | Waktu entry | Saat entry |
| `asset` | Nama aset | Saat entry |
| `side` | LONG/SHORT | Saat entry |
| `score` | Skor sinyal | Saat entry |
| `meta_delta` | Output meta | Saat entry |
| `oi_score` | Skor OI | Saat entry |
| `funding_score` | Skor funding | Saat entry |
| `liq_score` | Skor liq | Saat entry |
| `ob_score` | Skor OB | Saat entry |
| `session_bonus` | Bonus sesi | Saat entry |
| `funding_rate` | Funding aktual | Saat entry |
| `realized_vol` | Realized vol | Saat entry |
| `trend_pct` | Trend pct | Saat entry |
| `expected_edge` | AI prediction | Saat entry |
| `actual_pnl_pct` | PnL aktual | Saat close |
| `duration_sec` | Durasi posisi | Saat close |
| `is_win` | NULL → 0/1 | NULL saat entry, diisi saat close |

### 10.6 Dynamic Risk Multiplier (`intelligence/dynamic_risk.py`)

```python
multiplier = 1.0 + ((edge - 0.5) × 0.3)
multiplier = clamp(multiplier, 0.85, 1.15)
```

Contoh:
- Edge 70% → multiplier 1.06 (naik 6%)
- Edge 30% → multiplier 0.94 (turun 6%)
- Edge 50% → multiplier 1.0 (netral)

### 10.7 History Ingestor (`intelligence/history_ingestor.py`)

Utilitas untuk mengimpor riwayat trading dari file Excel ke `kara_ml.db` — berguna untuk bootstrap training ketika bot baru dimulai dan belum punya data live.

---

## 11. Meta Scoring

### 11.1 Cara Kerja

Meta-learning melacak performa historis per **pattern key**:

```python
pattern_key = f"{mode}_{asset}_{side}"
# Contoh: "standard_BTC_LONG"
```

### 11.2 Logika Penerapan (`_apply_meta_learning()`)

```python
# Baca dari tabel meta_pattern_stats di SQLite
stats = db.get_meta_pattern_stats(chat_id, pattern_key)

if stats.win_rate >= threshold_good:
    return +8  # Pattern ini terbukti profitable
elif stats.win_rate <= threshold_bad:
    return -12  # Pattern ini terbukti merugi
else:
    return 0   # Netral, tidak cukup data
```

- Delta **+8**: Pattern berkinerja baik
- Delta **-12**: Pattern berkinerja buruk  
- Delta **0**: Belum cukup data (min samples diperlukan)

**Parameter meta-learning v7.0.1** (dikalibrasi ulang):

| Parameter | Nilai Lama | Nilai Baru | Alasan |
|---|---|---|---|
| `meta_min_samples` | 5 | **10** | n=5 terlalu noisy; n=10 memberikan confidence interval yang lebih baik |
| `meta_boost_threshold` | 0.68 | 0.68 | Tidak berubah |
| `meta_penalty_threshold` | 0.45 | **0.35** | Lebih agresif mendeteksi pattern buruk |
| `meta_max_delta` | 15 | **10** | Nudge lebih kecil, dampak noise lebih rendah |

### 11.3 Update Meta Stats

Diperbarui setelah setiap posisi ditutup:
- Win → incrementkan `wins` dan `total` untuk pattern_key tersebut
- Loss → incrementkan hanya `total`
- Win rate = `wins / total`

Data disimpan di tabel `meta_pattern_stats` di SQLite utama.

---

## 12. Telegram Bot

### 12.1 Setup

- Library: `python-telegram-bot` v21+
- Access code: `KARA2026`
- Maksimum percobaan masuk: 3 kali
- Blokir akses setelah 3 percobaan gagal: **1 jam**
- TOS consent: Diperlukan saat pertama kali menggunakan bot

### 12.2 Daftar Perintah

| Perintah | Fungsi |
|---|---|
| `/start` | Mulai bot, cek access code, tampilkan menu utama |
| `/help` | Tampilkan semua perintah yang tersedia |
| `/status` | Status bot, balance, daily PnL, mode aktif |
| `/pos` | Daftar posisi terbuka saat ini |
| `/journal` | Riwayat trading (posisi yang sudah tutup) |
| `/export` | Export riwayat ke format CSV |
| `/mode` | Tampilkan mode saat ini (paper/live, std/scalper) |
| `/scalper` | Beralih ke scalper mode |
| `/standard` | Beralih ke standard mode |
| `/paper` | Beralih ke paper execution mode |
| `/live` | Beralih ke live execution mode |
| `/settings` | Lihat dan ubah pengaturan (leverage, dll) |
| `/signal` | Request manual scan sinyal |
| `/setleverage <n>` | Set leverage default pengguna |
| `/setmaxpos <n>` | Set jumlah maksimum posisi bersamaan |
| `/resetml` | Reset ML model dan experience buffer |

### 12.3a Perubahan Tombol Posisi (v7.0.1)

Tombol close posisi di Telegram diperbarui tampilannya:
- Sebelum: `❌ BTC L` / `❌ ETH S`
- Sesudah: `Close BTC 📈` / `Close ETH 📉`
- Tombol "Close All Positions" menghilangkan emoji berlebih
- Tombol **Batal** di flow `/live` kini berfungsi dengan benar (callback `close_settings` ditambahkan)

### 12.3 Notifikasi Otomatis

Bot mengirimkan notifikasi Telegram secara otomatis untuk:
- Sinyal baru terdeteksi (dengan breakdown skor)
- Posisi dibuka
- TP1 / TP2 tercapai
- Trailing stop terkena
- Posisi ditutup (dengan PnL card PNG)
- Daily summary (daily card PNG)
- Kill switch diaktifkan
- Alert drawdown/daily loss

### 12.4 PnL Card (`notify/pnl_card.py`)

Gambar PNG 900×560 piksel yang dihasilkan saat posisi ditutup:
- Pill LONG/SHORT (berwarna)
- Nama aset
- PnL % (font 88px — hero number)
- PnL dalam USD
- Harga entry dan exit
- Durasi hold
- Skor sinyal
- Session PnL
- Total equity
- Tag alasan exit (SL/TP1/TP2/Trail/Time)
- Karakter KARA di sisi kanan

### 12.5 Daily Card (`notify/daily_card.py`)

Gambar PNG 900×560 piksel untuk laporan harian:
- Tanggal
- Daily PnL % (hero number besar)
- Balance awal dan akhir hari
- Jumlah total trade, win, loss
- Win rate %
- Trade terbaik dan terburuk hari itu
- Max drawdown harian
- Badge mode trading

---

## 13. Dashboard Web

### 13.1 Teknologi

- **Framework**: FastAPI
- **UI**: HTML dengan Tailwind CSS
- **Chart**: LightweightCharts (TradingView)
- **Real-time**: WebSocket (`/ws`)

### 13.2 API Endpoints

| Endpoint | Method | Keterangan |
|---|---|---|
| `/api/ping` | GET | Health check |
| `/api/health` | GET | Status bot dan komponen |
| `/api/overview` | GET | Overview trading: balance, PnL, posisi |
| `/api/history` | GET | Riwayat trading |
| `/api/users` | GET | Daftar user aktif |
| `/api/ml_decision_feed?chat_id=` | GET | 30 trade terakhir per user (v7.1.0: filter per-user + dedup) |
| `/api/ml_status` | GET | Status model ML |
| `/api/ml_export?type=` | GET | Export CSV ML data |
| `/ws` | WebSocket | Update real-time |

### 13.3 Fitur Dashboard

- Grafik harga real-time menggunakan LightweightCharts
- Overview balance dan PnL
- Daftar posisi terbuka
- Riwayat trading
- Status komponen sistem

*Catatan: Dashboard dibaca secara partial (150 baris pertama). Fitur lengkap mungkin lebih dari yang tercantum di sini.*

---

## 14. Database & Persistensi

### 14.1 File Database

| File | Tipe | Isi |
|---|---|---|
| `kara_data.db` | SQLite | Data utama bot |
| `kara_ml.db` | SQLite | Experience buffer untuk ML |
| `kara_intelligence.pkl` | Pickle | Model ML terlatih |
| `users.json` | JSON | Data pengguna (encrypted secrets) |

### 14.2 Tabel SQLite Utama (`kara_data.db`)

| Tabel | Isi |
|---|---|
| `vol_cache` | Cache volatilitas regime per aset |
| `paper_positions` | Posisi paper trading terbuka dan tertutup |
| `paper_state` | State akun paper (balance, equity, dll) |
| `signals_history` | Riwayat sinyal yang dihasilkan |
| `risk_state` | State risiko (daily loss, drawdown, kill switch, **asset_trade_times** v7.1.0) |
| `history_snapshots` | Snapshot historis untuk analisis |
| `meta_pattern_stats` | Statistik win/loss per pattern key |
| `oi_snapshots` | Snapshot OI per cycle |
| `trade_history` | Riwayat trade yang sudah selesai |

### 14.3 Enkripsi

- Wallet secret (private key agent) dienkripsi menggunakan **Fernet** (symmetric encryption)
- Kunci Fernet diambil dari environment variable

### 14.4 Hard Reset

```bash
KARA_HARD_RESET=true python main.py
```

Fungsi `hard_reset_all_data()`:
- Hapus semua tabel SQLite di `kara_data.db`
- Hapus `kara_ml.db`
- Hapus `kara_intelligence.pkl`
- **Tidak menghapus** `users.json` (akun pengguna tetap)

---

## 15. Deployment

### 15.1 Docker

```dockerfile
# docker-compose.yml tersedia
# Volume persistent dipasang untuk data SQLite
```

**Persistent volume**: Data SQLite dan model ML disimpan di volume agar tidak hilang saat container restart.

### 15.2 Railway

Bot didesain untuk deployment di **Railway.app**:
- Support volume persistent
- Environment variables melalui Railway dashboard
- Auto-restart jika crash

### 15.3 Environment Variables

| Variable | Keterangan |
|---|---|
| `HL_SECRET_KEY` | Private key Hyperliquid wallet |
| `HL_ACCOUNT_ADDRESS` | Alamat wallet Hyperliquid |
| `TELEGRAM_TOKEN` | Token Telegram Bot |
| `KARA_MODE` | `paper` atau `live` |
| `KARA_HARD_RESET` | `true` untuk reset penuh |
| `HL_FERNET_KEY` / `FERNET_KEY` | Kunci enkripsi Fernet (v7.0.1: bug double-assignment diperbaiki, `HL_FERNET_KEY` prioritas) |
| `KARA_LIVE_MAX_DRAWDOWN_PCT` | Max drawdown live mode (default: 0.25 = 25%) |
| `KARA_LIVE_DAILY_LOSS_HARD_PCT` | Daily loss hard cap live mode (default: 0.15 = 15%) |

### 15.4 Requirements

File `requirements.txt` mencakup dependency utama:
- `hyperliquid-python-sdk`
- `python-telegram-bot>=21.0`
- `fastapi` + `uvicorn`
- `aiosqlite`
- `scikit-learn`
- `numpy`, `pandas`
- `Pillow`
- `httpx`
- `python-dotenv`
- `cryptography`
- `pydantic>=2.0`

---

## 16. Keterbatasan yang Diketahui

### 16.1 Implementasi Aktif tapi Mungkin Belum Optimal

| Fitur | Status | Catatan |
|---|---|---|
| `FULL_AUTO = True` hardcoded | Aktif | Default True, threshold LONG/SHORT sekarang berbeda |
| Paper balance $62.50 | Aktif | Hasil dari `1_000_000 / 16_000` — angka tidak intuitif |
| AI Edge filter `< 45%` | Aktif hanya jika `is_ready=True` | Selama startup, tidak ada filtering AI |
| `time_based_exit_hours = 8.0` di config | Diskrepansi | Rule F menggunakan 6 jam, bukan 8 jam |
| Meta min_samples | **Diubah ke 10** (v7.0.1) | Lebih baik dari sebelumnya (5), tapi n=10 masih relatif kecil |
| Skor LONG threshold | **Diturunkan ke 45/52** (v7.0.1) | Dikalibrasi setelah session bonus dihapus dari skor (deflasi ~13-15 pts) |

### 16.2 Keterbatasan Teknis

| Keterbatasan | Keterangan |
|---|---|
| Liquidation data | Jika WS liquidation tidak tersedia, digunakan proxy OI — kurang akurat |
| dashboard/app.py | Hanya 150 baris yang dibaca — fitur tambahan mungkin ada |
| telegram.py | Sangat besar (>40K token) — hanya 500 baris yang dibaca; beberapa handler mungkin terlewat |
| hyperliquid_client.py | Hanya 200 baris pertama yang dibaca — implementasi lengkap mungkin berbeda |
| Testnet vs Mainnet | API testnet Hyperliquid sering mengalami perubahan struktur response |

### 16.3 Fitur yang Ada di Kode tapi Belum Diverifikasi Aktif

| Fitur | File | Status |
|---|---|---|
| History Ingestor (import Excel) | `intelligence/history_ingestor.py` | Utilitas manual, tidak dipanggil otomatis |
| Backtester | `backtest/backtester.py` | Modul terpisah, tidak terintegrasi ke loop utama |
| WebSocket `userEvents` | `data/ws_client.py` | Tersedia tapi hanya berguna untuk live mode |
| `load_from_chain()` recovery | `execution/live_executor.py` | Hanya dipanggil saat startup di live mode |

### 16.4 Risiko Operasional

| Risiko | Mitigasi |
|---|---|
| Bot crash saat posisi terbuka | Live mode: SL on-chain tetap aktif + sync dari chain saat restart; Paper mode: dari SQLite |
| Hyperliquid API down | 3-layer fallback, circuit breaker 502 |
| Telegram token tidak valid | Graceful fallback — bot jalan tanpa Telegram |
| Rate limit API | Semaphore 8 concurrent + throttle 0.12s |
| WS disconnect | Auto-reconnect exponential backoff hingga 120s; v7.0.1: WS loop tidak pernah mati, kirim Telegram alert jika max retry tercapai |

---

## 17. Ringkasan Fitur

### 17.1 Matriks Fitur Lengkap

| Kategori | Fitur | Status |
|---|---|---|
| **Signal** | Multi-factor scoring (0–100) | ✅ Aktif |
| **Signal** | OI + Funding analyzer | ✅ Aktif |
| **Signal** | Liquidation analyzer | ✅ Aktif |
| **Signal** | Orderbook analyzer | ✅ Aktif |
| **Signal** | Scalper fast scorer (EMA/RSI/CVD) | ✅ Aktif |
| **Signal** | Vol regime detection | ✅ Aktif |
| **Signal** | Session bonus/penalty | ✅ Aktif |
| **Signal** | 3-of-4 consensus filter | ✅ Aktif |
| **Signal** | Bull-bear gap filter | ✅ Aktif |
| **Signal** | 15m MTF confirmation (scalper) | ✅ Aktif |
| **Risk** | Score-based position sizing | ✅ Aktif |
| **Risk** | Daily loss limits (soft + hard) | ✅ Aktif |
| **Risk** | Max drawdown kill switch | ✅ Aktif |
| **Risk** | Post-loss cooldown (5h) | ✅ Aktif |
| **Risk** | Per-asset repeat guard (max 2/hari + 2h cooldown) | ✅ Aktif (v7.1.0) |
| **Risk** | Equity protection multiplier | ✅ Aktif |
| **Risk** | Expected Value (EV) filter | ✅ Aktif |
| **Risk** | Vol-aware SL/TP calculation | ✅ Aktif |
| **Risk** | Jam diblokir (8-9 UTC) | ✅ Aktif |
| **Position** | TP1/TP2 partial close | ✅ Aktif |
| **Position** | Trailing stop | ✅ Aktif |
| **Position** | Breakeven protection | ✅ Aktif |
| **Position** | Time-based exit (scalper) | ✅ Aktif |
| **Position** | Time-based exit (standard: flatline/pullback) | ✅ Aktif |
| **Execution** | Paper mode (simulasi) | ✅ Aktif |
| **Execution** | Live mode (Hyperliquid mainnet) | ✅ Aktif |
| **Execution** | POST_ONLY orders (maker) | ✅ Aktif (live) |
| **Execution** | On-chain SL placement | ✅ Aktif (live) |
| **Execution** | Recover dari chain setelah restart | ✅ Aktif (live) |
| **Execution** | FULL_AUTO (auto-execute ≥ 60) | ✅ Aktif (hardcoded) |
| **AI** | HistGradientBoosting classifier | ✅ Aktif |
| **AI** | Dynamic risk multiplier | ✅ Aktif |
| **AI** | Experience buffer (SQLite) | ✅ Aktif |
| **AI** | Auto-retrain setelah 300 samples | ✅ Aktif |
| **AI** | History ingestor (Excel import) | ⚙️ Utilitas manual |
| **Meta** | Pattern-based win rate tracking | ✅ Aktif |
| **Meta** | Meta delta ±8/±12 | ✅ Aktif |
| **Telegram** | Multi-user dengan access code | ✅ Aktif |
| **Telegram** | Semua perintah `/start` – `/resetml` | ✅ Aktif |
| **Telegram** | PnL card PNG per trade | ✅ Aktif |
| **Telegram** | Daily summary card PNG | ✅ Aktif |
| **Telegram** | TOS consent gate | ✅ Aktif |
| **Dashboard** | FastAPI web UI | ✅ Aktif |
| **Dashboard** | WebSocket real-time | ✅ Aktif |
| **Dashboard** | LightweightCharts | ✅ Aktif |
| **Data** | REST API Hyperliquid (3-layer fallback) | ✅ Aktif |
| **Data** | WebSocket real-time (OB/trades/funding/liq) | ✅ Aktif |
| **Data** | Vol regime cache (60 menit) | ✅ Aktif |
| **DB** | SQLite multi-tabel | ✅ Aktif |
| **DB** | Fernet encryption untuk wallet secret | ✅ Aktif |
| **DB** | Hard reset via env var | ✅ Aktif |
| **Deploy** | Docker + docker-compose | ✅ Tersedia |
| **Deploy** | Railway-compatible | ✅ Tersedia |
| **Deploy** | Persistent volume | ✅ Tersedia |
| **Backtest** | Backtesting engine | ⚙️ Modul terpisah, tidak di loop utama |

### 17.2 Nilai Konfigurasi Kunci

| Parameter | Nilai | Keterangan |
|---|---|---|
| KARA_VERSION | **"7.1.0"** | Versi bot |
| PAPER_BALANCE_USD | $62.50 | Saldo simulasi awal |
| FULL_AUTO | True | Auto-execute aktif |
| BLOCKED_HOURS_UTC | [8, 9] | Jam 08:00 dan 09:00 UTC diblokir |
| min_score_to_signal (LONG) | **45** | Threshold notifikasi LONG (was 55) |
| min_score_to_signal (SHORT) | **72** | Threshold notifikasi SHORT (NEW) |
| min_score_to_auto_trade (LONG) | **52** | Threshold eksekusi otomatis LONG (was 60) |
| min_score_to_auto_trade (SHORT) | **75** | Threshold eksekusi otomatis SHORT (NEW) |
| min_bull_bear_gap (LONG) | 18 | Gap minimum untuk LONG |
| min_bull_bear_gap_short | 20 | Gap minimum untuk SHORT |
| Standard leverage | 10× | Default dan maksimum |
| Scalper leverage | 25× default, 35× max | Leverage tinggi untuk scalper |
| Scalper SL floor (Normal) | **1.0%** | ATR-based, was 0.70% (v7.1.0) |
| Scalper SL floor (High-Vol) | **1.5%** | Was 1.2% (v7.1.0) |
| Scalper TP1 | **1.43%** | Was 1.0%, R:R tetap 1.43× (v7.1.0) |
| Scalper TP2 | **2.14%** | Was 1.5%, R:R tetap 2.14× (v7.1.0) |
| Per-asset max trades/hari | **2** | Repeat guard, blokir trade ke-3+ (v7.1.0) |
| Per-asset cooldown | **2 jam** | Setelah trade ke-2, cooldown sebelum total blokir (v7.1.0) |
| Standard risk/trade | 1.0% | Persentase risiko per trade |
| Scalper risk/trade | 4.0% | Lebih agresif |
| Daily loss limit (live) | 5% | Soft stop |
| Daily loss hard (live) | **15%** | Hard stop (was 8%) |
| Max drawdown (live) | **25%** | Kill switch (was 20%) |
| Daily loss limit (paper) | 80% | Soft stop simulasi |
| Cooldown setelah loss | 5 jam | Post-loss-cooldown |
| Max positions (standard) | 10 | Posisi bersamaan |
| Max positions (scalper) | 3 | Posisi bersamaan |
| ML min samples | 300 | Sebelum model ditraining |
| ML retrain interval | 12 jam | Frekuensi retraining |
| Meta min_samples | **10** | Minimum sample sebelum meta aktif (was 5) |
| Meta max_delta | **±10** | Cap meta adjustment (was ±15) |
| Meta penalty threshold | **0.35** | Win rate < 35% = penalti (was 0.45) |
| WS reconnect max | 120 detik | Backoff maksimum |
| Vol cache TTL | 60 menit | Masa aktif cache volatilitas |
| Market metadata cache | 5 menit | Masa aktif cache metadata |
| API throttle sleep | 0.12 detik | Delay antara REST calls |
| API semaphore | 8 concurrent | Maks request bersamaan |

---

---

## 18. Changelog

### v7.1.0 — Memory Guard + SL Floor Fix (8 Mei 2026)

**Root cause**: Bot tidak punya memory per-aset → stuck loop PENDLE/APE/ASTER 10× berturut-turut. Data WR: 1st trade 62% → 2nd 48% → 3rd+ 23%.

| # | Fix | File | Keterangan |
|---|---|---|---|
| 1 | Per-asset repeat guard | `risk/risk_manager.py` | Max 2 trade/asset/hari, blokir trade ke-3+ |
| 2 | Per-asset cooldown 2 jam | `risk/risk_manager.py` | Setelah trade ke-2, cooldown 2 jam lalu blokir sampai besok |
| 3 | Persist counter survive restart | `risk/risk_manager.py` | `_asset_trade_times` disimpan ke DB — Railway redeploy tidak bisa bypass |
| 4 | Scalper SL floor 0.70%→1.00% | `config.py` | ATR rendah selalu kena floor; dinaikkan agar SL lebih realistis |
| 5 | Scalper SL floor High-Vol 1.2%→1.5% | `engine/scoring_engine.py` | vol_cache stale TTL 60m bisa underestimate vol saat HIGH_VOL |
| 6 | TP scalper proporsional | `config.py` | TP1 1.43%, TP2 2.14%, trailing 0.50% — R:R identik (1.43×/2.14×) |
| 7 | Live decision feed dedup | `dashboard/app.py` | Hapus `GROUP BY pos_id` yang menyebabkan duplikat saat `pos_id` NULL |
| 8 | Live decision feed per-user | `dashboard/app.py`, `dashboard/templates/dashboard.html` | Endpoint terima `chat_id` param; frontend pass `S.userId` |

**Catatan teknis repeat guard**:
- `_asset_trade_times: Dict[str, List[float]]` — key format: `"{asset}_{YYYY-MM-DD}"`
- Dipersist via `_persist_risk_state()` dan di-restore saat `_load_risk_state()`
- Key hari kemarin otomatis di-drop saat restore (filter `endswith(today)`)
- `record_asset_trade()` dipanggil di `paper_executor.open_position()` dan `live_executor.open_position()`
- Reset otomatis setiap `reset_daily()` (tengah malam UTC)

---

### v7.0.1 — Live Mode Safety Audit (7 Mei 2026)

**10 critical fixes untuk live trading yang aman:**

| # | Fix | File | Keterangan |
|---|---|---|---|
| 1 | `session.initialize()` dipanggil saat startup | `main.py` | LiveExecutor client sekarang terhubung saat boot |
| 2 | Bug double-assignment `FERNET_KEY` diperbaiki | `config.py` | `HL_FERNET_KEY` tidak lagi di-overwrite oleh `FERNET_KEY` |
| 3 | `sync_positions_from_chain()` saat startup | `live_executor.py` | Recovery posisi orphan jika bot crash sebelumnya |
| 4 | Peringatan CRITICAL jika live user ada tapi mode bukan live | `main.py` | Mencegah live user tidak sengaja di paper mode |
| 5 | SL on-chain ditempatkan setelah setiap entry live | `live_executor.py` | Safety net selalu aktif di exchange |
| 6 | Verifikasi fill dari chain, bukan asumsi 100% | `live_executor.py` | `_wait_for_fill` cek `assetPositions` aktual |
| 7 | Kill-switch live mode tidak auto-reset | `risk_manager.py` | Live mode lebih aman; paper tidak terpengaruh |
| 8 | Risk live mode diperketat via env var | `config.py`, `risk_manager.py` | Max DD 25%, daily hard 15% |
| 9 | WS loop tidak pernah mati; Telegram alert jika max retry | `ws_client.py` | Koneksi lebih resilient |
| 10 | Debug `print()` di `config.py` dihapus | `config.py` | Log lebih bersih |

**Perubahan scoring (v7.0.1):**
- Session bonus dihapus dari formula skor — kini menjadi **threshold adjuster**
- OI magnitude bonus dihapus — diganti dengan **OI-tier threshold adjustment**
- Threshold LONG diturunkan (45/52) karena skor rata-rata turun ~13-15 pts
- Threshold SHORT dinaikkan signifikan (72/75) berdasarkan data audit (net −$12.55)

**Perubahan minor (7 Mei 2026):**
- Tombol close posisi Telegram diperbarui: `Close ASSET 📈/📉`
- Tombol Batal di flow `/live` diperbaiki (`close_settings` callback)
- Notifikasi update hanya dikirim sekali per release (berdasarkan git SHA, bukan deployment ID)

---

*Dokumen ini diperbarui untuk mencerminkan codebase KARA v7.1.0. Beberapa file besar (telegram.py, hyperliquid_client.py, dashboard/app.py) hanya terbaca sebagian karena keterbatasan ukuran. Nilai-nilai yang ditandai dengan catatan mungkin memiliki detail tambahan di bagian file yang tidak terbaca.*
