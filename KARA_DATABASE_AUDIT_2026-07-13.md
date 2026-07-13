# KARA Database Audit Report

## Executive Summary

Audit memakai database Railway production secara read-only sesuai `DATABASE_AUDIT_GUIDE.md`:

- `/data/kara_data.db`
- `/data/kara_ml.db`
- Periode closed trade: 2026-07-11 13:52:04 UTC sampai 2026-07-13 16:20:10 UTC, 50,47 jam.
- Sampel utama: 554 closed trade, 554 exact trade-to-ML joins, 560-562 persisted signal selama snapshot audit.
- Kedua database: `PRAGMA integrity_check = ok`.
- Semua 554 trade punya ML row exact-match pada `pos_id`, `chat_id`, asset, dan side. Dua ML row belum punya closed trade.
- Snapshot berubah selama audit karena bot aktif: signal bertambah 560 menjadi 562 dan ML row 554 menjadi 556. Semua angka performa memakai fixed closed-trade snapshot 554 row.

Hasil utama:

1. Stop-loss bukan terlalu sering secara frekuensi. Hanya 32/554 trade (5,78%). Dampaknya sangat besar: net -$83,26, mean -$2,60, median -$2,69, PF 0,068. Stop-loss menghapus lebih dari seluruh net profit strategi sebelum kontribusi exit lain.
2. Time Exit bukan hampir selalu loss. Time Exit 396/554 (71,48%), WR 50,51%, net -$10,11, PF 0,938, expectancy -$0,026. Persepsi benar pada dominasi frekuensi, salah pada win rate.
3. Time Exit mencampur mekanisme berlawanan. Pada enriched cohort, `microstructure_invalid` 39/39 loss, net -$64,90; `no_follow_through` n=217 justru net +$82,98, PF 3,057. Mematikan Time Exit akan membuang mekanisme positif.
4. Root cause terbesar: mismatch target dan horizon. Pada attribution signal unik, planned TP1 median 1,20%, TP2 median 2,00%; MFE median enriched hanya 0,183%, winner median 0,452%, Time Exit median 0,152%. Hanya 3/40 attributed enriched trade (7,5%) mencapai planned TP1 berdasarkan observed MFE. Kode native scalper sebenarnya membatasi TP1 <=0,70% dan TP2 <=1,10%, tetapi level itu ditimpa ATR localization menjadi 1,5x dan 2,5x SL sebelum clock 12-18 menit.
5. Signal score tidak monoton terhadap outcome. Full sample PF: score 60-64 = 1,403; 65-71 = 1,131; 72+ = 1,342. Enriched cohort lebih buruk: 60-64 PF 1,801, 65-71 PF 1,086, 72+ PF 0,878. Sizing justru menaikkan risk dari 2,5% ke 3,0%-3,5% pada score tinggi.
6. Entry-location gate punya edge terbatas tetapi `weak` merugi. Enriched: excellent n=149, PF 1,385, +$26,17; valid n=97, PF 1,241, +$13,28; weak n=71, PF 0,644, -$14,47. Ini bukti paling bersih untuk memperbaiki representasi/gating entry, bukan menambah filter acak.
7. Overall strategy belum terbukti stabil. Full sample: WR 56,86%, PF 1,251, expectancy +$0,117/trade, net +$64,95, max drawdown -$32,40. Bootstrap 95% CI expectancy [-$0,010, +$0,249] melintasi nol. Enriched cohort: PF 1,137, expectancy +$0,071 dengan CI [-$0,103, +$0,253]. Periode hanya 50,47 jam.

Kesimpulan utama: masalah bukan stop terlalu sempit dan bukan Time Exit secara umum. Masalah utama ialah target yang tidak konsisten dengan horizon/MFE, kualitas entry `weak`, score yang belum calibrated tetapi dipakai untuk conviction sizing, serta telemetry yang belum cukup untuk mengaudit MAE, slippage, pure scalper vs fallback, dan counterfactual exit.

## Temuan Utama

