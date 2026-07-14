# KARA Entry Pipeline Audit - 2026-07-15

## Executive Summary

Audit ini menilai pipeline entry, kualitas feature, false signal, dan kesiapan feature untuk keputusan deployment. Audit tidak mengubah strategy behavior, config, schema, database, atau production deployment.

Snapshot production read-only saat query final:

- `trade_history`: 700.
- `signals_history`: 714.
- `ml_experience`: 708.
- Exact trade-to-ML join: 700/700.
- Enriched cohort dengan observed MFE: 467.
- Periode closed trade: 2026-07-11 13:52:04 UTC sampai 2026-07-14 16:51:27 UTC, sekitar 75 jam.
- `PRAGMA integrity_check`: `ok` untuk main DB dan ML DB.
- Batas deployment hanya inferred dari first weak-confirmation event: 2026-07-13 19:05:21 UTC.

Hasil utama:

1. Entry accepted masih positif secara ekonomi, tetapi follow-through terbatas. Enriched n=467: MFE >=0,35% 32,76%, median MFE 0,1827%, net PnL +$40,32, mean +$0,0863, PF 1,170, WR 49,04%. Confidence: High untuk statistik snapshot; Low untuk generalisasi karena hanya sekitar 75 jam.
2. Score gagal berperilaku sebagai ranking probabilitas. Score 60-64 punya MFE hit 39,05%, PF 1,623, net +$24,67; score 72+ hanya 28,32%, PF 0,940, net -$4,06. Score sekarang mencampur direction strength, trade quality, failure risk, session, location, dan meta adjustment. Confidence: High untuk semantic mismatch dan non-monotonicity; Medium untuk dampak sizing; causal uplift belum dapat dihitung.
3. Volatility dan trend strength punya asosiasi lebih kuat daripada score. Low-vol tercile PF 1,766 dan net +$34,31, sedangkan high-vol PF 0,939 dan net -$6,79. Trend strength >=3% PF 0,710 dan net -$22,53. Ini association, bukan bukti untuk hard gate. Confidence: Medium.
4. Entry-location memberi kontradiksi penting. `weak` buruk secara ekonomi, n=73, PF 0,836, net -$6,67, tetapi follow-through MFE tidak buruk: hit 39,73%, lebih tinggi daripada `excellent` 27,80%. Karena itu `weak` tidak dapat disebut pure entry failure dan tidak boleh langsung diblok. Confidence: High untuk kontradiksi deskriptif; Low untuk mekanisme kausal.
5. Immediate-entry control weak justru positif. Candidate n=78, outcome n=77, seluruhnya LONG: mean return 18 menit +0,1203%, PF 1,571, positive rate 58,44%, MFE hit 46,75%, TP1 hit 40,26%, SL hit 12,99%. Confirmed n=8 dan rejected-chase n=4 ialah `insufficient sample`. Treatment confirmed belum dapat dibandingkan adil dengan control hanya dari PnL confirmed.
6. Feature observability ialah blocker terbesar. Raw numeric CVD, RSI, EMA, VWAP, ATR, volume, OI, dan support/resistance distance tidak dipersist. Accepted reason cohort hanya inferred n=48 dan banyak feature universal, sehingga ablation/control tidak tersedia.
7. ML association belum siap deployment. Complete-case n=463 menunjukkan mutual information untuk score 0,050 (`p=0,0149`), realized volatility 0,218 (`p=0,00498`), dan trend 0,130 (`p=0,00498`). Namun purged chronological logistic AUC turun dari 0,609, 0,630, 0,574 menjadi 0,493; model log loss lebih buruk daripada base rate pada folds 1, 3, dan 4, hanya marginal lebih baik pada fold 2. Association bukan causation; MI bukan deployment proof.
8. Audit timing entry terlalu awal/terlambat tidak mungkin dilakukan. Tidak ada pre-entry move, full fixed-horizon path, MAE semua trade, order/fill sequence, atau raw distance ke support/resistance. MAE baru tersedia pada weak shadow, bukan seluruh accepted trade.
9. SHORT entry-quality audit tidak mungkin dilakukan. Signal all-time punya SHORT n=24, tetapi current enriched MFE cohort seluruhnya LONG n=467. PnL SHORT lama tidak boleh dicampur dengan current enriched entry audit. Status: `data belum cukup`.

Kesimpulan: prioritas bukan menambah indicator atau threshold. Prioritas ialah telemetry candidate-level, pemisahan semantic score, data-contract repair, lalu shadow experiment purged out-of-sample. Tidak ada numeric causal impact estimate yang valid dari snapshot ini. Angka deletion cohort hanya upper bound historis, bukan expected uplift.

## Scope dan Data Quality

### Scope

- Unit utama: accepted/executed entry yang punya exact trade-to-ML join.
- Primary quality label: observed polling-based MFE >=0,35%, bukan guaranteed intrabar high/low.
- Secondary label: realized PnL ekonomi.
- Weak experiment unit: seluruh armed candidate, termasuk confirmed, expired, dan rejected.
- Production access: read-only; query dibatasi ke `SELECT`, `PRAGMA integrity_check`, dan `PRAGMA table_info` (`tools/database_entry_audit.py:1-5`, `tools/database_entry_audit.py:66-87`).
- Output aggregate-only; raw owner/chat/trade/event ID tidak dilaporkan.

### Completeness dan Join

