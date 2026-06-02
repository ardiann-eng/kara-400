# KARA — Audit #19 (3 Juni 2026) — VERIFIKASI DEPLOY PACK 2 JUNI

> **Aturan:** Tidak ada kesimpulan tanpa data produksi fresh (Railway DB, single-user dedup).
> Korelasi **DIRECTIONAL** untuk SHORT. Sampel < 100 = bug-detection, bukan validasi strategi.
> Action item: `[what]` → `[why]` → `[how to verify]`.
> **Satu variabel per deploy mulai 3 Juni** — pack 2 Juni = multi-fix (akui atribusi kabur).

---

## 0. KONTEKS — Audit #18 (149 trade, user 7667519263)

| Metrik | Nilai | Catatan |
|--------|-------|---------|
| Win rate | 33.1% | Rendah, OK karena R:R |
| Profit factor | **2.31** | Bot **profit** (+$8.66) |
| Trailing fire % | **18.9%** (28/149) | Edge: trailing **100% WR**, +$49 |
| Time exit % | **48.6%** (72/149) | Bleed **−$39** |
| Momentum death % | 28.2% (42/149) | −$3.9 |
| Score↔PnL r | **~0** | Tidak prediktif |
| OB strong wall (ob_dir≥12) | WR **27%**, −$5.6 | Bukan replikasi Audit #17 |
| CVD bonus (+6 moderate) | WR **42%**, +$15.3 | **Prediktif** (bukan inverse) |

**Root cause tetap:** terlalu sedikit trade sampai **ATR trail**; time_exit = gejala entry flat / tidak reach TP1.

---

## 1. PERUBAHAN YANG HARUS DIVERIFIKASI (deploy 2 Juni 2026)

### Pack A — Exit / trail (P0)

| # | Fix | File | Target |
|---|-----|------|--------|
| T1 | `partial_tp1_at_sl_multiple` 0.7→**0.5** | config.py | TP1 lebih cepat → trail |
| T2 | `atr_trail_post_tp1_extra_pct` +0.3%→**+0.1%** | config.py + risk_manager.py | Trail arms sooner post-TP1 |
| T3 | Early trail **0.10%** (`time_exit_early_trail`, `early_trail_activation`) | config.py + risk_manager.py | Beat momentum_death |
| T4 | Momentum death: peak < **0.10%** + hold **4m** | risk_manager.py | Jangan kill +0.12% tick |
| T5 | **Rule D0** pre-TP1 trail exit | risk_manager.py | `Pre-TP1 trail hit` di log |

`[how to verify]` trailing_stop % **≥25%** (was 19%); time_exit % **<40%** (was 49%); PF **≥2.0**.

### Pack B — Scoring (P1)

| # | Fix | File | Target |
|---|-----|------|--------|
| S1 | OB crowded: edge **+15→+8** (opsi A) jika LONG+bid+**TRENDING_UP** atau SHORT+ask+**TRENDING_DOWN** | config.py + scoring_engine.py | Kurangi skor palsu 70–100; tidak over-block |
| S2 | OB non-crowded strong wall tetap **+15** | scoring_engine.py | CHOPPY / counter-HTF masih full bonus |
| S3 | LONG + **CHOPPY**: `dir_move` **0.20%**; leading → **0.15%** | config.py + scoring_engine.py | `long_choppy_low_momentum` di skip log |
| S4 | Leading CVD threshold **≥6** (was ≥10, never fired) | scoring_engine.py | CVD leading path hidup |

`[how to verify]`
- Log: `crowded=1` + reason `reduced (+8 not +15)` — bukan +3 kecuali config diubah lagi.
- Volume trade turun **<30%** vs pre-deploy (kalau >40% → naik crowded bonus ke 10).
- Score decile 71+ PnL tidak dominan negatif.
- `score↔PnL` r trending **>0** (target +0.10 lalu +0.15).

### Pack C — Belum deploy (audit berikutnya)

| # | Item | Prioritas |
|---|------|-----------|
| N1 | Asset hard block (0W n≥2) | P1 |
| N2 | Persist `cvd_pts`, `ob_crowded`, `vote_margin` ke DB | P1 |
| N3 | Pecah exit reason: `early_loss_cut` vs `time_limit_*` | P2 |
| N4 | Micro-live HL $10–20 | Sebelum live penuh |

