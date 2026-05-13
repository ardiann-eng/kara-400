# KARA Database Exporter

## Tujuan
Mengekspor database SQLite `kara_data.db` (di Railway) ke file CSV yang bisa dibaca AI IDE untuk audit scoring & risk management.

## File Hasil Export
| File | Keterangan |
|---|---|
| `_schema.json` | Schema lengkap semua tabel (nama kolom, tipe data, PK) |
| `{nama_tabel}.csv` | Satu file CSV per tabel di database |
| `SCORING_AUDIT.csv` | View khusus: trades + komponen scoring + korelasi per komponen |
| `SCORING_AUDIT_JOINED.csv` | Jika tabel trades dan signals terpisah, ini hasil JOIN-nya |

## Cara Pakai di Railway

### Opsi 1: Railway CLI (Paling Cepat)
```bash
# Di laptop, dalam folder repo KARA
railway login
railway link  # pilih project KARA
railway run python export_kara_db.py

# Download hasilnya (gunakan Railway dashboard atau SCP jika punya akses)
```

### Opsi 2: One-Off Job di Dashboard
1. Push `export_kara_db.py` ke repo
2. Buka Railway Dashboard -> project KARA
3. Buat "Job" baru, set Start Command: `python export_kara_db.py`
4. Jalankan job, lalu download folder `kara_export/` dari volumes

### Opsi 3: Jalankan di Local (jika DB sudah diunduh)
```bash
# Letakkan kara_data.db di folder yang sama
python export_kara_db.py

# Hasil ada di folder kara_export/
```

## Environment Variables (Opsional)
| Variable | Default | Keterangan |
|---|---|---|
| `KARA_DB_PATH` | `kara_data.db` | Path ke file SQLite |
| `KARA_EXPORT_DIR` | `kara_export` | Folder output CSV |

## Setelah Export
1. Zip folder `kara_export/`
2. Letakkan di root project KARA di laptop
3. AI IDE (Claude Code / Cursor) akan otomatis scan file CSV saat audit

## Catatan untuk Audit Scoring
Script otomatis mendeteksi kolom scoring (OB_, OI_, RSI, CVD, Volume, MTF, dll).
Jika ditemukan, script menghitung korelasi tiap komponen dengan PnL dan menandai:
- `[KEEP]` jika |correlation| > 0.15
- `[REMOVE/ZERO]` jika |correlation| <= 0.15

Hasil perhitungan ini tercetak di log dan bisa digunakan untuk kalibrasi bobot.
