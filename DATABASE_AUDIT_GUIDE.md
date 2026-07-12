# KARA Database Audit Guide

Panduan ini untuk AI atau operator yang mengaudit KARA dari database Railway.

## Tujuan

Audit harus menjawab dengan data:

- Strategi, aset, sisi, score, regime, dan sesi mana yang menghasilkan atau merugi.
- Kenapa trade berakhir `stop_loss`, `time_exit`, atau `trailing_stop`.
- Apakah loss berasal dari kualitas entry, level SL/TP, waktu hold, execution, atau data yang tidak lengkap.
- Perubahan kecil mana yang paling mungkin memperbaiki root cause.

Jangan menyimpulkan hanya dari log bila database tersedia.

## Safety Rules

- Query Railway hanya read-only: `SELECT`, `PRAGMA integrity_check`, `PRAGMA table_info`.
- Jangan jalankan `INSERT`, `UPDATE`, `DELETE`, `DROP`, `VACUUM`, atau script reset.
- Jangan print environment variable atau secret. Hindari `railway variables --json`.
- Jangan copy database dari Railway kecuali user meminta export eksplisit.
- Jangan pakai `ls -lah` output Railway SSH untuk ukuran file. Output CLI dapat salah format.
- Verifikasi ukuran pakai `wc -c` dan header SQLite pakai `od`.

## Railway Paths

Production saat ini memakai:

```text
DB_PATH=/data/kara_data.db
STORAGE_DIR=/data
ML_DB=/data/kara_ml.db
TRADE_XLSX=/data/trade_history.xlsx
```

Gunakan CLI dari root repository:

```powershell
& "C:\Users\ARDI\AppData\Roaming\npm\railway.cmd" status
& "C:\Users\ARDI\AppData\Roaming\npm\railway.cmd" volume list
& "C:\Users\ARDI\AppData\Roaming\npm\railway.cmd" ssh -- wc -c /data/kara_data.db /data/kara_ml.db /data/trade_history.xlsx
& "C:\Users\ARDI\AppData\Roaming\npm\railway.cmd" ssh -- od -An -tx1 -N16 /data/kara_data.db
```

SQLite header wajib mulai dengan:

```text
53 51 4c 69 74 65 20 66 6f 72 6d 61 74 20 33 00
```

## Database Inventory

`/data/kara_data.db`:

| Table | Fungsi | Nilai audit |
|---|---|---|
| `trade_history` | Closed trade journal | Sumber utama exit, PnL, score, entry/exit |
| `signals_history` | Signal yang lolos pre-trade | Audit keputusan sebelum entry |
| `paper_positions` | Posisi masih terbuka | Exposure dan state saat audit |
| `paper_state` | Balance/equity paper | Drawdown dan konsistensi saldo |
| `meta_pattern_stats` | EMA outcome per pattern | Cek meta learning memberi boost/penalty tepat |
| `vol_cache` | Volatility/regime terakhir | Konteks level SL, TP, trail |
| `oi_snapshots` | Riwayat OI per asset | Validasi thesis OI |
| `risk_state` | State guard risiko per user | Cooldown dan risk guard |
| `history_snapshots` | Equity/PnL timeline | Drawdown dan perubahan equity |

`/data/kara_ml.db`:

| Table | Fungsi |
|---|---|
| `ml_experience` | Feature entry dan label outcome ML |

## Required Data Checks

Lakukan ini sebelum membaca performa:

1. Jalankan `PRAGMA integrity_check`; hasil wajib `ok`.
2. Catat jumlah row setiap table.
3. Catat rentang `created_at` pada `trade_history` dan `signals_history`.
4. Bandingkan closed `trade_history` dengan labeled `ml_experience`.
5. Hitung signal tanpa closed trade dan closed trade tanpa signal.
6. Catat field JSON yang kosong atau berubah antar deployment.

Jika `trade_history` kosong tetapi log menunjukkan trade close, laporkan sebagai persistence defect. Jangan audit strategy dari data parsial tanpa label jelas.

## Trade JSON Contract

`trade_history` menyimpan kolom ringkas plus JSON `data`.

Field minimum expected:

