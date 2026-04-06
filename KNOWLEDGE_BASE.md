# 📖 Kitab Suci KARA (Ultimate Knowledge Base)

Dokumen ini adalah **Ultimate Source of Truth** untuk proyek KARA. Dirancang agar asisten AI atau pengembang baru memahami seluruh sistem dari 0 hingga operasional penuh.

---

## 🌸 Section 1: Filosofi & Visi
**KARA** adalah asisten trading futures (Hyperliquid) yang dirancang dengan tiga pilar utama:
1. **Safety First**: Pengelolaan risiko yang sangat ketat (isolated margin, low leverage).
2. **Premium Interface**: Pengalaman pengguna yang elegan dan responsif (Telegram & Dashboard).
3. **Multi-User Platform**: Infrastruktur yang mampu melayani banyak trader secara independen.

- **Karakter**: Sopan, Professional (Kawaii-style), namun tegas soal disiplin trading.
- **Tech Stack**: Python 3.12+, FastAPI (Dashboard), Pydantic (Schemas), WebSockets (Real-time data).

---

## 🏗️ Section 2: Arsitektur Sistem (Global to Local)

### 1. Orchestrator (`main.py`)
Pusat syaraf utama yang melakukan:
- Inisialisasi klien HL dan WebSocket.
- Monitoring pasar secara global 24/7.
- **User Registry**: Menyimpan `UserSession` per `chat_id` Telegram.
- **Signal Dispatching**: Memasukkan sinyal pasar ke setiap sesi user yang memenuhi syarat risiko.

### 2. UserSession & Isolation (`core/user_session.py`)
Kunci dari sistem multi-user. Setiap user memiliki:
- **RiskManager**: Mengelola sizing dan batas rugi harian spesifik user tersebut.
- **Executor**: Menangani pengiriman order (Paper/Live) untuk akun user tersebut.
- **IDR Balance**: Saldo virtual lokal (misal: Rp1.000.000).

### 3. Database Persistence (`core/db.py`)
- Menggunakan file JSON `data/users.json` (dirancang demi kemudahan deployment di Railway/Docker).
- Sinkronisasi status dilakukan setiap kali ada perubahan saldo, mode, atau konfigurasi user.

---

## 📡 Section 3: Pipeline Data & Scoring Engine

### 1. Data Flow (Real-Time)
`Hyperliquid WS` -> `KaraWebSocketClient` -> `MarketDataCache` -> `ScoringEngine`.
- **MarketDataCache**: Singleton yang menyimpan "snapshot" pasar terbaru (Orderbook, Trades, Funding, Liquidations).

### 2. Signal Engine Logic (`engine/scoring_engine.py`)
Setiap aset di-scan dan diberi skor **0-100**:
- **OI + Funding (0-25 pts)**: Mencari area "crowded trades".
- **Liquidation Map (0-25 pts)**: Menghitung risiko *cascade* berdasarkan kepadatan likuidasi.
- **Orderbook (0-25 pts)**: Menganalisa *imbalance* antara pembeli dan penjual.
- **Session & Regime (0-25 pts)**: Bonus untuk volatilitas yang sehat dan sesi trading sibuk (NY/London).

### 3. Scalper vs Standard Mode
- **Standard**: Fokus pada tren besar, score minimal 56, interval scan 1 menit.
- **Scalper**: Agresif (HFT), score minimal 45, interval scan 5-10 detik. Menggunakan indikator EMA8/21, RSI, dan CVD Ratio secara bersamaan.

---

## 🛡️ Section 4: Risk Management & Eksekusi

### 1. Position Sizing
Rumus Sizing: `(Account Equity * Risk %) / (Entry Price * SL % * Leverage)`.
- Jika `fixed_margin` aktif (misal Rp50.000), maka rumus di atas diabaikan dan margin tetap digunakan.

### 2. Advanced Take-Profit (3 Stages)
KARA membagi posisi menjadi tiga bagian untuk memaksimalkan profil risiko:
1. **TP1 (40% posisi)**: Ditutup saat profit ~2-4%. SL langsung digeser ke Entry (Breakeven).
2. **TP2 (35% posisi)**: Ditutup saat profit ~5-8%.
3. **Trailing Stop (25% posisi)**: Sisa posisi dibiarkan "berlari" dengan trailing stop (offset ~3%) untuk menangkap tren panjang.

---

## 💸 Section 5: Multi-User & IDR Logic

### 1. Paper vs Live
- **Paper**: Simulasi lokal yang 99% mirip kondisi market asli (fee, slippage).
- **Live**: Menggunakan "Agent Wallet" Hyperliquid untuk eksekusi on-chain (Mainnet).

### 2. Konversi IDR
Semua angka USD diubah ke IDR menggunakan konstanta `USD_TO_IDR` di `config.py` sebelum dikirimkan ke Telegram. Hal ini memudahkan user Indonesia memantau nilai aset aslinya.

---

## 🌸 Section 6: UI & Personality Guidelines

### 1. Gaya Bahasa Telegram
- **Notifikasi**: Selalu gunakan header emoji yang jelas (🎯 Sinyal, ✅ Profit, 🛑 Loss).
- **Konfirmasi**: Memberikan pilihan tombol inline agar user merasa memegang kendali.
- **Command Utama**:
  - `/start`: Onboarding user baru.
  - `/pos`: Monitor posisi aktif dengan tombol [Close] instan.
  - `/status`: Ringkasan akun (Ekuitas, PnL Hari ini).

### 2. Dashboard
- Visualisasi grafik PnL dan riwayat trading secara real-time.
- Desain minimalis, gelap, dengan aksen warna premium (Ungu/Hijau).

---

## 🛠️ Section 7: Maintenance & Troubleshooting
- **Logs**: `kara.log` menyimpan semua aktivitas teknis.
- **Excel**: `trade_history.xlsx` untuk audit performa manual di Excel.
- **Reset**: User bisa mereset saldo paper lewat command `/paper`.

---
*Dibuat oleh AI Assistant untuk KARA Master — April 2025.*
