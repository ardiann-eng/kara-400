# 🚀 Panduan Deploy KARA Bot ke Railway — Tutorial Lengkap

> **Target**: Bot KARA berjalan 24/7 di cloud Railway dengan dashboard bisa diakses dari mana saja.

---

## Daftar Isi

1. [Persiapan Sebelum Deploy](#1-persiapan-sebelum-deploy)
2. [Push Code ke GitHub](#2-push-code-ke-github)
3. [Buat Project di Railway](#3-buat-project-di-railway)
4. [Set Environment Variables](#4-set-environment-variables)
5. [Tambah Volume (Persistent Storage)](#5-tambah-volume-persistent-storage)
6. [Generate Domain Public](#6-generate-domain-public)
7. [Trigger Deploy & Verifikasi](#7-trigger-deploy--verifikasi)
8. [Monitoring & Logs](#8-monitoring--logs)
9. [Update Code (Re-deploy)](#9-update-code-re-deploy)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Persiapan Sebelum Deploy

### Cek file wajib ada di project:
- [x] `Dockerfile` ✅
- [x] `requirements.txt` ✅
- [x] `railway.json` ✅ (sudah dibuat)
- [x] `.gitignore` (pastikan `.env` masuk di sini!) ✅

### Pastikan `.env` TIDAK ikut ke GitHub
Buka `.gitignore` dan pastikan ada baris ini:
```
.env
```

---

## 2. Push Code ke GitHub

### Jika belum punya repo GitHub:

1. Buka [github.com](https://github.com) → **New repository**
2. Nama repo: `kara-bot` (disarankan **Private**)
3. Jangan centang "Initialize with README"
4. Klik **Create repository**

### Push dari terminal (di folder project KARA):

```bash
# Pertama kali setup
git remote add origin https://github.com/USERNAME/kara-bot.git
git branch -M main
git push -u origin main
```

### Jika sudah ada repo, cukup:

```bash
git add .
git commit -m "feat: prepare for Railway deployment"
git push
```

> ⚠️ **PENTING**: Pastikan file `kara_data.db`, `kara.log`, `kara_state.json`, `.env`
> tidak ikut ter-push. Kalau sudah terlanjur, hapus dari tracking dulu:
> ```bash
> git rm --cached kara_data.db kara.log kara_state.json
> git commit -m "fix: remove sensitive files from tracking"
> git push
> ```

---

## 3. Buat Project di Railway

1. Buka [railway.app](https://railway.app/) → Login dengan akun GitHub
2. Klik **+ New Project**
3. Pilih **Deploy from GitHub repo**
4. Authorize Railway untuk akses GitHub jika diminta
5. Pilih repo `kara-bot` → Klik **Deploy Now**

> 💡 Deploy pertama akan **GAGAL** — ini normal karena env variables belum diset.
> Lanjutkan ke langkah berikutnya.

---

## 4. Set Environment Variables

Ini langkah **paling penting**. Buka project KARA di Railway → Tab **Variables**.

### Cara Input: Gunakan Raw Editor (lebih cepat)

Klik **RAW Editor** di pojok kanan atas, lalu **copy-paste semua ini** dan ganti nilainya:

```
# === HYPERLIQUID ===
HL_WALLET_ADDRESS=0x_WALLET_ADDRESS_KAMU
HL_PRIVATE_KEY=0x_PRIVATE_KEY_KAMU
KARA_DATA_SOURCE=mainnet
KARA_TRADE_MODE=paper

# === TRADING MODE ===
KARA_TRADING_MODE=standard

# === TELEGRAM ===
TELEGRAM_BOT_TOKEN=TOKEN_BOT_TELEGRAM_KAMU
TELEGRAM_CHAT_ID=CHAT_ID_KAMU
ALLOWED_CHAT_IDS=CHAT_ID_KAMU

# === SECURITY ===
SECRET_KEY=GANTI_DENGAN_RANDOM_STRING_PANJANG_MIN_32_KARAKTER
KARA_ACCESS_CODE=KARA2026
FERNET_KEY=

# === STORAGE ===
STORAGE_DIR=/app/storage
DB_PATH=/app/storage/kara_data.db

# === SERVER ===
PORT=8080
DASHBOARD_HOST=0.0.0.0

# === LOGGING ===
LOG_LEVEL=INFO
```

### Penjelasan setiap variabel:

| Variable | Wajib | Contoh Nilai | Keterangan |
|---|---|---|---|
| `HL_WALLET_ADDRESS` | ✅ | `0xAbCd...` | Wallet Hyperliquid kamu |
| `HL_PRIVATE_KEY` | ✅ | `0x1234...` | Private key wallet (**RAHASIA!**) |
| `KARA_DATA_SOURCE` | ✅ | `mainnet` | Data harga real dari mainnet |
| `KARA_TRADE_MODE` | ✅ | `paper` | **Mulai `paper` dulu!** Ganti `live` kalau siap |
| `KARA_TRADING_MODE` | ✅ | `standard` | `standard` atau `scalper` |
| `TELEGRAM_BOT_TOKEN` | ✅ | `123456:ABC...` | Dari @BotFather di Telegram |
| `TELEGRAM_CHAT_ID` | ✅ | `12345678` | Chat ID kamu (cek via @userinfobot) |
| `ALLOWED_CHAT_IDS` | ✅ | `12345678` | Boleh multiple: `111,222,333` |
| `SECRET_KEY` | ✅ | string acak | Minimal 32 karakter, bebas |
| `KARA_ACCESS_CODE` | ✅ | `KARA2026` | Kode untuk registrasi ke bot |
| `FERNET_KEY` | ⚪ | (kosong) | Untuk enkripsi multi-user (opsional) |
| `STORAGE_DIR` | ✅ | `/app/storage` | Lokasi database di container |
| `DB_PATH` | ✅ | `/app/storage/kara_data.db` | Path penuh database SQLite |
| `PORT` | ✅ | `8080` | Port dashboard (Railway inject otomatis) |
| `DASHBOARD_HOST` | ✅ | `0.0.0.0` | Harus `0.0.0.0`, bukan `localhost`! |
| `LOG_LEVEL` | ⚪ | `INFO` | `DEBUG` kalau mau verbose |

> 🔐 **KEAMANAN**: `HL_PRIVATE_KEY` adalah variabel paling sensitif.
> Railway menyimpannya sebagai secret terenkripsi dan tidak akan pernah muncul di logs.

---

## 5. Tambah Volume (Persistent Storage)

Tanpa volume, database (`kara_data.db`) akan **hilang** setiap kali bot restart/re-deploy.
Ini berarti semua history trade, data user, dan PnL akan terhapus!

### Langkah-langkah:

1. Di Railway project, klik **+ New** → pilih **Volume**
2. Beri nama: `kara-storage`
3. Setelah volume selesai dibuat, buka kembali **service KARA** (bukan service volume)
4. Pergi ke tab **Settings** → scroll ke bawah cari bagian **Volumes**
5. Klik **Mount a Volume**
6. Pilih volume `kara-storage`
7. Isi **Mount Path**: `/app/storage`  ← **Harus persis sama!**
8. Klik **Mount**

> ✅ Setelah ini, semua data tersimpan permanen meskipun container restart.

---

## 6. Generate Domain Public

Agar dashboard KARA bisa dibuka dari browser:

1. Buka tab **Settings** di service KARA
2. Cari bagian **Networking** → **Public Networking**
3. Klik **Generate Domain**
4. Railway memberikan URL seperti: `kara-production-xxxx.up.railway.app`

Dashboard bisa diakses di:
```
https://kara-production-xxxx.up.railway.app
```

> 💡 Bisa juga pakai custom domain sendiri di bagian yang sama.

---

## 7. Trigger Deploy & Verifikasi

### Re-deploy setelah semua diset:

1. Buka tab **Deployments**
2. Klik **Redeploy** pada deployment terakhir (atau push commit baru ke GitHub)

### Cek di tab Logs, harus muncul:
```
📡 [KARA_DEBUG] SYSTEM_PORT ENV: 8080
🚀 [KARA_DEBUG] BINDING DASHBOARD TO: 0.0.0.0:8080
...
DASHBOARD LIVE ON: http://0.0.0.0:8080
...
[KARA] Bot started in PAPER mode
```

### Checklist verifikasi:

| Test | Cara | Hasil yang Diharapkan |
|---|---|---|
| Dashboard | Buka URL Railway di browser | Halaman KARA muncul |
| Telegram | Kirim `/start` ke bot | Bot membalas dengan menu |
| Health | Buka `URL/api/health` | JSON `{"status": "ok"}` |
| Logs | Tab Logs Railway | Tidak ada error merah |

---

## 8. Monitoring & Logs

- **Tab Logs**: Lihat output bot real-time
- **Tab Metrics**: Grafik CPU, RAM, dan network usage
- **Restart manual**: Tab Deployments → klik `...` → **Restart**

Railway otomatis restart container jika health check gagal (dikonfigurasi di `Dockerfile`).

---

## 9. Update Code (Re-deploy)

Setiap update code lokal:

```bash
git add .
git commit -m "feat: deskripsi perubahan"
git push
```

Railway **otomatis detect push** ke branch `main` dan langsung re-deploy.
Zero-downtime — bot tetap jalan sampai versi baru siap.

---

## 10. Troubleshooting

### ❌ Build gagal

Lihat **Build Logs** di tab Deployments.

Kemungkinan penyebab:
- Package di `requirements.txt` tidak kompatibel
- Dockerfile syntax error

Solusi: Fix lokal, push ulang.

---

### ❌ Container crash saat start (Exit code 1)

Lihat **Deploy Logs**.

Penyebab paling umum — env variable kosong. Pastikan ini semua ada:
```
PORT=8080
DASHBOARD_HOST=0.0.0.0
STORAGE_DIR=/app/storage
DB_PATH=/app/storage/kara_data.db
HL_WALLET_ADDRESS=0x...
HL_PRIVATE_KEY=0x...
```

---

### ❌ Database hilang setelah restart

Volume belum di-mount. Ulangi **Section 5** dan pastikan mount path persis `/app/storage`.

---

### ❌ Dashboard tidak bisa diakses

1. Pastikan **Generate Domain** sudah dilakukan (Section 6)
2. Pastikan `PORT=8080` ada di Variables
3. Pastikan `DASHBOARD_HOST=0.0.0.0` (bukan `localhost`!)
4. Cek logs — cari error "bind failed" atau "address already in use"

---

### ❌ Telegram bot tidak merespons

1. Cek `TELEGRAM_BOT_TOKEN` benar (dari @BotFather)
2. Cek `TELEGRAM_CHAT_ID` benar (dari @userinfobot)
3. Di logs, cari `[Telegram]` untuk status koneksi
4. Pastikan bot tidak di-block

---

### ❌ Error "list index out of range" di logs

Ini error API Hyperliquid yang sudah ada fallback-nya — bot tetap berjalan.
Kalau sangat sering muncul, berarti ada perubahan format API dari Hyperliquid.

---

## Checklist Final Sebelum Declare "Done"

```
[ ] Code di-push ke GitHub (tanpa .env, .db, .log)
[ ] Project Railway dibuat dan connect ke GitHub repo
[ ] Semua env variables wajib sudah diisi (minimal 14 variabel)
[ ] Volume kara-storage di-mount ke /app/storage
[ ] Domain public sudah di-generate
[ ] Re-deploy berhasil (logs: "Bot started")
[ ] Dashboard bisa dibuka di browser
[ ] Telegram bot merespons /start
[ ] URL /api/health mengembalikan {"status": "ok"}
```

---

## Estimasi Biaya Railway

| Plan | Harga | Cocok untuk |
|---|---|---|
| Trial | $5 credit gratis | Testing awal |
| Hobby | $5/bulan | Bot 24/7 (lebih dari cukup untuk KARA) |
| Pro | $20/bulan | Jika butuh lebih banyak resource |

KARA dengan 1 instance biasanya pakai ~256MB RAM dan CPU minimal.
**Plan Hobby $5/bulan sudah lebih dari cukup.**

---

*Tutorial ini ditulis untuk KARA v7.0.0 — April 2026*