```text
pos_id
asset
side
reason
entry_price
exit_price
size
notional
pnl
pnl_pct
score
meta_boost
meta_pattern_key
timestamp
```

Catat missing field. Saat ini data close belum wajib menyimpan `trade_mode`, `strategy_source`, `duration`, `MFE`, `MAE`, SL trigger, atau slippage. Jangan mengarang nilai field itu.

## Mandatory Analyses

Semua angka harus menyebut sample size (`n`) dan periodenya.

1. Overall: trade count, net PnL, win rate, average PnL, median PnL, profit factor, max drawdown.
2. Exit: group `reason`; count, WR, total/average/median PnL, ROE.
3. Direction: long vs short, lalu reason per side.
4. Asset: minimum `n >= 3`; sort total PnL dan expectancy paling buruk.
5. Score bucket: `60-64`, `65-71`, `72+`; pisahkan per exit dan side.
6. Entry source/mode: `trade_mode` dari joined signal. Pisahkan pure scalper, standard, dan fallback bila field tersedia.
7. Regime, realized volatility, trend, session bonus, OI, orderbook, funding: ambil dari `signals_history` atau `ml_experience`.
8. Time: UTC hour/day; cek clustered losses setelah minimum sample yang memadai.
9. Meta: pattern samples, WR EMA, PnL EMA, delta saat entry, outcome aktual. Jangan percaya pattern di bawah `meta_min_samples`.
10. Data completeness: join rate trade-to-signal dan trade-to-ML label.

## Diagnosis Rules

Gunakan aturan bukti ini:

| Observasi | Diagnosis yang layak |
|---|---|
| `time_exit` dominan, net negatif, entry source bukan pure scalper | Mismatch horizon signal dan exit clock |
| `stop_loss` loss jauh melebihi SL desain | Periksa trigger-to-fill gap, polling cadence, slippage, ATR level aktual |
| WR positif tetapi net PnL negatif | Payoff/risk-reward atau loss tail buruk |
| Loss terkonsentrasi satu asset dan cukup sample | Asset-specific filter atau regime mismatch |
| Loss terkonsentrasi score rendah | Threshold/gating terlalu longgar |
| Loss sama pada score tinggi | Feature score tidak predictive pada regime/source tersebut |
| ML label banyak kosong | Intelligence model tidak punya supervised feedback cukup |
| Signal tidak bisa di-join ke trade | Audit entry feature tidak dapat dipercaya |

Jangan menyimpulkan dari sample kurang dari 10 trade. Tulis `insufficient sample`.

## Query Method

Railway image belum tentu punya `sqlite3` CLI. Gunakan Python standard library melalui Railway SSH. Script harus:

- membuka URI SQLite read-only: `file:/data/kara_data.db?mode=ro`;
- memakai `sqlite3.Row`;
- JSON-decode kolom `data` dengan fallback aman;
- hanya mencetak aggregate dan row anonim yang relevan;
- tidak mencetak secret atau data user sensitif.

Contoh koneksi:

```python
conn = sqlite3.connect("file:/data/kara_data.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
```

## Reporting Format

Laporan audit wajib berurutan:

1. Scope: source, period, sample count, data-quality gaps.
2. Findings: severity tinggi ke rendah, dengan angka dan table/field sumber.
3. Root cause: jelaskan mekanisme code yang membuat pola data tersebut.
4. Recommendation: perubahan spesifik, target file/function/config, expected effect, risk, dan metric validasi.
5. Non-recommendation: hal yang belum layak diubah karena data kurang.
6. Verification plan: metrik before/after, minimum sample, dan rollback condition.

## Required Telemetry Improvement

Trade close harus menambah field berikut agar audit berikut tidak bergantung pada inferensi:

```text
strategy_source
trade_mode
signal_score_raw
signal_score_final
entry_regime
entry_realized_vol
entry_spread_pct
entry_session_utc
planned_sl_price
planned_sl_pct
trigger_price
fill_price
slippage_bps
duration_sec
mfe_pct
mae_pct
time_exit_trigger
```

Tambahkan lewat code change terpisah. Jangan mengubah schema atau backfill saat audit read-only.
