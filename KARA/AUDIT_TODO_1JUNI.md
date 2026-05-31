# KARA — Audit #18 (1 Juni 2026) — VERIFIKASI 5 FIX AUDIT #17

> **Aturan untuk besok (AI/Kiro): TIDAK ADA kesimpulan tanpa data produksi fresh.**
> Setiap klaim "fix bekerja" HARUS dibuktikan dengan korelasi/WR per bucket dari DB Railway.
> Setiap fix yang gagal trigger → ROOT CAUSE, bukan disable. Setiap action item format:
> `[what]` → `[why]` → `[how to verify]`. Sampel < 100 = bug-detection, BUKAN validasi final.

---

## 0. KONTEKS — Apa yang di-deploy di Audit #17 (31 Mei)

Audit #17 menemukan **root cause sebenarnya** dari slow-bleed (PF 0.59, PnL −Rp230rb / 56 trade):
**score agregat INVERSE (r=−0.159)** karena menjumlahkan 1 sinyal prediktif (orderbook) dengan
~8 komponen noise/inverse. Bukan masalah hold time, bukan masalah threshold.

### 5 Fix yang di-deploy (semua di `engine/scoring_engine.py` + `oi_funding_analyzer.py`)

| # | Fix | File | Bukti pra-deploy | Marker log |
|---|-----|------|------------------|-----------|
| 1 | **OB-dominant scoring** — strong wall (ob_dir≥12) +15, OB lawan arah −20 | scoring_engine.py ~L2017 | score r −0.159 → +0.242 (replay) | `ob_edge=` di `[SCORE-DEBUG]` |
| 2 | **Disable liq** (`_LIQ_SCORING_ENABLED=False`) | scoring_engine.py ~L1516 | 0/174 fire (0%) | `LIQ=0` selalu |
| 3 | **OI momentum 5m** (bukan 1h) + threshold 0.001→0.0005 | oi_funding_analyzer.py ~L165 | 5m r=+0.228 vs 1h −0.064 (candle HL) | reasons "OI/Funding bullish/bearish" |
| 4 | **AI advisory-only** (`_AI_SCORING_ENABLED=False`) — verdict disimpan, tidak ubah score | scoring_engine.py ~L1139 | timeout 67%, conf r=−0.101, score_adj r=−0.053 | `🧠 AI (advisory)` di reasons |
| 5 | **Vote-margin gate** — skip kalau `vote_margin<2` | scoring_engine.py ~L760 | PF 0.59→0.67, hemat $4.07 (replay) | `reason=low_vote_consensus` |

**Commit boundary:** catat mtime file `/app/engine/scoring_engine.py` di Railway = batas PRE/POST.

---

## 1. STEP PERTAMA — Tarik data & tentukan boundary (WAJIB sebelum analisis)

```powershell
# Ikuti AUDIT_RUNBOOK.md Step 1-1b. Single-user dedup (user 7667519263).
venv\Scripts\python.exe tmp\export_audit17.py   # reuse, atau buat export_audit18.py
```

`[what]` Tarik `trade_history` + `signals_history` + `ai_verdicts` fresh dari Railway.
`[why]` Audit #17 datanya 100% cocok jurnal user — wajib pertahankan ground-truth itu.
`[how to verify]` Cocokkan total PnL hasil tarik vs jurnal Telegram user. Harus identik.

**Deploy boundary check:**
```powershell
railway ssh --service rare-youthfulness "stat -c %Y /app/engine/scoring_engine.py"
```
`[what]` Dapatkan epoch deploy. Pisahkan trade PRE vs POST.
`[why]` Hanya trade POST yang menguji 5 fix. Jangan campur.
`[how to verify]` Minimal 20+ trade POST untuk bug-detection. Kalau < 20 → tunggu, jangan paksa kesimpulan.

---

## 2. TUNTUTAN VERIFIKASI PER FIX (AI harus jawab dengan ANGKA, bukan opini)

### Fix #1 — OB-dominant scoring → **TARGET UTAMA**

`[what]` Hitung ulang korelasi `score ↔ PnL` (Pearson + Spearman) pada trade POST.
`[why]` Ini fix inti. Audit #17 replay = +0.242. Harus konfirmasi di forward data.
`[how to verify]`
- ✅ **PASS** jika r > +0.15 (predictive) DAN top-quartile WR > 50%.
- ⚠️ **NEUTRAL** jika 0.00 < r < +0.15 (stop merusak, belum predictive) → pertahankan, lanjut tuning.
- 🔴 **FAIL** jika r < 0 → fix tidak transfer ke forward data. **Cari kenapa**: regime beda? OB fire rate turun? Jangan rollback tanpa root cause.
- Cek bucket: `ob_dir≥12` harus WR > 55%. `ob_dir<0` harus minim (veto bekerja).

