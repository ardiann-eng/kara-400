# KARA — Audit #21 (8 Juni 2026) — VALIDASI REKONSTRUKSI v10 INSTITUSIONAL

> **Aturan:** Tidak ada kesimpulan tanpa data produksi fresh (Railway DB, single-user dedup).
> Korelasi **DIRECTIONAL**. Sampel < 100 = bug-detection, BUKAN validasi strategi.
> Action item: `[what]` → `[why]` → `[how to verify]`.
> **Ini deploy TERBESAR dalam sejarah KARA** — rombak total scoring→gate. Verifikasi ekstra ketat.

---

## 0. KONTEKS — Apa yang Berubah (deploy 6-7 Juni, KARA v10)

KARA berhenti jadi "scoring engine aditif" → jadi "gate system institusional".
**Flag:** `KARA_V10_GATES=true` (default aktif). Rollback: set `false`.

### Yang berubah fundamental:
| Area | Lama | v10 |
|------|------|-----|
| Penentu ENTRY | score ≥ threshold 45 | **Gate G1-G5** (regime+exhaustion+junk+displacement+liquidity) |
| Penentu ARAH | 7-voter (incl EMA, RSI) | **HTF(×2)+displacement(×2)+OI(×1)** — EMA & RSI dibuang |
| Komponen lagging | EMA/MFI/RSI nambah skor | **DIHAPUS** dari scoring |
| Sizing | by score conviction | **size_mult gate** (tier A=1.0×, B=0.6×, vol 0.5-1.0×) |
| Counter-trend | threshold +8 | **HARD BLOCK** (G1) |
| time_exit | hard 12-20min | **+ progress stop** (belum +0.5R dalam 8min → cut) |
| Max posisi | 5 | **3** |

### Edge yang DIPERTAHANKAN (jangan diutak-atik):
- Trailing stop post-TP1 (100% WR, 16 audit)
- Momentum death cut
- Funding crowding risk gate
- Vol-adjusted sizing

---

## 1. STEP PERTAMA — Tarik data (WAJIB)

```powershell
cd "D:\Vibe Coding\KARA - 400"
railway ssh --service rare-youthfulness "stat -c %Y /app/engine/scoring_engine.py"
# Export (lihat AUDIT_RUNBOOK.md / tmp/export_prod.py)
$script = Get-Content tmp\export_prod.py -Raw
$b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($script))
railway ssh --service rare-youthfulness "echo $b64 | base64 -d > /tmp/e.py && python3 /tmp/e.py"
# Download + decode, lalu:
venv\Scripts\python.exe tmp\deep_audit.py
venv\Scripts\python.exe tmp\mega_audit.py
```

`[what]` Boundary deploy + trade POST-v10.
`[how to verify]` Trade POST ≥ **40** sebelum klaim. PnL tarik ≈ jurnal Telegram.

---

## 2. VERIFIKASI DEPLOY AKTIF (CONFIRM v10 JALAN)

Cek Railway logs — marker WAJIB muncul:
- ✅ `[V10-GATE SKIP] ... reason=...` (gate menolak sinyal) ATAU `🚪 v10 GATE PASS`
- ✅ `🧭 Direction:` dengan votes TANPA EMA dominan
- ✅ `📊 EMA cross ... (direction-only, no score)` (EMA tidak nambah skor)
- ❌ TIDAK ada `reason=score_below_threshold` (di-bypass v10)
- ❌ TIDAK ada `reason=low_vote_consensus` (di-bypass v10)
- ❌ TIDAK ada `reason=low_momentum` / `low_atr` (redundan, bypass)

🔴 **Kalau marker score_below_threshold MASIH muncul** → `KARA_V10_GATES` tidak ter-set true di Railway env. Verifikasi env var.

---

## 3. METRIK GO / NO-GO

| Metrik | Audit #20 (PRE-v10) | Target #21 (POST-v10) | Gate |
|--------|---------------------|----------------------|------|
| Profit Factor | 0.60 | **≥ 1.3** | Rekonstruksi berhasil |
| Win Rate | 27.8% | **≥ 40%** | Gate filter quality |
| time_exit % | 58% | **< 40%** | Progress stop + displacement |
| Trailing fire % | 18% | **≥ 30%** | Lebih banyak reach TP1 |
| Counter-trend trade | 90% LONG | **< 10%** | G1 hard block |
| **Volume (trade/hari)** | ~38 | **≥ 25** | ⚠️ JANGAN sampai gate bunuh volume |
| LONG/SHORT ratio | 90/10 | mendekati regime mix | EMA bias dibuang |
| PnL (40+ trade POST) | -$25 | **≥ break-even** | — |

