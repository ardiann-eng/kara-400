# KARA — Audit #20 (6 Juni 2026) — VERIFIKASI MEGA-FIX 4 JUNI

> **Aturan:** Tidak ada kesimpulan tanpa data produksi fresh (Railway DB, single-user dedup).
> Korelasi **DIRECTIONAL**. Sampel < 100 = bug-detection, bukan validasi strategi.
> Action item: `[what]` → `[why]` → `[how to verify]`.
> **Deploy 4 Juni = multi-fix (7 perubahan). Atribusi per-komponen wajib diukur.**

---

## 0. KONTEKS — Audit #19 (169 trade, user 7667519263, 2-4 Juni)

| Metrik | Nilai | Root Cause |
|--------|-------|------------|
| Win rate | 37.3% | Entry timing + exhaustion |
| Profit factor | **0.75** | Pre-TP1 trail scratch (87/92 trailing = coin flip) |
| Trailing WR | **54.3%** (was 100%) | Bug: arm +0.10%, width 0.12% = stop di -0.02% |
| Time exit % | 32% (54/169) | −$57.05, WR 11% — entry di flat market |
| Score↔PnL r | +0.093 | Mildly positive (noise inflation masked edge) |
| OI/Funding r | **−0.179** (p=0.02) | Mild funding +8 = constant LONG bias (fire 38%) |
| CVD extreme | WR **26.3%** (n=76) | Entry saat flow exhausted, both sides |
| RSI momentum | WR 34.9% vs 41.7% tanpa | Overbought confirmation = late entry |
| L/S Ratio | WR 12.5% saat disagree | Contrarian signal melawan arah final trade |
| OB strong + CVD mod | WR **54.2%** | **Kombinasi edge terbaikx** |
| OB strong + CVD ext | WR **27.0%** | CVD extreme membunuh OB edge |

---

## 1. PERUBAHAN YANG HARUS DIVERIFIKASI (deploy 4 Juni)

### Pack A — Exit System (P0, dampak terbesar)

| # | Fix | File | Root Cause | Expected |
|---|-----|------|------------|----------|
| E1 | **Rule D0 (pre-TP1 trail) DIHAPUS** | risk_manager.py | Stop di -0.02% → 87 scratch exits | Trailing kembali ~100% WR |
| E2 | **L1 early trail arming DISABLED** | risk_manager.py | Arm di +0.10% = terlalu dini | Trade develop ke TP1 |
| E3 | `early_trail_activation` 0.10%→**0.25%** | config.py | Noise threshold | Safety net di +0.25% |
| E4 | `quick_profit_threshold` 0.20%→**0.40%** | config.py | Cut runners terlalu dini | Biarkan berkembang |
| E5 | `atr_trail_post_tp1_extra` 0.1%→**0.2%** | config.py | Trail arms terlalu dekat TP1 | Need real extension |

`[how to verify]`
- ✅ trailing_stop WR **≥85%** (was 54%)
- ✅ trailing_stop count **TURUN** (hanya post-TP1 yang fire, bukan scratch)
- ✅ time_exit % **NAIK sedikit** (trade yang dulu di-cut pre-TP1 sekarang jadi time_exit/momentum_death)
- ✅ PF **≥1.5** (was 0.75)
- 🔴 trailing WR < 70% → cek apakah early_trail (Rule F) masih fire terlalu dini

### Pack B — Scoring Fixes (P0-P1)

| # | Fix | File | Root Cause | Expected |
|---|-----|------|------------|----------|
| S1 | **OI/Funding: mild = ZERO poin** | oi_funding_analyzer.py | Fire 38% = constant LONG bias | OI r naik dari −0.179 → ≥0 |
| S2 | **OI Section 4: px_min 0.05%→0.15%** | oi_funding_analyzer.py | Noise confirmation (fire 58%) | OI hanya fire saat REAL move |
| S3 | **OI max pts: ±22→±5** | oi_funding_analyzer.py | OI bukan primary edge | Proporsional, bukan dominan |
| S4 | **CVD extreme: 0 → PENALTY −5** | scoring_engine.py | WR 26.3%, exhaustion | Kurangi entry saat exhausted |
| S5 | **RSI overbought (>65): +8 → PENALTY −4** | scoring_engine.py | Late entry confirmation | Stop masuk di top |
| S6 | **RSI fresh (<60): +8 → +3** | scoring_engine.py | Proporsional | Tetap konfirmasi tanpa inflate |
| S7 | **MFI cap +8→+4, MFI>85 = penalty −3** | scoring_engine.py | Redundan CVD, exhaustion | Hilangkan inflation |
| S8 | **OB counter-trend (TRENDING_DOWN+LONG) = 0** | scoring_engine.py | Wall melawan trend = dibreak | OB TRENDING_DOWN WR naik |
| S9 | **L/S Ratio: guard kontradiksi** | scoring_engine.py | +12 bear saat trade LONG = kontradiksi | Hanya kasih poin kalau aligned |
| S10 | **Basis cap ±10→±3** | oi_funding_analyzer.py | Double-count dengan funding | Proporsional |

