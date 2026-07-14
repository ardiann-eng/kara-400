# Session Note - 2026-07-15 Entry Pipeline Audit

## Scope

- Menulis audit entry pipeline production dalam `KARA_ENTRY_AUDIT_2026-07-15.md`.
- Menggunakan fixed final read-only snapshot dan code map repository.
- Original audit tidak mengubah strategy behavior, config, schema, database, test, atau runtime code; follow-up MTF gate dicatat di bawah.
- Tidak deploy, restart, commit, push, atau mutate production.

## Evidence Snapshot

- Production main DB dan ML DB diaudit read-only.
- `PRAGMA integrity_check = ok` untuk keduanya.
- Closed trade: 700.
- Persisted signal: 714.
- ML row: 708.
- Exact trade-to-ML join: 700/700.
- Enriched observed-MFE cohort: 467.
- Closed period: 2026-07-11 13:52:04 UTC sampai 2026-07-14 16:51:27 UTC, sekitar 75 jam.
- Inferred weak deployment boundary: 2026-07-13 19:05:21 UTC.
- Current enriched cohort seluruhnya LONG; all-time signal SHORT n=24 tidak dicampur ke current entry-quality audit.

## Main Findings

- Enriched n=467: MFE >=0,35% 32,76%, median MFE 0,1827%, PF 1,170, WR 49,04%, net +$40,32.
- Score non-monotonik: 60-64 PF 1,623 versus 72+ PF 0,940.
- Weak location negatif secara ekonomi tetapi MFE follow-through tidak buruk; hard block tidak didukung.
- Low volatility paling baik secara ekonomi; high volatility punya MFE lebih tinggi tetapi PF di bawah satu.
- Trend strength >=3% negatif; aligned versus counter-trend tidak memberi gate proof.
- Weak immediate-entry control n=78 candidate, n=77 outcome, mean 18m +0,1203%, PF 1,571; confirmed n=8 insufficient.
- Complete-case ML n=463 menunjukkan MI pada score, volatility, dan trend, tetapi purged chronological model gagal mengalahkan base log loss pada 3/4 folds.
- Raw feature observability, exact candidate ledger, and deployment provenance menjadi blocker utama.
- Early/late timing dan SHORT MFE quality tidak dapat diaudit. `data belum cukup`.

## Code Map Reviewed

- `engine/scoring_engine.py`: native scalper fetch/scoring, feature stack, semantic score, MTF, location gate, weak integration, and standard market-data path.
- `engine/weak_confirmation.py`: candidate treatment dan immediate-control MFE/MAE/18m outcome.
- `engine/analyzers/orderbook_analyzer.py`: standard VWAP/CVD contract.
- `engine/analyzers/oi_funding_analyzer.py`: funding/OI scoring.
- `data/hyperliquid_client.py`: funding/OI missingness, OI units/proxy, REST orderbook VWAP.
- `intelligence/feature_engine.py`: persisted model feature encoding.
- `intelligence/experience_buffer.py`: ML entry/outcome persistence contract.
- `models/schemas.py`: score breakdown, signal, and position provenance fields.
- `core/db.py`: signal persistence.
- `tools/database_entry_audit.py`: read-only aggregate audit, inferred signal, candidate analysis, MI, and purged walk-forward.
- `config.py`: forced scalper execution, inactive blocked hours, and market scan config.

## Files and Tools

Files added/updated:

- Added `KARA_ENTRY_AUDIT_2026-07-15.md`.
- Added `tools/database_entry_audit.py`.
- Added `tests/test_database_entry_audit.py`.
- Added `SESSION_NOTES/2026-07-15_ENTRY_PIPELINE_AUDIT.md`.
- Updated `SESSION_NOTES/README.md`.

Evidence tooling referenced:

- `tools/database_entry_audit.py`.
- `tests/test_database_entry_audit.py`.

Production audit queries were run read-only during this session. Report preserves fixed final aggregate snapshot; production database was not copied or changed.

## Recommendations Recorded

1. Add exact raw feature snapshot and candidate ledger, including rejected candidate and fixed +18m path.
2. Persist exact `signal_id`, strategy source, deployment/config version, and market-data provenance.
3. Separate direction, quality, failure, regime, and calibrated probability; do not use raw score for sizing.
4. Fix OI units/missingness, VWAP provenance, discarded OI proxy, and CVD side contracts before standard parameter optimization.
5. Continue weak treatment and paired immediate-control tracking; no hard block.
6. Add equal-risk shadow.
7. Do not add indicators, thresholds, blocked hours, or ML gating from current sample.