| Severity | Temuan | Bukti | Confidence |
|---|---|---|---|
| Critical | Target scalper tidak cocok dengan horizon 12-18 menit | Attributed signal n=270: TP1 median 1,20%, TP2 2,00%. Enriched MFE n=321: median 0,183%; winner median 0,452%. Attributed enriched n=40: hanya 7,5% mencapai TP1 | High untuk mismatch; Medium untuk magnitude karena signal join inferred |
| High | Stop-loss jarang tetapi tail loss menghancurkan payoff | n=32, 5,78% trades, net -$83,26, mean -$2,60, PF 0,068; signed exit move median -0,939% | High |
| High | Score belum predictive secara monoton, tetapi mengontrol risk | Enriched 72+ n=68, PF 0,878 vs 60-64 n=73, PF 1,801; code risk 2,5%-3,5% | High untuk non-monotonicity; Medium untuk causal PnL impact |
| High | `weak` entry-location cohort negatif | n=71, WR 42,25%, PF 0,644, net -$14,47 | High in-sample; Medium generalization |
| Medium | Time Exit label menyembunyikan dua proses berbeda | `microstructure_invalid` n=39, net -$64,90; `no_follow_through` n=217, net +$82,98 | High descriptive; Low untuk counterfactual quality |
| Medium | Winner-to-loser masih terjadi | 16/321 enriched (4,98%) punya MFE >=0,35% lalu final PnL <=0; net -$9,20. Sepuluh berakhir `profit_lock_stop`, enam `time_exit` | High |
| Medium | SHORT payoff buruk | n=24, WR 54,17%, PF 0,526, expectancy -$0,262; avg winner $0,54 vs loser -$1,21 | Medium, sample pendek |
| Medium | Meta boost +5 berkorelasi negatif | n=29, WR 34,48%, PF 0,367, net -$20,46 | Medium; deployment/regime confounding belum dipisahkan |
| Low | Jam 10 UTC terlihat buruk | n=25, WR 24%, PF 0,229, net -$24,57 | Low; hanya tiga tanggal dan multiple-comparison risk |

## Audit Signal Scanning

### Cakupan signal

Persisted signal snapshot:

- 562 signal.
- 538 LONG, 24 SHORT.
- Semua `trade_mode=scalper`.
- Score: 60-64 n=158; 65-71 n=268; 72+ n=136.
- Regime: normal n=333; high_vol n=167; trending n=24; ranging n=15; low_vol n=13; volatile n=10.
- Entry location: excellent n=151; valid n=99; weak n=71; missing n=241.

Database hanya menyimpan signal yang lolos pre-trade. Rejected scan, candidate tanpa signal, dan future return semua candidate tidak disimpan. Karena itu false-positive rate scanner secara unconditional tidak dapat dihitung. Angka outcome hanya conditional pada persisted/executed signal.

### Kualitas score/confidence

Full sample:

| Score | n | WR | PF | Expectancy | Net |
|---|---:|---:|---:|---:|---:|
| 60-64 | 156 | 63,46% | 1,403 | +$0,151 | +$23,59 |
| 65-71 | 263 | 52,09% | 1,131 | +$0,064 | +$16,74 |
| 72+ | 135 | 58,52% | 1,342 | +$0,182 | +$24,62 |

Enriched cohort setelah telemetry/exit deployment berubah:

| Score | n | WR | PF | Expectancy | Net |
|---|---:|---:|---:|---:|---:|
| 60-64 | 73 | 54,79% | 1,801 | +$0,282 | +$20,59 |
| 65-71 | 180 | 47,78% | 1,086 | +$0,045 | +$8,02 |
| 72+ | 68 | 44,12% | 0,878 | -$0,085 | -$5,80 |

Hipotesis: score tinggi merepresentasikan strength tetapi tidak probability-calibrated untuk horizon scalper.

Verifikasi: ranking outcome tidak monoton pada full maupun enriched cohort. Score 72+ enriched lebih buruk daripada 60-64 pada WR, PF, dan expectancy. Time Exit score 72+ juga negatif: n=89, PF 0,540, net -$23,88. Score 60-64 Time Exit positif: n=114, PF 1,788, net +$23,99.

Root cause mekanistik: `get_risk_pct()` menganggap score sebagai conviction dan meningkatkan risk (`risk/risk_manager.py:373-402`), padahal score belum calibrated sebagai conditional win probability/EV. Confidence: High untuk mismatch semantic; Medium untuk total PnL attribution.

### Entry-location quality

Enriched cohort memberi separation lebih baik daripada score:

| Quality | n | WR | PF | Expectancy | Net |
|---|---:|---:|---:|---:|---:|
| excellent | 149 | 50,34% | 1,385 | +$0,176 | +$26,17 |
| valid | 97 | 51,55% | 1,241 | +$0,137 | +$13,28 |
| weak | 71 | 42,25% | 0,644 | -$0,204 | -$14,47 |

