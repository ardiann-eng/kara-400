# Notification Policy and Paper versus Bybit Comparison

Date: 2026-07-30

## Scope

- Remove Bybit chart button from `Position Executed` notification.
- Suppress automatic signal notification unless position opening succeeds.
- Compare historical Paper evidence with current Bybit Demo evidence without
  treating different periods or code versions as an A/B test.
- Railway audit remained read-only. No production data changed or exported.

## Notification Changes

- `send_position_opened()` now sends no reply keyboard. Other chart helpers and
  unrelated notification types remain unchanged.
- Automatic execution now calls `open_and_notify_auto_position()`.
- The helper opens first. A `None` result sends neither signal nor position-opened
  notification. A confirmed position sends signal followed by position-opened.
- Regression tests cover both failed and successful open results and the absent
  chart button.

## Data Identity

- Current local `data/kara_data.db` is 118,784 bytes, SHA-256
  `5bc6941574baad36dbde73b1bf95ef627cd029f6cc5991d5f02bc7053e25dfba`, integrity
  `ok`, and has zero `trade_history` rows.
- Railway `/data/kara_data.db` is 1,986,560 bytes, SHA-256
  `c210cbbab6699af1ad3adfdd7c76f21d2ead18a09f6089712e9fca53f4effbe4`.
- They are not the same database. Current local DB cannot support a Paper-profit
  claim.
- Git's old DB snapshot (`2bc435e:data/kara_data.db`) predates the current
  `trade_history` schema and also cannot reproduce the July Paper audit.
- Historical Paper evidence therefore comes from the fixed Railway audit recorded
  in `KARA_DATABASE_AUDIT_2026-07-13.md`, not the current local file.

## Descriptive Comparison

### Paper

- Period: 2026-07-11 13:52 UTC through 2026-07-13 16:20 UTC.
- 554 closed positions over 50.47 hours.
- Net +$64.95, approximately +Rp1,039,200 at Rp16,000/USD.
- WR 56.86%, PF 1.251, expectancy +$0.117/position.
- Confidence interval crossed zero, so stability was not proven.

### Bybit Demo

- Period: 2026-07-26 17:56 UTC through 2026-07-30 12:25 UTC.
- 188 actual position lifecycles represented by 204 close rows.
- Net -$24.466045, approximately -Rp391,457.
- Position-level WR 30.85%, PF 0.3102.
- Stored fees total $18.033663, approximately Rp288,539.
- Adding stored fees back leaves about -$6.432382, approximately -Rp102,918.
  Fees explain much of net loss but do not turn this Bybit sample profitable.
- Time Exit: 164 positions, net -$31.326745. Only 20/188 positions reached TP1.
- All current Bybit positions were LONG.

## Root Causes and Limits

1. **Paper accounting is optimistic relative to Bybit.** Paper simulates a 0.03%
   adverse spread at entry and exit but deducts no exchange trading fee. Bybit uses
   actual entry/exit fill fees. This is a proven mechanical difference.
2. **Current Bybit signals had weak follow-through.** Only 10.64% reached TP1 and
   Time Exit produced most losses. This is descriptive evidence, not proof that the
   Time Exit rule caused those losses.
3. **Samples are not comparable A/B cohorts.** Dates, market regimes, deployment
   versions, target rules, execution system, balance/allocation, and persisted fields
   differ. No causal claim that migration itself destroyed edge is valid.
4. **Known Bybit defects affected accounting/state.** Native TP matching could select
   a stale closed position for a reused symbol. This is fixed locally in the prior
   2026-07-30 work but not deployed. Its exact PnL impact cannot be reconstructed.

## Non-Recommendations

- Do not change TP, SL, Time Exit, score threshold, blocked assets, or hours based on
  this cross-period comparison.
- Do not call current local DB the profitable Paper source.
- Do not claim fees are the only cause; fee-free Bybit result remains negative.

## Next Valid Measurement

- For every accepted candidate, shadow the same entry and exits in Paper while Bybit
  executes it.
- Use identical signal ID, levels, quantities, and timestamps.
- Compare planned price, Paper fill, Bybit fill, fee, slippage, TP reach, and final
  PnL per candidate for at least 150 positions, preferably 300.
- Persist deployment version and exact Time Exit subtype.

## Deployment Status

- Not deployed, restarted, committed, or pushed.

## Verification

- Focused notification tests: `20 passed`.
- Full suite: `258 passed`.
- `py_compile` passed for changed Python files.
- `git diff --check` passed; only existing Windows line-ending warnings emitted.