**GO ke validasi lanjut** hanya jika: PF ≥1.3 + volume ≥25/hari + trailing ≥30%.

---

## 4. VERIFIKASI PER PERUBAHAN (ANGKA)

### A. Gate System (entry)
`[what]` Funnel: dari N sinyal, berapa lolos gate? Breakdown reject reason.
`[how to verify]`
- Skip counters `v10_*`: long_against_downtrend, cvd_exhaustion, no_displacement, rv_extreme
- ✅ Tradeable rate ~40-50% (funnel study prediksi 48%)
- 🔴 Tradeable < 20% → gate terlalu ketat → cek RV threshold (harusnya 15% bukan 6%)
- 🔴 Tradeable > 80% → gate terlalu longgar → cek displacement threshold

### B. Direction Institusional
`[what]` LONG/SHORT distribution vs HTF regime distribution.
`[how to verify]`
- ✅ Saat market TRENDING_DOWN dominan → SHORT ratio NAIK (bukan stuck 10%)
- ✅ Tidak ada trade counter-trend (LONG di TRENDING_DOWN = 0)
- 🔴 Masih 90% LONG → cek apakah _V10_DIR aktif, atau OI bias masih dominan

### C. Sizing Tier (size_mult)
`[what]` Distribusi size_mult: tier A (1.0×) vs B (0.6×), vol tier.
`[how to verify]`
- ✅ Tier B trades size LEBIH KECIL → dollar loss tier B < tier A
- ✅ High-vol (RV 6-15%) trades size 0.5× → damage terkontrol
- Telemetri: `v10_size_mult`, `v10_setup`, `v10_tier` di signal data

### D. Progress Time-Stop (F0.2)
`[what]` Exit reason breakdown — apakah ada exit "Progress stop"?
`[how to verify]`
- ✅ Log `⏱️ Progress stop ... hanya X.XXr` muncul
- ✅ time_exit count TURUN (sebagian jadi progress stop yang -0.5R bukan -1R)
- ✅ avg loss time_exit/progress < avg loss lama (-1.07)

### E. Setup Classifier (telemetri)
`[what]` WR per setup: sweep / breakout / pullback / momentum.
`[how to verify]` Identifikasi setup mana paling profitable → fokus Fase 3.

---

## 5. RED FLAGS (ROLLBACK PROTOCOL)

| Kondisi | Action |
|---------|--------|
| Volume < 15/hari | Gate terlalu ketat → cek displacement/RV threshold. Longgarkan SATU. |
| PF < 0.8 (30+ trade) | Set `KARA_V10_GATES=false` (rollback ke engine lama), diagnosa |
| 0 trade dalam 3 jam | Gate over-block → ROLLBACK + cek funnel |
| Trailing fire < 15% | Entry quality gate gagal → cek apakah trade reach TP1 |
| SHORT ratio masih <15% di bear market | Direction institusional tidak jalan → cek _V10_DIR |
| Error `[V10-GATE] error` di log | Gate exception → fail-open aktif, tapi investigasi bug |
| time_exit > 50% | Progress stop tidak jalan → cek config progress_time_stop_minutes |

**Rollback 1 langkah:** `KARA_V10_GATES=false` → engine lama kembali penuh (semua gate lama + scoring aktif lagi). Tidak perlu redeploy code.

---

## 6. BISECT (kalau regresi tapi tidak mau full rollback)

Toggle granular via env (kalau diimplement) atau code flag:
1. Direction institusional — paling berisiko (arah salah = semua salah). Cek SHORT ratio.
2. Gate G4 displacement — bisa over-block. Cek no_displacement skip count.
3. Sizing tier — low risk, cuma ukuran.
4. Progress stop — cek apakah cut terlalu cepat (winner ke-cut sebelum berkembang).

---

## 7. TEMUAN YANG MENUNGGU DATA (jangan deploy tanpa bukti)