### Fix #2 — Disable liq

`[what]` Konfirmasi `liquidation_score=0` di semua signal POST.
`[why]` Pastikan disable benar-benar aktif (bukan cuma di kode).
`[how to verify]` 100% signal liq=0. Kalau ada non-zero → `_LIQ_SCORING_ENABLED` tidak ter-deploy.
**Task lanjutan (jika ada waktu):** turunkan threshold OKX di `_calc_liq_cluster` ($2K→$500) dan ukur apakah liq mulai fire. Kalau fire >0% dan WR per bucket bagus → re-enable dengan bukti.

### Fix #3 — OI momentum 5m

`[what]` Hitung `oi_funding_score ↔ PnL` directional pada trade POST.
`[why]` Audit #17: lama (1h) r=−0.072 inverse. Target: 5m bikin netral/positif.
`[how to verify]`
- ✅ PASS jika r naik dari −0.072 ke ≥ 0.00.
- Cek: trade dengan OI bonus (+8/+14/+22) sekarang harus WR ≥ trade tanpa OI bonus (dulu kebalik: 29% vs 48%).
- 🔴 Kalau masih inverse → momentum 5m belum cukup; pertimbangkan kalibrasi threshold funding ke realita HL (extreme 0.0003→0.0001).

### Fix #4 — AI advisory-only

`[what]` Konfirmasi AI tidak lagi ubah score: `score_before == score_after` di tabel `ai_verdicts` POST.
`[why]` Pastikan AI benar advisory, tidak veto/boost.
`[how to verify]`
- Semua verdict POST: `score_adj` boleh ada di DB tapi `score_after == score_before`.
- Tidak ada `[AI-VETO]` di log POST.
- **Re-enable hanya jika:** timeout < 20% DAN confidence r(PnL) > +0.10 (saat ini 67% & −0.101).

### Fix #5 — Vote-margin gate

`[what]` Hitung berapa signal di-skip `reason=low_vote_consensus` + WR trade POST yang lolos.
`[why]` Audit #17: margin 0-1 = WR 25%. Gate harusnya buang trade rugi ini.
`[how to verify]`
- Log `[SKIP] ... reason=low_vote_consensus` muncul.
- Semua trade POST harus `vote_margin ≥ 2`.
- WR trade POST harus naik dari 34% (baseline) karena trade margin-rendah ter-buang.
- 🔴 Kalau trade margin<2 masih muncul → `vote_margin` tidak ter-populate (cek `out_components`).

---

## 3. METRIK AGREGAT — GO / NO-GO

| Metrik | Audit #17 (PRE-fix) | Target #18 | Gate |
|--------|---------------------|-----------|------|
| Score↔PnL r | −0.159 | **> +0.15** | Fix #1 berhasil |
| Profit Factor | 0.59 | **> 1.0** | Bot stop bleed |
| Win Rate | 34% | **> 42%** | Break-even (avg R:R 1.15 → BE WR 46.5%) |
| Time exit % | 66% | < 55% | Entry quality naik |
| Trailing fire % | 14.3% | **≥ 25%** | Edge utama hidup (cek konflik hold-aware SL) |
| PnL (single user) | −Rp230rb | **≥ break-even** | — |

`[what]` Isi tabel ini dengan angka POST. Bandingkan PRE vs POST head-to-head.
`[why]` 5 fix harus terlihat efeknya agregat, bukan cuma per-komponen.
`[how to verify]` Kalau PF naik > 1.0 DAN r > +0.15 → **fix tervalidasi**, lanjut ke threshold tuning. Kalau tidak → bisect (lihat §5).

---

## 4. RESIDUAL — Yang BELUM ditangani Audit #17 (investigasi besok)

### 4a. Hold-aware SL menekan trailing (carry-over dari Audit #16)
`[what]` Audit #16 (P0-4) melebarkan SL → TP1 ke 1.05% → trailing fire turun 14.3%.
`[why]` Trailing = satu-satunya edge (100% WR). Kalau fix #1-5 naikkan PF tapi trailing masih < 20% → ini bottleneck berikutnya.
`[how to verify]` Cek trailing fire rate POST. Kalau < 20% → pertimbangkan rollback hold-aware SL (cap TP1 balik ke 0.8%). **Uji replay dulu.**

### 4b. Threshold entry (HANYA jika Fix #1 PASS)
`[what]` Naikkan `min_score_to_enter` untuk panen tier-atas score yang SEKARANG sudah prediktif.
`[why]` Audit #17: score baru prediktif → gating jadi defensible (bukan lazy lagi). TAPI cuma valid kalau Fix #1 confirm r > +0.15 di forward data.
`[how to verify]` JANGAN sentuh sebelum §2 Fix #1 = PASS. Satu variabel per deploy.

