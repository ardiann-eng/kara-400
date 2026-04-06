# 🚀 Panduan Deployment Railway — KARA Bot

Ikuti langkah-langkah di bawah ini untuk memindahkan KARA dari komputer lokal ke cloud (Railway) agar bot bisa berjalan 24/7 tanpa henti.

## 1. Persiapan GitHub
Pastikan semua file project KARA sudah di-upload ke repository GitHub (Private sangat disarankan karena ada file konfigurasi).
> [!IMPORTANT]
> Jangan pernah meng-upload file `.env` ke GitHub. Kita akan memasukkan variabel tersebut langsung di Railway.

## 2. Membuat Project di Railway
1. Buka [Railway.app](https://railway.app/) dan login dengan GitHub.
2. Klik **+ New Project**.
3. Pilih **Deploy from GitHub repo**.
4. Pilih repository KARA Anda.
5. Klik **Deploy Now**. (Deployment awal mungkin gagal, ini normal karena variabel belum diset).

## 3. Memasukkan Environment Variables
Ini adalah langkah paling krusial. Buka tab **Variables** di Railway project Anda, lalu masukkan semua isi dari file `.env` lokal Anda:

| Variable Name | Value (Contoh) |
| :--- | :--- |
| `HL_PRIVATE_KEY` | `0x... (Private Key Anda)` |
| `WALLET_ADDRESS` | `0x... (Wallet Address Anda)` |
| `TELEGRAM_TOKEN` | `8678... (Bot Token dari BotFather)` |
| `HL_TESTNET` | `True` (atau `False` untuk Live) |
| `FULL_AUTO` | `True` (atau `False`) |
| `DASHBOARD_HOST` | `0.0.0.0` |
| `PORT` | `8888` (Biarkan Railway yang mengatur ini otomatis) |

> [!TIP]
> Anda bisa menggunakan fitur **"Raw Editor"** di tab Variables untuk langsung copy-paste isi file `.env` sekaligus.

## 4. Konfigurasi Domain (Dashboard)
Agar Anda bisa mengakses dashboard dari luar:
1. Buka tab **Settings** di Railway.
2. Cari bagian **Networking** > **Public Domain**.
3. Klik **Generate Domain**.
4. Gunakan URL yang diberikan (misal: `kara-production.up.railway.app`) untuk membuka dashboard Anda.

## 5. Database (PENTING! agar PnL tidak hilang)
Karena Railway menggunakan sistem *ephemeral*, file database `.db` Anda akan terhapus setiap bot restart kecuali Anda menggunakan **Volume**:
1. Buka project KARA Anda di Dashboard Railway.
2. Klik **+ Add Service** > **Volume**.
3. Beri nama (misal: `kara-storage`).
4. Di tab **Settings** pada service KARA Anda, cari bagian **Volumes** dan klik **Mount Volume**.
5. Masukkan mount path: `/app/storage` (Harus sama persis!).
6. Klik **Mount**.

## 6. Monitoring & Logs
- Pantau pergerakan bot di tab **Logs**.
- Jika Anda melihat pesan `🟢 Dashboard client connected`, berarti dashboard siap digunakan.
- Gunakan perintah `/start` di Telegram untuk memastikan bot sudah mengenali identitas Anda di server baru.

## 🚀 Bonus: Deployment via Railway CLI
Jika Anda lebih suka menggunakan Terminal/CLI:

1. **Install CLI**: `npm i -g @railway/cli`
2. **Login**: `railway login`
3. **Init New Project**: `railway init` (Ini akan membuat project baru secara otomatis)
4. **Push Environment**: `railway variables set $(cat .env | xargs)`
5. **Deploy**: `railway up`

---
**KARA is now ready to conquer the markets 24/7!** 🌸🚀
