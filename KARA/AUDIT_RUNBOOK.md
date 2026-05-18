# KARA Score Audit Runbook

Jalankan audit ini setiap **7 hari** atau setelah perubahan besar pada scoring engine.
Butuh: Railway CLI login, Python venv aktif, minimal 200 trades di production DB.

---

## Step 1 — Pull data dari Railway production

```powershell
# Di project root (D:\Vibe Coding\KARA - 400)

$script = @'
import sqlite3, json
conn = sqlite3.connect("/app/storage/kara_data.db")
conn.row_factory = sqlite3.Row
cur = conn.cursor()
for tbl, key in [("trade_history","trades"), ("signals_history","signals"), ("meta_pattern_stats","meta")]:
    cur.execute(f"SELECT * FROM {tbl} ORDER BY rowid")
    rows = [dict(r) for r in cur.fetchall()]
    with open(f"/tmp/{key}.json","w") as f:
        json.dump(rows, f, default=str)
    print(f"{tbl}: {len(rows)} rows")
'@
$b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($script))
railway ssh --service kara-400 "echo $b64 | base64 -d > /tmp/e.py && python3 /tmp/e.py"
```

```powershell
# Download ke lokal
railway ssh --service kara-400 "base64 /tmp/trades.json"  | Out-File tmp\trades_b64.txt  -Encoding ascii
railway ssh --service kara-400 "base64 /tmp/signals.json" | Out-File tmp\signals_b64.txt -Encoding ascii
railway ssh --service kara-400 "base64 /tmp/meta.json"    | Out-File tmp\meta_b64.txt    -Encoding ascii

# Decode
$t = Get-Content tmp\trades_b64.txt  -Raw; [System.IO.File]::WriteAllBytes("tmp\trades_prod.json",  [Convert]::FromBase64String($t.Trim()))
$s = Get-Content tmp\signals_b64.txt -Raw; [System.IO.File]::WriteAllBytes("tmp\signals_prod.json", [Convert]::FromBase64String($s.Trim()))
$m = Get-Content tmp\meta_b64.txt    -Raw; [System.IO.File]::WriteAllBytes("tmp\meta_prod.json",    [Convert]::FromBase64String($m.Trim()))
```

---

## Step 2 — Jalankan analisis + dashboard

```powershell
venv\Scripts\python.exe audit_score_analysis\analyze.py
venv\Scripts\python.exe audit_score_analysis\dashboard.py
```

---

## Step 3 — Jalankan PnL simulation

```powershell
venv\Scripts\python.exe tmp\replay_pnl.py
Get-Content tmp\replay_pnl.txt
```

---

## Step 4 — Buka dashboard

```powershell
start audit_score_analysis\kara_score_audit_dashboard.html
```

---

## Step 5 — Baca hasil dan cari anomali

Cek hal-hal berikut di output `analyze.py`:

### Red flags (perlu investigasi):
- `oi_funding_score` atau `liquidation_score` atau `orderbook_score` semua = 0 → analyzer tidak firing (bug F1 kembali)
- Score decile tertinggi WR < 40% → score masih inverse predictive
- `momentum_exit` WR < 50% → re-enable harus dibatalkan
- Profit factor < 0.7 → strategi masih rugi signifikan
- Score ↔ PnL Pearson r < 0.10 → score tidak prediktif

### Green flags (strategi bekerja):
- `trailing_stop` WR > 90% dan n > 20 → trailing aktif lebih sering (F4 bekerja)
- Score decile 7-9 WR > 55% → score mulai prediktif
- Profit factor > 0.85 → mendekati break-even
- `momentum_exit` tidak muncul → F2 bekerja

---

## Step 6 — Prompt untuk AI audit

Paste prompt ini ke AI (Kiro/Claude/GPT) bersama output dari Step 2-3:

```
Saya punya data audit KARA trading bot dari Railway production.
Berikut hasil analyze.py dan replay_pnl.txt.

Tolong:
1. Bandingkan dengan audit sebelumnya (AUDIT_REPORT.md di audit_score_analysis/)
2. Cek apakah F1-F5 fixes masih bekerja atau ada regresi
3. Identifikasi finding baru yang belum ada di audit sebelumnya
4. Berikan rekomendasi P0/P1/P2 berdasarkan data terbaru
5. Update AUDIT_REPORT.md dengan findings baru

Data periode: [isi tanggal]
Total trades: [isi dari output]
Win rate: [isi]
Total PnL: [isi]
```

---

## Referensi

| File | Deskripsi |
|---|---|
| `audit_score_analysis/AUDIT_REPORT.md` | Laporan audit terakhir |
| `audit_score_analysis/analyze.py` | Script analisis utama |
| `audit_score_analysis/dashboard.py` | Generator dashboard HTML |
| `audit_score_analysis/kara_score_audit_dashboard.html` | Dashboard interaktif |
| `tmp/replay_pnl.py` | PnL simulation old vs new |
| `tmp/replay_audit.py` | Score replay (logic comparison) |
| `KARA/AUDIT_RUNBOOK.md` | File ini |

---

## Changelog Audit

| Tanggal | Trades | WR | PnL | Profit Factor | Catatan |
|---|---|---|---|---|---|
| 2026-05-18 | 338 (12 jam) | 48.8% | −$67.22 | 0.65 | Baseline. F1-F5 fixes diterapkan. |
| _(next audit)_ | | | | | |
