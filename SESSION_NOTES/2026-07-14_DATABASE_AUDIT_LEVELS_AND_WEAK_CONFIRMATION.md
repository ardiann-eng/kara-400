# Session Note - 2026-07-14 Database Audit, Scalper Levels, and Weak Confirmation

## Scope

- Audit end-to-end database Railway production berdasarkan `DATABASE_AUDIT_GUIDE.md`.
- Implementasi single ownership untuk level SL/TP scalper.
- Implementasi next-candle structural confirmation untuk entry-location `weak`.
- Tambah treatment telemetry dan immediate-entry shadow outcome untuk weak candidate.
- Tidak ada deploy atau restart Railway pada sesi ini.
- Tidak ada database production yang diubah atau diekspor selama audit.

## Audit Source

- Main DB: `/data/kara_data.db`.
- ML DB: `/data/kara_ml.db`.
- Kedua DB dibuka read-only dengan SQLite URI `mode=ro`.
- Kedua DB menghasilkan `PRAGMA integrity_check = ok`.
- Periode closed trade: 2026-07-11 13:52:04 UTC sampai 2026-07-13 16:20:10 UTC.
- Durasi sample: 50.47 jam.
- Closed trade: 554.
- Exact trade-to-ML join: 554/554.
- Enriched cohort: 321.
- Legacy cohort: 233.

Laporan lengkap:

- `KARA_DATABASE_AUDIT_2026-07-13.md`.

Extractor read-only:

- `tools/database_audit_inventory.py`.
- `tools/database_audit_analysis.py`.
- `tools/database_audit_robustness.py`.

## Temuan Audit Utama

### Overall

- Win rate: 56.86%.
- Profit factor: 1.251.
- Expectancy: +$0.117 per trade.
- Net PnL: +$64.95.
- Max drawdown: -$32.40.
- Bootstrap 95% CI expectancy: -$0.010 sampai +$0.249.
- Edge full sample belum statistically separated dari nol.

### Stop Loss

- Stop loss hanya 32/554 trade atau 5.78%.
- Stop loss bukan terlalu sering secara frekuensi.
- Net stop-loss: -$83.26.
- Profit factor stop-loss: 0.068.
- Mean: -$2.60.
- Median: -$2.69.
- Stop-loss cohort enriched punya median MFE hanya 0.095%.
- Data tidak mendukung memperlebar stop.

### Time Exit

- Time Exit: 396/554 atau 71.48%.
- Win rate: 50.51%.
- Net: -$10.11.
- Profit factor: 0.938.
- Time Exit tidak hampir selalu loss.
- `no_follow_through`: n=217, net +$82.98, PF 3.057.
- `microstructure_invalid`: n=39, 39 loss, net -$64.90.
- `microstructure_invalid` belum boleh diubah tanpa post-exit counterfactual karena rule aktif setelah trade sudah adverse.

### Entry Location

- `excellent`: n=149, PF 1.385, net +$26.17.
- `valid`: n=97, PF 1.241, net +$13.28.
- `weak`: n=71, PF 0.644, net -$14.47.
- Weak cohort cukup buruk untuk treatment experiment, tetapi belum cukup untuk permanent hard-disable.

### Score and Sizing

Enriched cohort:

- Score 60-64: n=73, PF 1.801, expectancy +$0.282.
- Score 65-71: n=180, PF 1.086, expectancy +$0.045.
- Score 72+: n=68, PF 0.878, expectancy -$0.085.

Raw score tidak monoton terhadap outcome, tetapi current sizing menaikkan risk dari 2.5% sampai 3.5%. Saran berikutnya ialah equal-risk shadow, bukan direct sizing change.

## Root Cause Level Scalper

Sebelum perubahan, dua jalur menghitung level scalper:

1. `ScoringEngine._build_scalper_signal()` membuat native level.
2. `TradeSignal.localize_for_user()` menimpa native level memakai ATR 1m.

Observed production attribution:

- Planned SL median: 0.80%.
- Planned TP1 median: 1.20%.
- Planned TP2 median: 2.00%.
- Winner MFE median: 0.452%.
- Winner MFE P75: 0.667%.
- Winner MFE P90: 1.126%.
- Time Exit MFE median: 0.152%.
- Hanya 7.5% attributed enriched trade mencapai planned TP1.

Countercheck formula volatility-native lama:

- Production realized-vol sample: n=560.
- Formula lama akan menghasilkan SL 1.50% pada 97.68% signal.
- Data stop-loss tidak mendukung widening dari 0.80% ke 1.50%.

## Scalper Level Change

Authoritative ladder sekarang:

- Initial SL: 0.80%.
- TP1: 0.45%.
- TP2: 0.75%.
- Trailing tetap menangkap move setelah TP ladder.

Rationale:

- TP1 0.45% sejajar dengan winner MFE median 0.452%.
- TP2 0.75% berada sedikit di atas winner P75 0.667%, tetapi jauh di bawah old median target 2.00%.
- Initial stop tidak diperlebar karena stop-loss tail sudah merusak dan stop cohort hampir tidak punya favourable excursion.

Implementation:

- `engine/scalper_levels.py` menjadi pure side-aware level calculator.
- `engine/scoring_engine.py` menjadi owner level scalper.
- `TradeSignal.localize_for_user("scalper")` hanya mengatur mode dan leverage.
- `main.py` tidak lagi fetch ATR 1m untuk scalper localization.
- Standard ATR/regime level path tetap tidak berubah.

Expected behavior:

- TP1 hit rate naik.
- Time Exit share turun.
- Win rate berpotensi naik.
- Average winner dapat turun.
- Trailing harus mempertahankan right tail.

## Weak Confirmation Treatment

Weak signal tidak hard-disabled. Treatment flow:

1. Weak candidate dengan pre-penalty score yang memenuhi existing weak threshold menerima existing penalty.
2. Candidate disimpan dan tidak langsung menjadi accepted signal.
3. Bot menunggu satu candle 1m baru yang sudah closed.
4. Side harus tetap sama.
5. Struktur harus tetap bull untuk LONG atau bear untuk SHORT.
6. Candle close harus follow-through dari original signal price.
7. Micro invalidation belum boleh pecah.
8. Harga belum boleh mencapai original TP1 agar bot tidak chase move terlambat.
9. Candidate timeout setelah 150 detik.

Decision status:

- `armed`.
- `confirmed`.
- `expired`.
- `rejected_side_flip`.
- `rejected_structure`.
- `rejected_invalidation`.
- `rejected_chase`.

Confirmed trade:

- Disimpan sebagai `entry_location_quality=weak_confirmed`.
- Tetap memakai penalized score weak awal.
- Scan kedua tidak boleh menaikkan conviction/risk secara diam-diam.
- ML encoding `weak_confirmed` sama dengan `weak`, yaitu 1.0, sampai calibration baru membuktikan pemisahan feature.
- Cooldown accepted signal baru dimulai setelah confirmation sukses.

## Weak Shadow Control

Setiap armed weak candidate tetap diamati virtual selama 18 menit, termasuk candidate yang rejected atau expired.

Shadow plan memakai original immediate-entry contract:

- Original signal price.
- Original executed SL.
- Original TP1.
- Original TP2.

Outcome telemetry:

- MFE.
- MAE.
- Return setelah 18 menit.
- TP1 hit.
- TP2 hit.
- SL hit.

Tujuan:

- Membandingkan immediate-entry control versus confirmed treatment per seluruh candidate.
- Mengukur avoided losers.
- Mengukur missed winners.
- Menghindari survivorship bias bila hanya confirmed trade yang dianalisis.

## New Database Tables

`weak_confirmation_events`:

- `event_id`.
- `asset`.
- `side`.
- `status`.
- `signal_price`.
- `observed_price`.
- `score`.
- `armed_at`.
- `decided_at`.

`weak_confirmation_outcomes`:

- `event_id`.
- `asset`.
- `side`.
- `signal_price`.
- `observed_price`.
- `mfe_pct`.
- `mae_pct`.
- `final_return_pct`.
- `tp1_hit`.
- `tp2_hit`.
- `sl_hit`.
- `completed_at`.

Schema changes additive dan idempotent. Migration menangani tabel outcome lama yang belum punya `tp2_hit`. Hard reset inventory juga mencakup kedua tabel baru.

## Files Changed for This Work

- `engine/scalper_levels.py`.
- `engine/weak_confirmation.py`.
- `engine/scoring_engine.py`.
- `models/schemas.py`.
- `main.py`.
- `config.py`.
- `core/db.py`.
- `intelligence/feature_engine.py`.
- `intelligence/intelligence_model.py`.
- `tests/test_signal_level_ownership.py`.
- `tests/test_weak_confirmation.py`.
- `KARA_DATABASE_AUDIT_2026-07-13.md`.
- `tools/database_audit_inventory.py`.
- `tools/database_audit_analysis.py`.
- `tools/database_audit_robustness.py`.

