# KARA — Audit #18 (1 Juni 2026) — VERIFIKASI DEPLOY AUDIT #17

> **Aturan untuk besok (AI/Kiro): TIDAK ADA kesimpulan tanpa data produksi fresh.**
> Setiap klaim "fix bekerja" HARUS dibuktikan dengan korelasi/WR per bucket dari DB Railway.
> Setiap fix yang gagal trigger → ROOT CAUSE, bukan disable. Setiap action item format:
> `[what]` → `[why]` → `[how to verify]`. Sampel < 100 = bug-detection, BUKAN validasi final.
> **Hitung SEMUA korelasi secara DIRECTIONAL** (untuk SHORT, balik tanda). Audit #17 sempat
> salah karena `margin` dihitung non-directional (+0.282 → ternyata +0.076).

---

## 0. KONTEKS — Root cause yang ditemukan Audit #17

Slow-bleed (PF 0.59, PnL −Rp230rb / 56 trade) BUKAN karena hold time / threshold.
**Root cause: score agregat INVERSE (r=−0.159)** karena menjumlahkan 1 sinyal prediktif
(orderbook, r=+0.337) dengan ~8 komponen noise/inverse yang menenggelamkannya.

Bukti komponen (directional, 56 trade):
| Komponen | r(PnL) | Verdict |
|----------|--------|---------|
| orderbook (OB) | **+0.337** | ✅ satu-satunya edge robust |
| momentum 5m | +0.228 | ✅ prediktif (candle HL asli) |
| CVD 5m (zona sedang) | +0.172 | ✅ prediktif (proxy candle) |
| net conviction (bull−bear) | +0.076 | noise |
| RSI/confirm | −0.125 | lagging |
| OI/funding (1h lama) | −0.072 | inverse |
| LOC (large order cluster) | **−0.284** | 🔴 inverse terburuk |
| liquidation | 0.000 | mati (0% fire) |

---

## 1. SEMUA PERUBAHAN YANG DI-DEPLOY (31 Mei) — 8 fungsional + 4 kosmetik

### A. FIX FUNGSIONAL (mempengaruhi scoring/trading) — WAJIB verifikasi

| # | Fix | File | Bukti pra-deploy | Marker log |
|---|-----|------|------------------|-----------|
| 1 | **OB-dominant scoring** — strong wall (ob_dir≥12) +15, OB lawan arah −20 | scoring_engine.py | score r −0.159 → +0.242 (replay) | `ob_edge=` di `[SCORE-DEBUG]` |
| 2 | **Disable liq** (`_LIQ_SCORING_ENABLED=False`) | scoring_engine.py | 0/174 fire | `LIQ=0` selalu |
| 3 | **OI momentum 5m** (bukan 1h) + threshold 0.001→0.0005 | oi_funding_analyzer.py | 5m r=+0.228 vs 1h −0.064 | reasons "OI/Funding bullish/bearish" |
| 4 | **AI advisory-only** (`_AI_SCORING_ENABLED=False`) | scoring_engine.py | timeout 67%, conf r=−0.101 | `🧠 AI (advisory)` di reasons |
| 5 | **Vote-margin gate** — skip kalau `vote_margin<2` | scoring_engine.py | PF 0.59→0.67, hemat $4.07 (replay) | `reason=low_vote_consensus` |
| 6 | **Disable LOC** (`_LOC_SCORING_ENABLED=False`) | scoring_engine.py | r=−0.284, no threshold saves it | `loc_pts=0` telemetry |
| 7 | **Re-enable CVD 5m ZONE** — bonus +6 hanya di zona sedang (0.3-0.7) | scoring_engine.py | 5m r=+0.172 (proxy) | `CVD 5m moderate` di reasons |
| 8 | **AI context data fix** — kirim `_net_move` (5m) bukan `trend_pct` (24h) | scoring_engine.py | AI salah baca momentum | `momentum_move_pct` + `trend_pct_24h` |

### B. FIX KOSMETIK (label/tampilan saja — TIDAK sentuh trading/PnL DB)