---

## 2. STEP PERTAMA — Tarik data (WAJIB)

```powershell
# Lihat KARA/AUDIT_RUNBOOK.md Step 1-1b
cd "D:\Vibe Coding\KARA - 400"
# User dedup: 7667519263
& "C:\Users\ARDI\AppData\Local\Programs\Python\Python312\python.exe" audit_score_analysis\analyze.py
```

`[what]` Boundary deploy: `railway ssh --service rare-youthfulness "stat -c %Y /app/config.py"`
`[why]` Hanya trade **POST** pack 2 Juni yang validasi fix.
`[how to verify]` Trade POST ≥ **30** sebelum klaim; PnL tarik ≈ jurnal Telegram.

Scripts audit lokal:
- `tmp/audit_ob_investigation.py`
- `tmp/audit_cvd.py`
- `tmp/component_correlation.py`

---

## 3. VERIFIKASI PER FIX (ANGKA)

### T1–T5 Trail pack
`[what]` Exit breakdown POST vs Audit #18 PRE.
`[how to verify]`
- ✅ trailing_stop n↑, WR tetap ~100%
- ✅ time_exit n↓ atau WR↑
- 🔴 trailing <20% → cek Rule D0 log; bisect T1 vs T3

### S1 OB crowded +8
`[what]` Bucket crowded strong-wall: WR, PnL, count vs PRE (−$5.6 / WR 27%).
`[how to verify]`
- ✅ PnL bucket naik; WR >30%
- ⚠️ Volume −15..30% acceptable
- 🔴 Volume −40% → `ob_crowded_wall_bonus_pts` 8→10

### S3 LONG CHOPPY 0.20%
`[what]` `skip_counters['long_choppy_low_momentum']` vs total LONG CHOPPY signals.
`[how to verify]` Skip ada tapi tidak >50% LONG CHOPPY; LONG CHOPPY WR/PnL membaik.

### CVD (tidak diubah 2 Juni — re-validate)
`[what]` Trade dengan reason `CVD 5m moderate` vs tanpa.
`[how to verify]` ✅ WITH bonus WR > WITHOUT (Audit #18: 42% vs 27%).

---

## 4. METRIK AGREGAT GO / NO-GO

| Metrik | Audit #18 PRE | Target #19 POST |
|--------|-----------------|---------------|
| Profit factor | 2.31 | **≥1.8** |
| Trailing fire % | 18.9% | **≥25%** |
| Time exit % | 48.6% | **<40%** |
| Total PnL (30+ trade POST) | +$8.66 cumulative | **≥ break-even POST window** |
| Score↔PnL r | ~0 | **>+0.10** |

**GO micro-live** hanya jika: PF POST ≥1.5 + trail ≥25% + 50 trade POST + executor tested.

---

## 5. RED FLAGS

| Kondisi | Action |
|---------|--------|
| Trailing fire <15% POST | T3/T4 terlalu agresif atau T1 belum cukup — replay T1 saja |
| PF <1.0 pada 30 trade POST | Bisect Pack A vs Pack B |
| Volume −50% | OB crowded atau CHOPPY gate terlalu ketat |
| CVD bonus WR < tanpa bonus | Investigasi proxy vs tick CVD |
| Score↔PnL r < −0.10 | OB/LOC regression — root cause |

---

## 6. TIMELINE 3 JUNI (WIB)

| Waktu | Action |
|-------|--------|
| Pagi | Deploy verify + boundary timestamp |
| Siang | Pull POST data, jalankan analyze + 3 script tmp |
| Sore | Isi tabel §3–§4 dengan angka; GO/NO-GO |
| Malam | Tulis `AUDIT_TODO_4JUNI.md`; satu deploy berikutnya (N1 asset block ATAU N2 telemetry) |

---

## 7. COMMIT BOUNDARY (3 Juni deploy)

Expected files touched:
- `config.py` — trail, OB crowded=8, momentum CHOPPY
- `risk/risk_manager.py` — P0 trail + momentum death + D0
- `engine/scoring_engine.py` — OB crowded, LONG CHOPPY gate

Marker log production:
- `Pre-TP1 trail hit`
- `OB wall aligned but crowded` +8
- `reason=long_choppy_low_momentum`
- `[SCORE-DEBUG] ... crowded=1`