## Tests and Verification

- Focused analyzer regression tests: 8 passed via `python -m unittest tests.test_database_entry_audit -v`.
- `python -m py_compile tools/database_entry_audit.py tests/test_database_entry_audit.py`: passed.
- Required final verification: inspect final diff, check exact section names/evidence, and run `git diff --check`.
- Unrelated DB modification and tracked deletions were already present and were not modified or reverted.

## Deployment Status

- Not deployed.
- No Railway restart.
- No database write or export.
- Original audit tidak mengubah strategy/config/runtime behavior; follow-up MTF gate belum deployed.

## Next Measurements

- Minimum 1.000 enriched candidate/trade across two non-overlapping windows for feature ablation, calibration, equal-risk, and regime shadow.
- Weak treatment: minimum 300 candidate and 50 confirmed.
- Exact candidate-to-trade join and future-path completeness target: 100% eligible rows.
- Track MFE, MAE, TP1-before-SL, fixed 18m return, signal age, planned/observed/fill price, spread/slippage, and deployment version.
- Separate LONG/SHORT and native-scalper/standard-fallback source.

## Residual Risk

- Snapshot hanya sekitar 75 jam dan current enriched cohort LONG-only.
- Candidate selection bias dan polling-based MFE tetap ada.
- Historical deletion upper bounds bukan expected uplift.
- Tidak ada numeric causal impact estimate yang valid.

## Recommendation 1 Follow-up: Active Scalper MTF Conflict Gate

Implementation evidence:

- Active `ScoringEngine._calculate_scalper_score` sekarang hard reject `LONG` saat non-neutral 15m MTF `bear` dan `SHORT` saat non-neutral 15m MTF `bull`.
- Conflict mengembalikan score `0` dengan reason side-specific. Raw score tidak lagi dapat melewati gate.
- Conditional discord penalty lama (`mtf_score_penalty`, lalu reject hanya berdasarkan post-penalty raw score) dihapus dari active scalper scorer.
- Aligned 15m MTF tetap memakai floor dan bonus lama. Neutral MTF tidak mengubah score atau reasons.
- Standard scorer, threshold, sizing, dan exit tidak berubah.
- Runtime regression memakai 30 candle OHLCV realistis, orderbook cache, dan trade/CVD cache melalui fungsi active; mencakup high-score LONG conflict, high-score SHORT conflict, aligned bonus, dan neutral pass.

Verification evidence:

- `tests/test_scalper_mtf_conflict.py`: 4 passed via Python 3.14 interpreter with project dependencies.
- Runtime coverage executes active scorer with realistic OHLCV/orderbook/CVD inputs for high-score LONG conflict, high-score SHORT conflict, aligned MTF, and neutral MTF.
- `python -m py_compile engine/scoring_engine.py tests/test_scalper_mtf_conflict.py`: passed, no output.
- `git diff --check`: passed; hanya warning line-ending existing working copy.
- Workspace default venv tidak punya `pytest` atau `pydantic`; focused runtime memakai installed Python 3.14 interpreter. Neighboring files memakai pytest-style collection dan tidak dikumpulkan oleh stdlib `unittest` discovery.

Expected tradeoff:

- Mengurangi entry scalper counter-trend 15m secara deterministik, termasuk setup 1m dengan raw score tinggi.
- Dapat memblokir reversal 1m yang kemudian profitable. Current audit tidak memberi causal uplift estimate untuk gate ini; impact harus dinilai sebagai before/after policy cohort, bukan historical deletion claim.

Deployment status:

- Not deployed.
- No Railway restart, commit, push, config change, schema change, atau database mutation.

Monitoring and rollback:

- Track rejected candidate side, raw pre-gate score, 15m trend, fixed +18m return, MFE, MAE, dan TP1-before-SL untuk seluruh MTF-conflict rejection.
- Bandingkan aligned, neutral, dan rejected-conflict cohort per LONG/SHORT pada minimal dua non-overlapping windows; laporkan sample size dan period.
- Rollback ke score-dependent policy hanya jika shadow outcomes menunjukkan conflict cohort punya positive out-of-sample expectancy dengan sample memadai, atau gate menimbulkan material signal starvation. Rollback target terbatas pada conflict branch di `_calculate_scalper_score`; aligned/neutral behavior tidak ikut berubah.
