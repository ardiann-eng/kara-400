# KARA Audit #8 — 23 Mei 2026

## Changes Deployed (22 Mei Malam)

1. **Whale/CVD sell side bug fix** — added `'A'` (HL format) to sell detection
2. **ATR gate LONG** — 0.0010 → 0.0013
3. **Vote margin gate** — margin < 4 → threshold +5
4. **OI score gate** — abs(OI) < 6 → threshold +3
5. **Funding negative bonus** — FR < 0 + LONG → threshold -3

## Baseline (Audit #7, 22 Mei)

| Metric | Value |
|---|---|
| Trades | 57 (5.6 jam) |
| Trades/hr | 10.2 |
| WR | 52.6% |
| PnL | +$8.23 |
| PF | 1.115 |
| Score↔PnL r | +0.098 |
| Trailing fire rate | 33.3% |
| time_exit % | 59.6% |
| time_exit WR | 29.4% |
| Whale fire rate | 89.6% (BROKEN — now fixed) |
| CVD | constant bullish (BROKEN — now fixed) |

## What to Check (in order)

### Tier 1: Apakah Bug Fix Tidak Merusak?

- [ ] **Overall PnL ≥ $0** (baseline +$8.23)
- [ ] **Score↔PnL r ≥ +0.08** (baseline +0.098)
- [ ] **Trailing fire rate ≥ 25%** (baseline 33.3%)
- [ ] **Trades/hr ≥ 5** (kalau <3 = filter terlalu ketat)

### Tier 2: Whale + CVD Fix Bekerja?

- [ ] **Whale fire rate** — target 20-50% (was 89.6%)
- [ ] **Whale buy vs sell** — harus ada MIX (bukan 100% buy lagi)
- [ ] **Whale-voted trades WR** — harus > overall WR (kalau < → disable whale)
- [ ] **CVD distribution** — harus ada bullish DAN bearish signals (bukan 100% bullish)
- [ ] **SHORT signal count** — harus naik (CVD fix memungkinkan bear detection)

### Tier 3: Filter Gates Bekerja?

- [ ] **ATR gate blocks** — berapa trades di-skip oleh ATR 0.0013? (`skip_counters["low_atr"]`)
- [ ] **Vote margin gate blocks** — berapa? (`_vote_margin_adj` fires)
- [ ] **OI gate blocks** — berapa? (threshold naik +3)
- [ ] **Funding bonus fires** — berapa LONG masuk karena FR < 0? Apakah profitable?
- [ ] **time_exit %** — target < 50% (was 59.6%)

### Tier 4: Regresi Check

- [ ] **OI score vs outcome** — masih predictive? (OI≥6 harus WR > OI<6)
- [ ] **Vote margin vs outcome** — high margin masih WR > low margin?
- [ ] **EXTREME regime** — masih best performer?
- [ ] **Score decile 9 (75-90)** — masih underperform? (whale fix should help)

## Red Flags (REVERT jika)

- PnL < -$5
- Score correlation < 0
- Trailing fire rate < 15%
- 0 trades dalam 6 jam
- Whale fire rate masih >80% (fix tidak bekerja)

## Success Criteria

- PnL > +$10
- WR > 55%
- Whale fire rate 20-50% dengan mix buy/sell
- time_exit < 50%
- Trailing fire rate maintained >30%

## Runbook

```powershell
# 1. Pull data (sama seperti AUDIT_RUNBOOK.md Step 1)
# 2. Deduplicate
# 3. Run: venv\Scripts\python.exe tmp\deep_audit7.py
# 4. Run: venv\Scripts\python.exe tmp\deep_audit7b.py
# 5. Run: venv\Scripts\python.exe tmp\deep_audit7c.py
# 6. Run: venv\Scripts\python.exe tmp\deep_audit7d.py
# 7. Paste output ke Kiro untuk analisis
```
