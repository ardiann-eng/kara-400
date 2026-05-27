# KARA — Audit #14 (28 Mei 2026, 23:00 WIB / 16:00 UTC)

## Context

Deploy 27 Mei malam berisi **8 fix** dari Audit #13 findings:

| # | Fix | Root Cause | Expected Impact |
|---|-----|-----------|-----------------|
| A1 | EMA fresh ≤2 (+8pts) | 57% signals got +10 (too easy), r=-0.185 INVERSE | Reduce score inflation, fewer premature entries |
| A2 | OB ×0.6 di ranging | OB r=-0.148 INVERSE in choppy (wall=trap) | OB tidak inflate score di wrong regime |
| A3 | HTF CHOPPY +3 threshold | 98.3% CHOPPY, threshold was 0 (disabled) | Raise bar slightly for entry quality |
| B | LONG TP1 ×0.80 choppy | LONG trailing 30% (was 53.8%), TP1 unreachable | Trailing activate lebih cepat untuk LONG |
| C | XAM 7min/0.08%/×5000 | XAM 10% fire, mostly small pts (4-6) | Fire lebih sering, lebih impactful |
| D1 | Trailing PnL double-count fix | Notification showed 2× actual profit | Correct PnL display |
| D2 | Stop Loss PnL double-count fix | Same bug as trailing | Correct PnL display |
| E | Momentum Death exit | 61% time_exit, flat trades bleed slowly | Exit flat trades at 3min, minimal loss |
| F1 | AI Intelligence — Mimo v2.5 Pro | No AI layer, scoring purely rule-based | AI adds ±8/-5 pts to score as 9th component |
| F2 | AI fallback key | Single key = rate limit = AI offline | Auto-switch ke key 2 kalau key 1 rate limited |
| F3 | AI startup health check | No visibility on AI connection status | Log jelas CONNECTED/RATE LIMITED/FAILED saat boot |
| F4 | AI Dashboard section | No visibility on AI verdicts | Dashboard tab "AI Intel" — verdicts, accuracy, pipeline |
| G | Momentum Death notification | New exit type, no notification template | Notif "MOMENTUM DEATH" dengan reasoning |

---

## Pre-Audit (28 Mei siang)

- [ ] Confirm deploy sukses (Railway logs: "Connected", no crash)
- [ ] **Cek AI connection log: `[AI-CONNECT] MIMO AI: CONNECTED` atau `RATE LIMITED`?**
- [ ] Cek logs: ada `momentum_death` skip/exit entries?
- [ ] Tunggu ~20 jam, kumpulkan 40+ trades
- [ ] Pull data dengan runbook Step 1

---

## Tier 1 — Apakah 8 Fix Bekerja?