| Dataset | n | Keterangan |
|---|---:|---|
| Closed trade | 700 | Snapshot final |
| Persisted signal | 714 | Accepted signal, bukan seluruh scanner candidate |
| ML row | 708 | Entry feature buffer |
| Exact trade-to-ML | 700 | 100% closed trade |
| Enriched observed MFE | 467 | 66,71% dari closed trade |
| Legacy/non-enriched | 233 | Tidak dipakai untuk current MFE feature audit |

Exact trade-to-ML coverage tinggi, tetapi exact trade-to-signal foreign key tidak tersedia dalam audit table. `signals_history` menyimpan `signal_id` (`core/db.py:510-527`), dan model position punya `signal_id` (`models/schemas.py:288-326`), tetapi durable closed-trade/ML audit contract belum memberi exact signal provenance. Reason/regime analysis karena itu memakai unique inferred attribution, bukan exact join.

### Periode dan Drift

- Closed period sekitar 75 jam dan hanya meliputi empat tanggal kalender.
- Boundary 2026-07-13 19:05:21 UTC berasal dari `MIN(weak_confirmation_events.armed_at)`, bukan explicit deployment version (`tools/database_entry_audit.py:756-762`, `tools/database_entry_audit.py:826-833`).
- Pre-boundary n=349: PF 1,127, net +$23,35, MFE hit 33,52%.
- Post-boundary n=118: PF 1,315, net +$16,97, MFE hit turun menjadi 30,51%.
- Perubahan mixed: ekonomi membaik, primary follow-through memburuk. Tidak ada dasar untuk causal deployment claim karena boundary inferred, market regime berubah, dan beberapa perubahan dapat masuk bersamaan.

### Batas Data

- Tidak ada rejected-candidate ledger untuk scanner umum.
- Tidak ada raw numeric CVD, RSI, EMA8/EMA21, VWAP, ATR, volume surge, OI, funding provenance, spread, atau orderbook depth snapshot.
- Tidak ada pre-entry move, signal age, support/resistance distance raw, exact signal-to-fill order events, atau fixed +1/+3/+5/+18 minute path untuk semua candidate.
- MFE ialah observed polling path, bukan candle intrabar maximum.
- Current accepted cohort seluruhnya scalper. `is_scalper` konstan; standard feature power tidak dapat diinferensikan.
- Current enriched cohort seluruhnya LONG. SHORT audit tidak tersedia.
- Minimum n=10 dipakai untuk statistik deskriptif; n<10 ditandai `insufficient sample`. Deployment recommendation memakai minimum lebih besar karena multiple comparison dan drift.

Residual risk: selection bias tetap besar. Database accepted signal tidak mengandung opportunity set scanner, sehingga false-positive rate unconditional dan false-negative rate tidak dapat dihitung.

## Entry Pipeline Overview

Current native scalper flow:

1. `_run_scalper()` mengambil mark price, 30 candle 1m, dan 15m MTF context (`engine/scoring_engine.py:446-489`).
2. `_calculate_scalper_score()` menghitung orderbook context, 1m momentum, EMA8/EMA21, RSI14, short-term CVD, volume surge, dan HH/HL structure (`engine/scoring_engine.py:807-1104`).
3. Direction dipilih dari bull versus bear points. Same-side 1m structure wajib; momentum berlawanan memblok signal (`engine/scoring_engine.py:1106-1150`).
4. Score dibentuk sebagai `direction_score + trade_quality_score - failure_risk_score` (`engine/scoring_engine.py:1152-1158`).
5. Session adjustment ditambahkan, threshold diterapkan, lalu meta adjustment ditambahkan dan threshold diterapkan lagi (`engine/scoring_engine.py:496-516`).
6. Signal dibangun memakai current authoritative scalper levels dan regime telemetry (`engine/scoring_engine.py:530-532`, `engine/scoring_engine.py:1340-1439`).
7. Entry-location gate memberi `invalid`, `weak`, `valid`, atau `excellent`; `weak` mendapat penalty dan current next-candle confirmation treatment (`engine/scoring_engine.py:534-699`).
8. Weak candidate menunggu next closed 1m candle, same-side structure, follow-through, no invalidation, dan no chase sampai TP1 (`engine/weak_confirmation.py:85-120`).
9. Accepted signal dipersist, lalu ML buffer menyimpan aggregate entry feature dan outcome saat close (`intelligence/experience_buffer.py:77-107`, `intelligence/experience_buffer.py:114-133`).

Semantic issue utama: satu final score digunakan untuk arah, kualitas, failure risk, session, meta, location bonus/penalty, threshold, dan downstream conviction. `ScoreBreakdown` sudah punya field terpisah (`models/schemas.py:130-153`), tetapi persisted ML contract tetap dominan aggregate score dan component standard yang mostly zero (`intelligence/feature_engine.py:5-31`).

Current production accepted signal seluruhnya scalper. Config juga memaksa execution ke scalper ketika `FORCE_SCALPER_ONLY` aktif, sementara standard scorer dapat hanya menjadi fallback source (`config.py:23-38`). Karena signal-source provenance tidak durable pada closed cohort, standard feature power dan pure-scalper versus fallback performance tidak dapat dipisahkan.

## Audit Setiap Feature

### 1. Final Score

| Score | n | MFE >=0,35% | PF | Net PnL |
|---|---:|---:|---:|---:|
| 60-64 | 105 | 39,05% | 1,623 | +$24,67 |
| 65-71 | 249 | 32,13% | 1,151 | +$19,71 |
| 72+ | 113 | 28,32% | 0,940 | -$4,06 |

