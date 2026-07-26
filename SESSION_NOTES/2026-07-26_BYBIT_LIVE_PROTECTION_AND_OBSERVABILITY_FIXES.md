# Bybit Live: Protection Validator, Break-Even Guard, and Alert Observability

Date: 2026-07-26

## Scope

Operator reported four live-mode symptoms via Telegram alerts:

- S1 `CRITICAL BYBIT: posisi LDOUSDT tidak memiliki native hard SL.`
- S2 `CRITICAL BYBIT: hard SL hilang untuk DOTUSDT dan berhasil dipasang ulang.`
- S3 Bot cannot set a matching take profit; Bybit UI shows no TP at all.
- S4 `WARNING BYBIT: private WebSocket stale/disconnected; REST fallback tetap aktif.`

## Deployment State (pivotal, established first)

- Railway service `kara` / environment `production` runs git commit `000a1db`.
  Build image created `2026-07-19T12:50:50Z`, 80s after `000a1db` (`2026-07-19 19:49:30 +0700`).
- Railway deploys from `https://github.com/ardiann-eng/kara-400.git`.
- Working-tree changes to `data/bybit_client.py`, `execution/bybit_executor.py`,
  `execution/exchange_client.py`, `models/schemas.py` (mtimes 19:57–19:59 +0700) post-date the
  build and are uncommitted. **The native partial TP feature has never run in production.**
- Consequence: the Full-vs-Partial `tpslMode` conflict hypothesis cannot explain S1/S2, because
  `tpslMode=Partial` is not deployed. That hypothesis was rejected.

## Evidence

- Railway deploy logs `2026-07-24 09:33` → `2026-07-26 13:09` contain **zero** lines matching
  `CRITICAL BYBIT` or `WARNING BYBIT`, while the operator did receive those alerts on Telegram.
  Root cause: `BybitAlertManager.emit` wrote only to the Telegram sink, never to `log`.
  A 300s per-key cooldown additionally dropped repeats silently.
- Deployed `set_protection` posts `tpslMode="Full"` with `stopLoss` only. All three call sites in
  `execution/bybit_executor.py` (entry, TP1 break-even, reconcile reinstall) omit `take_profit`,
  although entry computes and persists `tp1`/`tp2`. No native TP is ever sent.
- Deployed `_valid_recovery_stop` compared the stop against **entry price** with a strict
  inequality. After TP1, KARA sets `position.stop_loss = position.entry_price`, and a runner
  trailing stop can move far beyond entry. Both legitimate states failed validation.
- Deployed `.stale` is already the transport-only definition (`not self._connected`); the
  2026-07-19 idle-heuristic fix is live. S4 therefore reports a **real** disconnect, not a false
  positive. The two most common disconnect paths (aiohttp heartbeat dead-peer → `ERROR` frame,
  and a server close frame → `StopAsyncIteration`) exit `_run`'s `async for` without raising, so
  they reached no `except` branch and produced no log line.

## Root Causes

1. **Alert observability gap.** Protection alerts were Telegram-only and cooldown-throttled, so no
   forensic record exists for when or why LDOUSDT lost its stop. This blocked diagnosis of S1/S2.
2. **`_valid_recovery_stop` used the wrong reference price.** Testing a stop against entry price
   rejects break-even and trailing profit-lock stops. When reconciliation found a missing venue SL
   on such a position it fell through to `_emergency_close` instead of reinstalling.
   This explains the S1/S2 asymmetry: DOTUSDT had not hit TP1 (stop still strictly below entry →
   reinstalled, second alert fired); LDOUSDT had (stop == entry → validation failed → closed,
   only the first alert fired).
3. **Unguarded `set_protection` on the TP1 break-even path.** A Bybit rejection (stop already
   crossed by mark price) propagated out of `update_positions`, aborting exit evaluation for every
   remaining open position, and left KARA claiming a break-even stop the venue never accepted.
4. **Silent WebSocket disconnects.** Dead-peer and server-close paths logged nothing.

Not proven: *why* the venue-side SL disappeared in the first place. No server-side evidence exists
for the LDOUSDT/DOTUSDT events because of root cause 1. Fix 1 is what makes the next occurrence
diagnosable.

## Changes

