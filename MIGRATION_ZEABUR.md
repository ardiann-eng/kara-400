# 🚀 Panduan Migrasi KARA ke Zeabur

Ikuti langkah-langkah di bawah ini untuk menghidupkan kembali bot KARA Anda di Zeabur. Zeabur memiliki performa yang lebih stabil dan "Free Tier" yang lebih cocok untuk bot trading 24/7.

## 1. Persiapan Akun
1. Buka [Zeabur.com](https://zeabur.com/) dan Login menggunakan akun GitHub Anda.
2. Klik **"Create Project"**.
3. Pilih Region: **Singapore** atau **Tokyo** (Sangat disarankan untuk bot trading agar koneksi ke Hyperliquid cepat).

## 2. Deploy dari GitHub
1. Klik **"Deploy Service"** > **"GitHub"**.
2. Pilih repository `kara-400`.
3. Zeabur akan otomatis membaca file `zeabur.json` yang sudah saya buatkan.

## 3. Memasukkan Environment Variables (PENTING)
Setelah service dibuat, buka tab **Variables** di Dashboard Zeabur, lalu masukkan isi dari file `.env` lokal Anda. 

| Variable Name | Value |
| :--- | :--- |
| `HL_PRIVATE_KEY` | *(Gunakan yang ada di .env Anda)* |
| `WALLET_ADDRESS` | *(Gunakan yang ada di .env Anda)* |
| `TELEGRAM_TOKEN` | *(Gunakan yang ada di .env Anda)* |
| `PORT` | `8080` (Default) |
| ... | (Lanjutkan untuk variabel lainnya) |

> [!TIP]
> Anda bisa copy-paste seluruh isi `.env` sekaligus jika Zeabur menyediakan fitur "Bulk Import" atau "Raw Editor".

## 4. Konfigurasi Domain
Agar dashboard dashboard bisa diakses:
1. Buka tab **Networking** di service Anda.
2. Klik **"Generate Domain"**.
3. Pilih akhiran `.zeabur.app` (Gratis).
4. Gunakan URL tersebut untuk membuka dashboard trading Anda.

## 5. Cek Status Bot
- Buka tab **Logs** di Zeabur.
- Cari pesan: `🟢 SYNC: Database connected and ready.`
- Buka Telegram dan ketik `/status` untuk memastikan bot sudah merespon.

---
**Selamat! KARA kini berjalan di server yang lebih kencang & stabil.** 🌸🚀