Score non-monotonik dan arahnya berlawanan dengan asumsi confidence. MI score tetap terdeteksi 0,050 (`p=0,0149`), tetapi MI hanya mendeteksi dependence, bukan monotonic positive ranking. Permutation importance average score negatif dan tidak stabil. Confidence: High bahwa raw score bukan calibrated probability; Medium bahwa semantic conflation menjadi mekanisme utama.

### 2. Entry Location

| Location | n | MFE >=0,35% | PF | Net PnL |
|---|---:|---:|---:|---:|
| excellent | 223 | 27,80% | 1,227 | +$23,46 |
| valid | 163 | 36,81% | 1,246 | +$22,22 |
| weak | 73 | 39,73% | 0,836 | -$6,67 |
| weak_confirmed | 4 | `insufficient sample` | `insufficient sample` | `insufficient sample` |

Kontradiksi harus dipertahankan: `weak` buruk secara ekonomi tetapi follow-through tidak buruk. Kemungkinan mekanisme mencakup payoff/MAE, entry extension, fill, risk, atau exit interaction; data belum membedakan. `excellent` juga tidak terbaik pada MFE, sehingga ordinal encoding `invalid=0 ... excellent=3` belum terbukti linear (`intelligence/feature_engine.py:29`). MI location hanya 0,0307 dengan `p=0,0796`, tidak lolos threshold deskriptif 5%.

Code risk: `_validate_entry_location()` fail-open sebagai `valid` bila candle structure tidak cukup (`engine/scoring_engine.py:1189-1211`). Current scaler score sendiri membutuhkan 21 candle, sehingga branch mungkin jarang pada native path, tetapi fallback semantic tetap tidak aman dan tidak terukur. Raw `extension`, `distance_pct`, `room_risk`, location type, dan invalidation provenance tidak dipersist.

### 3. Realized Volatility

| Vol tercile | n | MFE >=0,35% | PF | Net PnL |
|---|---:|---:|---:|---:|
| low | 158 | 22,15% | 1,766 | +$34,31 |
| mid | 154 | 37,01% | 1,159 | +$12,80 |
| high | 155 | 39,35% | 0,939 | -$6,79 |

Follow-through naik bersama volatility, tetapi ekonomi turun. Ini menunjukkan MFE-hit saja tidak cukup: adverse path, payoff capture, spread, atau tail loss mungkin memburuk pada high vol. MI 0,218 (`p=0,00498`) ialah association terkuat. Purged permutation importance realized volatility positif pada 3/4 folds, tetapi model aggregate tetap gagal mengalahkan base rate secara konsisten. Confidence: Medium untuk regime interaction; Low untuk bentuk rule optimal.

### 4. Trend Strength dan Alignment

| Absolute trend | n | MFE >=0,35% | PF | Net PnL |
|---|---:|---:|---:|---:|
| <1% | 118 | 36,44% | 1,353 | +$20,61 |
| 1-3% | 212 | 32,08% | 1,417 | +$42,24 |
| >=3% | 137 | 30,66% | 0,710 | -$22,53 |

Trend strength >=3% negatif, tetapi trend-aligned versus counter-trend punya MFE dan PF serupa. Tidak ada proof untuk alignment gate. Trend MI 0,130 (`p=0,00498`), tetapi permutation importance positif hanya 1/4 folds. Kemungkinan trend strength menjadi proxy volatility, extension, atau consumed move. Raw pre-entry move tidak ada, sehingga mekanisme tidak teridentifikasi. Confidence: Medium untuk descriptive interaction; Low untuk intervention.

### 5. Session

Session bonus tidak punya MI/predictive evidence stabil. Session bucket `0` n=30 punya PF 0,342 dan net -$11,18, tetapi sample hanya tiga hari, banyak jam/session dibandingkan, dan deployment/regime confounding besar. `BLOCKED_HOURS_UTC` saat ini kosong (`config.py:468-475`). Tidak ada dasar memblok jam/session baru. Confidence: Low.

### 6. Meta Adjustment

- Meta `+5`: n=47, PF 0,445, net -$28,26, MFE hit 34,04%.
- Meta `-2`: n=77, PF 2,920, net +$34,00.

Hasil berlawanan dengan semantic adjustment, tetapi descriptive saja. Meta assignment bergantung historical cohort dan dapat confounded asset, regime, score, serta deployment. Meta MI dan purged importance null. Jangan membalik atau mematikan meta dari snapshot 75 jam. Confidence: Medium untuk semantic warning; Low untuk causal action.

### 7. Momentum, EMA, RSI, dan Structure

Inferred raw-reason cohort hanya n=48. CVD dan structure universal; RSI, EMA, dan momentum muncul pada 47/48. Tidak ada absent control yang memadai. Feature selection telah collapse: accepted signal hampir wajib membawa kumpulan feature sama karena same-side structure dan momentum gate (`engine/scoring_engine.py:1094-1150`).

RSI overbought continuation juga menjadi universal/redundan dengan bullish EMA dan momentum pada current LONG cohort (`engine/scoring_engine.py:1058-1069`). Akibatnya audit tidak dapat mengisolasi incremental power RSI versus EMA versus momentum versus structure. Raw numeric RSI, EMA spread/slope, candle momentum, dan structure distance tidak dipersist. Confidence: High bahwa feature-level attribution tidak tersedia; Low untuk menentukan feature mana harus dihapus.