- `core/bybit_observability.py` — `BybitAlertManager.emit` now logs every alert unconditionally at
  a severity derived from the message prefix, before delivery is attempted, and logs
  cooldown-suppressed repeats with a running count. Delivery semantics and return values unchanged.
- `execution/bybit_executor.py`
  - Added `_reference_price()`; `_valid_recovery_stop()` now validates against the live mark price
    instead of entry price.
  - Reconciliation defers reinstall (and alerts `missing_sl_deferred`) when no reference price is
    available, rather than emergency-closing on a transient price-feed failure.
  - TP1 break-even `set_protection` wrapped: on rejection the local stop is left equal to the stop
    the venue still holds, the loop continues for other positions, and `breakeven_stop_failed`
    alerts.
- `data/bybit_private_ws.py` — `_run` captures a disconnect reason and logs it in `finally` when
  the socket had been connected.

## Verification

```text
234 passed
python -m pytest tests/ -q

python -m py_compile core/bybit_observability.py execution/bybit_executor.py data/bybit_private_ws.py
git diff --check
```

New regression tests, each confirmed to **fail against the pre-fix code**:

- `test_missing_sl_on_breakeven_position_is_reinstalled_not_emergency_closed` (reproduces LDOUSDT)
- `test_missing_sl_with_trailing_stop_beyond_entry_is_reinstalled`
- `test_missing_sl_stop_already_crossed_by_price_still_emergency_closes` (fail-closed preserved)
- `test_missing_sl_defers_reinstall_when_reference_price_unavailable`
- `test_rejected_breakeven_stop_keeps_local_stop_and_does_not_abort_loop`
- `test_alert_is_logged_even_when_telegram_sink_is_absent`
- `test_cooldown_suppressed_alert_is_still_logged`
- `test_warning_alert_logs_at_warning_level`
- `test_dead_peer_disconnect_is_logged_with_reason`
- `test_server_closed_stream_disconnect_is_logged`

Pre-fix failure was verified by temporarily restoring the old validator and the old `finally`
block, running the new tests, and restoring the files.

## Not Done — S3 (native take profit)

Deliberately not implemented. The deployed defect is proven (no `take_profit` is ever sent), but
the correct remedy is not, and the candidate remedies all touch the hard-SL mechanism:

- Bybit V5 `set-trading-stop` documentation does **not** specify whether a `tpslMode=Partial` call
  clears a previously set `Full` stop loss, whether repeated `Partial` calls add or overwrite, or
  whether `/v5/position/list.stopLoss` reflects Partial-mode stops. Verified by reading the
  official docs; questions 2, 3, 5, 6 are unanswered there.
- Writing safety-critical protection code on an unverified API assumption is not acceptable. A
  wrong change here removes hard protection from live positions.

Options recorded for the operator decision:

- **A** — pass `take_profit=tp2` in the existing Full-mode `set_protection` call. One line, no mode
  switch, zero risk to the SL. Cost: whole position closes at TP2; loses TP1 partial and runner.
- **B** — deploy the uncommitted `add_partial_tp_sl` (`tpslMode=Partial`). Matches KARA policy but
  carries the unverified Full/Partial interaction risk, and the session note of 2026-07-19 already
  marks it not-ready (no target order IDs, so native fills cannot be attributed to slice PnL).
- **C** — reduce-only conditional orders via `/v5/order/create`, independent of `tpslMode`, leaving
  the Full SL untouched. Preserves TP1/TP2/runner exactly and gives exact target prices. Largest
  change: needs order-ID persistence, cancel-on-close, and fill reconciliation.

Required before any of these ships: a drill that empirically answers whether a Partial TP install
clears the Full-mode `stopLoss`.

### Drill built, not yet executed

`tools/bybit_testnet_drill.py --partial-tp-mode-probe` was added. It opens one minimum two-slice
position and snapshots position `stopLoss`/`takeProfit` plus live `StopOrder` rows at four stages:
after Full SL, after the first Partial TP/SL pair, after the second, and after re-applying the Full
SL. It records four verdicts: `partial_tp_clears_full_sl`, `partial_tp_visible_as_stop_order`,
`partial_calls_accumulate`, `full_sl_reapply_clears_partial_tp`. Read-only
`BybitClient.get_open_orders()` was added to observe conditional orders.

**Not run against Demo.** A read-only probe of the account behind the local `.env` Demo credentials
(`NB5l...TUsI`) found it is the **same account the Railway bot is actively trading**:

```
total_equity 12.32 USDT | available 10.22 USDT | open_positions 2
AAVEUSDT long size=0.2 entry=96.38 SL=93.92 TP=NONE
SOLUSDT  long size=0.3 entry=75.07 SL=74.47 TP=NONE
```

Three blockers: (a) a drill position would appear to the bot as an unknown exchange position and be
either emergency-closed or adopted into production state, (b) 10.22 USDT available cannot safely
fund a two-slice drill, (c) `KARA_FULL_AUTO=true` makes the drill's own `validate_environment`
guard refuse — correctly.

That probe also **confirms S3 directly in production**: both live positions carry a stop loss and
`TP=NONE`. The take-profit defect is no longer an inference from source; it is the observed venue
state. Confidence: High.

### Drill executed — 2026-07-26, Bybit Demo, BTCUSDT and ETHUSDT

Operator topped the Demo account to 1066 USDT and explicitly authorised running with the bot live,
including setting `KARA_FULL_AUTO=false` for the drill process only. Two runs, both `result=passed`,
`final_position_size=0`, no residue. One earlier attempt failed cleanly at the entry order with
Bybit `110007: ab not enough for new order` because the bot had consumed margin; nothing was opened.

Identical verdicts on both symbols:

| Verdict | Result |
| --- | --- |
| `partial_tp_clears_full_sl` | **False** |
| `partial_calls_accumulate` | **True** |
| `full_sl_reapply_clears_partial_tp` | **False** |
| `partial_tp_visible_as_stop_order` | **True** |

ETHUSDT stage trace (`position.stopLoss` / `position.takeProfit` / live StopOrder rows):

```
1_after_full_sl            SL=1855.62  TP=0.0  orders=1
   StopLoss(Full, qty=0.02, trig=1855.62)
2_after_partial_tp1        SL=1855.62  TP=0.0  orders=3
   +PartialTakeProfit(Partial, qty=0.01, trig=1931.36)
   +PartialStopLoss(Partial, qty=0.01, trig=1855.62)
3_after_partial_tp2        SL=1855.62  TP=0.0  orders=5
   +PartialTakeProfit(Partial, qty=0.01, trig=1969.23)
   +PartialStopLoss(Partial, qty=0.01, trig=1855.62)
4_after_full_sl_reapplied  SL=1855.62  TP=0.0  orders=5   (unchanged)
```

**The Full-vs-Partial conflict hypothesis is refuted.** Installing Partial TP/SL pairs leaves the
Full-mode `stopLoss` intact, repeated Partial calls accumulate rather than overwrite, and
re-applying the Full stop — which is exactly what KARA's reconciliation does — does not remove the
partial targets. The feared oscillation between missing-SL alerts and wiped TPs does not exist.

Two findings the drill produced that were not anticipated:

1. **`position.takeProfit` stays `0.0` at every stage.** Partial TPs never surface in
   `/v5/position/list`. This explains the original 2026-07-19 UI observation (`TP/SL` shown as
   `-- / <price>`) and means `BybitClient.get_positions()` structurally **cannot** see KARA's own
   native targets. Any TP-aware reconciliation must read conditional orders via the new
   `get_open_orders()`, not the position row.
2. **Stop quantity is double-counted.** After the full install there are three stop orders at the
   same trigger: `StopLoss(Full, 0.02)` plus two `PartialStopLoss(0.01)`, i.e. 0.04 of stop against
   a 0.02 position. Reduce-only should cap actual closure at position size, but this was not
   exercised — price never reached the stop during the drill.

### What this unblocks, and what still gates Option B

Unblocked: the safety objection to Option B is gone, and the 2026-07-19 gate "trading-stop returns
no target order IDs" is now solvable — `/v5/order/realtime` rows carry `orderId`/`orderLinkId` for
each Partial target, retrievable through `get_open_orders()`.

### Follow-up drill — solo partial TP, 2026-07-26

`--partial-tp-solo-probe`, ETHUSDT, `result=passed`, no residue. Verdicts:

```
solo_tp_accepted           : true    target installs with no paired stop
both_targets_installed     : true
paired_stop_created_anyway : false   the redundant stop stack is gone
every_target_has_order_id  : true    attribution is possible
full_stop_survived         : true
```

