# KARA Audit #6 — 22 Mei 2026

## Context
Deployed 4 fixes malam 21 Mei:
1. ATR gate: LONG ≥ 0.0010, SHORT ≥ 0.0015
2. SHORT momentum gate: 0.25% min dir_move
3. SHORT candle confirmation: 3/5 bearish for leading signals
4. short_min_funding_rate: relaxed to -0.0003

## What to Check (in order)
1. Pull fresh data dari Railway (ikuti AUDIT_RUNBOOK.md)
2. ATR gate firing rate — berapa trades di-block oleh `low_atr`?
3. time_exit count — turun dari 65% (68/104) ke berapa?
4. SHORT performance — WR naik dari 22%? Berapa SHORT yang lolos?
5. ALGO SHORT — apakah lolos setelah funding fix dan profitable?
6. **TREND-FLIP effectiveness** — berapa kali fire? Win rate? Apakah pisau bermata dua?
   - Cek: flip yang profitable vs flip yang loss
   - Kalau flip WR < 30% → DISABLE, ganti dengan BLOCK (skip trade)
   - Kalau flip WR > 45% → KEEP
   - Double confirmation (price below EMA21 + EMA8<EMA21) cukup ketat?
7. Overall PnL — flip positive?
8. Score↔PnL correlation — improving dari r=-0.023?
9. Trailing stop firing rate — maintained >20%?

## Red Flags (pause trading if)
- ATR gate blocks >80% trades (too tight)
- PnL worse than -$0.63 (regression)
- Score correlation goes negative again

## Success Criteria
- time_exit < 50% of trades
- SHORT WR > 35%
- Overall PnL positive
- Trailing fire rate > 20%

## Key Data Points from Audit #5 (baseline)
- 104 trades, 5.2 jam, 20.1 trades/hr
- WR 35.6%, PnL -$0.63, PF 1.788
- time_exit: 68/104 (65%), WR 8.8%, -$41.85
- trailing_stop: 23/104 (22%), WR 100%, +$31.54
- SHORT: 27 trades, WR 22%, -$3.42
- LONG: 77 trades, WR 40%, +$2.79
- sig_realized_vol r=+0.457 (strongest predictor)

## Files
- Runbook: `KARA/AUDIT_RUNBOOK.md`
- Analysis: `audit_score_analysis/analyze.py`
- Previous report: `audit_score_analysis/AUDIT_REPORT.md`
- Persona: `.kiro/steering/persona.md` (has Audit #5 data)