### 8. CVD dan Order Flow

Scalper CVD mengklasifikasikan buy dari side `('B', 'buy', 'Ask')` dan sell dari `('S', 'sell', 'Bid')` (`engine/scoring_engine.py:1002-1011`). Standard orderbook analyzer memakai contract lain: `('buy', 'B', True)` sebagai buy (`engine/analyzers/orderbook_analyzer.py:196-210`). Perbedaan venue semantics antara aggressor side, resting side, `Ask`, dan `Bid` berisiko membalik klasifikasi. Tanpa raw trade-side counts dan normalized CVD snapshot, production effect tidak dapat diverifikasi.

CVD reason universal pada inferred n=48, sehingga tidak punya control. OI/orderbook aggregate ML fields mostly zero; raw orderbook imbalance/depth/spread juga tidak dipersist. Confidence: High untuk observability defect; Medium untuk code-contract risk; Low untuk realized impact.

### 9. Volume

Reason cohort inferred:

- Volume present n=11: PF 5,460, MFE hit 45,45%.
- Volume absent n=37: PF 1,016, MFE hit 24,32%.

Ini hipotesis, bukan proof. n=11 kecil, reason presence hanya muncul ketika surge threshold terlewati, dan sample dipilih dari accepted signal. Raw volume, surge ratio, exchange unit, dan absent-versus-missing status tidak ada. Volume surge mengikuti arah score yang sudah terbentuk (`engine/scoring_engine.py:1077-1092`), sehingga confounding kuat. Confidence: Low.

### 10. 15m MTF

Reason cohort inferred:

- MTF present n=37: PF 1,675.
- MTF absent n=11: PF 0,449.

Hipotesis saja. Current code memberi bonus bertingkat untuk alignment tetapi langsung menambahkan discord penalty ke `raw`, lalu final score dibangun ulang dari direction/quality/failure buckets (`engine/scoring_engine.py:1121-1158`). Jalur ini awkward: penalty memodifikasi `raw` yang tidak menjadi input final score, sementara `failure_risk_pts` juga ditambah. Hard-reject check memakai `raw`, final score memakai bucket lain. Raw MTF state/strength tidak dipersist. Confidence: Medium untuk semantic/code issue; Low untuk performance impact.

### 11. Orderbook, VWAP, OI, dan Funding

- Orderbook reason n=2: `insufficient sample`.
- OI/funding reason n=1: `insufficient sample`.
- VWAP reason n=1: `insufficient sample`.
- Funding hampir degenerate pada ML data; scalper signal mengisi funding `0.0` secara eksplisit (`engine/scoring_engine.py:1405-1438`).
- OI dan orderbook aggregate scores mostly zero. Current accepted cohort scalper, sehingga standard analyzer power tidak dapat diinferensikan.

Data-contract defects pada standard path:

- WS orderbook membentuk `vwap=mid` dan `vwap_deviation_pct=0`, sehingga VWAP feature mati pada jalur itu (`engine/scoring_engine.py:1487-1515`).
- `get_oi_data()` mendokumentasikan contracts, menghitung local `oi_usd`, tetapi mengembalikan contracts dan membuang computed `oi_change_24h_proxy` menjadi `0.0` (`data/hyperliquid_client.py:656-695`).
- Standard logging menyebut `oi_usd` sebelum konversi, sementara liquidation path baru mengalikan contracts dengan mark (`engine/scoring_engine.py:1476-1485`, `engine/scoring_engine.py:1580-1585`). Unit contract berisiko salah pada consumer lain.
- Missing funding/OI mengembalikan angka nol, bukan explicit missingness (`data/hyperliquid_client.py:643-674`). Nol lalu sulit dibedakan dari market value valid.
- REST orderbook menghitung depth-weighted book VWAP, bukan traded market VWAP (`data/hyperliquid_client.py:764-793`). Provenance tidak disimpan.
- Market scan config mendefinisikan liquidity/funding filters (`config.py:478-494`), tetapi evidence audit menyatakan configured market filters inactive pada current path. Jangan menganggap current accepted universe sudah lolos filter tersebut.

Fix data contract layak sebelum parameter optimization. Namun standard path tidak aktif sebagai identifiable current cohort, jadi execution priority lebih rendah daripada telemetry scalper. Confidence: High untuk code defects; Low untuk current PnL impact.

### 12. Scalper Indicator

`is_scalper` bernilai satu untuk seluruh accepted cohort. Feature variance nol; importance tidak dapat diestimasi. Standard feature power juga tidak dapat diinferensikan. Signal-source provenance perlu membedakan `native_scalper`, `standard_fallback_as_scalper`, dan future variants.

## Feature Importance Analysis

### Complete-Case Association

ML analysis memakai n=463 complete rows dan target observed MFE >=0,35%. Feature whitelist hanya entry-time fields; outcome, duration, exit, MFE/MAE, dan PnL dilarang sebagai input (`tools/database_entry_audit.py:31-63`, `tools/database_entry_audit.py:558-577`).

| Feature | Mutual information | Permutation p | Interpretasi |
|---|---:|---:|---|
| realized_vol | 0,218 | 0,00498 | Association terkuat; bukan causal rule |
| trend_pct | 0,130 | 0,00498 | Association kuat, stability buruk |
| score | 0,050 | 0,0149 | Dependence ada, tetapi ranking non-monotonik |
| location | 0,0307 | 0,0796 | Belum melewati 5%; ordinal encoding dipertanyakan |
| meta/session/micro-risk | null | tidak signifikan | Tidak ada evidence incremental stabil |