| # | Fix | File | Gejala lama |
|---|-----|------|-------------|
| K1 | TP1/TP2 % pakai level TP (bukan current_price) | telegram.py | TP1=TP2 % identik / TP2<TP1 |
| K2 | Daily report timing — laporkan hari KEMARIN saat reset | telegram.py + main.py | Trades=0, Best/Worst $0 padahal ada PnL |
| K3 | PnL card double-count — `pnl` bukan `pnl_realized+pnl` | telegram.py | Kartu +14% (2×) vs teks/DB +7% |
| K4 | Label momentum_death (was mislabeled "Time Exit") | main.py | Chat "Momentum Death" tidak pernah muncul |

**PENTING:** Fix K1-K4 hanya display. **Field `reason`/`pnl_usd` di DB SELALU benar** → semua
audit data tetap akurat. Jangan ragukan angka DB karena bug display ini.

**Commit boundary:** `railway ssh --service rare-youthfulness "stat -c %Y /app/engine/scoring_engine.py"`

---

## 2. STEP PERTAMA — Tarik data & tentukan boundary (WAJIB)

```powershell
# AUDIT_RUNBOOK.md Step 1-1b. Single-user dedup (user 7667519263).
venv\Scripts\python.exe tmp\export_audit17.py
railway ssh --service rare-youthfulness "stat -c %Y /app/engine/scoring_engine.py"
```
`[what]` Tarik `trade_history` + `signals_history` + `ai_verdicts`. Pisahkan PRE/POST deploy.
`[why]` Hanya trade POST yang menguji 8 fix. Audit #17 data 100% cocok jurnal user — pertahankan.
`[how to verify]` Total PnL hasil tarik = jurnal Telegram. Trade POST ≥ 20. Kalau <20 → tunggu.

---

## 3. VERIFIKASI PER FIX (jawab dengan ANGKA)

### Fix #1 — OB-dominant → **TARGET UTAMA**
`[what]` Korelasi `score ↔ PnL` (Pearson + Spearman) directional, trade POST.
`[how to verify]` ✅ r > +0.15 DAN top-quartile WR > 50%. ⚠️ 0<r<+0.15 = netral (pertahankan).
🔴 r<0 → cari kenapa (regime? OB fire turun?) JANGAN rollback tanpa root cause.
Bucket: `ob_dir≥12` WR>55%, `ob_dir<0` minim (veto jalan).

### Fix #2 — Disable liq
`[how to verify]` 100% signal POST `liquidation_score=0`. Task opsional: turunkan threshold OKX
($2K→$500), ukur fire rate. Re-enable HANYA jika fire>0% + WR bucket bagus.

### Fix #3 — OI momentum 5m
`[how to verify]` `oi_funding_score ↔ PnL` directional naik dari −0.072 ke ≥0.00. Trade dengan OI
bonus harus WR ≥ trade tanpa (dulu kebalik 29% vs 48%). 🔴 Masih inverse → kalibrasi threshold
funding ke realita HL (extreme 0.0003→0.0001).

### Fix #4 — AI advisory-only
`[how to verify]` `ai_verdicts` POST: `score_after == score_before`. Tidak ada `[AI-VETO]` di log.
Re-enable HANYA jika timeout<20% DAN conf r(PnL)>+0.10.

### Fix #5 — Vote-margin gate
`[how to verify]` Log `reason=low_vote_consensus` muncul. SEMUA trade POST `vote_margin≥2`. WR naik
dari 34%. 🔴 Trade margin<2 masih ada → `vote_margin` tidak ter-populate (cek `out_components`).

### Fix #6 — Disable LOC
`[what]` Konfirmasi LOC tidak lagi nambah skor + ukur dampak hilangnya inflasi skor.
`[how to verify]` `loc_pts=0` di semua signal (telemetry boleh tercatat tapi tidak masuk bull/bear).
73% trade dulu dapat LOC → skor turun ~6-10 pts → cek apakah trade jelek (immediate-reverser) berkurang.
**Catatan:** LOC adalah trade-tape (lagging). "Follow the money" sejati butuh on-chain (Arkham/Nansen)
+ hold lebih panjang = FITUR BARU pasca-deadline, bukan ini.

### Fix #7 — Re-enable CVD 5m ZONE ⚠️ PALING PERLU VALIDASI
`[what]` CVD 5m candle-PROXY (bukan tick CVD asli). Bonus +6 hanya zona sedang (0.3-0.7).
`[why]` Re-test Audit #17: CVD 5m r=+0.172 (vs disable 24 Mei yang pakai window 80-trade lagging).
Zona: flat(-0.3..0.3)=WR 0%, sedang(0.3..0.7)=WR 50%, ekstrem(0.7..1.0)=WR 37% exhaustion.
`[how to verify]`
- ✅ Trade dengan `CVD 5m moderate` reason → WR > trade tanpa.
- 🔴 Kalau CVD-bonus trades WR < rata-rata → proxy candle ≠ tick CVD; **disable lagi** atau
  ganti ke tick CVD asli dari WS cache.
