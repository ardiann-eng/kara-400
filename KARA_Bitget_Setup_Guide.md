# KARA × Bitget — Panduan Setup Lengkap

**Versi:** KARA 8.0+ (Bitget Execution Integration)
**Tanggal:** 2026-05-14

Dokumen ini menjelaskan cara mengaktifkan **Bitget Execution Mode** di KARA Bot, di mana:
- 🧠 **Sinyal trading** tetap dianalisis dari data **Hyperliquid** (data feed terbaik untuk scalping)
- 💸 **Eksekusi otomatis** dilakukan di **Bitget USDT-M Futures** (akun real-money kamu)

---

## 1. Persiapan Sebelum Mulai

### A. Akun Bitget
- Buat akun di [bitget.com](https://www.bitget.com)
- Selesaikan **KYC level 1 minimum**
- Top up USDT ke **Futures Wallet** (bukan spot)
  - **Minimum disarankan:** $20 USDT untuk testing
  - **Untuk live trading aktif:** $100+ disarankan

### B. Environment Variable di Server (Admin)
Set env var berikut di Railway / Docker:

```bash
# Wajib: aktifkan Bitget execution mode
KARA_EXECUTION_EXCHANGE=bitget

# Wajib: encryption key (jika belum ada)
HL_FERNET_KEY=<generate dengan: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">

# Opsional: global Bitget API (fallback kalau user belum input sendiri)
BITGET_API_KEY=
BITGET_SECRET_KEY=
BITGET_PASSPHRASE=

# Opsional: tuning price bridge (default sudah aman)
KARA_PRICE_BRIDGE_MAX_GAP=0.003   # 0.3% max gap HL vs Bitget
KARA_PRICE_BRIDGE_TTL=2.0         # cache 2 detik
```

**Setelah set env var, restart bot.** Cek log harus muncul:
```
[BITGET] Initializing Bitget integration ...
[BITGET] Symbol registry: 60+ assets available
[BITGET] Integration ready (EXECUTION_EXCHANGE=bitget)
```

---

## 2. Cara User Connect Bitget Lewat Telegram

### Langkah 1: Buat API Key Bitget

Di **bitget.com**, ikuti tutorial ini (atau ketik `/bitgettut` di chat KARA):

1. **Login** ke akun Bitget
2. Buka **Profil → API Management** ([direct link](https://www.bitget.com/account/newapi))
3. Klik **"Create API"** → pilih **"System-generated"**
4. Isi form:
   - **API Name:** `KARA-Bot`
   - **Passphrase:** buat passphrase 8–32 karakter — **catat baik-baik!**
   - **Permissions:**
     - ✅ **Read** (wajib)
     - ✅ **Trade** (wajib)
     - ❌ **Withdraw** (**JANGAN dicentang** — biarkan kosong)
   - **IP Whitelist:** kosongkan (atau tambah IP server jika kamu tahu)
5. Selesaikan verifikasi (2FA / Email / SMS)
6. **Salin 3 data** ini — Bitget hanya menampilkan sekali:
   - `API Key` (mis. `bg_1a2b3c...`)
   - `Secret Key` (string panjang)
   - `Passphrase` (yang kamu buat di langkah 4)

### Langkah 2: Aktivasi di Telegram

1. Ketik **`/live`** di chat dengan KARA Bot
2. Bot akan menampilkan warning risiko + 2 tombol:
   - **📘 Lihat Tutorial API Bitget** — buka panduan in-chat
   - **🚀 Saya Sudah Punya API Key** — langsung lanjut input
3. Pilih **"🚀 Saya Sudah Punya API Key"**
4. Bot minta kirim credentials dalam **satu baris** dengan format:
   ```
   API_KEY:SECRET_KEY:PASSPHRASE
   ```
   Contoh:
   ```
   bg_1a2b3c4d5e:abcdef123456789:MyPass2026
   ```
5. Bot akan otomatis:
   - ✅ Verifikasi credentials ke Bitget
   - 🔐 Enkripsi dengan Fernet sebelum simpan
   - 🗑️ Hapus pesan kamu yang berisi credentials (keamanan)
   - 🔄 Re-init session sebagai `BitgetExecutor`
6. Konfirmasi sukses akan menampilkan saldo & jumlah posisi terbuka

### Langkah 3: Atur Leverage (Opsional)

Default: pakai leverage dari trading mode (`scl_max_leverage` = 20x untuk scalper).

Untuk atur khusus Bitget:
```
/setbitgetlev 10        # set max leverage Bitget = 10x
/setbitgetlev 0         # reset ke default trading mode
```

Atau via conversation:
1. Ketik `/setbitgetlev` (tanpa angka)
2. Bot tanya angka leverage (1–125)
3. Kirim angka, selesai

**Catatan penting:**
- Bitget punya **per-asset max leverage** yang bervariasi (BTC 125x, altcoin bisa 5x–50x)
- KARA akan otomatis **cap leverage** ke nilai terkecil antara:
  - `bitget_max_leverage` (user config kamu)
  - `bitget_per_asset_max` (Bitget API)
  - `signal.suggested_leverage` (dari scoring engine)

---

## 3. Commands Bitget di Telegram

| Command | Fungsi |
|---------|--------|
| `/live` | Mulai setup Bitget (atau HL Agent kalau EXECUTION_EXCHANGE=hyperliquid) |
| `/bitget` | Lihat status koneksi: saldo, posisi, leverage cap |
| `/bitgettut` | Buka tutorial buat API Key Bitget |
| `/setbitgetlev <N>` | Set max leverage Bitget (0 = pakai default) |
| `/bitgetreset confirm` | Hapus credentials Bitget, paksa setup ulang |
| `/settings` | Pusat kendali config (leverage umum, max positions, dll) |
| `/status` | Status akun & posisi (auto-detect Bitget atau HL) |
| `/pos` | List posisi terbuka (di exchange manapun yang aktif) |

---

## 4. Cara KARA Bekerja di Bitget Mode

### 4.1 Pipeline Sinyal
```
┌─────────────┐    ┌──────────────┐    ┌─────────────┐    ┌──────────────┐    ┌────────────┐
│ Hyperliquid │ →  │ ScoringEngine│ →  │ PriceBridge │ →  │ RiskManager  │ →  │  Bitget    │
│   (data)    │    │  (sinyal)    │    │ (HL→Bitget) │    │ (size+check) │    │ (eksekusi) │
└─────────────┘    └──────────────┘    └─────────────┘    └──────────────┘    └────────────┘
```

### 4.2 Yang Dilakukan PriceBridge

Saat sinyal datang dari HL dengan harga `$78,000`:
1. Ambil harga Bitget current (prefer WS cache, fallback REST)
2. Cek gap — kalau > **0.3%** → skip trade (proteksi terhadap divergence ekstrem)
3. Recalculate **entry/SL/TP1/TP2/TP3** pakai harga Bitget dengan persentase yang sama
4. Pass signal yang sudah adjusted ke BitgetExecutor

Contoh log:
```
[BRIDGE] BTC LONG | HL=78000.00 → Bitget=78015.50 (gap=0.020%) | SL pct=1.20% TP2 pct=2.50%
```

### 4.3 Yang Dilakukan BitgetExecutor

Saat `open_position`:
1. **Set margin mode** → `isolated` (paralel dengan langkah 2)
2. **Set leverage** untuk side+symbol (hedge mode)
3. **Place IOC limit order** dengan buffer 5 bps untuk fill probability tinggi
4. Tunggu fill (max 6 detik) → fallback **market order** kalau IOC tidak fill
5. **Place on-exchange Stop Loss** (`pos_loss` plan order) — aktif walau bot crash
6. Simpan posisi shadow + record ke Excel + risk manager

Saat `update_positions` (per-tick):
1. Ambil harga Bitget (WS push, sub-second freshness)
2. RiskManager cek triggers (TP1/TP2/TP3/SL/trailing/momentum)
3. Partial close pakai market order dengan `reduceOnly=YES`
4. Setelah TP1 hit → SL on-exchange di-update ke breakeven+0.1%

### 4.4 Symbol Mapping HL → Bitget

| HL Symbol | Bitget Symbol | Catatan |
|-----------|---------------|---------|
| `BTC` | `BTCUSDT` | Direct map |
| `ETH` | `ETHUSDT` | Direct map |
| `kPEPE` | `PEPEUSDT` | Contract multiplier ×1000 (auto-handled) |
| `kBONK` | `BONKUSDT` | Contract multiplier ×1000 |
| `VVV`, `FARTCOIN`, `MON` | (none) | HL-exclusive — di-skip dari scan |

Mapping lengkap di [utils/symbol_registry.py](utils/symbol_registry.py).

---

## 5. Low-Latency Architecture

KARA dioptimasi untuk eksekusi cepat di Bitget:

### Optimasi yang Sudah Diterapkan
1. **HTTP/2 + Connection Pool** (`httpx.AsyncClient` dengan keep-alive)
2. **WebSocket Mark Price** — push-based update, lebih cepat dari REST polling
3. **Price Cache 1.5s TTL** — burst signal de-dupe tanpa flooding API
4. **Concurrent set_margin + set_leverage** — paralel `asyncio.gather`
5. **IOC Limit Order** dengan 5 bps buffer — fill cepat tanpa terlalu agresif
6. **Symbol Registry Pre-filter** — scanner skip asset yang tidak ada di Bitget
7. **Per-position WS Subscribe** — hanya stream asset yang punya posisi

### Latency Breakdown (Estimasi)
```
Signal generated         : 0ms
PriceBridge (WS cache)   : ~5ms     (sub-second update)
Risk check + sizing      : ~3ms
set_margin + set_lev     : ~80ms    (paralel)
place_order (IOC limit)  : ~120ms
Order fill confirm       : ~200-400ms (Bitget matching)
TPSL placement           : ~80ms
─────────────────────────────────
Total signal → fill      : ~400-600ms
```

Dibanding REST-only polling: penghematan ~200ms per cycle dari WS price.

---

## 6. Troubleshooting

### "❌ Verifikasi gagal: [40762] sign signature error"
**Penyebab:** Secret Key salah atau ada whitespace.
**Solusi:** Salin ulang Secret Key dari Bitget, pastikan tidak ada spasi di awal/akhir.

### "❌ Verifikasi gagal: [40007] Invalid API key"
**Penyebab:** API Key tidak valid atau sudah di-delete di Bitget.
**Solusi:** Buat API Key baru, kirim ulang via `/live`.

### "❌ [BRIDGE] gap > 0.3% — skip trade"
**Penyebab:** Harga HL vs Bitget terlalu jauh (volatile / news).
**Solusi:** Tunggu market settle. Atau tambah env `KARA_PRICE_BRIDGE_MAX_GAP=0.005` (0.5%) untuk lebih permisif (TIDAK disarankan).

### "Sinyal datang tapi tidak ada eksekusi"
**Cek di log:**
- `[BITGET] {asset}: tidak ada di Bitget, skip` → asset HL-only, normal
- `[BITGET] {asset}: size {n} < min {m}` → modal terlalu kecil untuk min order Bitget
- `[BITGET] {asset}: place_order failed` → cek connectivity, restart bot

### Posisi Tidak Sinkron Setelah Restart
KARA otomatis sync via `sync_positions_from_chain()` saat session init. Kalau masih tidak sinkron:
1. Ketik `/bitget` — cek apakah credentials masih valid
2. Restart bot — startup akan re-sync
3. Cek dashboard Bitget langsung untuk verifikasi posisi

---

## 7. Keamanan

- ✅ **API Secret & Passphrase di-enkripsi** dengan Fernet (`HL_FERNET_KEY` env)
- ✅ **Pesan credentials user di-delete otomatis** setelah verifikasi
- ✅ **API Key tidak boleh punya Withdraw permission** (bot reject saat verify)
- ✅ **SL aktif di server Bitget** sebagai `pos_loss` plan order — aktif walau bot mati
- ⚠️ **Backup `HL_FERNET_KEY` di safe place** — kalau hilang, credentials user tidak bisa di-decrypt

---

## 8. Migration dari Hyperliquid Execution

User yang sebelumnya pakai HL execution dan ingin pindah ke Bitget:

1. **Admin set:** `KARA_EXECUTION_EXCHANGE=bitget` di env
2. **Restart bot** — semua session akan rebuild
3. **User existing dengan HL agent wallet:** posisi lama HL tetap di-track via shadow,
   tapi posisi baru akan dieksekusi di Bitget setelah user setup `/live` ulang
4. **Catatan:** sinyal `VVV`, `FARTCOIN`, `MON` dll yang HL-only akan otomatis di-skip dari scanner

---

## File yang Terlibat

```
data/bitget_client.py          # REST client (auth, orders, account)
data/bitget_ws_client.py       # WebSocket public mark price
execution/base_executor.py     # Abstract interface
execution/bitget_executor.py   # Implementation BaseExecutor untuk Bitget
utils/symbol_registry.py       # HL ↔ Bitget mapping + contract size
utils/price_bridge.py          # HL price → Bitget price adjustment
core/user_session.py           # Executor factory (paper / HL / Bitget)
main.py                        # Integration + position monitor routing
notify/telegram.py             # /live, /bitget, /setbitgetlev, /bitgettut
config.py                      # BITGET_* env vars + EXECUTION_EXCHANGE
models/schemas.py              # User.bitget_* fields + UserConfig.bitget_max_leverage
```

---

**Selesai!** Bot kamu siap eksekusi via Bitget dengan sinyal dari Hyperliquid.

Pertanyaan / issue: hubungi admin via Telegram.
