# Session Notes Index

## 2026-07-12

- `2026-07-12_SCALPER_EXIT_AND_FALLBACK.md`
- Audit database Railway.
- Perbaikan grace period scalper dan time exit berbasis state market.
- Revalidation native 1m untuk standard fallback.

- `2026-07-12_META_HIERARCHY.md`
- Audit delta meta pattern.
- Meta hierarchy specific, asset-side, side-bucket, dan side.
- Delta evidence-based dan update dashboard Meta Pattern.

- `2026-07-12_ML_DATA_PIPELINE.md`
- Audit model ML yang masih netral karena data belum cukup.
- Feature entry dan label exit baru untuk follow-through scalper.
- Progress dashboard memakai enriched outcome.

- `2026-07-12_PRE_TP1_PROFIT_LOCK.md`
- Audit trailing sebelum TP1.
- Early trail full close diganti menjadi profit lock.
- Telemetry TP1, TP2, MFE, dan label profit lock.

- `2026-07-12_BYBIT_LIVE_MIGRATION_PHASES_1_9.md`
- Migrasi live execution dari Hyperliquid ke Bybit.
- Kontrak, config fail-closed, bridge harga, REST, executor, persistence, `/live`, dan private WS.
- Safety invariants, focused tests, batas testnet, serta tambahan Phase 11-12.

- `2026-07-12_BYBIT_NEXT_STEPS.md`
- Handoff kerja berikutnya dari Phase 13 sampai Phase 17/mainnet rollout.
- Acceptance criteria, larangan arsitektur, testnet drills, utang Phase 10, dan definisi selesai.

## 2026-07-13

- `2026-07-13_BYBIT_TESTNET_TEST_REMINDER.md`
- Reminder wajib untuk Phase 14C real Bybit testnet drills.
- Checklist akun, command CLI, evidence gate, stop conditions, dan larangan mainnet.

## 2026-07-14

- `2026-07-14_DATABASE_AUDIT_LEVELS_AND_WEAK_CONFIRMATION.md`
- Audit database Railway production pada 554 closed trade.
- Single ownership level scalper berbasis MFE dan horizon 12-18 menit.
- Next-candle structural confirmation dan shadow-control telemetry untuk weak entry.

## Reading Order

- Baca note scalper exit dan fallback untuk perubahan entry serta time exit.
- Baca note meta hierarchy untuk memory pattern dan dashboard meta.
- Baca note ML data pipeline untuk status model dan training data.
- Baca note pre-TP1 profit lock untuk trailing, TP1, dan TP2.
- Baca note Bybit live migration untuk arsitektur live baru dan status rollout testnet.
- Baca Bybit next steps sebelum melanjutkan implementasi atau testnet.
- Baca Bybit testnet reminder sebelum membuat order testnet nyata.
- Baca database audit, scalper levels, dan weak confirmation sebelum deploy atau mengubah strategy/risk berikutnya.