`[how to verify]`
- Score↔PnL r **> +0.10** (was +0.093, seharusnya naik karena noise hilang)
- OI/Funding r **≥ 0** (was −0.179)
- CVD extreme trade count **TURUN** (lebih banyak di-skip oleh threshold)
- CVD moderate WR **tetap ≥45%** (edge utama entry)
- L/S contradiction count = 0 (semua L/S-fire trades harus aligned)
- Score decile top **WR > 45%** (was 45.5% — should maintain/improve)
- Volume trade per jam **≥ 0.8** (gate tidak over-block)

---

## 2. STEP PERTAMA — Tarik data (WAJIB)

```powershell
cd "D:\Vibe Coding\KARA - 400"
# Deploy boundary
railway ssh --service rare-youthfulness "stat -c %Y /app/config.py"
# Export
$script = Get-Content tmp\export_prod.py -Raw
$b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($script))
railway ssh --service rare-youthfulness "echo $b64 | base64 -d > /tmp/e.py && python3 /tmp/e.py"
# Download + decode (lihat AUDIT_RUNBOOK.md Step 1-1b)
```

`[what]` Boundary deploy: mtime config.py. Hanya trade POST-deploy yang validasi fix.
`[why]` Deploy 4 Juni = 7 fix sekaligus. Butuh boundary presisi.
`[how to verify]` Trade POST ≥ **40** sebelum klaim apapun. PnL tarik ≈ jurnal Telegram.

---

## 3. VERIFIKASI PER FIX (ANGKA)

### E1-E5: Trail System

| Metrik | Audit #19 (PRE) | Target #20 (POST) |
|--------|-----------------|-------------------|
| trailing_stop count | 92 (55% of trades) | **<30%** (hanya post-TP1) |
| trailing_stop WR | 54.3% | **≥85%** |
| trailing avg PnL% | +0.025% | **≥+0.20%** (real runners) |
| time_exit count | 54 (32%) | ≤45% (some ex-scratch trades move here) |
| time_exit WR | 11.1% | ≥15% (better entries overall) |

`[how to verify]`
- Log: "Pre-TP1 trail hit" **TIDAK ADA** (Rule D0 dihapus)
- Log: "Quick-profit exit" hanya fire di **≥+0.40%** (bukan 0.20%)
- Trailing winners avg pnl% ≥ 0.15% (meaningful, not scratch)

### S1-S3: OI/Funding

`[what]` Korelasi `oi_funding_score ↔ PnL` directional, POST-deploy.
`[how to verify]`
- ✅ r ≥ 0 (was −0.179)
- ✅ Log: "Flat/noise funding" lebih sering muncul (mild = no pts)
- ✅ "OI surge + price up" hanya fire saat price_5m > 0.15% (bukan 0.05%)
- ✅ Max OI kontribusi dalam 1 signal = 8 pts (bukan 22)
- 🔴 r masih < −0.10 → cek apakah slope (Section 2) masih terlalu besar

### S4: CVD Extreme Penalty

`[what]` CVD extreme trades: count, WR, PnL vs Audit #19.
`[how to verify]`
- ✅ CVD extreme trade COUNT turun (penalty −5 buat score turun → banyak di-block threshold)
- ✅ CVD extreme yang masih lolos: WR > 30% (sudah filtered ke yang punya edge lain)
- ✅ CVD moderate WR **tetap ≥45%** (tidak terpengaruh)
- 🔴 CVD extreme count sama → threshold terlalu rendah, naikkan penalty ke −8

### S5-S6: RSI Momentum

`[what]` RSI momentum trades: WR per RSI bucket.
`[how to verify]`
- ✅ Log "RSI overbought + momentum → late entry PENALTY" muncul
- ✅ Log "RSI fresh momentum" hanya saat RSI < 60
- ✅ SHORT + RSI momentum WR **≥35%** (was 26.9%)
- 🔴 "late entry PENALTY" > 50% signals → RSI almost always overbought → reduce to penalty −2

### S7: MFI

`[what]` MFI contribution: avg pts per signal, WR bucket.
`[how to verify]`
- ✅ MFI max = +4 (bukan +8 lagi)
- ✅ MFI > 85 → log "flow exhaustion penalty −3"
- ✅ MFI-high trades WR **≥ MFI-low WR** (no longer noise/inverse)