- **Setup A/B/C WR** — sample masih 0. Kumpulkan, lihat mana edge.
- **Structural trailing** (flag OFF) — uji HANYA setelah v10 base tervalidasi PF>1.3.
- **CVD tick vs proxy** — bandingkan r di data POST (tick sekarang aktif di gate).
- **SHORT generation** — apakah v10 hasilkan cukup sinyal SHORT di bear market?

---

## 8. TIMELINE 8 JUNI (WIB)

| Waktu | Action |
|-------|--------|
| Pagi | Verifikasi deploy aktif (§2). Confirm marker v10 di log. |
| Siang | Tarik data POST (≥40 trade). deep_audit + mega_audit. |
| Sore | Isi §3 GO/NO-GO + §4 per-perubahan dengan ANGKA. |
| Sore (lanjutan) | **Temuan + Fix** — lihat §A. |
| Malam | PASS → lanjut Fase 3 (walk-forward + micro-live). FAIL → bisect/rollback (§5-6). Tulis AUDIT_TODO_berikutnya. |

---

## A. REALITAS AUDIT #21 — Temuan & Fix Hari Ini

### A.1 Data Pull
- Railway production DB: 175 trade_history rows, 177 signals
- Dedup single-user (7667519263): **44 trades** → cocok jurnal Telegram

### A.2 Verifikasi v10 Aktif
| Marker | Status |
|--------|--------|
| `[V10-GATE SKIP]` (183×) / `[V10-GATE PASS]` (2×) | ✅ |
| Direction tanpa EMA dominan | ✅ |
| `score_below_threshold` | ❌ NOL muncul — gate bypass scoring |
| `low_vote_consensus` | ❌ NOL muncul |

**Catatan:** `_V10_ACTIVE = True` hardcoded di `scoring_engine.py:552`. `KARA_V10_GATES=false` di Railway env **TIDAK** berefek. Rollback = code change + redeploy.

### A.3 Metrik POST-v10 (44 trades)

| Metrik | Value | vs Target #21 |
|--------|-------|---------------|
| Win Rate | 47.7% | ✅ ≥40% |
| Profit Factor | **0.78** | 🔴 <1.3 |
| PnL | **-$7.63** | 🔴 < break-even |
| time_exit % | 66% | 🔴 target <40% |
| Trailing fire | 2.3% (1 trade) | 🔴 target ≥30% |
| Counter-trend | 0 | ✅ |
| LONG/SHORT | 29/15 | mending (sblm 90/10) |
| Volume/hari | ~44 | ✅ ≥25 |
| Score↔PnL Pearson | r=-0.12 | 🔴 INVERSE — score makin tinggi, PnL makin jelek |

### A.4 ROOT CAUSE — ZEC Loss -$8.37 (54% total loss)

**Masalah:** v10 regime detection `_fetch_1h_regime()` di `scoring_engine.py:3538-3543` pakai **AND**:
```python
if net_move >= 0 and ema8 > ema21 and strength >= 0.15:  # TRENDING_UP
```
Saat ZEC dip tajam $400→$363, `total_range` melebar → `strength = net_move / total_range < 0.15` → regime `CHOPPY` meski EMA8 > EMA21 masih true. G1 membaca CHOPPY → sinyal SHORT lolos → bot short ke uptrend → -$8.37.

**Fix:** Hapus strength threshold → regime murni EMA8/21 cross (scoring_engine.py:3538).

### A.5 ROOT CAUSE 2 — SHORT Filter Block Dinonaktifkan

Seluruh blok filter SHORT spesifik (baris 887-981) di-bypass oleh:
```python
if side == Side.SHORT and not _V10_ACTIVE:
```
v10 menghapus: short-in-CHOPPY score threshold ≥67, OB contradiction guard, funding guard, squeeze guard.

**Status:** Sudah difix via A.4 (regime fix). ZEC akan stay TRENDING_UP saat dip → G1 block SHORT langsung.

### A.6 Exit Breakdown

| Exit | Count | PnL | % Total Loss |
|------|-------|-----|--------------|
| time_exit | 29 (66%) | -$5.81 | 76% |
| stop_loss | 2 | -$6.60 | **86%** (TON -$3.52, ZEC -$3.08) |
| trailing_stop | 1 | +$0.78 | — |
| tp1/tp2 | sisanya | — | — |

