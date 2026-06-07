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

## A. FASE 2.1 — Perubahan 7 Juni (Sore)

### A.1 Ringkasan Perubahan

| # | File | Change | Dampak |
|---|------|--------|--------|
| 1 | `engine/scoring_engine.py:3538` | Hapus strength ≥0.15 dari regime detection | ZEC stay TRENDING_UP saat dip → G1 block SHORT |
| 2 | `engine/gate_system.py:46` | RV_HARD_MAX 15% → **8%** | RV r=-0.50 p=0.0005 → cutoff lebih ketat |
| 3 | `engine/gate_system.py:225-229` | Vol-tier 6-8% → **0.3×** (was 0.5×) | RV tinggi = size minimal |
| 4 | `engine/gate_system.py:248-251` | Tier S/A/B: OB trend-aligned = **S**, liquidity context = A, basic = B | Entry quality tier |
| 5 | `risk/risk_manager.py:1461` | Progress stop action `progress_stop` instead of `time_exit` | Separasi audit trail |
| 6 | `execution/*.py` | Add `progress_stop` ke full-close list | Progress stop bisa close posisi |
| 7 | `main.py`, `notify/*.py` | progress_stop mappings + card | Progress stop tampil di log & card |
| 8 | `notify/telegram.py` | Signal card: Score → `💎 Tier {gate_tier}` | User-friendly |
| 9 | `core/db.py` | `signals_history` simpan `tier` bukan `score` | Filter by tier |
| 10 | `models/schemas.py` | TradeSignal: `v10_tier`, `v10_setup` | Signal carry gate metadata |

### A.2 Perubahan Fase 2.1 (7 Juni Sore)

| # | File | Change | Dampak |
|---|------|--------|--------|
| **P1** | `engine/gate_system.py` | **Scalp regime:** G1 ganti 1h EMA8/21 → scalp EMA5/13 dari 1m closes. Counter-trend TIDAK di-block, hanya size reduction: 0.75× (CT scalp), 0.55× (double CT), 0.90× (CHOPPY) | Scalping 15m pakai trend 5-13m, bukan 1h. Counter-trend fade valid |
| **P3** | `engine/gate_system.py` | **Diversity scoped:** key `{asset}` → `{asset}_{side}_{setup}`. Penalty turun 0.6→0.75. Hanya penalize entry SAMA arah+setup | LONG sweep lalu LONG breakout 60s = no penalty (setup beda) |
| **CVD** | `engine/gate_system.py` | **CVD momentum rewrite:** EWMA baseline + linear regression slope. History 8→15 points. Bobot: slope 70% / deviation 30%. Threshold -0.08→-0.06 | Deteksi peak-decline: old +0.079 (ALLOW) → new -0.063 (REJECT) |
| **RANK** | `main.py:1064-1078` | **Signal ranking multi-dimensi:** `(tier, setup, -OB, -score)`. Setup priority: sweep(0) > breakout(1) > pullback(2) > momentum(3) | Grade-S sweep didahulukan dari grade-B momentum |
| **S-GRADE** | `engine/gate_system.py` | **Grade S pakai scalp_regime:** OB trend-aligned cek `scalp_regime` (5-13m), bukan `htf_regime` (1h). Jauh lebih sering terjadi | Konsisten dengan P1, timeframe sinkron |
| **OB-ADAPT** | `engine/gate_system.py` | **OB threshold per aset:** track median `|ob_dir|` 100 scan. Threshold = max(4, min(24, median×2)). BTC butuh ~22, ZEC butuh ~6, fallback 12 | Adaptif ke depth orderbook per aset |
| **SCHEMA** | `models/schemas.py` | TradeSignal tambah `gate_ob_dir`, `gate_net_move`, `gate_cvd_dir` | Data ranking tersimpan di signal |
| **OB-CLUSTER** | `engine/scoring_engine.py:578-625` | **OB depth clustering:** cluster bid/ask 0.2% step, detect level dengan volume >1.5× avg. Pass ke gate sebagai `ob_levels` | Support/resistance dari orderbook depth, bukan cuma OB dir |
| **MARGIN-ORDER** | `risk/risk_manager.py:398-420` | **Hard margin cap 15% dipindah SEBELUM `size_mult`:** cap diterapkan dulu, baru tier A/B ×1.0/×0.6 | Tier A vs B sekarang beneran beda size finalnya |

### A.3 Metrik PRE vs POST Fase 2.1