### Purged Chronological Validation

| Fold | ROC AUC | Log loss versus base |
|---|---:|---|
| 1 | 0,609 | Model lebih buruk |
| 2 | 0,630 | Model marginal lebih baik |
| 3 | 0,574 | Model lebih buruk |
| 4 | 0,493 | Model lebih buruk; ranking setara/di bawah acak |

Fold dibuat expanding chronological dan hanya memakai training trade yang sudah closed sebelum test boundary plus purge 30 detik (`tools/database_entry_audit.py:524-555`). Ini lebih relevan daripada random split, tetapi periode tetap pendek.

Permutation importance tidak stabil:

- Realized volatility positif pada 3/4 folds.
- Score average negatif.
- Trend positif hanya 1/4 folds.
- Feature lain tidak stabil atau null.

Kesimpulan: full-sample MI menemukan association, tetapi generalization chronological gagal. MI bukan deployment proof. Model tidak layak menjadi gate, sizing input, atau probability label. Confidence: High untuk keputusan tidak deploy ML; Medium untuk ranking association.

## Entry Quality Analysis

### Follow-Through dan Ekonomi

Enriched n=467:

- MFE >=0,35%: 32,76%.
- Median MFE: 0,1827%.
- Net PnL: +$40,32.
- Mean PnL: +$0,0863 per trade.
- PF: 1,170.
- WR: 49,04%.

Sekitar dua pertiga entry tidak mencapai observed MFE 0,35%. Namun cohort tetap positif karena MFE hit bukan satu-satunya source PnL dan exit/payoff berbeda. Jangan menyamakan `MFE <0,35%` dengan losing trade atau false signal secara otomatis.

### Early/Late Timing

Status: tidak dapat diaudit.

Data yang hilang:

- Move dari signal formation awal sampai planned signal price.
- Signal candle close dan signal age saat order dibuat.
- Trigger, order submit, ack, fill, dan slippage sequence.
- Full post-entry path dengan fixed sampling interval.
- MAE seluruh accepted trade.
- Raw support/resistance level dan distance saat decision.

MFE rendah tidak membedakan entry terlalu dini, entry terlalu lambat, thesis salah, atau polling yang melewatkan intrabar high. Weak shadow sekarang menyimpan candidate MAE dan 18-minute outcome (`engine/weak_confirmation.py:24-64`), tetapi hanya weak candidate. Confidence: High bahwa current timing audit impossible.

### Weak Immediate-Control

Weak candidate ledger:

- Candidate n=78; completed outcome n=77.
- Seluruh candidate LONG.
- Mean final return 18 menit +0,1203%.
- PF 1,571.
- Positive return 58,44%.
- MFE >=0,35% 46,75%.
- TP1 hit 40,26%.
- SL hit 12,99%.

By status:

- `confirmed`: n=8, mean +0,261%; `insufficient sample`.
- `expired`: n=63, mean -0,0129%, PF 0,948.
- `rejected_chase`: n=4, return besar positif; `insufficient sample` dan mechanically selected setelah move kuat.

Angka overall menjelaskan kontradiksi `weak`: immediate weak candidate dapat follow-through, tetapi accepted weak economic outcome tetap negatif. Confirmation treatment mungkin mengubah entry price, selected cohort, missed winners, dan risk path. Belum ada candidate-level paired treatment outcome yang cukup untuk menentukan benefit. Jangan memakai PF confirmed saja karena survivorship bias.

## Long vs Short Analysis

### LONG

- Current enriched entry-quality cohort: LONG n=467.
- Semua statistik score, location, volatility, trend, ML, dan weak shadow dalam laporan ini pada praktiknya merepresentasikan LONG current cohort.
- Generalisasi ke SHORT tidak valid.

### SHORT

- Persisted signal all-time: SHORT n=24.
- Current enriched MFE cohort: SHORT n=0.
- Weak candidate cohort: SHORT n=0.

Status: `data belum cukup`; SHORT entry-quality MFE audit impossible. PnL SHORT lama berasal dari cohort/behavior berbeda dan tidak boleh dicampur untuk menilai current entry feature. Minimum evidence berikutnya harus menyimpan exact feature contract dan fixed-horizon path pada SHORT candidate, termasuk rejected candidate. Confidence: High.

## Market Regime Analysis

### Volatility Regime

Low volatility menghasilkan ekonomi terbaik walau MFE hit terendah. High volatility menghasilkan MFE hit tertinggi tetapi PF di bawah satu. Ini konsisten dengan adverse excursion/tail-cost atau capture problem, bukan kurangnya favourable movement.

Hipotesis pengukuran:

- High-vol entry punya MAE dan stop-before-TP1 lebih tinggi.
- Spread/slippage dan polling miss membesar pada high vol.
- Fixed level/risk interaction tidak cocok dengan path distribution.
- Move sudah lebih consumed sebelum entry pada high vol.

Semua hipotesis memerlukan raw path dan execution timestamps. Tidak ada hard volatility gate recommendation.

### Trend Regime

Absolute trend >=3% merugi, tetapi alignment tidak membedakan hasil. Kemungkinan masalah bukan arah berlawanan, melainkan extension/late continuation setelah move besar. Tanpa pre-entry move dan distance ke structure, `late entry` belum terbukti.

