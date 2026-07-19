# Bybit Demo Drill Mode

Date: 2026-07-19

## Evidence

- Bybit official Demo Trading documentation specifies REST endpoint `https://api-demo.bybit.com`.
- Demo keys are created after switching a mainnet account to Demo Trading; they are separate from testnet keys.
- Demo supports wallet balance, order, position, leverage, trading-stop, and order history endpoints used by the drill.
- Demo does not list `/v5/user/query-api` as supported. Testnet preflight had used that endpoint.
- The prior testnet drill reached a valid testnet key but reported `Available USDT: 0.0`; no real lifecycle was run.

Source: https://bybit-exchange.github.io/docs/v5/demo

## Implementation

- `BybitClient` has explicit `demo=True` mode selecting `https://api-demo.bybit.com`.
- Demo and testnet cannot both be enabled.
- Demo preflight proves authenticated wallet and position reads; it does not call unsupported `/v5/user/query-api`.
- Demo drill requires `--environment demo --confirm-demo`, prints `BYBIT DEMO TRADING`, and requires typed `DEMO` before entry.
- Testnet path remains `--environment testnet --confirm-testnet` and remains blocked by its existing testnet-only guards.
- Drill refuses non-positive available USDT before confirmation or order submission.
- Default Demo evidence is written to `SESSION_NOTES/bybit_demo_drills.jsonl`; Testnet evidence remains `SESSION_NOTES/bybit_testnet_drills.jsonl`.
- Real Demo drill exposed Bybit code `110043` before entry: target 1x leverage already applied. No entry was submitted and final position was zero. Client now treats only this documented idempotent leverage response as success; all other leverage errors still fail.
- `--partial-close` now uses two independently valid minimum slices, caps total partial-drill notional at `250 USDT`, and persists partial fill price/size plus protected remainder size. Prior `0.001 BTC` evidence did not exercise partial close.
- `--hold-protected` opens smallest valid position, installs and reads native SL, then exits with `held_protected` while position stays open. `--recover-protected` accepts exactly one expected protected position and closes it reduce-only only after a second explicit confirmation. It refuses an unprotected position without submitting any close order.
- `--simulate-missing-sl` requires an already protected exact position. It cancels only full-position SL using documented `stopLoss: "0"`, re-reads REST state, then emergency closes reduce-only only after REST proves SL absent.
- Private WS has explicit Demo endpoint `wss://stream-demo.bybit.com/v5/private`. `--ws-reconnect-check` requires an already protected exact position, closes only local WS transport, waits for authenticated reconnect, and requires callback REST account plus position/SL reconciliation. It leaves position open for next recovery drill.

## Test Results

```text
79 passed
python -m py_compile data/bybit_client.py tools/bybit_testnet_drill.py
git diff --check passed
```

## Real Demo Evidence

Evidence file: `SESSION_NOTES/bybit_demo_drills.jsonl`.

After the `110043` fix, six Demo lifecycles passed on 2026-07-19. All six had native hard SL present and REST final position size zero:

| Scenario | Qty | Partial fill | Protected remainder | Result |
| --- | ---: | ---: | ---: | --- |
| BTC LONG full | 0.001 | n/a | n/a | passed |
| BTC LONG partial | 0.002 | 0.001 | 0.001 | passed |
| BTC SHORT full | 0.001 | n/a | n/a | passed |
| ETH LONG full | 0.01 | n/a | n/a | passed |
| ETH SHORT full | 0.01 | n/a | n/a | passed |
| ETH LONG partial | 0.02 | 0.01 | 0.01 | passed |

The prior `110043` record failed before any entry; it had `entry_fill_price: 0`, no order link ID, and final position size zero. It is a superseded client idempotency defect, not a failed exchange lifecycle.

Protected recovery evidence, 2026-07-19:

1. `hold_protected` BTC LONG `0.001` entered at `64677.7`, REST observed native SL `64030.9`, and REST position remained `0.001`.
2. New drill process ran `recover_protected`, re-read exact BTC LONG `0.001` and the same native SL `64030.9`, then reduce-only closed at `64677.8`.
3. Recovery reported `exchange_zero`, final position `0`, and `result: passed`.