Hipotesis: weak-location gate masih meloloskan false positive struktural.

Verifikasi: `weak` negatif di WR, PF, expectancy, median, dan net; n=71 cukup menurut guide. Namun ini belum membuktikan solusi "block weak" generalize, karena weak memerlukan score tinggi dan dapat berinteraksi dengan bug target/horizon.

Confidence: High untuk descriptive edge; Medium untuk causal intervention.

### Regime, volatility, trend, funding, OI, orderbook

Volatility tercile full cohort:

| Volatility | n | PF | Expectancy |
|---|---:|---:|---:|
| Low tercile | 186 | 1,589 | +$0,163 |
| Mid tercile | 185 | 1,286 | +$0,141 |
| High tercile | 183 | 1,073 | +$0,046 |

Loss tail membesar pada high volatility: avg loser -$1,49 vs -$0,66 low tercile. Bukti mendukung volatility-risk mismatch, tetapi CI expectancy high tercile [-$0,225, +$0,325] lebar.

Trend strength:

- abs trend <1%: n=140, PF 1,622, +$0,243/trade.
- abs trend 1-3%: n=252, PF 1,277, +$0,129/trade.
- abs trend >=3%: n=162, PF 0,983, -$0,009/trade.

Temuan berlawanan dengan asumsi bahwa trend kuat otomatis lebih baik. Belum layak menjadi filter karena realized trend dapat correlated dengan volatility dan session.

Funding seluruh 554 trade masuk bucket near-zero. Tidak ada variasi cukup untuk menguji funding.

OI raw tidak disimpan. `oi_score` sebenarnya combined `oi_funding_score`; 506/554 row bernilai 0. Orderbook score 505/554 row bernilai 0. Data terlalu sparse/degenerate untuk membuktikan edge OI/orderbook.

Volume entry tidak disimpan. Momentum raw, CVD, RSI, spread, dan latency signal juga tidak tersedia pada exact trade join.

### Market timing

Jam 10 UTC buruk: n=25, PF 0,229, -$24,57. Namun periode hanya tiga hari dan banyak bucket jam diuji. Ini hypothesis-generation, bukan bukti untuk blocked-hours filter.

Hari:

- 2026-07-11: n=131, PF 1,534, +$24,60.
- 2026-07-12: n=222, PF 1,513, +$47,08.
- 2026-07-13: n=201, PF 0,944, -$6,73.

Regime/deployment drift besar. Tidak ada dasar untuk filter hari.

## Audit Entry Execution

Exact trade-to-signal join tidak tersedia karena `signal_id` tidak disimpan di `trade_history` atau `ml_experience`. Audit memakai attribution konservatif: asset, side, score identik; signal mendahului ML entry <=30 detik; candidate harus unik dua arah. Hasil 270/554 attributed, tetap diberi label inferred.

Pada n=270 inferred attribution:

- Planned-to-paper-fill adverse slippage mean 2,99 bps.
- Median 2,98 bps.
- P90 3,81 bps.

Angka cocok dengan simulator spread/noise paper dan terlalu kecil untuk menjelaskan PF rendah atau Time Exit dominan. Ini bukan trigger-to-fill slippage live.

Entry terlalu dini/terlambat tidak dapat dibuktikan karena database tidak menyimpan pre-entry move, signal candle close, post-signal path, atau MAE. MFE rendah pada losers menunjukkan banyak signal tidak pernah follow-through, tetapi tidak membedakan entry terlalu dini dari thesis salah.

## Audit Position Management

### Position sizing

Kode sizing:

- Score 60-67: risk 2,5%.
- Score 68-74: risk 3,0%.
- Score >=75: risk 3,5%.
- Stop risk dinormalisasi melalui `size = balance * risk_pct / (sl_pct * leverage)`.
- Margin cap 35% equity dapat mengubah risk aktual.

Sumber: `risk/risk_manager.py:266-355`, `risk/risk_manager.py:373-402`.

Masalah: score tidak calibrated tetapi langsung mengontrol risk. Proxy risk-normalized return pada enriched n=317, `pnl_usd / (notional * micro_risk_pct)`:

- Score 60-64: n=72, mean +0,410 proxy-R, PF 2,307.
- Score 65-71: n=180, mean +0,063, PF 1,158.
- Score 72+: n=65, mean -0,034, PF 0,926.

Ini bukan audited R multiple karena `micro_risk_pct` dapat berasal dari micro invalidation atau planned SL fallback. Arah hasil tetap menolak asumsi bahwa high score pantas otomatis mendapat risk lebih besar.

### Leverage dan exposure

Leverage aktual tidak disimpan pada closed trade. Notional dan size ada, tetapi account balance, concurrent exposure saat entry, dan risk budget portfolio tidak ada. Audit leverage/exposure lengkap belum dapat dibuat.

### Planned reward:risk

Pada inferred signal n=270:

- Planned SL mean 0,803%, median 0,800%, p90 0,800%.
- Planned TP1 mean 1,176%, median 1,200%, p90 1,200%.
- Planned TP2 mean 1,961%, median 2,000%, p90 2,000%.
- Mean planned RR1 1,47; RR2 2,44.

RR nominal terlihat baik, tetapi horizon tidak memberi cukup MFE untuk merealisasikannya. RR desain tanpa target-hit probability menyesatkan.

## Audit Exit Logic

### Exit comparison

| Exit | n | Share | WR | PF | Mean | Median | Net |
|---|---:|---:|---:|---:|---:|---:|---:|
| stop_loss | 32 | 5,78% | 15,63% | 0,068 | -$2,602 | -$2,693 | -$83,26 |
| time_exit | 396 | 71,48% | 50,51% | 0,938 | -$0,026 | +$0,006 | -$10,11 |
| profit_lock_stop | 31 | 5,60% | 67,74% | 2,362 | +$0,227 | +$0,084 | +$7,05 |
| trailing_stop | 64 | 11,55% | 100% | N/A | +$1,723 | +$1,274 | +$110,25 |
| close_all | 30 | 5,42% | 80,00% | 21,43 | +$1,151 | +$0,312 | +$34,52 |
| manual | 1 | 0,18% | 100% | N/A | +$6,497 | +$6,497 | +$6,50 |

Catatan: reason ialah final closer. Trade `stop_loss` dapat sudah merealisasikan partial profit, menjelaskan lima positive cumulative-PnL stop rows pada full cohort. Enriched stop-loss n=15 semuanya loss.

### Mengapa stop-loss merusak

Hipotesis 1: SL terlalu sempit.

Hasil: tidak terbukti. Signed exit move median -0,939%, sedangkan planned SL mayoritas 0,80%. Observed MFE stop-loss enriched n=15 median hanya 0,095%, p90 0,309%; tidak ada yang mencapai MFE 0,35%. Signal ini hampir tidak pernah benar sebelum stop. Memperlebar SL kemungkinan hanya memperbesar loss bila thesis sama.

Hipotesis 2: execution overshoot memperbesar stop loss.

Hasil: data tidak cukup. Hanya satu enriched stop punya unique signal attribution pada robustness snapshot; overshoot 4,70 bps. Trigger price, fill price, polling delay, dan slippage tidak disimpan. Confidence Low.

Hipotesis 3: position risk terlalu besar pada signal yang tidak calibrated.

Hasil: didukung. Stop mean -$3,31 untuk losing stop rows, sementara overall avg winner +$1,03. Score tinggi mendapat risk lebih besar tanpa monotonic edge. Confidence Medium-High.

Hipotesis 4: stop loss ialah endpoint false-positive entry, bukan root cause exit.

Hasil: didukung. Stop cohort MFE sangat rendah dan weak-location cohort negatif. Confidence Medium karena MAE/pre-entry path tidak ada.

### Mengapa Time Exit terlihat buruk

Time Exit median hold 12,06 menit; p10 12,00; p90 18,03. Cluster tepat pada state machine 12/18 menit membuktikan exit clock mendominasi outcome.

Namun subtype berbeda:

| Trigger | n | WR | PF | Mean | Net |
|---|---:|---:|---:|---:|---:|
| microstructure_invalid | 39 | 0% | 0 | -$1,664 | -$64,90 |
| no_follow_through | 217 | 54,84% | 3,057 | +$0,382 | +$82,98 |
| missing/legacy | 140 | 57,86% | 0,508 | -$0,201 | -$28,19 |