### EMA Tighten (FIX A1)
- [ ] **EMA +8 (fresh) rate** — target **<40%** (dari 57% di Audit #13)
- [ ] **EMA +4 (medium) rate** — target **>50%** (dari 41%)
- [ ] **EMA correlation** — target **≥ 0** (dari -0.185)
- [ ] Kalau EMA +8 masih >50% → tighten ke ≤1 candle

### OB Reduction (FIX A2)
- [ ] **OB avg score** — target **<4** (dari +5.2 di Audit #13)
- [ ] **OB correlation** — target **≥ 0** (dari -0.148)
- [ ] Cek: OB=18 masih muncul? Harusnya max 10 di ranging
- [ ] Kalau OB masih inverse → zero OB di ranging (bukan 0.6)

### HTF CHOPPY +3 (FIX A3)
- [ ] **Effective threshold** — cek logs: threshold=48 (45+3) di CHOPPY?
- [ ] **Frequency** — target **1.8-2.5/hr** (dari 2.56, sedikit turun OK)
- [ ] Kalau frequency <1.0/hr → revert ke +0 (terlalu ketat)
- [ ] Kalau frequency masih >3/hr → naikkan ke +5

### LONG TP1 ×0.80 (FIX B)
- [ ] **LONG trailing fire** — target **>40%** (dari 30%)
- [ ] **LONG WR** — target **>48%** (dari 42.5%)
- [ ] **LONG PnL** — target **≥ $0** (dari -$0.02)
- [ ] Cek signal reasons: ada "LONG choppy adj" message?

### XAM Enhancement (FIX C)
- [ ] **XAM fire rate** — target **>15%** (dari 10.3%)
- [ ] **XAM avg pts** — target **>6** (dari avg 4-6)
- [ ] **XAM correlation** — any positive data = good
- [ ] Kalau XAM >40% → threshold terlalu rendah, revert ke 0.10%

### PnL Display Fix (FIX D1+D2)
- [ ] **Trailing stop notification** — PnL masuk akal? (bukan 2× lipat)
- [ ] **Stop loss notification** — PnL correct?
- [ ] Bandingkan notification PnL vs trade_history PnL — harus match

### Momentum Death (FIX E)
- [ ] **Momentum death fire rate** — target **10-25%** of all exits
- [ ] **Momentum death avg loss** — target **< $0.10** (minimal loss)
- [ ] **Time exit rate** — target **<45%** (dari 61%)
- [ ] **Time exit avg loss** — should be LARGER per trade (karena yang kecil sudah di-catch momentum death)
- [ ] Kalau momentum death >40% → threshold 0.05% terlalu ketat, naikkan ke 0.08%
- [ ] Kalau momentum death 0% → cek apakah logic deployed correctly

### AI Intelligence (FIX F1-F4)
- [ ] **AI connection** — cek Railway logs: `[AI-CONNECT] MIMO AI: CONNECTED` atau `RATE LIMITED`?
- [ ] **AI score adj** — cek signal reasons: ada `AI: conf=X.XX (state) → +Xpts`?
- [ ] **AI fire rate** — berapa % signals yang dapat AI adjustment?
- [ ] **AI avg confidence** — target 0.45-0.65 (tidak terlalu tinggi/rendah)
- [ ] **AI fallback** — kalau key 1 rate limited, apakah auto-switch ke key 2?
- [ ] **Dashboard AI Intel tab** — buka `/dashboard` → tab "AI Intel" → data muncul?
- [ ] Kalau AI selalu timeout → cek network Railway ke `token-plan-cn.xiaomimimo.com`
- [ ] Kalau AI confidence selalu >0.8 → model terlalu optimistic, review prompt

---

## Tier 2 — Quality Metrics

- [ ] **Overall WR** — target >48% (dari 44.4%)
- [ ] **Overall PF** — target **>1.3** (dari 1.128)
- [ ] **Total PnL** — target **>+$5** (dari +$2.77)
- [ ] **Score↔PnL r** — target **>+0.10** (dari +0.069)
- [ ] **Trailing stop rate** — target **>40%** (dari 31.5%)
- [ ] **Time exit rate** — target **<40%** (dari 61.1%)
- [ ] **Avg loss per trade** — target **<$0.50** (momentum death should reduce)

### LONG Performance (main focus — was regressing)
- [ ] LONG WR — target >48% (dari 42.5%)
- [ ] LONG trailing — target >40% (dari 30.0%)
- [ ] LONG PnL — target >+$2

### SHORT Performance (should maintain)
- [ ] SHORT WR — target >45% (dari 50.0%)
- [ ] SHORT trailing — target >30% (dari 35.7%)
- [ ] SHORT PnL — target ≥$0 (dari +$2.79)

---

## Tier 3 — Component Correlation

- [ ] OB correlation — target **≥ 0** (dari -0.148)
- [ ] EMA correlation — target **≥ 0** (dari -0.185)
- [ ] RSI correlation — should stay **>+0.15** (was +0.228, BEST)
- [ ] FUND correlation — should stay **>+0.10** (was +0.132)
- [ ] XAM correlation — any positive data = good

---

## Tier 4 — Exit Breakdown Analysis

| Exit Type | Audit #13 | Target #14 | Notes |
|-----------|-----------|------------|-------|
| trailing_stop | 31.5% (17/54) | >40% | Fix B should help LONG |
| time_exit | 61.1% (33/54) | <40% | Momentum death catches flat trades |
| momentum_death | N/A (new) | 10-25% | New exit type — flat trades |
| stop_loss | 7.4% (4/54) | <10% | Should stay low |

- [ ] **Exit type distribution** — verify momentum_death exists in data
- [ ] **Momentum death WR** — should be ~50% (flat = coin flip, but loss minimal)
- [ ] **Momentum death avg PnL** — target **> -$0.10** (near zero)
- [ ] **Time exit avg PnL** — will be MORE negative per trade (only real losers left)
- [ ] **Total drag from exits** — target **< -$10** (dari -$16.85)

---

## 🚨 Red Flags (Rollback)

| Kondisi | Action |
|---|---|
| PF < 0.8 | Rollback semua 8 fix |
| Frequency < 0.5/hr | HTF +3 terlalu ketat → revert A3 |
| Momentum death > 50% | Threshold 0.05% terlalu ketat → naikkan ke 0.10% |
| LONG WR < 35% (worse than #13) | Fix A1/A2 terlalu agresif → revert |
| SHORT WR < 35% | Regresi dari fix → bisect |
| 0 trades dalam 6+ jam | Scoring collapse |
| Trailing < 20% | TP1 ×0.80 masih terlalu tinggi → try ×0.70 |

---

## 🔧 Tuning Matrix

| Kondisi | Action |
|---|---|
| EMA +8 masih >50% | Tighten ke ≤1 candle = fresh |
| OB masih inverse (r < -0.10) | Zero OB di ranging (bukan ×0.6) |
| Frequency < 1.0/hr | Revert HTF +3 → +0 |
| Momentum death > 40% | Threshold 0.05% → 0.08% |
| Momentum death 0% | Cek deploy, mungkin logic tidak ter-trigger |
| LONG trailing masih < 35% | TP1 ×0.80 → ×0.70 (match SHORT) |
| XAM fire > 35% + PnL negatif | Revert threshold ke 0.10% |
| Time exit masih > 50% | Early loss cut -0.2% → -0.15% (more aggressive) |

---

## Decision Points (28 Mei)

| Kondisi | Keputusan |
|---|---|
| PF > 1.3 + trailing > 40% + freq > 1.5/hr | ✅ **START LIVE EXECUTOR DEV** |
| PF 1.1-1.3 + momentum_death working | ⚠️ Fine-tune, audit 29 Mei |
| PF < 1.0 | ❌ Bisect: revert A1+A2 dulu (most impactful) |
| Momentum death working + time_exit < 40% | ✅ Exit system improved |
| LONG still regressing (WR < 40%) | Revert A1, keep A2+A3+B |

---

## Timeline

| Waktu (WIB) | Action |
|---|---|
| 27 Mei malam | Push + deploy 8 fix |
| 28 Mei 00:00-23:00 | Collect trades (~23 jam) |
| **28 Mei 23:00** | **Audit #14** |
| 29 Mei | If pass → live executor dev. If fail → diagnose. |

---

## Referensi (Audit #13 Baseline)

| Metric | Audit #13 | Target #14 |
|---|---|---|
| Trades | 54 (21.1 hrs) | 40-60 |
| Trades/hr | 2.56 | 1.8-2.5 |
| WR | 44.4% | >48% |
| PnL | +$2.77 | >+$5 |
| PF | 1.128 | >1.3 |
| Score↔PnL r | +0.069 | >+0.10 |
| Trailing fire | 31.5% | >40% |
| Time exit | 61.1% | <40% |
| Momentum death | N/A | 10-25% |
| EMA +10 rate | 57% | <40% |
| OB avg score | +5.2 | <4 |
| OB correlation | -0.148 | ≥0 |
| EMA correlation | -0.185 | ≥0 |
| XAM fire | 10.3% | >15% |
| LONG WR | 42.5% | >48% |
| LONG trailing | 30.0% | >40% |
| SHORT WR | 50.0% | >45% |
| SHORT trailing | 35.7% | >30% |

---

## Bisect Order (kalau ada regresi)

1. **Revert E** (momentum death) — paling experimental, bisa exit valid trades too early
2. **Revert A1** (EMA tighten) — mungkin terlalu ketat, reduce frequency
3. **Revert A3** (HTF +3) — combined with A1 bisa terlalu restrictive
4. **Revert A2** (OB ×0.6) — unlikely cause regresi tapi cek
5. **Revert C** (XAM) — additive, worst case = no change
6. **Revert B** (LONG TP1) — unlikely cause regresi (hanya affect exit, not entry)
7. **D1+D2** (PnL display) — cosmetic only, NEVER revert

---

## Catatan

### Kenapa 8 Fix Sekaligus (Lagi)
Deadline 1 Juni = 4 hari. Semua fix address ROOT CAUSES dari data:
- A1-A3 = **scoring quality** (reduce inverse components)
- B = **exit improvement** (mirror SHORT success to LONG)
- C = **additive signal** (XAM proven edge when fires)
- D1-D2 = **bug fix** (display only, no trading impact)
- E = **structural exit improvement** (reduce bleed from flat trades)

### Expected Compound Effect
- A1+A2+A3 together: fewer premature entries → time_exit turun
- B: LONG trailing naik → more profit captured
- E: flat trades exit early → avg loss per trade turun drastis
- Net: PF should improve from both sides (more profit + less loss)