- **TASK:** instrument tick CVD asli (per-trade side B/A) untuk bandingkan vs proxy candle.
  Proxy mungkin tidak sama dengan tick — ini risiko utama fix #7.

### Fix #8 — AI context data
`[how to verify]` Verdict AI POST: cek `momentum_move_pct` sekarang = nilai 5m (kecil, ±0.x%),
bukan trend 24h (±%besar). Reasoning AI tidak lagi salah sebut "opposing momentum -4%".

---

## 4. METRIK AGREGAT — GO / NO-GO

| Metrik | Audit #17 (PRE) | Target #18 | Gate |
|--------|------------------|-----------|------|
| Score↔PnL r | −0.159 | **> +0.15** | Fix #1 berhasil |
| Profit Factor | 0.59 | **> 1.0** | Bot stop bleed |
| Win Rate | 34% | **> 42%** | Break-even (R:R 1.15 → BE WR 46.5%) |
| Time exit % | 66% | < 55% | Entry quality naik |
| Trailing fire % | 14.3% | **≥ 25%** | Edge utama hidup (§6a) |
| Volume (trade/hr) | 1.21 | ≥ 0.8 | Gate tidak over-block |
| PnL (single user) | −Rp230rb | **≥ break-even** | — |

`[how to verify]` PF>1.0 DAN r>+0.15 → fix tervalidasi, lanjut threshold tuning (§6b).
Kalau tidak → bisect (§7).

---

## 5. TEMUAN BARU YANG BELUM DI-FIX (investigasi, JANGAN deploy tanpa data)

### 5a. Bybit L/S ratio — HIDUP, bukan DEAD (koreksi asumsi lama)
`[what]` Asumsi lama "Bybit blocked 403" SALAH untuk endpoint market-data. Tes dari Railway:
`/v5/market/account-ratio` → **HTTP 200**, data real-time (WLD 2.85). Komponen L/S dapat data asli.
`[temuan]` Tapi sebagai sinyal: L/S fired n=12 → WR 25% (vs absent 36%). DAN ada **bug kontradiksi**:
6 trade L/S contrarian bilang SHORT (+12 bear) tapi trade tetap LONG → L/S melawan arah final.
`[how to verify / next]` Kumpulkan 50+ trade dengan L/S. Kalau tetap inverse → tambah guard
kontradiksi (L/S tidak beri poin kalau lawan arah final, mirip guard OB-vs-SHORT). n=12 TERLALU KECIL.

### 5b. CVD proxy vs tick CVD (lihat Fix #7)
`[what]` Fix #7 pakai proxy candle. Tick CVD asli (side B/A per trade) bisa beda.
`[next]` Instrument tick CVD, bandingkan r vs proxy di data POST.

---

## 6. RESIDUAL (carry-over)

### 6a. Hold-aware SL menekan trailing (dari Audit #16)
`[what]` P0-4 Audit #16 melebarkan SL → TP1 1.05% → trailing fire turun 14.3%.
`[how to verify]` Trailing fire POST. Kalau <20% → rollback hold-aware SL (cap TP1 → 0.8%). Replay dulu.

### 6b. Threshold entry (HANYA jika Fix #1 PASS)
`[what]` Naikkan `min_score_to_enter` untuk panen tier-atas. Skor sudah ramping (LOC/liq hilang)
→ angka absolut turun, threshold HARUS dikalibrasi ulang ke skala baru.
`[why]` Setelah Fix #1 confirm prediktif, gating jadi defensible (bukan lazy). Satu variabel per deploy.

---

## 7. KALAU REGRESI — PROTOKOL BISECT

8 fix fungsional deploy bersama (melanggar satu-variabel — diakui, deadline). Kalau POST < PRE:

`[what]` Toggle per fix via flag, urut paling berisiko:
1. Fix #1 (OB-dominant) — dampak skor terbesar. Cek `ob_dir<0` veto over-block.
2. Fix #7 (CVD proxy) — paling belum tervalidasi (proxy ≠ tick). Set `_CVD_ENABLED=False`, ukur.
3. Fix #5 (vote-gate) — cek volume drop >40%? Longgarkan ke skip-margin-0-saja.
4. Fix #6 (LOC) / Fix #3 (OI 5m) — cek dampak masing-masing.
`[how to verify]` Flag toggle (`_LOC_SCORING_ENABLED`/`_CVD_ENABLED`/`_AI_SCORING_ENABLED` style),
replay per toggle pada data POST.

---

## 8. RED FLAGS

| Kondisi | Action |
|---------|--------|
| Score↔PnL r < −0.10 POST | Fix #1 gagal transfer → root cause. JANGAN rollback buta. |
| Volume turun > 50% | Vote-gate/OB-veto/CVD terlalu agresif → longgarkan satu per satu. |
| Trailing fire < 15% | Hold-aware SL bottleneck → §6a. |
| PF < 0.7 (≥30 trade POST) | Salah satu fix merusak → bisect §7. Curigai Fix #7 (CVD proxy) dulu. |
| CVD-bonus trades WR < avg | Proxy ≠ tick → disable CVD lagi (Fix #7 rollback). |
| `low_vote_consensus` skip = 0 | Vote-gate tidak jalan → cek populate. |
| AI `score_after != score_before` | Fix #4 tidak ter-deploy → verifikasi commit. |
| liq/LOC score != 0 | Fix #2/#6 tidak ter-deploy. |

---

## 9. DEADLINE 1 JUNI — STATUS REALISTIS

Target live 1 Juni **TIDAK tercapai** (PF 0.59 PRE-fix). Status jujur:

| Kriteria | Target | PRE-fix | Realistis |
|----------|--------|---------|-----------|
| PF > 1.3 (3 audit konsisten) | ≥1.3 | 0.59 | Butuh #18,#19,#20 PASS |
| Score↔PnL r > +0.15 | ≥+0.15 | −0.159 / +0.242 replay | #18 konfirmasi |
| Trailing fire ≥ 25% | ≥25% | 14.3% | §6a |
| Live executor tested | ✅ | ❌ | Belum mulai |
| Slippage measured | ✅ | ❌ | Belum mulai |

**Rekomendasi:** Geser GO-LIVE ke **setelah 3 audit PF>1.0 berturut** (4-6 Juni earliest).
1 Juni = hari validasi deploy #17, BUKAN go-live.

---

## 10. TIMELINE BESOK (1 Juni)

| Waktu (WIB) | Action |
|-------------|--------|
| Pagi | Tarik data POST (§2). Boundary + trade POST ≥ 20. |
| Siang | Verifikasi 8 fix fungsional (§3). Angka, bukan opini. Prioritas #1 dan #7. |
| Sore | Metrik agregat GO/NO-GO (§4). PASS → threshold (§6b). FAIL → bisect (§7). |
| Malam | Tulis `AUDIT_TODO_2JUNI.md` + next single-variable deploy. |

---

## 11. PRINSIP YANG TIDAK BOLEH DILANGGAR

1. **Data mengalahkan hipotesis.** Audit #17 sempat salah (sign-error SHORT: margin +0.282→+0.076).
   Hitung directional yang BENAR.
2. **Disable = pilihan terakhir + ada path fix dicatat.** OI di-FIX (timeframe). LOC di-disable
   setelah 3 angle threshold diuji semua rugi + flaw konseptual (lagging). Liq disable 2+ audit 0% fire.
3. **AI = detektor kontradiksi, bukan oracle.** Insight AI → RULE deterministik (vote-gate), bukan
   bergantung API timeout 67%. AI yang "pintar tapi salah" sering karena dikasih DATA salah (Fix #8).
4. **Replay gate sebelum deploy scoring.** r > +0.15 + bucket/decile sehat.
5. **Sampel < 100 = bug-detection.** Jangan over-claim "tervalidasi". L/S (n=12), CVD (proxy) = lemah.
6. **Satu variabel per deploy MULAI SEKARANG.** 8 fix sekaligus = terakhir kali (deadline). Besok
   deploy lagi → SATU saja, biar bisa atribusi.
7. **Bug display ≠ bug data.** K1-K4 cuma label Telegram. DB selalu benar. Audit pakai DB.