`microstructure_invalid` baru aktif ketika trade sudah adverse <=-0,30% plus structure/momentum invalid (`risk/risk_manager.py:935-957`). Kerugian pada bucket itu bukan bukti exit menyebabkan loss. Tanpa post-exit counterfactual, belum diketahui apakah rule menyelamatkan trade dari SL atau memotong reversal.

`no_follow_through` positif. Rule ini tidak boleh dimatikan.

Legacy/missing trigger negatif, tetapi subtype tidak tersedia. Cohort ini tidak dapat dipakai untuk perubahan spesifik.

### Winner menjadi loser

Enriched n=321:

- 16 trade (4,98%) punya observed MFE >=0,35% lalu final cumulative PnL <=0.
- Net -$9,20; median -$0,298.
- 10 berakhir `profit_lock_stop`, net -$5,18.
- 6 berakhir `time_exit`, net -$4,03.

Profit lock memindahkan stop ke entry +0,05%, tetapi paper fills memakai current polling price, partial accounting/fees dapat menghasilkan final loss, dan state update tidak langsung dipersist ketika lock arm. Sumber: `risk/risk_manager.py:790-812` dan executor persistence path.

Root cause belum sepenuhnya terbukti karena trigger/fill dan partial-event ledger tidak tersedia.

### Exit terlalu cepat atau lambat

- Time Exit terlalu cepat dibanding planned TP: High confidence. TP1 median 1,20% versus Time Exit MFE median 0,152%; 12-18 menit tidak konsisten dengan target itu.
- `no_follow_through` terlalu cepat: tidak didukung; bucket positif kuat.
- `microstructure_invalid` terlalu cepat: belum dapat disimpulkan tanpa future path.
- Trailing terlalu cepat/lambat: tidak terbukti. Full cohort trailing sangat positif, tetapi hanya sembilan enriched trailing rows; deployment changed, sample baru insufficient.

## Validasi Statistik

### Overall

| Metric | Value |
|---|---:|
| Trades | 554 |
| Wins | 315 |
| Win Rate | 56,86% |
| Wilson 95% CI WR | 52,70%-60,92% |
| Net PnL | +$64,95 |
| Expectancy | +$0,117/trade |
| Bootstrap 95% CI expectancy | -$0,010 sampai +$0,249 |
| Profit Factor | 1,251 |
| Average Winner | +$1,027 |
| Average Loser | -$1,082 |
| Payoff Ratio | 0,949 |
| Median PnL | +$0,084 |
| Max Drawdown | -$32,40 |
| Median Holding Time | 12,05 menit |

Expectancy USD belum statistically separated dari nol. ROE mean +0,978% per trade dengan bootstrap CI +0,077% sampai +1,913%, tetapi `pnl_pct` semantics berubah lintas deployment dan tidak layak dibandingkan sebagai satu homogeneous cohort.

### MFE

MFE tersedia hanya enriched n=321 dan berbasis polling, bukan true intrabar:

| Cohort | n | Median MFE | P75 | P90 |
|---|---:|---:|---:|---:|
| Overall enriched | 321 | 0,183% | 0,480% | 0,857% |
| Final winner | 156 | 0,452% | 0,667% | 1,126% |
| Final loser | 165 | 0,064% | 0,169% | 0,347% |
| Time Exit | 256 | 0,152% | 0,319% | 0,563% |
| Profit lock | 31 | 0,596% | 0,850% | 1,091% |
| Stop loss | 15 | 0,095% | 0,198% | 0,309% |

MAE tidak disimpan. Audited R Multiple tidak dapat dihitung.

### Session bonus

- Bonus +10: n=145, PF 2,120, +$51,10.
- Bonus +14: n=220, PF 1,214, +$24,86.
- Bonus +4: n=160, PF 1,039, +$3,07.
- Bonus 0: n=23, PF 0,074, -$12,25.

Bonus 0 tampak buruk tetapi n=23 dan dapat menjadi proxy deployment/session. Jangan jadikan filter sebelum walk-forward lebih panjang.

### Asset

Hanya dua contoh loss dengan sample mendekati/di atas rule guide:

- ARB: n=13, PF 0,374, net -$8,64.
- EIGEN: n=9, WR 0%, net -$18,72; `insufficient sample` karena n<10.

Periode 50 jam terlalu pendek untuk asset blacklist. Tidak direkomendasikan.

## Daftar Root Cause

Urut dampak terbesar:

1. **Level contract mismatch antara native scalper dan localization**. Native builder mengkalibrasi TP untuk 12 menit (`engine/scoring_engine.py:1157-1175`), lalu `localize_for_user()` menimpa level memakai ATR RR 1,5/2,5 (`models/schemas.py:194-248`), sementara main mengira scalper tetap fixed (`main.py:1054-1078`). Bukti target vs MFE kuat. Confidence High.
2. **Signal score dipakai sebagai conviction sizing sebelum calibration**. Score tidak monoton, enriched 72+ negatif, tetapi risk naik sampai 3,5%. Confidence High untuk design defect; Medium untuk exact drawdown contribution.
3. **Weak entry-location cohort tidak punya edge pada deployment ini**. n=71, PF 0,644, net -$14,47. Confidence Medium-High.
4. **Stop-loss ialah tail endpoint dari non-follow-through signal dan position risk, bukan frekuensi berlebih**. 5,78% frequency tetapi -$83,26; stop MFE sangat rendah. Confidence Medium-High.
5. **Exit taxonomy mencampur good and bad processes**. Time Exit aggregate menyembunyikan `no_follow_through` positif dan `microstructure_invalid` negative-by-construction. Confidence High.
6. **Winner protection tidak punya durable trigger/fill/event telemetry**. 16 winner-to-loser; mekanisme exact tidak bisa dipisahkan antara polling gap, partial accounting, fee, atau state loss. Confidence Medium untuk symptom, Low untuk precise cause.
7. **SHORT payoff asymmetry**. WR >50% tetapi PF 0,526; sample n=24. Confidence Medium-Low.
8. **Meta +5 mungkin reward pola yang tidak generalize**. n=29, PF 0,367. Current meta state bukan historical state dan sample pendek. Confidence Medium-Low.

## Solusi Prioritas

### Quick Wins

#### 1. Perbaiki kontrak level scalper, bukan ubah angka stop secara acak

Perubahan:

- `main.py:_handle_signals`: jangan jalankan ATR-based `localize_for_user()` untuk mengubah SL/TP native scalper.
- Pertahankan level dari `ScoringEngine._build_scalper_signal()` atau buat satu authoritative `calculate_scalper_levels()` yang dipakai scanner dan executor.
- Localization hanya mengisi leverage/sizing/user fields untuk scalper.
- Simpan `planned_sl_pct`, `planned_tp1_pct`, `planned_tp2_pct`, dan `level_source`.

Alasan statistik:

- Planned TP1 median 1,20%; MFE winner median 0,452%; Time Exit median MFE 0,152%.
- Hanya 7,5% attributed enriched trade mencapai planned TP1.

Trade-off:

- TP lebih dekat meningkatkan TP hit/WR dan mengurangi Time Exit.
- Average winner dapat turun karena lebih banyak partial profit diambil cepat.
- Trailing runner tetap dibutuhkan untuk menjaga right tail.

Perkiraan dampak:

- WR: naik.
- PF: kemungkinan naik bila loss tail tidak berubah dan TP realization naik.
- Expectancy: arah positif, magnitude belum dapat dihitung tanpa event replay.
- Drawdown: kemungkinan turun karena realized profit lebih sering, tetapi stop-loss tail tetap ada.

Risiko:

- Target terlalu dekat dapat fee-churn dan memotong convex winners.
- Jangan deploy langsung penuh; A/B shadow diperlukan.

#### 2. Pisahkan score dari risk sizing selama calibration

Perubahan:

- Jangan menurunkan leverage secara global.
- Jalankan equal-risk shadow/control untuk semua score bucket.
- Gunakan score hanya untuk selection sampai isotonic/logistic calibration out-of-sample menunjukkan monotonic conditional EV.
- Setelah calibration, sizing gunakan predicted EV uncertainty-adjusted, bukan raw score threshold.

Alasan statistik:

- Enriched PF score 72+ 0,878 vs score 60-64 1,801.
- Proxy risk-normalized return juga memburuk saat score naik.

Trade-off:

- Jika high score kembali punya edge pada regime lain, equal-risk mengurangi upside.
- Mengurangi model-risk dan drawdown concentration sekarang.

Perkiraan dampak:

- WR: tidak berubah karena entry sama.
- PF: USD PF dapat membaik bila high-score loss tidak lagi overweight; trade-level return PF tetap sama.
- Expectancy USD: bisa turun bila future high-score edge nyata; current evidence mengarah naik.
- Drawdown: turun bila loss high-score tidak overweight.

