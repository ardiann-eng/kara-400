# Bybit Testnet Test Reminder

Date created: 2026-07-13

## Reminder

Codebase architecture and automated test infrastructure are ready for controlled Bybit testnet validation.

Real Bybit testnet testing has **not** been performed yet. Do not claim testnet operational readiness, mainnet readiness, or full-auto readiness until this checklist has real exchange evidence.

Current automated verification:

```text
101 passed
```

## Fixed Safety State

Keep these values during initial testnet drills:

```env
KARA_EXECUTION_EXCHANGE=bybit
KARA_TRADE_MODE=paper
KARA_FULL_AUTO=false
BYBIT_TESTNET=true
BYBIT_TESTNET_ONLY=true
FERNET_KEY=<stable Fernet key>
```

Do not set:

```env
BYBIT_TESTNET=false
BYBIT_TESTNET_ONLY=false
KARA_FULL_AUTO=true
```

## Account Preparation

Prepare a Bybit testnet account at:

```text
https://testnet.bybit.com
```

Required account state:

- Unified account.
- One-way position mode.
- Testnet USDT balance available.
- No open BTCUSDT position before the first BTC drill.
- No active BTCUSDT order before the first BTC drill.
- API key has account read permission.
- API key has contract trading permission.
- API key has no withdrawal permission.
- IP whitelist matches the machine/server running the drill when whitelist is enabled.

Never put API key or API secret in this file, Git, command arguments, screenshots, logs, or chat messages.

## First Drill

Run from the workspace terminal:

```powershell
$env:BYBIT_TESTNET="true"
$env:BYBIT_TESTNET_ONLY="true"
$env:KARA_FULL_AUTO="false"
python -m tools.bybit_testnet_drill --confirm-testnet --symbol BTC --side long
```

The tool asks for API key and API secret through hidden prompts. After checking the masked account and explicit `BYBIT TESTNET` environment, type exactly:

```text
TESTNET
```

## First Drill Pass Gate

Evidence must contain:

```json
{
  "environment": "BYBIT TESTNET",
  "symbol": "BTCUSDT",
  "side": "long",
  "hard_sl_present": true,
  "reconciliation_result": "exchange_zero",
  "final_position_size": 0.0,
  "result": "passed"
}
```

Evidence file:

```text
SESSION_NOTES/bybit_testnet_drills.jsonl
```

If `result` is `failed`, `hard_sl_present` is false, or `final_position_size` is not zero:

1. Stop all further drills.
2. Inspect Bybit testnet UI immediately.
3. Close any remaining position manually with reduce-only behavior where available.
4. Record failure without credentials.
5. Fix and re-run the same scenario before moving forward.

## Drill Sequence

- [ ] 1. BTC LONG entry, native hard SL, full close, exchange zero.
- [ ] 2. BTC LONG entry, partial close, protected remainder, full close, exchange zero.
- [ ] 3. BTC SHORT entry, native hard SL, full close, exchange zero.
- [ ] 4. ETH LONG entry, native hard SL, full close, exchange zero.
- [ ] 5. ETH SHORT entry, native hard SL, full close, exchange zero.
- [ ] 6. Restart while a protected position is open; recover exact position.
- [ ] 7. Remove testnet hard SL manually; verify reinstall or emergency close.
- [ ] 8. Disconnect/reconnect private WS; verify forced REST reconciliation.
- [ ] 9. Open two smallest valid positions; test close-all and exchange zero.
- [ ] 10. Try `/paper` with a live position; verify blocking, close-all, reconciliation, then switch.
- [ ] 11. Trigger and verify Telegram alerts without exposing secrets.
- [ ] 12. Complete at least 50 manual testnet lifecycles.
- [ ] 13. Complete at least 100 controlled automatic lifecycles only after manual drills pass.

## Per-Drill Evidence

Record only non-secret fields:

- UTC timestamp.
- Masked account identifier.
- Symbol and side.
- Masked/truncated `orderLinkId` if needed.
- Quantity.
- Entry fill price.
- Exit fill price.
- Actual fee.
- Hard SL present yes/no.
- Partial remainder size when applicable.
- WS connected/stale state.
- Reconciliation result.
- Final exchange position size.
- Pass/fail result.

## Stop Conditions

Stop testnet testing immediately on any event below:

- Duplicate order submission.
- Position without native hard SL after entry.
- Emergency close failure.
- Final exchange size not zero after cleanup.
- Unknown exchange position not safely resolved.
- Credential, signature, or auth payload appears in logs/report.
- `/paper` discards a live session while exchange position remains.
- Close-all reports success while any exchange position remains.
- Mainnet URL or environment is detected.

## Mainnet Block

Mainnet remains prohibited until all conditions pass:

- Phase 14C evidence complete.
- Zero orphan positions.
- Zero unprotected positions after entry.
- Zero duplicate creates.
- Zero credential leaks.
- Every close-all drill confirms exchange zero.
- Phase 16 audit has no P0/P1 findings.

## Resume Instruction

When returning to this work:

1. Read `SESSION_NOTES/2026-07-12_BYBIT_LIVE_MIGRATION_PHASES_1_9.md`.
2. Read `SESSION_NOTES/2026-07-12_BYBIT_NEXT_STEPS.md`.
3. Read this reminder.
4. Inspect `SESSION_NOTES/bybit_testnet_drills.jsonl` if it exists.
5. Verify Bybit UI has zero unexpected positions before starting another drill.
6. Continue from the first unchecked drill only.

Current Phase 14C status: **PENDING - zero real testnet lifecycle evidence recorded**.
