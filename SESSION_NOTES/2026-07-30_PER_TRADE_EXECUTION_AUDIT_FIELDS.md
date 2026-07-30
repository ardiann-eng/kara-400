# Per-Trade Bybit Execution Audit Fields

Date: 2026-07-30

## Scope

- Add exact execution evidence to every successfully opened Bybit position and
  every locally observed close.
- Reuse existing signal context through exact `signal_id`; do not duplicate RSI or
  orderbook research fields into every close row.
- Add a read-only audit join tool.
- No strategy, threshold, sizing, TP, SL, or Time Exit decision changed.
- No Railway deployment, restart, database write, export, commit, or push.

## Existing Evidence Reused

Railway read-only inventory before change:

- `signals_history`: 479 rows.
- `trade_history`: 206 close rows / 190 position lifecycles.
- 205/206 close rows and 189/190 positions exact-join to a signal by `signal_id`.
- RSI is parseable from existing signal reasons for 479/479 signals.
- Orderbook score exists on 479/479 signals but is zero throughout this sample.
- Numeric orderbook imbalance is present in some signal reason strings; absence is
  retained as `null`, never converted to zero.
- Existing successful close rows had actual fill, fee, and initial quantity, but no
  exact close quantity, entry spread, exit spread, trigger, Time Exit subtype, MFE,
  or MAE.

## Changes

### Position entry facts

Persisted on `Position` JSON:

- signal price;
- initial SL/TP1/TP2;
- Bybit mark, best bid, best ask, and spread at accepted entry quote;
- estimated fill and estimated slippage;
- actual entry fill gap versus mark;
- entry order fill latency and signal age;
- quote timestamp;
- KARA version and deployment commit when available.

The existing Bybit live quote call supplies these values. No extra entry network call
was added.

### Close facts

Persisted in each `trade_history.data` close row:

- exact `qty_closed`;
- entry-fee allocation and actual exit fee separately;
- trigger price and observed exit price;
- Time Exit subtype;
- post-fill mark/bid/ask/spread quote and timestamp;
- post-fill quote label `exit_quote_timing=post_fill`;
- actual fill difference versus the post-fill estimated quote, semantically named
  `exit_fill_vs_post_quote_pct` rather than claiming pre-fill slippage;
- duration, MFE, MAE, initial levels, current protection stop, and deployment fields.

Exit order always runs before the optional quote request. Audit collection therefore
cannot delay a reduce-only close. Quote failure logs a warning and leaves fields null.

Native TP settlement records exact target trigger and fill. Exchange-vanished closes
continue to use Bybit closed-PnL truth and retain unknown fee/quote fields rather than
inventing values.

### MFE and MAE

- Existing `trailing_high` remains the best observed price.
- New `adverse_price` tracks the worst observed polling price.
- Both update inside the existing position check and do not affect decisions.
- MAE is polling-observed, not tick-perfect exchange path data.

### Audit tool

`tools/trade_execution_audit.py` opens SQLite read-only, joins signals and close rows
by exact `signal_id`, collapses partial exits into one position, extracts existing RSI
and orderbook imbalance, and emits JSON rows.

Example:

```powershell
python tools/trade_execution_audit.py --db "D:/path/to/kara_data.db"
```

## Compatibility

- No SQL table migration required; additive fields live in existing JSON blobs.
- Pydantic defaults make old persisted positions readable.
- Old trades remain auditable with new fields null.
- No historical backfill is performed.

## Verification

- Focused Bybit/persistence/audit/exit tests: `59 passed`.
- Full suite: `263 passed`.
- `py_compile` passed for changed Python and test files.
- `git diff --check` passed; only existing Windows line-ending warnings emitted.
- Local audit tool returned `[]` because current local DB has zero trades, matching
  the prior identity audit.

## Next Measurement

- Deploy only with operator authorization.
- Review completeness after first 20 newly closed positions.
- Initial economic review after 150 positions; prefer 300.
- Required completeness targets for new positions: entry quote 100% when live risk
  gate is active, exact close quantity/fee 100% for local/native-target fills, exact
  signal join 100%, Time Exit subtype 100% for Time Exit rows.

## Residual Risks

- Exit spread is captured immediately after fill, not before fill, by design to avoid
  delaying exits. It describes nearby post-fill market state, not causal pre-fill
  slippage.
- Venue-side stop/external closes may lack exact fee and quote detail when Bybit's
  closed-PnL response does not provide them.
- RSI/orderbook imbalance parsing depends on existing human-readable reason format.
  Future scorer changes should add typed signal fields rather than silently changing
  text parsing contracts.