| Metrik | PRE (44 trade, old v10) | Target POST (F2.1) | Perubahan |
|--------|------------------------|-------------------|-----------|
| Profit Factor | **0.78** | **≥ 1.0** | CVD fix deteksi exhaustion + Grade S lebih sering |
| Win Rate | 47.7% | **≥ 45%** | Anti-chase tetap, displacement proof |
| time_exit % | 66% | **< 50%** | Progress stop (dari F1) + CVD fix cegah bad entry |
| Trailing fire | 2.3% | **≥ 10%** | Grade S lebih sering → lebih banyak TP1 reach |
| SHORT ratio | 34% (15/44) | **≥ 25%** | Scalp regime tidak block SHORT di CHOPPY |
| Counter-trend trade | 0 | **wajar <15%** | G1 bukan hard block, fade entry boleh |
| Grade S trades | ~0% (dulu jarang) | **≥ 10%** | OB threshold adaptif + scalp regime alignment |
| Volume/hari | ~44 | **≥ 30** | Gate tidak over-block |

### A.4 Exit Breakdown (PRE — baseline)

| Exit | % Total | PnL | Notes |
|------|---------|-----|-------|
| time_exit | 66% | -$5.81 | Masalah utama — CVD fix + scalp regime expected turunkan |
| stop_loss | 5% | -$6.60 | ZEC/TON — regime fix seharusnya cegah |
| trailing_stop | 2.3% | +$0.78 | Akan naik dengan Grade S lebih banyak |
| tp1/tp2 | sisanya | — | — |

### A.5 Verifikasi Deploy Fase 2.1

Cek Railway logs — marker WAJIB muncul:
- ✅ `[P1] G1: ...` — scalp regime active (bukan long_against_downtrend)
- ✅ `g1_ct=0.75` / `g1_dbl=0.55` / `g1_choppy=0.90` di log size_mult
- ✅ `scalp=TRENDING_UP` di gate PASS log (bukan cuma htf=)
- ✅ `RANKED` — signal sorted by tier+setup+OB+score
- ✅ Grade S `tier=S` muncul di log
- ❌ TIDAK ada `long_against_downtrend` / `short_against_uptrend` (G1 lama)

### A.6 What's Next — Audit Fase 2.1

**Urutan prioritas audit (setelah deploy + kumpul ≥40 trade):**

1. **CVD exhaustion rate:** hitung % reject vs total gate call. Expected ~10-15% sinyal kena CVD exhaustion reject (dulu false positive karena old method). Bandingkan dengan data PRE.
2. **Grade S vs Grade A/B PnL:** kumpulkan sampel. Target: Grade S WR ≥45%, PF ≥1.0. Grade B WR ≥35%.
3. **Setup WR breakdown:** apakah sweep benar-benar outperform momentum? Data menentukan.
4. **OB threshold adaptif:** cek distribusi threshold per aset. Apakah BTC dan ZEC punya threshold yang proporsional dengan depth real?
5. **Diversity penalty count:** hitung berapa kali diversity multiplier < 1.0. Apakah terlalu sering atau jarang?
6. **Signal ranking efficacy:** apakah top-3 ranked signals outperform bottom-3? Cek PnL per rank position.
7. **Scalp regime vs outcome:** apakah counter-trend scalp (G1=0.75) benar-benar rugi? Atau malah profit karena fade strategy valid?
8. **Time exit turun:** dari 66% ke target <50%. Hitung ulang setelah F2.1.
9. **Trailing fire rate:** dari 2.3% ke target ≥10%.

**Red flag spesifik Fase 2.1:**
- **Volume drop >50%** → G1 menjadi terlalu longgar (sinyal jelek lolos)? Atau justru terlalu ketat?
- **SHORT ratio <15%** → scalp regime masih bias LONG? Cek distribusi scalp_regime.
- **Grade S = 0** → OB threshold terlalu tinggi (cek median per aset) atau scalp regime selalu CHOPPY.
- **All signals same rank** → ranking dimensi tidak cukup diskriminatif.

### A.7 Known Issues (belum fix)

- **Max hold masih 25-35 menit** di risk_manager (score-driven hold). Harusnya 15 menit flat.
- **Setup classifier masih dekoratif** — label tidak pengaruhi size atau exit. Sweep, breakout, pullback, momentum diperlakukan sama.
- **Dead code:** liquidation analyzer (0% fire rate), AI Intelligence (r=-0.101 inverse). Masih jalan tapi tidak dipakai.
- **ATR minimum gate disabled** — tidak ada proteksi untuk aset dengan ATR terlalu rendah.

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
