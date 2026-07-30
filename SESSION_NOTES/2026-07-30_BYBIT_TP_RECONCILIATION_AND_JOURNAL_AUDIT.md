# Bybit TP Reconciliation and Journal Audit

Date: 2026-07-30

## Scope

- Railway production database and logs audited read-only.
- Exact code path checked from native TP order, reconciliation, fill accounting,
  persistence, and Telegram journal.
- No Railway data changed or exported.

## Evidence

- `/data/kara_data.db`: `PRAGMA integrity_check = ok`.
- Period: 2026-07-26 12:09:33 UTC to 2026-07-30 06:35:16 UTC.
- Journal rows: 204; actual position lifecycles: 188.
- Net PnL: -$24.466045, exactly -Rp391,457 at configured Rp16,000/USD.
- Actual lifecycle WR: 58W / 130L = 30.85%, not journal's 74W / 130L = 36.3%.
- 29 partial-slice rows existed. Sixteen were extra rows from multi-slice lifecycle;
  thirteen were partial-only lifecycles with no final close row.
- TP was not wholly broken: 20 TP1 rows, 9 TP2 rows, and 6 trailing rows existed.
  Twenty of 188 positions reached TP1 (10.64%).
- Time Exit dominated: 164/188 positions, net -$31.326745, 36 winners, 128 losers.
- All 188 positions were LONG; 187 were native scalper. Deployment therefore has no
  SHORT cohort and cannot establish side robustness.
- Time Exit median hold was 12.053 minutes. Stored telemetry omitted
  `time_exit_trigger`, so `no_follow_through` versus `max_hold` cannot be audited.
- Time Exit gross price movement using persisted initial quantity was -$11.508730;
  stored fees were $16.411319. Twenty-one rows did not reconcile because persistence
  stores initial quantity, not actual `qty_closed`. This is a telemetry gap, not proof
  that venue PnL was calculated incorrectly.
- Railway logs repeatedly showed targets ending `cancelled without fill` followed by
  `native_tp_fill_unattributed` for reused symbols.

## Root Causes

### Native TP reconciliation

`BybitExecutor._reconcile_locked()` matched a venue position to the first local row
with equal symbol and side. Closed positions remain in `_position_symbols`, and the
match omitted `PositionStatus.OPEN`. Reusing a symbol could therefore sync venue size
and settle target IDs against a stale closed lifecycle instead of the current open
position. This explains cancelled-target attribution warnings and can prevent TP fill
lifecycle/accounting from reaching the correct position.

Fix: require matched local position status to be `OPEN`. Regression reproduces a
closed BTC lifecycle followed by a new BTC lifecycle and confirms TP1 settles only on
the current position.

### Journal semantics

`cmd_journal()` counted every persisted close slice as one closed trade. TP1/TP2 are
partial realized ledger events, not separate closed positions. This inflated closed
count and winner count, while asset and exit statistics mixed slices with lifecycles.

Fix: collapse rows by base `pos_id`, sum slice PnL once, and classify by final close
reason. Partial-only lifecycles are shown as unresolved and excluded from closed-position
statistics. Legacy rows without slice metadata remain compatible.

## Strategy Finding

Loss is real after correcting journal semantics. Main descriptive mechanism is failed
follow-through before TP1, not missing TP orders alone: only 10.64% of positions reached
TP1 and 164 Time Exits lost -$31.33. No causal Time Exit change is justified because
post-exit counterfactual and persisted trigger subtype are absent.

Do not widen SL, change TP distance, blacklist assets, or disable Time Exit from this
audit. Candidate-level MFE/MAE and post-exit prices are still required.

## Verification

- Focused Bybit/journal/client/persistence tests: `63 passed`.
- Full suite: `255 passed`.
- `py_compile` passed for changed Python files.
- `git diff --check` passed; only existing Windows line-ending warnings emitted.
- No deployment, restart, commit, or production write.

## Monitoring and Rollback

After deployment, monitor:

- `native_tp_fill_unattributed` count; expected sharp reduction for bot-owned fills.
- TP1 fill attribution and final lifecycle row join rate.
- partial-only unresolved lifecycle count; expected to return to zero after positions close.
- position-level WR/PF, Time Exit subtype, exact `qty_closed`, fee, and cumulative PnL.

Rollback if open venue positions become unmatched or recovered as unknown despite a
current open local position.
