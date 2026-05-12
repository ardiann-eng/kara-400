# 📜 KARA Change Log

Semua perubahan teknis dan pembaruan arsitektur pada bot KARA dicatat di sini.

---

## [8.0.1] — 2026-05-12 — OBSERVABILITY & AUTOPSY PROTOCOL

> **"If you can't measure it, you can't improve it. If you can't explain it, you can't trust it."**

### 📊 FIX 1 — Railway Telemetry & Structured Logging
- **JSON Logger**: Activated for cloud drains (`RAILWAY_ENVIRONMENT=true`).
- **Heartbeat Monitoring**: Structured logs for Quant Aggression features (ATR-SL, Partial Exit, Funding Contra).
- **Signal Audit Trail**: Skip reason counters (`skip_counters`) and 5min summary logs.
- **Enhanced Health API**: `/api/health` dashboard endpoint now exposes telemetry metrics.

### 🧠 FIX 2 — Rule-Based Autopsy Engine
- **Deterministic Post-Mortem**: 16 templates mapping trade data to human-readable insights (No AI dependency).
- **Max Drawdown Tracking**: Captures `max_unrealized_loss` per position for risk auditing.
- **Excel Export**: New "Autopsy" column in trade logs.
- **Top Insight Aggregator**: Analyzes last 20 trades to identify recurring strategic failure/success patterns.

### 🌸 FIX 3 — Dynamic Git-Based Changelog
- **Generator Engine**: Automatically parses conventional commits and keywords from git history.
- **Telegram Notification**: Automatic pretty-formatted update messages sent to Admin on deploy.
- **Command /whatsnew**: Users can manually trigger the dynamic changelog display.

---

## [8.0.0] — 2026-05-12 — QUANT AGGRESSION PROTOCOL

> **"Frequency is the Asset. Exit is the Edge."**

### 🔓 FIX 1 — Entry Gates Unlocked
- **Funding veto → fade_mode**: Extreme funding no longer blocks entries. Flagged as `fade_mode` for contrarian tuning (tighter SL 0.8×, wider TP 1.5×, max 12min)
- **Mean-reversion guard REMOVED**: Score cap at 60 deleted. High scores get runner-mode exit treatment
- **Regime multiplier restored**: Trending ×1.2 (was ×1.0). Late-trend ×1.15 with flag instead of punitive ×0.7

### 🎯 FIX 2 — Layered Partial Profit & Breakeven Engine
- **SL-distance TP layers**: TP1 @1.0× SL (close 40%), TP2 @1.5× SL (close 30%), Trail @2.0× SL (remaining 30%)
- **Breakeven trigger**: SL → entry+0.1% at 0.8× SL distance
- **Partial tracking**: `partial_exits_done` field prevents double-firing

### 📊 FIX 3 — Score-Driven Exit Matrix
- **Variable time exit**: ≥66=25min | ≥61=20min | ≥56=15min | <56=10min
- **Dynamic SL/TP**: High score=wider SL/TP (runner). Low score=tight SL/TP (quick scalp)
- **Late-trend**: -5min time, +30% TP2. **Fade mode**: 0.8× SL, 1.5× TP2, max 12min

### 📈 FIX 5 — Funding Contrarianism
- Extreme funding points: 12→18. High funding: 8→12

### ⏱️ FIX 6 — Time Exit Grace for Runners
- TP1 hit → deadline extends 50%. TP1 hit + profit → time_exit skipped (ATR trail handles)

### 🏗️ Schema & Config
- Position: `partial_exits_done`, `scaled_in`, `original_entry_price`, `scale_in_count`, `extended_deadline`
- ScalperConfig: partial TP multipliers, breakeven threshold, scale-in/re-entry configs
- Both executors updated for new partial ratios and breakeven SL

---

## [7.1.0] — 2026-05-08

### 🔴 SHORT Signal Improvements
- **SHORT Threshold Scalper aktif**: `min_score_short_signal` dan `min_score_short_auto` kini di-enforce di `_run_scalper()` — sebelumnya hanya ada di `_run_standard` yang tidak aktif (BUG-1 fixed)
- **SHORT threshold diubah ke 62**: Sebelumnya 72/75, kini 62 untuk signal dan auto-execute
- **Funding rate filter untuk SHORT**: SHORT diblok jika funding rate di bawah `short_min_funding_rate` (0.00001)
- **Short Squeeze Detection**: SHORT diblok jika price spike >1% + OI drop >5% dalam 1 menit terakhir
- **Bearish RSI Divergence**: Deteksi price higher-high tapi RSI lower-high → `bear_pts +10`; berlaku juga arah sebaliknya untuk LONG
- **Bearish Rejection Wick**: Deteksi upper wick >1.5× body dengan close bearish → `bear_pts +8`
- **Funding Cost Warning Telegram**: Saat funding rate >0.005%/8h, sinyal SHORT menampilkan estimasi biaya funding per 8h