This proves only REST recovery of an unchanged, protected position. It does not prove recovery behavior when SL is absent or WebSocket state is stale.

Missing-SL emergency-close evidence, 2026-07-19:

1. `hold_protected` BTC LONG `0.001` entered at `64674.8`; REST observed native SL `64028.1`.
2. `missing_sl_emergency_close` re-read that exact protected position, cancelled SL through Bybit `stopLoss: "0"`, and REST then reported SL absent (`missing_sl_detected: true`).
3. Tool submitted emergency reduce-only close at `64674.7`; REST final position was zero and result passed.

`hard_sl_present: true` in the missing-SL evidence means SL existed in pre-cancellation verification. `missing_sl_detected: true` is proof it was absent before emergency close.

Private WS reconnect evidence, 2026-07-19:

1. `hold_protected` BTC LONG `0.001` entered at `64639.8`; REST observed native SL `63993.4`.
2. `ws_reconnect_rest_reconciliation` authenticated a Demo private WS, closed local WS transport, and observed one reconnect.
3. Reconnect callback forced REST account and exact position read. It verified BTC LONG `0.001` and SL `63993.4`; evidence has `ws_reconnect_count: 1`, `forced_rest_reconciliation: true`, and `ws_state: "reconnected"`.
4. Follow-up recovery re-read the same SL and reduce-only closed at `64639.8`; REST final position was zero.

Multi-position close-all evidence, 2026-07-19:

1. `multi_position_close_all` opened BTCUSDT `0.001` and ETHUSDT `0.01`, each with native hard SL verified before closing.
2. Tool submitted reduce-only close for both symbols.
3. Evidence has `close_all_closed_symbols: ["BTCUSDT", "ETHUSDT"]`, total fee `0.09167116`, final position size zero, and `exchange_zero`.

Paper-switch guard verification:

- `KaraTelegram.cmd_paper` forces exchange reconciliation for Live users before Paper activation.
- Any remaining position displays a close-all/cancel choice and does not activate Paper.
- `paper_close_all_confirm` does not activate Paper when close-all reports failure or executor still has open positions.
- These are automated handler tests only. No Telegram bot process was started and no real `/paper` callback was sent.

## Deployment Status

No deployment, restart, commit, or real order. Local source change only.

## Runbook

Create Demo API key in `www.bybit.com` after switching to Demo Trading. Do not use a testnet key.

To avoid repeated prompts, store only Demo drill credentials in ignored local `.env`:

```env
BYBIT_DEMO_API_KEY=<demo API key>
BYBIT_DEMO_API_SECRET=<demo API secret>
```

Set both values or neither. The drill rejects a partial pair and falls back to hidden prompts only when both are absent. Never use `BYBIT_API_KEY`, mainnet, or testnet credential names for Demo.

```powershell
$env:KARA_FULL_AUTO = "false"
python -m tools.bybit_testnet_drill --environment demo --confirm-demo --symbol BTC --side long
```

Enter Demo API key and secret. Confirm only after tool prints `BYBIT DEMO TRADING`, masked account, positive available USDT, BTCUSDT, and quantity. Type `DEMO` exactly.

Protected restart/recovery drill:

```powershell
$env:KARA_FULL_AUTO = "false"
python -m tools.bybit_testnet_drill --environment demo --confirm-demo --symbol BTC --side long --hold-protected
# Simulate process exit/restart. Confirm exchange UI shows one BTCUSDT position and native SL.
python -m tools.bybit_testnet_drill --environment demo --confirm-demo --symbol BTC --side long --recover-protected
```

Do not run recovery when UI or tool reports missing SL. It refuses that condition without closing; inspect Demo exchange state before manual emergency close.

## Monitoring and Rollback

- Required evidence: native hard SL present, `reconciliation_result: exchange_zero`, `final_position_size: 0.0`, and `result: passed`.
- Stop on duplicate order, missing hard SL, cleanup failure, or non-zero final position.
- Roll back Demo use by stopping commands; testnet behavior remains separate.
- Demo orders persist only 7 days per Bybit documentation. Demo is not real-exchange testnet proof.
- Remaining real-exchange gaps: Testnet lifecycle and mainnet remain untested. Demo Trading is not mainnet proof.