Workspace juga memiliki perubahan Bybit dan perubahan lain dari sesi/user lain. Jangan revert atau menganggap semua diff pada file shared berasal dari pekerjaan audit ini.

## Verification

- Focused regression tests: 23 passed.
- Test mencakup weak confirmation LONG/SHORT, next closed candle, side flip, structure rejection, invalidation, chase, timeout, MFE/MAE shadow, level ownership, scalper exit state, execution contracts, dan meta hierarchy.
- `py_compile` passed untuk file yang diubah.
- `git diff --check` passed.
- Scorer AST/source integration contract passed.
- Full scorer runtime import pada interpreter test alternatif terblokir karena environment itu tidak punya `numpy`.
- Weekly-review test collection terblokir karena environment itu tidak punya `pandas`.
- Blocker dependency tersebut bukan assertion failure dari perubahan ini.

## Not Deployed

- Bot belum direstart.
- Railway belum dideploy.
- New tables belum dibuat di production sampai startup code baru dijalankan.
- Belum ada post-change production outcome.

## Monitoring After Deploy

### Level Change

Pantau hanya trade dengan deployment/config version baru setelah versioning tersedia:

- TP1 hit rate.
- TP2 hit rate.
- Time Exit share.
- Trailing-stop share.
- Average winner.
- Average loser.
- Profit factor.
- MFE capture ratio.
- Max drawdown.

Minimum review:

- 150 trade untuk safety checkpoint.
- 300 trade untuk initial comparison.
- Pisahkan side dan volatility tercile bila sample cukup.

Rollback candidate:

- PF di bawah 1 setelah 150 comparable trade.
- Drawdown lebih dari 1.25x comparable baseline.
- Average winner turun lebih cepat daripada loss reduction.

### Weak Confirmation

Minimum review:

- 150 weak candidate untuk initial signal.
- Prefer 300 weak candidate.
- Minimal 50 confirmed trade.

Metrik utama per candidate:

- Confirmation rate.
- Treatment return per candidate.
- Immediate-entry shadow return per candidate.
- Avoided loser rate.
- Missed winner rate.
- TP1-before-SL rate.
- MFE dan MAE treatment versus shadow.
- Drawdown treatment versus shadow.

Jangan mengevaluasi treatment hanya dari PF confirmed trade karena itu survivorship bias.

## Next Priority

1. Tambah `deployment_version`, `strategy_version`, `config_hash`, dan `level_source`.
2. Persist exact `signal_id`, `strategy_source`, dan fallback provenance ke closed trade/ML.
3. Tambah full exit event ledger untuk TP1, TP2, lock, trigger, fill, fee, dan slippage.
4. Tambah post-exit +5/+15/+30 menit untuk `microstructure_invalid`.
5. Tambah equal-risk shadow sebelum mengubah conviction sizing.
6. Audit ulang setelah minimum sample baru tercapai.

## Profit Lock Bug Follow-up

Kasus production LIT mengonfirmasi bug execution/reporting:

- Dua LIT LONG berakhir `profit_lock_stop` pada actual exit 2.3795.
- Entry: 2.38062469 dan 2.3804948.
- Final ROE: -0.94% dan -1.04% pada 25x.
- MFE sekitar +0.95%.
- `tp1_hit=false`, jadi ini early pre-TP1 lock, bukan TP1 partial lock.

Root cause:

- Risk manager mengklasifikasikan lock dari planned stop yang sudah di atas entry.
- Paper executor sebelumnya menghitung fill/PnL memakai current polling price setelah harga melewati stop.
- Telegram menampilkan planned lock tetapi percentage dan PnL memakai actual late poll price.
- Telegram juga salah mengklaim `TP1 locked, remainder exited above entry`.
- Early lock mutation sebelumnya tidak langsung dipersist pada paper dan tidak langsung mengubah exchange protection pada Bybit.

Fix:

- Risk action membawa `trigger_price`.
- Paper profit-lock fill dimodelkan dari trigger plus simulated closing spread, bukan late polling price.
- Trade/action menyimpan actual `exit_price` dan `trigger_price` terpisah.
- Telegram menampilkan actual fill dan trigger lock secara terpisah.
- Wording membedakan early pre-TP1 lock dari TP1 partial lock.
- Jika actual cumulative PnL negatif karena gap/slippage/fee, UI menyatakannya eksplisit.
- Paper lock state dipersist tepat saat arm.
- Bybit protection diperbarui tepat saat arm dan dinormalisasi ke tick size.
- PnL card memakai loss color bila cumulative PnL negatif walau reason `profit_lock_stop`.

Verification:

- Profit-lock, scalper exit, execution contract, dan Bybit executor focused suite: 33 passed.
- Pure regression mereproduksi LONG polling di bawah entry tetapi trigger/fill lock tetap di atas entry pada paper.
- Live tetap tidak dapat menjamin net profit bila market gap atau fee melebihi lock buffer; UI sekarang jujur terhadap actual fill dan cumulative net PnL.

### Final Contract Decision

Setelah review user, pre-TP1 early profit lock dihapus sepenuhnya.

Final behavior:

- Jika TP1 belum hit, bot tidak memindahkan stop berdasarkan MFE saja.
- Position tetap dikelola original SL, TP1, dan applicable Time Exit.
- `profit_lock_stop` hanya dipakai setelah `tp1_hit=true` dan partial TP1 sudah direalisasikan.
- Paper TP1 memindahkan remainder stop ke entry +0.05%.
- Bybit TP1 memindahkan remainder stop ke normalized breakeven.
- Exact breakeven stop setelah TP1 tetap diklasifikasikan `profit_lock_stop`.
- Telegram tidak punya lagi wording `early pre-TP1 lock armed`.
- Config `early_trail_arm_pct`, `early_trail_pct`, `short_early_trail_*`, dan `long_early_trail_*` dihapus.
- `Position.early_profit_lock` tetap ada sebagai legacy persisted field untuk compatibility data lama, tetapi tidak di-arm oleh current state machine.

Alasan:

- Label profit lock harus punya satu arti: TP1 sudah bank profit, remainder dilindungi.
- Historical LIT loss berasal dari pre-TP1 lock dan late polling fill, bukan post-TP1 remainder.
- Menghapus pre-TP1 branch menghilangkan ambiguity serta state/protection path tambahan.

Verification final contract:

- Pre-TP1 MFE +0.40% tidak mengubah stop.
- TP1 tetap mengambil partial.
- Post-TP1 exact breakeven stop diklasifikasikan sebagai `profit_lock_stop`.
- Focused paper/live/exit suite: 33 passed.

## Do Not Change Yet

- Jangan memperlebar SL.
- Jangan mematikan seluruh Time Exit.
- Jangan mengubah `microstructure_invalid` tanpa counterfactual.
- Jangan blacklist asset atau jam dari sample 50 jam.
- Jangan menaikkan score threshold.
- Jangan aktifkan ML untuk gating atau sizing.
- Jangan menambah perubahan strategi lain sebelum level dan weak treatment punya post-deploy sample cukup.

## Paper/Live Mode Switch Safety

Intent dikonfirmasi:

- `/paper` saat sudah Paper memang reset sesi simulasi, saldo, posisi, paper state, dan risk state.
- Behavior reset tersebut bukan defect dan tetap dipertahankan.

Safety fixes:

- `/live` sekarang diblok bila posisi Paper masih terbuka.
- `/live` saat sudah Live diblok bila posisi Bybit masih terbuka, sehingga credential tidak dapat diganti dan meninggalkan account lama tanpa monitoring.
- Guard posisi dijalankan saat command dimulai dan diulang saat callback activation untuk menutup race selama credential setup.
- Untuk user Live, guard selalu force-reconcile exchange lebih dulu.
- `/paper` dari Live force-reconcile sebelum membaca `open_positions`.
- Reconciliation failure membatalkan perpindahan secara fail-closed dan Live monitoring tetap aktif.
- Callback `Tutup semua lalu Paper` force-reconcile ulang sebelum close-all.
- Jika close-all gagal atau exchange/local position belum kosong, mode tetap Live.
- Duplicate `FERNET_KEY` assignment dihapus. Satu source sekarang tetap mendukung `HL_FERNET_KEY` dengan fallback `FERNET_KEY`.

Verification:

- Focused Telegram, UserSession, Bybit executor, startup validation, dan credential tests: 48 passed.
- Regression coverage mencakup open Paper position block, open Live position credential-rotation block, reconcile failure block, live close-all failure, activation rollback, dan single Fernet assignment.