### S8: OB Counter-Trend

`[what]` OB strong + TRENDING_DOWN + LONG: count, WR.
`[how to verify]`
- ✅ Log "OB wall... COUNTER HTF... no bonus" muncul
- ✅ OB strong overall WR **≥42%** (was 40%, karena counter-trend tidak inflate)
- ✅ OB strong + CVD moderate WR **≥50%** (was 54.2% — the real edge)

### S9: L/S Guard Kontradiksi

`[what]` L/S signal splits: aligned vs contradiction.
`[how to verify]`
- ✅ Log "contradiction" saat LONG + L/S says SHORT → **0 pts given**
- ✅ Tidak ada trade LONG dengan L/S bear pts > 0
- ✅ L/S aligned trades (SHORT + L/S SHORT): WR ≥ 35%
- 🔴 L/S aligned WR < 30% → signal sendiri noisy → reduce pts ke ±4

---

## 4. METRIK AGREGAT GO / NO-GO

| Metrik | Audit #19 (PRE) | Target #20 (POST) | Gate |
|--------|-----------------|-------------------|------|
| Profit Factor | 0.75 | **≥ 1.5** | Trail fix + scoring fix |
| Win Rate | 37.3% | **≥ 40%** | Fewer bad entries |
| Trailing WR | 54.3% | **≥ 85%** | Only post-TP1 |
| Score↔PnL r | +0.093 | **≥ +0.15** | Noise removed |
| OI↔PnL r | −0.179 | **≥ 0** | Mild bias removed |
| Time exit % | 32% | **≤ 40%** | Acceptable ceiling |
| Volume (trade/hr) | ~4.7 | **≥ 2.0** | Gates don't over-block |
| PnL (40+ trade POST) | −$20.27 | **≥ break-even** | Fundamental shift |

**GO micro-live** hanya jika: PF ≥1.5 + trail WR ≥85% + 50 trade POST + score r > +0.10.

---

## 5. RED FLAGS

| Kondisi | Action |
|---------|--------|
| Trailing WR < 70% POST | Early trail (Rule F) masih fire terlalu dini → naikkan `early_trail_activation` ke 0.35% |
| PF < 1.0 (40+ trade) | Bisect: disable scoring fixes (keep trail fix) → isolate |
| Volume turun > 60% | CVD penalty + RSI penalty terlalu agresif → reduce penalty ke −3 |
| Score↔PnL r < 0 | Scoring fixes backfired → cek RSI penalty over-blocking good trades |
| CVD moderate WR < 35% | CVD proxy degraded → recheck proxy vs tick CVD |
| time_exit > 55% | Trades tidak reach TP1 → TP1 terlalu jauh? Lower to 0.4×SL |
| L/S aligned WR < 25% | L/S signal sendiri noise → reduce ke ±2 pts |
| OB strong WR < 35% | CVD extreme still poisoning → verify penalty firing |

---

## 6. BISECT PROTOCOL (jika regresi)

7 fix sekaligus. Urut paling berisiko jika harus isolasi:

1. **S4 (CVD penalty −5)** — terbesar dampaknya ke volume. Cek: berapa trade di-block?
2. **S5 (RSI penalty −4)** — fire rate 64.5%. Kalau semua RSI fire → over-penalize.
3. **E1-E2 (trail disabled)** — JANGAN revert ini. Data 100% jelas (87 scratch).
4. **S1-S3 (OI overhaul)** — bisa temporary re-enable mild +4 (bukan +8) kalau volume drop.
5. **S9 (L/S guard)** — paling aman, hanya prevent kontradiksi.
6. **S7 (MFI cap)** — low risk, MFI noise bukan driver.
7. **S8 (OB counter-trend)** — low risk, hanya TRENDING_DOWN+LONG subset.

Toggle per fix: `_CVD_PENALTY_ENABLED`, `_RSI_PENALTY_ENABLED` etc bisa ditambah kalau perlu.

---

## 7. EXPECTED TRADE FLOW POST-FIX