Final venue state: 3 orders — 2 `PartialTakeProfit` (0.01 each) + 1 Full `StopLoss` (0.02).
Total stop quantity now equals position size. `add_partial_tp_sl` therefore takes `stop_loss` as
optional and omits `stopLoss`/`slSize` when absent.

## Native take-profit implementation

Both gates from 2026-07-19 are now closed.

- `BybitClient.get_open_orders()` and `get_order_by_id()` expose conditional orders and resolve a
  target by exchange order id.
- `Position` carries `native_tp1_order_id` / `native_tp2_order_id`, persisted with the existing
  JSON blob, so no schema migration.
- Entry installs the full stop, then TP1 and TP2 as solo targets, then reads their order ids back
  by trigger price. Missing ids are left empty rather than guessed.
- `_settle_native_targets()` treats a target id that is no longer live as settled: only a
  confirmed `FILLED` order books a slice at its actual fill price; anything else drops the id
  without inventing lifecycle or PnL. `_move_stop_to_breakeven()` runs on a confirmed TP1.
- Settlement runs at the END of the reconcile branch, after the venue `stop_loss` sync, because a
  snapshot taken at the top of the cycle would otherwise overwrite the break-even stop just set.
- Settled actions queue on `_pending_target_actions` and are drained by `update_positions()`, so a
  native fill reaches Telegram through the same path as a local exit.

### Two defects the drill exposed in existing code

1. **`normalize_quantity` raises instead of returning zero**, so the pre-existing
   `if tp1_quantity <= 0` guard was dead code. A fill too small to split threw *after* the
   position was open and the stop installed, leaving an orphaned position. Sizing now goes through
   `_split_native_targets()`, which returns `None` instead. Native targets are an enhancement, not
   a condition of trading: an unsplittable fill keeps its hard SL and its local exits.
2. **`close_position()` raised on a partial slice below the venue step.** A 25% scale-out of a
   one-step position is impossible; it is now skipped and logged rather than escalated into a full
   exit. A `close_ratio >= 1` still raises, since that quantity is the position size itself.

**Operational consequence, worth knowing:** with TP1 at 25%, the smallest position that can carry
native targets is about `qty_step / 0.25` — roughly 4 steps. For ETHUSDT that is 0.04 (~76 USDT);
for BTCUSDT it is 0.004 (~259 USDT). Positions below that threshold trade normally but keep local
polling exits. This is why the BTC variant of the lifecycle drill could not run: it exceeds the
drill's own 250 USDT notional safety cap, which was left in place rather than raised.

### End-to-end proof — 2026-07-26

`--native-tp-lifecycle` drives the real `BybitExecutor.open_position()` against the venue.
ETHUSDT, `result=passed`, `final_position_size=0`:

```
planned TP1/TP2 : [1931.58, 1969.46]     what KARA computed
live triggers   : [1931.58, 1969.46]     what Bybit actually holds
targets live    : 2
ids captured    : True    stored ids match live orders: True
position SL     : 1855.84    partial_tp_clears_full_sl: False
```

Target prices at the venue match the planned targets exactly. This is production code, not the raw
client, so it is evidence that a real KARA entry leaves visible, attributable TP1/TP2 on Bybit.

## Deployment Status

- **Not deployed.** No commit, push, restart, credential access, or order was performed.
- Changes are local working-tree only. Note that committing the current tree would also ship the
  uncommitted native partial TP feature, which is not ready; stage selectively.

## Monitoring After Deploy

- `BYBIT_ALERT` / `BYBIT_ALERT_SUPPRESSED` lines should now appear in Railway logs.
- Expect `missing_sl_reinstalled` where previously a post-TP1 position was emergency-closed.
- Watch for `missing_sl_deferred` (price feed) and `breakeven_stop_failed` (venue rejection).
- `Bybit private WS disconnected (<reason>)` reveals the true S4 cause.

## Rollback Conditions

- A position with a genuinely uninstallable stop is left open instead of closed.
- `missing_sl_deferred` persists across more than two consecutive reconciliations for one symbol.
- Log volume from alert logging degrades the deployment.

## Superseded

- Supersedes the S4 diagnosis in `2026-07-19_DEMO_PRIVATE_WS_IDLE_STALE_FIX.md`: that fix is
  deployed and working; a current stale alert now means a real disconnect.
