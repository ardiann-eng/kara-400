# 📖 Kitab Suci KARA: The Infinite Knowledge Base

Selamat datang di pusat kesadaran **KARA** (Knowledge-driven Autonomous Risk-aware Assistant). Dokumen ini adalah panduan komprehensif dari nol untuk memahami identitas, arsitektur, dan logika operasional bot ini.

---

## 🌸 0. Apa itu KARA?

**KARA** adalah asisten trading berbasis AI yang dirancang khusus untuk ekosistem **Hyperliquid Futures**. Berbeda dengan bot konvensional yang hanya menggunakan indikator teknikal (RSI/MACD), KARA menggabungkan data **Market Intelligence**:
- **Open Interest (OI)**: Mengukur partisipasi pasar.
- **Funding Rates**: Mendeteksi kerumunan trader (crowding) yang berisiko terlikuidasi.
- **Liquidation Heatmaps**: Mencari area pembersihan harga (liquidity hunt).
- **Orderbook Imbalance**: Mengintip kekuatan beli/jual secara *real-time*.

**Kepribadian**: "Kawaii-Professional Maid". KARA bertindak sebagai pelayan setia bagi tuannya (Master), menjaga aset dengan disiplin besi, tapi tetap ramah dan sopan di Telegram.

---

## 🏗️ 1. Arsitektur & Performa (The Secret Sauce)

KARA dirancang agar super kencang di **Railway** (Memory 512MB) tanpa kehilangan akurasi 100 market.

### A. Rahasia Scan 100 Market < 1 Detik
KARA menggunakan teknik **Batch Metadata Fetching**. Alih-alih bertanya ke bursa satu-per-satu untuk tiap koin, KARA memanggil endpoint `metaAndAssetCtxs` yang langsung menarik info harga, funding, harganya, dan OI untuk 100 koin sekaligus dalam **satu panggilan API**.

### B. Persistence (Ingatan Abadi)
Meskipun bot di-restart di Railway, KARA tidak akan lupa:
- **SQLite Cache**: Menyimpan status "Sifat Market" (Regime) sehingga tidak perlu download grafik ulang setiap detik.
- **Local DB**: Saldo Paper, riwayat posisi, dan konfigurasi user tersimpan aman di `/app/data/kara_user.db`.

---

## 🧠 2. Otak KARA: Scoring Engine

Setiap koin dievaluasi setiap 60 detik dengan skor **0 - 100**.

### Analisa Utama (Analis):
1.  **OI & Funding (Cap 40 pts)**: Memberikan poin besar jika terjadi *OI Expansion* (+18 pts) di mana harga naik bersamaan dengan masuknya uang baru.
2.  **Liquidation Analyzer (Cap 40 pts)**: Mendeteksi kluster likuidasi besar yang bisa memicu "Long Squeeze" atau "Short Squeeze".
3.  **Orderbook Analyzer (Cap 20 pts)**: Menganalisa ketebalan antrean harga.

### Multiplier (Penguat Skor):
Skor mentah lalu dikalikan dengan dua faktor:
- **Trend Multiplier**: Skor dikali **1.1x** jika koin sedang dalam tren kenceng (pergerakan > 1.5% dalam 24 jam). Jika tidak, dikali **0.95x** (penalti tipis).
- **Volatility Multiplier**: Skor dipangkas jika market sedang terlalu liar (*Extreme Vol*) untuk menjaga keamanan saldo.

**Target Sinyal (Signal)**: Jika Skor Final **≥ 52**, KARA akan langsung meletuskan sinyal atau *open posisi* otomatis.

---

## 🛡️ 3. Jantung KARA: Risk & Execution

KARA tidak pernah berjudi. Setiap posisi dihitung secara matematis.

### A. Position Sizing
Rumus: `(Saldo * Risiko%) / (Jarak Stop Loss * Leverage)`.
- Default **Leverage**: 20x (Standard) atau 25x-35x (Scalper).
- **Stop Loss**: Selalu dipasang saat posisi dibuka (Gak ada istilah "ngambang").

### B. Exit Strategy (3 Tahap)
KARA sangat pintar mengunci keuntungan:
1.  **TP1 (40% Posisi)**: Tutup saat profit kecil tercapai. Stop-loss langsung ditarik ke harga beli (Breakeven).
2.  **TP2 (35% Posisi)**: Ambil porsi profit kedua.
3.  **Trailing Stop (Sisa 25%)**: Membiarkan profit "berlari" sejauh mungkin sesuai tren pasar.

---

## 📱 4. Interaksi Master & KARA (UI)

KARA berkomunikasi melalui Telegram dengan gaya bahasa sopan namun detail.

### Command Utama:
- `/pos`: Monitor posisi aktif. Tombol [Refresh] sekarang bersifat **instan** (< 0.5 detik) karena mengambil data dari WebSocket cache.
- `/status`: Cek saldo akun, PnL hari ini, dan riwayat "Daily Reset".
- `/paper`: Reset saldo simulasi ke awal ($1,000).

---

## 🛠️ 5. Pedoman Developer (Anatomi Kode)

Bagi Master yang ingin memodifikasi KARA, ini petanya:
- `/engine`: Logika trading (Scoring, Analyzers).
- `/core`: Mesin utama (Database, Registry, UserSession).
- `/data`: Komunikasi bursa (REST API, WebSocket).
- `/notify`: Telegram Bot Interface.
- `/risk`: Manager keamanan dan sizing.

---

## 🔮 6. Masa Depan KARA

Visi KARA adalah menjadi asisten autonomous penuh yang bisa beradaptasi sendiri dengan kondisi market bullish, bearish, maupun sideways tanpa intervensi manusia, sambil tetap menjaga ekuitas Master di atas segalanya.

---
*Dokumen ini diperbarui secara berkala oleh KARA AI Core — April 2026.*