### Session Regime

Session 0 n=30 terlihat buruk, tetapi tiga hari dan banyak comparison membuat false discovery risk tinggi. Tidak ada blocked-hour change.

### Deployment Cohort

| Cohort | n | MFE >=0,35% | PF | Net PnL |
|---|---:|---:|---:|---:|
| Pre inferred boundary | 349 | 33,52% | 1,127 | +$23,35 |
| Post inferred boundary | 118 | 30,51% | 1,315 | +$16,97 |

Hasil mixed. Jangan menyebut deployment menyebabkan perbaikan. Explicit `deployment_version`, `strategy_version`, dan `config_hash` belum tersedia.

## False Signal Analysis

### Definisi Operasional

Tidak ada satu label false signal yang cukup:

- Follow-through failure: observed MFE <0,35% dalam actual lifecycle.
- Economic false positive: final net PnL <=0.
- Structural false positive: thesis/invalidation gagal sebelum target.
- Candidate false positive: accepted candidate gagal pada fixed horizon, dibanding seluruh opportunity set.

Current database hanya mendukung dua definisi pertama secara parsial pada accepted trade. Structural path dan unconditional candidate false-positive rate belum tersedia.

### Observed Failure Patterns

1. High score: score 72+ punya follow-through dan ekonomi terburuk. Ini false-confidence pattern, bukan bukti threshold harus dinaikkan.
2. High volatility: banyak favourable move tetapi ekonomi negatif. Ini lebih dekat payoff/path problem daripada pure signal absence.
3. Strong trend >=3%: ekonomi negatif tanpa alignment separation. Hypothesis: extension atau regime-risk interaction.
4. Weak: ekonomi negatif tetapi MFE follow-through baik. Tidak boleh dilabel pure false entry.
5. Meta +5: ekonomi sangat negatif tetapi MFE hit tidak abnormal rendah. Hypothesis: sizing/payoff/confounding, bukan direction failure.
6. Feature stack universal: CVD/structure universal dan RSI/EMA/momentum 47/48 membuat feature contribution tidak identifiable.

### Current Deletion Upper Bounds

Historical net losses pada score 72+, weak, high vol, trend >=3%, session 0, dan meta +5 bukan additive dan cohort saling overlap. Menghapus bucket secara retrospektif juga mengabaikan rejected alternatives, changed exposure, ordering, dan regime drift. Karena itu:

- Angka loss bucket ialah current deletion upper bound sebelum overlap/counterfactual adjustment.
- Angka tersebut bukan expected uplift.
- Tidak ada numeric causal impact estimate yang valid.
- Measurement target boleh directional: calibration slope membaik, out-of-sample log loss mengalahkan base, candidate-level PF/mean return membaik, dan drawdown tidak memburuk.

## Root Cause Ranking

| Rank | Root cause | Evidence | Confidence | Impact causal |
|---:|---|---|---|---|
| 1 | Raw feature dan candidate observability tidak cukup | Tidak ada raw CVD/RSI/EMA/VWAP/ATR/volume/OI, rejected ledger, exact signal provenance, atau full path | High | Tidak dapat dihitung |
| 2 | Score semantic conflation dan calibration failure | Score bucket non-monotonik; direction/quality/failure/session/meta/location masuk satu angka | High | Tidak dapat dihitung; historical high-score loss bukan uplift estimate |
| 3 | Regime interaction tidak dimodelkan sebagai payoff/path problem | High vol MFE naik tetapi PF turun; trend >=3% negatif; alignment tidak membantu | Medium | Tidak dapat dihitung |
| 4 | Feature selection collapse/redundancy | CVD/structure universal; RSI/EMA/momentum 47/48; absent control hilang | High | Tidak dapat dihitung |
| 5 | Weak treatment belum punya paired treatment/control sample cukup | Immediate control positif; confirmed n=8; accepted weak ekonomi negatif tetapi MFE baik | High | Tidak dapat dihitung |
| 6 | Standard data-contract defects | OI units/missingness, discarded OI proxy, WS VWAP=mid, provenance hilang | High untuk code | Current impact Low/unknown karena cohort scalper |
| 7 | CVD side semantic inconsistency | Scalper dan standard memakai mapping buy/sell berbeda | Medium | Tidak dapat dihitung |
| 8 | 15m MTF penalty semantic awkward | `raw` dimodifikasi untuk hard reject, final score memakai bucket lain; no raw state persistence | Medium | Tidak dapat dihitung |
| 9 | Entry-location fail-open dan raw location fields hilang | Insufficient structure menjadi `valid`; location ordinal tidak stabil | Medium | Tidak dapat dihitung |
| 10 | Live telemetry/deployment version gap | Boundary inferred; order/fill and strategy version absent | High | Tidak dapat dihitung |

Root causes 1-5 langsung membatasi keputusan entry. Root causes 6-9 perlu diperbaiki sebelum standard feature optimization, tetapi priority eksekusinya lebih rendah karena current accepted cohort tidak mengidentifikasi standard path.

## Improvement Recommendation

### 1. Persist Exact Raw Feature Snapshot dan Candidate Ledger

Tambah point-in-time telemetry, tanpa mengubah decision:

- `candidate_id`, exact `signal_id`, `decision`, reject reason, asset, side, source scorer, and fallback provenance.
- `deployment_version`, `strategy_version`, `config_hash`, feature schema version.
- Raw RSI, EMA8, EMA21, EMA spread/slope, momentum returns, candle counts/timestamps.
- Raw CVD buy/sell volume, normalized side contract, sample size, age, source venue.
- Raw orderbook imbalance, depth, spread, VWAP value/type/source/age.
- Raw volume and surge ratio.
- Funding/OI value, units, source, age, and explicit missingness.
- Entry-location type, support/resistance level, extension, invalidation distance, room/risk.
- Planned signal price, observed price, order submit/ack/fill, actual fill, and signal age.
- Candidate future path at fixed +1/+3/+5/+12/+18 minutes, MFE, MAE, TP1/SL order, including rejected candidate.

Impact target: audit completeness mendekati 100% dan exact candidate-to-trade join 100%. Tidak ada numeric PnL uplift claim.

### 2. Rebuild Score Semantics

Pisahkan output menjadi:

- Direction evidence.
- Trade quality.
- Failure risk.
- Regime context.
- Calibrated probability/expected value, hanya setelah purged OOS validation.

Jangan memakai raw score untuk sizing. `ScoreBreakdown` sudah menyediakan sebagian field, tetapi persistence dan downstream use perlu dipisah. Impact target: monotonic calibration dan OOS log loss konsisten lebih baik daripada base. Current score-bucket deletion loss ialah upper bound, bukan expected uplift.

### 3. Fix Proven Data-Contract Defects Sebelum Parameter Optimization

- Standardize OI units: contracts dan USD disimpan terpisah.
- Jangan ubah missing funding/OI menjadi valid zero tanpa missing flag.
- Persist actual computed `oi_change_24h` proxy atau hapus proxy claim; jangan hitung lalu discard.
- Bedakan traded VWAP, orderbook depth-weighted price, dan `mid` fallback.
- Normalize aggressor-side semantics untuk CVD dan test dengan venue payload fixture.
- Persist source/provenance/age setiap market datum.

No behavior change dulu. Current standard power tidak dapat diinferensikan, jadi priority setelah scalper telemetry.

### 4. Continue Weak Treatment, Jangan Hard Block

Immediate control cukup baik dan confirmed n=8 belum cukup. Lanjutkan treatment plus paired candidate-level outcome. Ukur confirmed treatment versus immediate-entry control per seluruh armed candidate, bukan hanya confirmed trades.

### 5. Equal-Risk Shadow

Bandingkan current score-based risk dengan constant/equal-risk accounting shadow pada exact accepted trade. Tujuan: mengisolasi selection quality dari conviction sizing. Jangan mengubah actual risk sampai OOS evidence stabil.

### 6. Jangan Tambah Indicator atau Threshold

Current feature stack sudah redundant dan unobservable. Indicator baru menambah degrees of freedom tanpa control. Raw ablation dan candidate shadow lebih bernilai daripada parameter sweep.

## Prioritas Implementasi

| Prioritas | Pekerjaan | Behavior change | Acceptance gate |
|---:|---|---|---|
| P0 | Exact candidate/raw feature/deployment telemetry | Tidak | Exact joins, explicit missingness, fixed-horizon paths, no secret fields |
| P0 | Candidate ledger untuk accepted dan rejected signal | Tidak | Seluruh scanned candidate punya terminal decision dan future outcome bila eligible |
| P1 | Equal-risk shadow accounting | Tidak | Per-trade paired current versus equal-risk result |
| P1 | Weak paired treatment/control completion | Tidak | >=300 weak candidate dan >=50 confirmed, outcome completeness tinggi |
| P1 | Score semantic split dalam shadow output | Tidak | Direction/quality/failure/regime fields persisted terpisah |
| P2 | OI/funding/VWAP/CVD data-contract repair dan fixture tests | Sebaiknya tidak dulu | Unit/provenance/missingness verified; standard source identified |
| P2 | Score calibration research | Shadow only | >=1.000 enriched entries, dua non-overlapping time windows, purged OOS beats base |
| P3 | Regime-adaptive policy research | Shadow only | Stable OOS benefit, no concentration/drawdown regression |
| P4 | Strategy gate/sizing change | Ya, future | Hanya setelah pre-registered test passes |

Tidak ada deploy, restart, strategy modification, atau database mutation dalam audit ini.

## Non-Recommendations

- Jangan hard-block `weak`; follow-through weak tidak buruk dan immediate control positif.
- Jangan menaikkan minimum score; score lebih tinggi justru lebih buruk.
- Jangan memakai raw score untuk menaikkan sizing.
- Jangan memblok session/jam 0 dari tiga hari data.
- Jangan membuat high-vol atau trend-strength hard gate dari in-sample bucket.
- Jangan menyimpulkan trend-aligned lebih baik; MFE dan PF aligned/counter-trend serupa.
- Jangan membalik meta `+5/-2` langsung; association confounded.
- Jangan mengaktifkan ML untuk gate atau sizing; chronological log loss gagal 3/4 folds.
- Jangan blacklist asset atau side dari current snapshot.
- Jangan mencampur old SHORT PnL dengan current LONG-only enriched MFE audit.
- Jangan menambah RSI/EMA/CVD/VWAP/volume indicator atau threshold sebelum raw ablation tersedia.
- Jangan menganggap historical deletion loss sebagai expected uplift.
- Jangan mengklaim entry terlalu cepat/terlambat tanpa pre-entry path dan MAE lengkap.
- Jangan mengklaim post-boundary improvement disebabkan deployment; boundary inferred dan metric mixed.

## Eksperimen/A-B Test