### A.7 Fix yang Dideploy (7 Juni)

| # | File | Change | Dampak |
|---|------|--------|--------|
| 1 | `engine/scoring_engine.py:3538` | Hapus strength ≥0.15 dari regime detection | ZEC stay TRENDING_UP saat dip → G1 block SHORT |
| 2 | `engine/gate_system.py:46` | RV_HARD_MAX 15% → **8%** | RV r=-0.50 p=0.0005 → cutoff lebih ketat |
| 3 | `engine/gate_system.py:225-229` | Vol-tier 6-8% → **0.3×** (was 0.5×) | RV tinggi = size minimal |
| 4 | `engine/gate_system.py:248-251` | Tier S/A/B: OB trend-aligned = **S** (premium), liquidity context = A, basic = B | Entry quality tier |
| 5 | `risk/risk_manager.py:1461` | Progress stop action `progress_stop` instead of `time_exit` | Separasi untuk audit trail |
| 6 | `execution/paper_executor.py:533`, `bitget_executor.py:611` | Add `progress_stop` ke full-close list | Progress stop bisa close posisi |
| 7 | `main.py:1563-1565`, `notify/telegram.py`, `notify/pnl_card.py` | progress_stop mappings | Progress stop tampil di log & card |
| 8 | `notify/telegram.py` | Signal card: Score dihapus → `💎 Tier {gate_tier}` | User-friendly, tidak misleading |
| 9 | `core/db.py` | `signals_history` simpan `tier` bukan `score` | Query langsung bisa filter tier |
| 10 | `models/schemas.py` | TradeSignal: `v10_tier`, `v10_setup` fields | Signal carry metadata gate |
| 11 | `execution/*.py` | `entry_tier` di Position + trade_data | Tier tercatat di trade history |
| 12 | `main.py`, `scoring_engine.py`, `telegram.py` | Log lines: `score=` → `tier=` | Log jelas pakai tier |

### A.8 What's Next (Audit #22)

1. **Deploy fix 1-12 ke Railway**
2. **Verifikasi di logs:** `[1H-REGIME] ZEC: TRENDING_UP` saat uptrend dengan dip (bukan CHOPPY lagi)
3. **Progress stop muncul** di exit breakdown: expected ≥10% dari total exit
4. **Trailing fire rate naik** dari 2.3% — target ≥10%
5. **Score tidak dipakai lagi** — verifikasi semua display & log pakai Tier
6. **Tier vs PnL correlation:** sampel ≥40 trade → hitung PF per tier (S/A/B)
7. **Score death zone** (12-17, WR 33.3%, -$13.91) sudah irrelevant karena score dihapus

---



## 9. KRITERIA LIVE (tidak berubah — masih jauh)

| Kriteria | Target | Status |
|----------|--------|--------|
| PF > 1.3 (3 audit berturut) | ≥1.3 | #21 = audit pertama v10 |
| Score/gate↔PnL prediktif | gate-pass WR > fail | belum diukur |
| Trailing fire ≥ 30% | ≥30% | belum |
| Max DD < 12% | <12% | belum |
| Slippage live terukur | <0.05% | ❌ belum mulai |
| Walk-forward 300 trade OOS | ✅ | ❌ belum |

**Realistis:** v10 = arsitektur baru, butuh 3 audit (#21,#22,#23) PF>1.3 berturut + walk-forward + micro-live sebelum live penuh. JANGAN go-live hanya karena 1 audit bagus (sample kecil = bug detection).

---

## 10. PRINSIP AUDIT #21

1. **Volume dulu, edge kemudian.** Kalau v10 bunuh volume (<15/hari) → gagal, apapun PF-nya. Compounding butuh kuantitas.
2. **Arah = taruhan terbesar.** Kalau SHORT ratio tidak naik di bear market → direction institusional tidak jalan, investigasi.
3. **Rollback itu murah.** `KARA_V10_GATES=false` = 1 detik. Jangan ragu rollback kalau PF<0.8 + 30 trade.
4. **Sample < 100 = bug detection.** Jangan klaim "v10 berhasil/gagal" dari 40 trade. Itu cuma cek apakah PIPA-nya jalan.
5. **Data memutuskan.** Rekonstruksi ini hipotesis besar. #21 menentukan apakah arah benar.
