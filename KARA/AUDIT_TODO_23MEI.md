# KARA Audit #7 — 23 Mei 2026

## Context: Perubahan Deploy 22 Mei 2026

### Fix 1: Direction Decision Restructured (ROOT CAUSE dari 87.7% wrong direction)
- **Sebelum:** `side = LONG if bull_setup >= bear_setup` — OB imbalance (r=-0.098) mendominasi direction
- **Sesudah:** Voting system 7 voters, OB DIKELUARKAN dari direction vote
- **Data basis:** OB dominates → WR 38.8%, PnL -$7.25. OI dominates → WR 54.2%, PnL +$7.83

### Fix 2: EXTREME Regime → Threshold +15 (bukan block)
- **Sebelum:** EXTREME hanya ×0.90 (tetap lolos)
- **Sesudah:** Threshold +15, hanya score 71+ yang lolos di EXTREME
- **Data basis:** EXTREME score<60 = WR 33.3%. Score 71+ = WR 44.4%, +$0.91

### Fix 3: Trend-Flip → Trend Structure Veto
- **Sebelum:** Flip LONG→SHORT kalau price below EMA21 (2 fires, 2 losses, -$4.76)
- **Sesudah:** Skip (jangan entry) kalau direction melawan trend structure
- **Data basis:** Flip 0% WR. SHORT structural WR 20%. Flip = masuk sisi lemah.

### Fix 4: 4H HTF Regime sebagai Direction Vote (weight 2)
- TRENDING_UP → +2 bull vote, TRENDING_DOWN → +2 bear vote
- **Data basis:** HTF aligned trades PnL +$3.74 vs choppy -$0.52

### Fix 5: Momentum Strength Confidence Vote
- Momentum >0.5% → extra +1 vote ke arah momentum
- **Data basis:** Mom 0.50%+ = WR 57.6%, PnL +$11.85. Mom 0.30-0.50% = WR 36.4%, -$12.61

### Fix 6: Large Trade Imbalance / Whale Detection (weight 2)
- Filter trades >3× median size (whale), hitung buy vs sell imbalance
- Vote +2 kalau whale imbalance >30% ke satu sisi
- **Data basis:** Belum ada — fitur baru, perlu validasi

---

## What to Check (in order)

### Tier 1: Apakah Fix Tidak Merusak? (HALT jika gagal)

1. **Overall PnL** — harus ≥ $0 (baseline Audit #6: +$0.58)
2. **Score↔PnL correlation** — harus ≥ +0.08 (baseline: +0.085). Kalau turun ke negatif → REVERT
3. **Trailing stop fire rate** — harus ≥ 25% (baseline: 33%). Kalau turun drastis → direction fix merusak
4. **Trades/hour** — estimasi turun ke 5-7/hr (dari 8.2). Kalau <3 → threshold terlalu ketat

### Tier 2: Apakah Direction Fix Bekerja?

5. **Direction accuracy** — berapa % trades yang harga bergerak SEARAH posisi dalam 5 menit pertama?
   - Baseline Audit #6: 12.3% (hanya 8/65 time_exit positif)
   - Target: >30%
6. **time_exit count** — harus turun dari 56.5% (65/115)
   - Target: <45%
7. **time_exit WR** — harus naik dari 12.3%
   - Target: >20%
8. **Whale vote firing rate** — berapa % signals punya "🐋" di reasons?
   - Kalau 0% → threshold 30% terlalu ketat, turunkan ke 20%
   - Kalau >80% → threshold terlalu longgar, naikkan ke 40%
   - Target: 20-50%

### Tier 3: Per-Component Validation

9. **Direction vote breakdown** — parse reasons untuk "🧭 Direction" line:
   - Berapa kali OI mendominasi vs EMA vs HTF vs Whale?
   - Apakah ada voter yang SELALU menang? (= yang lain tidak berguna)
10. **EXTREME regime** — berapa signals di-block oleh threshold +15?
    - Kalau 0 → EXTREME jarang terjadi (OK)
    - Kalau >50% total signals → terlalu banyak di-block
11. **Trend structure veto** — berapa kali fire? (`skip_counters["trend_structure_veto"]`)
    - Kalau 0 → kondisi jarang terpenuhi (OK, conservative)
    - Kalau >30% → terlalu agresif, longgarkan threshold dari 0.2% ke 0.3%
12. **SHORT performance** — WR harus >25% (baseline: 20%)

### Tier 4: Whale Detection Validation (BARU — belum ada baseline)

13. **Whale vote vs outcome** — trades dengan whale vote: WR berapa?
    - Parse signals reasons untuk "🐋"
    - Match ke trade outcome
    - Kalau whale-voted trades WR < overall WR → whale detection COUNTER-PREDICTIVE → DISABLE
    - Kalau whale-voted trades WR > overall WR + 10% → KEEP dan naikkan weight
14. **Whale vote alignment** — apakah whale vote sering AGREE atau DISAGREE dengan final direction?
    - Kalau selalu agree → redundant (tidak menambah info)
    - Kalau sering disagree tapi trade tetap win → whale vote noise

---

## Red Flags (REVERT semua changes jika)

- PnL < -$5 (regression dari +$0.58)
- Score correlation < 0 (inverse kembali)
- Trailing fire rate < 15% (edge hilang)
- 0 trades dalam 6 jam (semua di-block)

## Success Criteria

- PnL > +$5 (10× improvement dari baseline)
- WR > 50%
- time_exit < 45%
- Direction accuracy > 30% (dari 12.3%)
- Trailing fire rate maintained >25%

## Baseline Data (Audit #6, 22 Mei)

| Metric | Value |
|---|---|
| Trades | 115 (14 jam) |
| Trades/hr | 8.2 |
| WR | 45.2% |
| PnL | +$0.58 |
| PF | 1.009 |
| Score↔PnL r | +0.085 |
| Trailing fire rate | 33% |
| time_exit % | 56.5% |
| time_exit WR | 12.3% |
| SHORT WR | 20% |
| OB↔PnL r | -0.098 (counter-predictive) |
| OI↔PnL r | +0.091 (predictive) |

## How to Parse New Components

```powershell
# Setelah pull data (Step 1 runbook), parse direction votes dan whale:
$signals = Get-Content tmp\signals_prod.json | ConvertFrom-Json
$signals | ForEach-Object {
    $reasons = if ($_.reasons -is [string]) { $_.reasons | ConvertFrom-Json } else { $_.reasons }
    $dirLine = $reasons | Where-Object { $_ -match "Direction" }
    $whaleLine = $reasons | Where-Object { $_ -match "Whale" }
    if ($dirLine) { Write-Host "$($_.asset) $($_.side): $dirLine" }
    if ($whaleLine) { Write-Host "  WHALE: $whaleLine" }
}
```

## Files Modified (22 Mei)

- `engine/scoring_engine.py` — direction voting, EXTREME threshold, trend veto, whale detection