### Prinsip Umum

- Unit randomization/evaluation: candidate, bukan executed trade saja.
- Semua arm menyimpan accepted dan rejected candidate serta fixed future path.
- Pre-register target, horizon, stop condition, dan analysis code.
- Gunakan chronological/purged out-of-sample validation, bukan random split.
- Minimum research gate: 1.000 enriched candidate/trade dan minimal dua non-overlapping market windows.
- Pisahkan LONG/SHORT, native-scalper/fallback source, volatility, dan deployment version bila n memadai.
- Report confidence interval, calibration, PF, mean candidate return, drawdown, turnover, and missed-winner/avoided-loser rates.
- Tidak ada numeric causal uplift target dari audit sekarang. Target hanya direction/range pengukuran yang ditetapkan sebelum melihat hasil.

### Experiment 1: Telemetry-Only Raw Feature Ablation

- Control: current decision dan execution.
- Shadow arms: recompute score/outcome tanpa satu feature family per arm: RSI, EMA, momentum, CVD, volume, MTF, location, session, meta.
- Behavior: tidak berubah.
- Primary: delta candidate-level MFE >=0,35% dan fixed 18m return OOS.
- Secondary: calibration, PF shadow, overlap, feature missingness.
- Minimum: 1.000 enriched candidates, dua windows; setiap feature state perlu >=100 observations. Feature dengan n<100 tetap hypothesis only.
- Decision gate: feature baru dianggap incremental bila sign benefit stabil pada kedua windows dan purged model beats base. Full-sample MI saja gagal gate.

### Experiment 2: Score Calibration Shadow

- Control: current raw score buckets.
- Shadow: calibrated probability/EV dari separated direction, quality, failure, dan regime fields.
- Behavior: tidak berubah; raw score tidak dipakai untuk new sizing.
- Primary: OOS log loss versus expanding base rate.
- Secondary: Brier score, calibration slope/intercept, monotonic bucket MFE/return.
- Minimum: 1.000 enriched observations dan dua windows.
- Pass target: model log loss lebih baik dari base pada kedua windows, bukan hanya average; AUC target measurement range >0,55 pada setiap window, bukan guaranteed uplift.
- Fail condition: fold AUC <=0,50 atau log loss lebih buruk dari base pada salah satu final validation window.

### Experiment 3: Weak Confirmation Paired Candidate Test

- Control: immediate entry shadow dari original signal price/levels.
- Treatment: current next-candle weak confirmation.
- Analysis population: seluruh armed weak candidate, bukan confirmed-only.
- Primary: mean return per candidate pada fixed 18m horizon dan drawdown/MAE.
- Secondary: PF, positive rate, TP1-before-SL, confirmation rate, avoided loser, missed winner, chase rejection cost.
- Minimum: 300 weak candidates dan 50 confirmed treatment trades.
- Current n=78 candidate dan n=8 confirmed belum cukup.
- Rollback/review trigger future: treatment candidate mean materially di bawah paired control pada both windows atau missed-winner cost melebihi avoided-loss benefit. Numeric threshold harus pre-registered setelah telemetry variance tersedia.

### Experiment 4: Equal-Risk Shadow

- Control: actual current sizing result.
- Shadow: constant risk per accepted trade dengan same entry/exit path.
- Behavior: tidak berubah.
- Primary: risk-normalized expectancy dan max drawdown.
- Secondary: contribution by score bucket, volatility, and trend strength.
- Minimum: 1.000 enriched trades/two windows.
- Pass target: equal-risk comparison menentukan apakah high-score sizing menambah atau mengurangi portfolio expectancy. Jangan menyebut proxy sebagai audited R bila fee, slippage, and exact initial risk belum lengkap.

### Experiment 5: Regime-Adaptive Shadow

- Control: current levels/selection.
- Shadow arms: predefined, low-complexity volatility/trend policies; no same-sample parameter sweep.
- Behavior: tidak berubah.
- Primary: fixed-horizon candidate EV and drawdown by volatility regime.
- Secondary: MFE/MAE ratio, TP1-before-SL, fill/slippage.
- Minimum: 1.000 enriched total dan >=200 per tested regime across two windows.
- Current evidence only supports testing, not high-vol blocking. Expected effect direction: reduce economic deterioration in high vol without deleting favourable movement; magnitude unknown.

### Experiment 6: Data-Contract Validation for Standard Features

- Fixture test normalized aggressor side for `B/S`, `buy/sell`, and venue `Bid/Ask` semantics.
- Compare WS `mid`, depth-weighted book price, and traded VWAP as separate fields.
- Verify OI contracts, mark price, OI USD, change horizon, and missingness.
- Run standard scorer shadow with explicit `strategy_source` only after contracts pass.
- Minimum performance analysis: >=1.000 identifiable standard-source candidates; current scalper-only accepted cohort tidak boleh dipakai.

## Residual Risk

- Market window sangat pendek; regime drift dapat membalik ranking.
- Candidate selection bias belum hilang sampai rejected ledger lengkap.
- Polling MFE dapat underestimate intrabar excursion.
- Cohort LONG-only membuat side generalization invalid.
- Multiple comparisons dapat menghasilkan bucket yang tampak kuat secara kebetulan.
- Feature repair dapat mengubah distributions; calibration lama tidak boleh dibawa otomatis.
- Strategy tetap berjalan dengan current behavior; audit ini tidak deployed dan tidak memberi rollback action.