#### 3. Ubah weak-location menjadi experiment gate, bukan hard permanent filter

Perubahan:

- Control: current weak handling.
- Treatment: require one additional orthogonal confirmation already computed, bukan threshold baru acak; contoh structure follow-through pada candle berikut atau retest confirmation.
- Jangan hard-disable sebelum treatment menang out-of-sample.

In-sample upper bound jika semua weak trade dihapus dari enriched cohort, tanpa mengklaim causal:

- Net +$22,81 menjadi +$37,28.
- PF sekitar 1,137 menjadi sekitar 1,296.
- WR 48,60% menjadi sekitar 50,40%.

Upper bound ini terkena selection/path dependency; bukan expected production uplift.

Trade-off:

- Frekuensi turun sekitar 22% enriched cohort.
- Confirmation delay dapat memperburuk entry price atau kehilangan impulse.

### Long-term Improvements

#### 4. Jadikan exit state-conditioned dan measurable

- Pertahankan `no_follow_through`; bukti sekarang positif.
- Jangan ubah `microstructure_invalid` sebelum future-path telemetry tersedia.
- Simpan price pada +5/+15/+30 menit setelah exit untuk menghitung avoided loss dan missed reversal.
- Pisahkan reason menjadi `time_exit_no_follow_through`, `time_exit_microstructure_invalid`, `time_exit_max_hold`, dan standard subtypes.

Expected effect belum dapat diestimasi jujur tanpa counterfactual. Risiko perubahan sekarang: mengubah loss -0,30% menjadi stop sekitar -0,80%.

#### 5. Rebuild score sebagai calibrated probability/EV model

- Target pertama: `P(MFE >= executable TP1 before MAE >= SL or max_hold)`.
- Target kedua: expected net R after fee/slippage.
- Gunakan purged walk-forward split berdasarkan waktu; embargo minimal max holding horizon.
- Calibrate probability dengan isotonic/logistic calibration hanya pada validation folds.
- Report Brier score, calibration slope/intercept, AUC, precision-recall, dan EV by decile.
- Jangan aktifkan model untuk sizing sampai minimum 1.000 enriched trades dan dua regime windows out-of-sample positif.

#### 6. Perbaiki meta learning

- Simpan pattern state saat entry, bukan join current EMA.
- Meta boost harus berdasarkan uncertainty-adjusted posterior/credible interval, bukan EMA point estimate setelah lima sample.
- Audit +5 bucket: current n=29, PF 0,367.
- Meta tidak boleh menaikkan risk sebelum OOS validation.

#### 7. Tambah full lifecycle telemetry

Wajib:

- `signal_id`, `strategy_source`, `fallback_from_standard`.
- `signal_score_raw`, `signal_score_final`.
- `entry_regime`, raw OI, volume, spread, orderbook, funding.
- planned entry/SL/TP dan level source.
- quote/trigger/fill price, latency, slippage bps, fee.
- leverage, account equity, initial risk USD, concurrent exposure.
- MAE, MFE, event timestamps, TP partial ledger.
- post-exit returns +5/+15/+30 menit.
- deployment/config/schema version.

## Rencana Implementasi

1. Freeze baseline definition: exact paper cohort only, enriched fields required, deployment version recorded.
2. Add telemetry fields through code-only persistence change; no historical backfill.
3. Refactor scalper level ownership so one function owns SL/TP. Add invariant test: executed scalper levels equal scanner-native levels unless explicit `level_source` says otherwise.
4. Add shadow alternative levels to every new trade. Do not change execution for first 200 trades.
5. Add equal-risk shadow ledger by score bucket.
6. Add weak-entry treatment arm using delayed structural confirmation.
7. Record post-exit counterfactual prices for every Time Exit.
8. Evaluate after minimum sample thresholds below.
9. Promote one change at a time. Keep rollback condition automatic.

## Eksperimen dan A/B Test

### Experiment A: Native level contract vs ATR-overwritten level