### 🧠 Intelligence Model
- **Fitur `is_short` ditambahkan**: Feature array naik dari 9 → 10 fitur; model kini bisa membedakan pattern LONG vs SHORT
- **Auto-invalidasi model lama**: Model pkl dengan 9 fitur otomatis dihapus dan retrain saat bot restart
- **`side` field di training data**: `get_features()` membaca field `side` dari experience buffer DB

### 📊 Meta Learning
- **`meta_min_samples` turun ke 5**: Pattern key aktif memberikan boost/penalty setelah 5 trade (sebelumnya 10)

### 🐛 Bug Fixes
- **Double-counting PnL fix** (`paper_executor`): `_execute_partial_close` untuk full-close (SL/trailing/time/momentum exit) sebelumnya menghitung `partial_pnl` dan menambahkannya ke balance, lalu memanggil `close_position()` yang menghitung ulang — mengakibatkan PnL dan balance double-counted, dan meta pattern / ML experience buffer diisi data yang korup
- **Live Decision Feed dedup**: Feed tidak lagi menampilkan sinyal yang sama berkali-kali akibat multi-user eksekusi; sekarang di-GROUP BY `pos_id`

### 💬 Telegram — Alasan KARA
- **Bucket baru "Sinyal SHORT"**: Reason dari wick, RSI divergence, dan squeeze muncul di bagian teratas
- **Panel "Analisis Risiko SHORT"**: Menampilkan status funding rate (nilai aktual), threshold, squeeze guard, dan unlimited-loss warning
- **System notes diperbarui**: Mention wick detection, RSI divergence, dan SHORT guard
- **Header sinyal mencantumkan side**: `BTC SHORT 🔴 (skor X/100)`

---

## [7.0.0] — 2026-04-13
### 🧠 Intelligence Layer (Major AI Update)
- **Self-Learning AI Engine**: Mengintegrasikan `HistGradientBoostingClassifier` (Scikit-Learn) untuk memprediksi probabilitas kemenangan trade secara real-time.
- **Expected Edge Logic**: Setiap sinyal kini dilengkapi estimasi probabilitas winrate (0-100%).
- **Experience Buffer**: Database SQLite baru (`ml_experience`) untuk merekam setiap fitur pasar saat entry dan hasil PnL untuk pembelajaran kontinu.
- **Dynamic Risk 2.0**: 
  - S sizing otomatis membesar hingga 2.5x jika AI mendeteksi probabilitas menang >80%.
  - Trade otomatis di-ABORT jika probabilitas menang menurut AI <40%.
- **History Warmup**: Mengintegrasikan skrip otomatis yang belajar dari file `trade_history.xlsx` (2.000+ real trades ingested).

### 🛠️ Core & Scoring Improvements
- **Fixed Score Inversion**: Membalikkan logika Funding & Liquidation dari *Contrarian* menjadi *Trend-Following* (menghilangkan anomali skor tinggi yang justru sering loss).
- **Session Bias Calibration**: Memperketat penalti di Asia Session untuk menghindari likuiditas rendah.
- **Fixed Scalper Crash**: Memperbaiki error `AttributeError` pada mode Scalper terkait parameter batas risiko maksimal.

---

## [6.2.0] — 2026-04-09
### 🛡️ Multi-User & Security
- **Multi-Wallet Support**: Arsitektur terisolasi di mana setiap user bisa memasang private key sendiri secara independen.
- **Fernet Encryption**: Rahasia user (Private Key) kini disimpan dengan enkripsi AES di database.
- **Onboarding Flow**: Pesan sambutan dan petunjuk penggunaan baru untuk pendaftar pertama.
- **Optimization**: Transisi penuh ke Async-Native UserSession untuk menghilangkan lag saat login banyak user sekaligus.

---

## [6.1.1] — 2026-04-09
### 🛡️ Risk Guard
- **Leverage Bypass Fix**: Memperbaiki bug di mana user bisa memaksa leverage melampaui batas yang diizinkan.
- **Triple-Cap Leverage**: Sizing sekarang mempertimbangkan 3 batas sekaligus: Sinyal, Preferensi User, dan Batas Maksimum dari Bursa (Dynamic Market Cap aware).

---

## [5.0.0] — 2026-04-06
### 📊 Web Dashboard
- Rilis perdana Dashboard Web berbasis Glassmorphism untuk memonitor market dan status bot secara visual.
- Sinkronisasi data real-time antara bot Telegram dan Dashboard via WebSocket.
