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

## 2026-07-15

- `2026-07-15_ENTRY_PIPELINE_AUDIT.md`
- Audit entry pipeline production pada 700 closed trade dan 467 enriched MFE outcomes.
- Feature observability, score calibration, weak candidate control, regime association, dan purged ML validation.
- Dokumentasi saja; tidak ada strategy behavior atau deployment change.

- `2026-07-15_BULL_EXHAUSTION_SHORT.md`
- Native scalper SHORT di atas +3% trend 24h memerlukan retest rejection MTF bear berbasis closed candle.

## 2026-07-19

- `2026-07-19_BYBIT_DEMO_DRILL_MODE.md`
- Bybit Demo Trading endpoint dan explicit drill mode setelah Testnet saldo tidak tersedia.

- `2026-07-19_PHASE10_RISK_AUDIT_FIXES.md`
- Audit ulang Phase 10; mainnet full-auto fail-closed dan kill-switch reset admin-only/persisten.

- `2026-07-19_PHASE16_MAINNET_READINESS_AUDIT.md`
- Audit whole-flow mainnet readiness; P0 Testnet evidence masih nol, mainnet tetap blocked.

- `2026-07-19_MAINNET_AUTO_AND_LIVE_SETUP.md`
- Mainnet full-auto acknowledgement kedua dan `/live` environment-aware credential flow.

- `2026-07-19_DEMO_CAPITAL_ONBOARDING_AND_PAPER_DEPRECATION_PLAN.md`
  - Plan per-user Demo capital, Telegram onboarding, top-100 Bybit universe, shared scalper profile, dan Paper deprecation bertahap.

- `2026-07-19_DEMO_CAPITAL_ONBOARDING_IMPLEMENTATION.md`
  - Environment eksplisit, allocation sizing, onboarding Demo/Mainnet, Demo virtual fund fail-closed, dan Paper retention audit.

- `2026-07-19_DEMO_PRIVATE_WS_IDLE_STALE_FIX.md`
  - Perbaikan false stale pada private WebSocket Bybit Demo saat akun idle; REST fallback tetap untuk transport putus.

- `2026-07-19_TELEGRAM_DEMO_ONBOARDING_UX.md`
  - Flow `/demo` dan `/live` lebih jelas, link resmi Demo API, recovery preflight HTTP 403, serta status environment/allocation.

- `2026-07-19_BYBIT_UNIVERSE_POLICY.md`
  - Allowlist BTC/ETH dihapus; eksekusi Bybit mengikuti candidate scanner yang punya metadata linear-USDT aktif, dengan guard execution lain tetap aktif.

- `2026-07-19_DEMO_EXACT_VIRTUAL_BALANCE.md`
  - Modal Demo menjadi saldo virtual target yang dipakai penuh; Mainnet tetap memakai allocation tanpa perubahan saldo nyata.

- `2026-07-19_BYBIT_CHART_LINKS.md`
  - Notifikasi posisi Bybit Demo/Mainnet mengarah ke chart Bybit dengan exact symbol registry, termasuk alias scanner.

- `2026-07-19_PNL_CARD_AND_TP_LIFECYCLE_MESSAGES.md`
  - Fix generator PnL Card crash dan status TP1/TP2 yang menjelaskan profit, sisa posisi, proteksi terkonfirmasi, serta langkah berikutnya.

- `2026-07-19_OPTION_B_TOTAL_RESET.md`
  - Otorisasi Opsi B, bukti audit sebelum hapus, reset one-shot via token, serta scope seluruh user/data/credential Telegram/Bybit.

- `2026-07-19_BYBIT_ENTRY_RECONCILIATION_RACE.md`
  - Incident LDO: race private WS/reconcile saat entry, limit orderLinkId emergency close, idempotensi native SL, dan guard lokal sebelum deploy.

## Reading Order

- Baca note scalper exit dan fallback untuk perubahan entry serta time exit.
- Baca note meta hierarchy untuk memory pattern dan dashboard meta.
- Baca note ML data pipeline untuk status model dan training data.
- Baca note pre-TP1 profit lock untuk trailing, TP1, dan TP2.
- Baca note Bybit live migration untuk arsitektur live baru dan status rollout testnet.
- Baca Bybit next steps sebelum melanjutkan implementasi atau testnet.
- Baca Bybit testnet reminder sebelum membuat order testnet nyata.
- Baca database audit, scalper levels, dan weak confirmation sebelum deploy atau mengubah strategy/risk berikutnya.
- Baca entry pipeline audit sebelum mengubah feature, score, entry gate, sizing, atau regime policy.