- Unit randomization: signal, stratified by side, volatility tercile, score bucket, and asset liquidity group.
- Control: current executed levels.
- Treatment: native scalper levels from `_build_scalper_signal()`.
- Shadow phase: 200 trades minimum per arm using same observed price path.
- Live paper phase: 300 trades per arm.
- Primary metric: net R/trade after simulated fee/slippage.
- Secondary: TP1 hit before Time Exit, PF, WR, MFE capture ratio, max drawdown.
- Success: treatment lower 95% CI expectancy > control mean or Bayesian P(treatment EV > control) >=95%, PF >=1,25, no >15% worse drawdown.
- Rollback: treatment PF <1 after 100 trades or drawdown >1,25x control.

### Experiment B: Equal-risk sizing vs raw-score conviction sizing

- Entry/exit identical; calculate parallel shadow PnL.
- Control: 2,5%/3,0%/3,5% tiers.
- Treatment: constant risk budget.
- Minimum 500 trades, at least 100 per score bucket.
- Primary: portfolio Sharpe-like mean/std R, max drawdown, expected shortfall 95%.
- WR unchanged by design.
- Promote conviction sizing only if calibrated score deciles show monotonic net R out-of-sample.

### Experiment C: Weak-entry confirmation

- Control: current weak gate.
- Treatment: next-candle/retest structural confirmation using existing features.
- Measure opportunity cost for rejected/delayed entries using same future path.
- Minimum 150 weak candidates per arm.
- Primary: net R per candidate, not per executed trade, agar lower frequency tidak memberi survivorship bias.
- Rollback: missed-winner cost melebihi avoided-loss benefit.

### Experiment D: Microstructure-invalid counterfactual

- Jangan mengubah execution dulu.
- Setelah each exit, record virtual hold to original SL, 12m, 18m, +30m.
- Minimum 100 `microstructure_invalid` events.
- Primary: avoided loss versus missed reversal in R.
- Current n=39 terlalu kecil dan tidak punya post-exit path.

### Experiment E: Profit-lock execution

- Record lock-arm timestamp, planned lock, trigger, observed quote, fill, fee, partial PnL.
- Compare software polling stop versus persisted/exchange protection shadow.
- Minimum 100 armed trades.
- Primary: winner-to-loser rate; current 16/321 overall enriched, 10 via profit lock.

### Experiment F: Score calibration

- Purged walk-forward, no random split.
- Minimum 1.000 enriched rows; at least 200 per major volatility regime.
- Primary: calibration error and net R by decile.
- Activation condition: monotonic decile EV in two consecutive OOS windows.

## Non-Recommendations

Belum layak dilakukan:

- Jangan memperlebar stop-loss. Stop cohort hampir tidak punya favourable excursion; data tidak menunjukkan stop terlalu sempit.
- Jangan mematikan Time Exit. `no_follow_through` menghasilkan +$82,98 dan PF 3,057.
- Jangan blacklist asset dari periode 50 jam. ARB hanya n=13; EIGEN n=9 insufficient.
- Jangan block jam 10 UTC dari n=25/3 hari.
- Jangan menaikkan confidence threshold. Score tinggi tidak lebih baik secara monoton.
- Jangan gunakan expected-edge ML untuk gating/sizing. 546/554 predictions berada 0,45-0,55; model hampir tidak discriminative.
- Jangan klaim leverage, MAE, exact R multiple, slippage live, atau pure scalper vs fallback sudah diaudit. Field tidak tersedia.

## Reproducibility

Script audit read-only:

- `tools/database_audit_inventory.py`
- `tools/database_audit_analysis.py`
- `tools/database_audit_robustness.py`

Semua script membuka SQLite dengan URI `mode=ro`, menjalankan aggregate anonim, dan tidak mencetak secret/chat ID/trade ID. Tidak ada database production yang disalin atau dimodifikasi.

Sumber mekanisme kode utama:

- `main.py:991-1101`
- `models/schemas.py:194-248`
- `engine/scoring_engine.py:1139-1186`
- `risk/risk_manager.py:266-402`
- `risk/risk_manager.py:689-1060`
- `intelligence/experience_buffer.py:77-153`
- `execution/paper_executor.py`
- `core/db.py`

## Final Assessment

Bot menunjukkan edge tipis pada full 554 trades, tetapi belum statistically robust dan memburuk pada enriched deployment cohort. Profit berasal terutama dari trailing/close-all; stop-loss tail dan target-horizon mismatch menekan PF. Prioritas benar bukan parameter tuning. Prioritas benar: satu authoritative scalper-level contract, calibration score sebelum sizing, experiment khusus weak entries, dan telemetry counterfactual exit.