---

## 5. KALAU REGRESI (PF turun / r negatif) — PROTOKOL BISECT

5 fix deploy bersama = melanggar satu-variabel. Kalau POST lebih buruk dari PRE:

`[what]` Bisect per fix, urutan dari paling berisiko:
1. Fix #1 (OB-dominant) — paling besar dampak ke score. Cek apakah `ob_dir<0` veto over-blocking.
2. Fix #5 (vote-gate) — cek apakah buang terlalu banyak trade (volume drop > 40%?).
3. Fix #3 (OI 5m) — cek apakah bonus OI sekarang fire di arah salah.
`[why]` Tanpa isolasi tidak bisa tahu fix mana yang regresi.
`[how to verify]` Toggle satu per satu via flag (`_AI_SCORING_ENABLED` style), replay per toggle.

---

## 6. RED FLAGS (halt / rollback)

| Kondisi | Action |
|---------|--------|
| Score↔PnL r < −0.10 POST | Fix #1 gagal transfer → root cause (regime? OB fire rate?). JANGAN langsung rollback. |
| Volume trade turun > 50% | Vote-gate + OB veto terlalu agresif → longgarkan margin gate ke skip-hanya-margin-0. |
| Trailing fire < 15% | Hold-aware SL bottleneck → prioritaskan §4a. |
| PF < 0.7 di POST (≥ 30 trade) | Salah satu fix aktif merusak → bisect §5. |
| `low_vote_consensus` skip = 0 | Vote-gate tidak jalan → cek `vote_margin` populate. |
| AI `score_after != score_before` | Fix #4 tidak ter-deploy → verifikasi commit. |

---

## 7. DEADLINE 1 JUNI — STATUS REALISTIS

Target live 1 Juni **tidak tercapai** (PF masih < 1.0 PRE-fix). Status jujur:

| Kriteria Live-Ready | Target | Saat ini (PRE-fix) | Realistis |
|---------------------|--------|---------------------|-----------|
| PF > 1.3 (3 audit konsisten) | ≥1.3 | 0.59 | Butuh #18, #19, #20 PASS |
| Score↔PnL r > +0.15 | ≥+0.15 | −0.159 (PRE) / +0.242 (replay) | #18 konfirmasi |
| Trailing fire ≥ 25% | ≥25% | 14.3% | §4a |
| Live executor tested | ✅ | ❌ | Belum mulai |
| Slippage measured | ✅ | ❌ | Belum mulai |

**Rekomendasi:** Geser GO-LIVE ke **setelah 3 audit PF > 1.0 berturut** (estimasi 4-6 Juni earliest).
1 Juni = hari validasi Fix #17, BUKAN go-live. Paper validation dulu, live executor paralel.

---

## 8. TIMELINE BESOK (1 Juni)

| Waktu (WIB) | Action |
|-------------|--------|
| Pagi | Tarik data POST (§1). Verifikasi boundary + jumlah trade POST ≥ 20. |
| Siang | Verifikasi 5 fix satu per satu (§2). Isi angka, bukan opini. |
| Sore | Metrik agregat GO/NO-GO (§3). Kalau PASS → rencana threshold (§4b). Kalau FAIL → bisect (§5). |
| Malam | Tulis `AUDIT_TODO_2JUNI.md` dengan keputusan + next single-variable deploy. |

---

## 9. PRINSIP YANG TIDAK BOLEH DILANGGAR BESOK

1. **Data mengalahkan hipotesis.** Audit #17 sempat salah (margin r=+0.282 ternyata +0.076 karena sign-error SHORT). Hitung directional yang BENAR.
2. **Disable = pilihan terakhir.** OI di-FIX (timeframe), bukan dibuang. Liq di-disable HANYA setelah 2 audit 0% fire + ada path fix yang dicatat.
3. **AI = detektor kontradiksi, bukan oracle.** Insight AI (LIT votes 4-4) jadi RULE deterministik (vote-gate), bukan bergantung API yang timeout 67%.
4. **Replay gate sebelum deploy.** Tidak ada perubahan scoring tanpa lolos replay (r > +0.15, decile/bucket sehat).
5. **Sampel < 100 = bug-detection.** Jangan over-claim "tervalidasi" dengan n kecil.
6. **Satu variabel per deploy** mulai sekarang. 5 fix sekaligus hari ini = terakhir kali. Besok kalau deploy lagi → satu saja, biar bisa atribusi.
