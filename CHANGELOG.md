# 📜 KARA Change Log

Semua perubahan teknis dan pembaruan arsitektur pada bot KARA dicatat di sini.

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
