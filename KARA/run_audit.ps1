# KARA Audit Runner
# Usage: .\KARA\run_audit.ps1
# Pulls production data, runs analysis, opens dashboard.

Set-Location "$PSScriptRoot\.."
$ErrorActionPreference = "Stop"

Write-Host "`n=== KARA Score Audit ===" -ForegroundColor Cyan
Write-Host "Project: $(Get-Location)"

# ── Step 1: Pull from Railway ─────────────────────────────────────────────────
Write-Host "`n[1/4] Pulling data from Railway..." -ForegroundColor Yellow

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

railway ssh --service kara-400 "base64 /tmp/trades.json"  | Out-File tmp\trades_b64.txt  -Encoding ascii
railway ssh --service kara-400 "base64 /tmp/signals.json" | Out-File tmp\signals_b64.txt -Encoding ascii
railway ssh --service kara-400 "base64 /tmp/meta.json"    | Out-File tmp\meta_b64.txt    -Encoding ascii

$t = Get-Content tmp\trades_b64.txt  -Raw; [System.IO.File]::WriteAllBytes("$PWD\tmp\trades_prod.json",  [Convert]::FromBase64String($t.Trim()))
$s = Get-Content tmp\signals_b64.txt -Raw; [System.IO.File]::WriteAllBytes("$PWD\tmp\signals_prod.json", [Convert]::FromBase64String($s.Trim()))
$m = Get-Content tmp\meta_b64.txt    -Raw; [System.IO.File]::WriteAllBytes("$PWD\tmp\meta_prod.json",    [Convert]::FromBase64String($m.Trim()))

Write-Host "Data pulled OK" -ForegroundColor Green

# ── Step 2: Run analysis ──────────────────────────────────────────────────────
Write-Host "`n[2/4] Running analysis..." -ForegroundColor Yellow
venv\Scripts\python.exe audit_score_analysis\analyze.py
Write-Host "Analysis OK" -ForegroundColor Green

# ── Step 3: Generate dashboard ────────────────────────────────────────────────
Write-Host "`n[3/4] Generating dashboard..." -ForegroundColor Yellow
venv\Scripts\python.exe audit_score_analysis\dashboard.py
Write-Host "Dashboard OK" -ForegroundColor Green

# ── Step 4: PnL simulation ────────────────────────────────────────────────────
Write-Host "`n[4/4] Running PnL simulation..." -ForegroundColor Yellow
venv\Scripts\python.exe tmp\replay_pnl.py
Write-Host ""
Get-Content tmp\replay_pnl.txt

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host "`n=== Audit Complete ===" -ForegroundColor Cyan
Write-Host "Dashboard: audit_score_analysis\kara_score_audit_dashboard.html"
Write-Host "Report:    audit_score_analysis\AUDIT_REPORT.md"
Write-Host ""

$open = Read-Host "Open dashboard in browser? (y/n)"
if ($open -eq "y") {
    Start-Process "audit_score_analysis\kara_score_audit_dashboard.html"
}