```
Signal masuk → scoring engine:
  ├─ OI/Funding: hanya extreme contrarian (±5-8) atau real OI confirmation (±3-5)
  ├─ OB: +15 (aligned, non-crowded, non-counter-trend) atau 0/+8
  ├─ CVD moderate: +6 (sweet spot) / CVD extreme: −5 (exhaustion)
  ├─ RSI: fresh +3 / overbought −4 / neutral 0
  ├─ MFI: +1 to +4 / exhaustion −3
  ├─ L/S: aligned +4-8 / contradiction 0
  ├─ EMA: +4-5 (unchanged, 96% fire = neutral floor)
  └─ Session: +10-14 (unchanged, marginal positive)

Score range expected: 40-75 (was 52-100 karena inflation)
Threshold 45 mungkin PERLU diturunkan ke 40 jika volume drop > 50%.

Trade enters → exit system:
  ├─ Momentum death: flat 4min + peak < 0.10% → exit (unchanged)
  ├─ Early loss cut: −0.2% after 5min → exit (unchanged)
  ├─ TP1 hit (0.5×SL ≈ 0.40%): close 50% → ATR trail arms
  ├─ ATR trail: post-TP1 + 0.2% extension → ride trend (THE EDGE)
  ├─ Quick profit: +0.40% + retrace 0.20% → exit (safety net)
  ├─ Early trail: +0.25% + retrace 0.15% → exit (safety net)
  └─ Time exit: 15-25min → exit (dump bucket, acceptable loss)
```

---

## 8. DATA POINTS YANG HARUS DI-PERSIST (Pack C — deploy berikutnya)

| # | Item | Prioritas | Reason |
|---|------|-----------|--------|
| N1 | Persist `cvd_pts`, `rsi_penalty`, `mfi_pts`, `ls_aligned` ke DB signals | P1 | Audit per-component tanpa re-parse reasons text |
| N2 | Asset hard block (0W n≥3) — GRASS, WLD, FIL | P1 | Known losers drain capital |
| N3 | Tick CVD vs proxy comparison | P2 | Validate proxy masih accurate |
| N4 | Score threshold recalibration (45 → ?) | P1 | Score range berubah post-fix |
| N5 | Micro-live HL $10-20 | P0 post-audit | GO criteria met → test real execution |

---

## 9. TIMELINE 6 JUNI (WIB)

| Waktu | Action |
|-------|--------|
| Pagi | Deploy verify: `railway ssh` boundary timestamp. Confirm code live. |
| Siang | Pull POST data. Minimum 40 trade. Jalankan `tmp/deep_audit.py`. |
| Sore | Verifikasi §3 per fix. Isi tabel dengan ANGKA. |
| Malam | GO/NO-GO (§4). PASS → deploy N2 (asset block). FAIL → bisect (§6). |

---

## 10. COMMIT BOUNDARY (4 Juni deploy)

Files touched:
- `config.py` — trail thresholds, quick profit, early trail
- `risk/risk_manager.py` — Rule D0 removed, L1 disabled
- `engine/scoring_engine.py` — CVD penalty, RSI overhaul, MFI cap, OB counter-trend, L/S guard
- `engine/analyzers/oi_funding_analyzer.py` — Mild=0, px_min 0.15%, cap ±5, basis ±3

Marker log yang harus muncul di production:
- ❌ `Pre-TP1 trail hit` (HARUS TIDAK ADA — Rule D0 dihapus)
- ✅ `CVD 5m extreme ... exhaustion PENALTY -5`
- ✅ `RSI ... overbought + momentum → late entry PENALTY -4`
- ✅ `RSI ... fresh momentum ... +3`
- ✅ `MFI ... flow exhaustion, late entry penalty (-3)`
- ✅ `OB wall... COUNTER HTF ... no bonus`
- ✅ `L/S ratio ... contradiction`
- ✅ `Flat/noise funding ... no signal` (mild positive path killed)
- ✅ `OI surge + price up → bullish confirmation (+5)` (hanya saat px > 0.15%)

---

## 11. PRINSIP AUDIT #20

1. **Trail = senjata. Hanya post-TP1.** Data: 5 real runners = 100% WR, +$2.23/trade. 87 scratch = 51.7% WR, +$0.31/trade. Pilihan obvious.
2. **Exhaustion ≠ momentum.** CVD extreme, RSI >65, MFI >85 = flow SUDAH HABIS. Entry setelah exhaustion = chasing, bukan trading.
3. **Constant signal = no signal.** OI mild fire 38%, RSI mom fire 64.5% = noise floor yang inflate score tanpa edge. Post-fix: komponen harus fire < 50% untuk dianggap diskriminatif.
4. **Contrarian tanpa follow-through = kontradiksi.** L/S bilang SHORT, trade ambil LONG → +12 bear merusak score integrity. Guard kontradiksi = basic hygiene.
5. **Wall melawan trend = akan dibreak.** LONG + bid wall + TRENDING_DOWN = wall yang akan dimakan. Jangan kasih bonus.
6. **Score range akan turun.** Post-fix, raw score ~40-75 (was 52-100). Threshold mungkin perlu recalibrate. JANGAN lower threshold pre-data — tunggu 40+ trade dulu.
7. **Data mengalahkan hipotesis.** Semua fix di atas BISA salah. Audit #20 menentukan. Metric wins.
