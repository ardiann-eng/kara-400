# Bybit Migration - Next Steps Handoff

Date: 2026-07-12

## Current Status

Completed:

- Phase 1: executor and exchange contracts.
- Phase 2: Bybit-only fail-closed config.
- Phase 3: Bybit symbol registry and Hyperliquid-to-Bybit price bridge.
- Phase 4: Bybit V5 REST client.
- Phase 5: Bybit entry, hard SL, close, and reconciliation executor.
- Phase 6: SQLite persistence, recovery, and entry circuit breaker.
- Phase 7: application wiring and venue-aware price monitor.
- Phase 8: encrypted per-user Bybit testnet `/live` setup.
- Phase 9: private Bybit WebSocket, order cache, REST fallback, reconnect reconciliation.
- Phase 11: `/paper`, close-all, manual close price routing, shutdown safety.
- Phase 12: WS execution/position/wallet event processing and deduplication.
- Phase 13A: scripted HTTP transport integration tests.
- Phase 13B: deterministic executor lifecycle and recovery tests.
- Phase 13C: WS reconnect/event ordering, user isolation, and `/paper` guard tests.
- Phase 13D: full executor-through-stateful-HTTP lifecycle and mixed failure paths.
- Phase 14A: opt-in Bybit testnet lifecycle drill runner.
- Phase 14B: drill-runner safety, cleanup, and unresolved-position tests.
- Phase 15A-D: per-user telemetry, instrumentation, alerts, and safe `/status` exposure.
- Phase 10A-E: live-risk hardening aligned to current scalper/user settings.

Skipped by user:

- Phase 10: live risk hardening.

Focused tests at last verified state after `/live` lifecycle fixes:

```text
101 passed
```

Last verification command:

```powershell
$env:PYTHONPATH=(Get-Location).Path; pytest -q --import-mode=prepend tests/test_execution_contracts.py tests/test_startup_validation.py tests/test_bybit_bridge.py tests/test_bybit_client.py tests/test_bybit_http_integration.py tests/test_bybit_executor_http_lifecycle.py tests/test_bybit_executor.py tests/test_bybit_persistence.py tests/test_user_session_bybit.py tests/test_bybit_credentials.py tests/test_bybit_private_ws.py tests/test_bybit_telegram_safety.py tests/test_bybit_testnet_drill.py tests/test_bybit_observability.py
```

Do not claim mainnet readiness. No real Bybit testnet lifecycle has been run.

## Fixed Architecture Rules

```text
Hyperliquid = read-only market data, scanning, candles, funding/OI, scoring, signals
Bybit       = only execution venue
```

Never reintroduce Hyperliquid execution.

- Do not restore `execution/live_executor.py`.
- Do not add a Hyperliquid private key execution path.
- Do not use Hyperliquid price for a Bybit manual close, TP, SL, or close-all.
- Do not bring back global `BYBIT_API_KEY` or `BYBIT_SECRET_KEY` configuration.
- Per-user Bybit credentials are mandatory.
- Credentials must stay Fernet-encrypted at rest.
- Never log API keys, API secrets, signatures, auth payloads, or `orderLinkId` together with secrets.
- Bybit REST remains source of truth. Private WS only reduces latency and triggers reconciliation.
- Do not enable mainnet.

## Environment Gate

Current intended testnet environment:

```env
KARA_EXECUTION_EXCHANGE=bybit
KARA_TRADE_MODE=paper
KARA_FULL_AUTO=false
BYBIT_TESTNET=true
BYBIT_TESTNET_ONLY=true
FERNET_KEY=<stable production Fernet key>
```

Important:

- `/live` activates a per-user **Bybit testnet** session after encrypted credential preflight and final confirmation.
- Mainnet must remain rejected while `BYBIT_TESTNET_ONLY=true`.
- Do not change `BYBIT_TESTNET_ONLY=false` until every Phase 14 drill and Phase 16 audit passes.
- Do not change `KARA_FULL_AUTO=true` during first real testnet lifecycle drills.

## Before Any Edit

1. Read `SESSION_NOTES/2026-07-12_BYBIT_LIVE_MIGRATION_PHASES_1_9.md`.
2. Read this handoff note.
3. Inspect `git status --short`.
4. Do not modify or revert unrelated changes, including `data/kara_data.db`.
5. Run existing focused tests before broad changes.
6. Use `apply_patch` for edits.

## Phase 13 - Integration Test Infrastructure

Goal: prove lifecycle behavior against controlled HTTP and WS mocks, not only simple fake methods.

### Phase 13 Progress

Completed 2026-07-13:

- Phase 13A added `tests/test_bybit_http_integration.py` with an aiohttp-compatible scripted transport.
- Signed private GET URL, query, timestamp payload, and signature are asserted.
- Signed POST endpoint, exact compact JSON body, and signature are asserted.
- Read `retCode=10006` and timeout retries are deterministic with no real waits.
- Create-order timeout raises `BybitAmbiguousOrderError` after exactly one POST.
- Realtime and history lookup preserve the same `orderLinkId`.
- Missing ambiguous order fails without any replacement create request.
- Phase 13B expanded executor coverage for partial/rejected/cancelled entry, emergency close, hard-SL plus emergency-close failure, partial/full/rejected close, and restart with present/missing hard SL.
- Phase 13C added out-of-order WS event coverage, execution deduplication, full reconnect auth/subscription sequencing, exactly one reconnect reconciliation, per-user client/credential isolation, and `/paper` live-position refusal.
- Focused suite: `60 passed`.
- Edited-module `python -m py_compile` passed.
- `git diff --check` passed; only existing Windows LF-to-CRLF warnings were reported.

Phase 13D completed 2026-07-13:

- Added `tests/test_bybit_executor_http_lifecycle.py` with a stateful V5 HTTP exchange simulator.
- Full executor entry, fill lookup, hard SL, partial close, and full close cross the real `BybitClient` request boundary.
- Every create request has a unique `orderLinkId`; closes are reduce-only.
- Mixed close-all proves one success cannot hide one unresolved asset.
- Unknown recovered position plus failed emergency close remains open/unprotected for reconciliation instead of being marked closed.
- Failed `paper_close_all_confirm` callback proves Paper mode cannot activate while a Bybit position remains.
- No test uses real credentials or `data/kara_data.db`.

Phase 13 is complete at controlled integration-test level. Real exchange behavior remains Phase 14 evidence work.

### Work

- Add a local mock Bybit V5 HTTP server or injectable HTTP transport for `BybitClient`.
- Add a controllable private WS fake/server for reconnect and event sequencing.
- Avoid real testnet credentials in automated tests.
- Keep test files independent from optional paper dependencies such as `pandas` when possible.

### Required Test Cases

- Correct signed GET request reaches expected endpoint and query.
- Correct signed POST request reaches expected endpoint and compact JSON body.
- `retCode=10006` rate-limit retry for reads.
- Read timeout retry behavior.
- Create-order timeout creates `BybitAmbiguousOrderError`.
- Ambiguous order lookup finds same `orderLinkId` in realtime.
- Ambiguous order lookup falls back to history.
- Ambiguous order not found causes safe failure, not duplicate create order.
- Full entry fill.
- Partial entry fill followed by reduce-only emergency close.
- Rejected entry.
- Cancelled entry.
- Hard SL setup reject followed by emergency close.
- Hard SL setup reject plus emergency-close failure results in `RECONCILIATION_REQUIRED`.
- Partial TP fill.
- Full reduce-only close.
- Close order rejected.
- Close-all with one successful close and one failed close.
- Restart with persisted position and valid exchange hard SL.
- Restart with persisted position but missing hard SL.
- Unknown exchange position with no safe stop triggers emergency close.
- Duplicate `order` event.
- Duplicate `execution` event by `execId`.
- Out-of-order order and execution events.
- WS disconnect then REST fallback.
- WS reconnect triggers exactly one forced reconciliation.
- User A credentials/client can never be used for user B.
- `/paper` cannot clear live state while an exchange position remains.

### Acceptance Criteria

- Tests do not call real Bybit.
- Every error path has deterministic assertion.
- No duplicate order create in timeout/reconnect tests.
- No test depends on current `data/kara_data.db`.
- All focused tests pass.
- `python -m py_compile` passes for edited modules.
- `git diff --check` passes.

## Phase 14 - Real Bybit Testnet Drill Runner

Goal: add a deliberately opt-in operator tool. It must not run from normal bot startup.

### Phase 14 Progress

Phase 14A and 14B completed 2026-07-13:

- Added `tools/bybit_testnet_drill.py`; normal bot startup never imports or runs it.
- Requires `--confirm-testnet`, `BYBIT_TESTNET=true`, `BYBIT_TESTNET_ONLY=true`, and `KARA_FULL_AUTO=false`.
- Accepts only BTC or ETH and LONG or SHORT.
- Reads credentials through hidden interactive prompts, not CLI arguments or report fields.
- Runs clock sync, metadata load, credential/account/permission/one-way-mode preflight, and existing-position refusal before order entry.
- Prints masked account and explicit `BYBIT TESTNET` environment before final operator confirmation.
- Uses 1x leverage and smallest quantity satisfying quantity-step, minimum-quantity, and minimum-notional rules.
- Installs a 1% native MarkPrice hard SL and verifies it from REST position state.
- Supports optional partial reduce-only close and verifies protected remainder.
- Full close and failure cleanup use actual exchange position size and reduce-only orders.
- Cleanup runs in `finally`; unresolved position produces failed evidence and non-zero result.
- JSONL evidence masks account and `orderLinkId` and never includes credentials.
- Automated safety tests cover gates, sizing, successful lifecycle, SL failure cleanup, unresolved cleanup, unknown symbol, and rejected operator confirmation.

Operator command, after environment and credential preparation:

```powershell
python -m tools.bybit_testnet_drill --confirm-testnet --symbol BTC --side long --partial-close
```

Phase 14C remains pending:

- No command with `--confirm-testnet` has been executed against real Bybit testnet.
- No real order, fill, hard SL, partial close, full close, or evidence report exists yet.
- Required 50 manual and 100 controlled automatic testnet lifecycles remain zero.
- Do not enable full-auto or mainnet.

### Safety Rules

- Tool requires explicit CLI flag such as `--confirm-testnet`.
- Tool refuses mainnet URL and `BYBIT_TESTNET=false`.
- Tool refuses missing `FERNET_KEY` if it reads stored user credentials.
- Tool refuses `KARA_FULL_AUTO=true`.
- Tool prints testnet environment and account before action.
- Tool requires user/operator confirmation before first order.
- Tool uses smallest valid contract size from loaded metadata.
- Tool refuses unknown symbol.
- Tool must clean up every created position in `finally`.
- Tool reports unresolved position as failure and non-zero exit code.

### Drill Order

Run manually, one scenario at a time. Start BTCUSDT or ETHUSDT only.

1. Public metadata and mark-price read.
2. Private credential preflight.
3. One-way mode check.
4. Small LONG entry.
5. Verify actual fill, size, average entry, and fee.
6. Verify native hard SL exists using REST position state.
7. Partial reduce-only close.
8. Verify remaining size and hard SL remain valid.
9. Full reduce-only close.
10. Verify exchange position size is zero.
11. Small SHORT entry and full close.
12. Restart/recreate session while a protected testnet position is open.
13. Verify persistence and reconciliation recover exact position.
14. Remove hard SL manually in testnet UI only if operator explicitly chooses this drill.
15. Verify recovery path reinstalls valid persisted SL or emergency-closes unsafe position.
16. Disconnect private WS or wait for reconnect.
17. Verify REST reconciliation after reconnect.
18. Test close-all with two small positions only after individual lifecycle passes.
19. Test `/paper` blocked with live testnet position, then close-all and switch paper.

### Evidence Required Per Drill

Record in a non-secret report:

- Date/time UTC.
- Testnet account identifier masked.
- Symbol.
- OrderLinkId masked/truncated if included.
- Entry fill price.
- Exit fill price.
- Quantity.
- Fee.
- Hard SL present yes/no.
- Reconciliation result.
- WS connected/stale state.
- Final Bybit position size.
- Result pass/fail.

Never put API credential values in report or logs.

### Acceptance Criteria

- At least 50 manual testnet position lifecycles.
- At least 100 controlled automatic lifecycles only after manual drills pass.
- Zero orphan positions.
- Zero unprotected positions after entry.
- Zero duplicate order submissions.
- Zero credential leaks.
- Every close-all drill verifies exchange zero positions.

## Phase 10 - Live Risk Hardening

Originally skipped, then completed 2026-07-13 using current KARA scalper/user settings rather than the earlier generic conservative proposal.

### Current-Setting Alignment

Current runtime defaults audited before implementation:

- `KARA_FORCE_SCALPER_ONLY=true`.
- Scalper user leverage default/reset cap: 20x.
- Scalper open-position reset cap: 3.
- Score risk tiers: 2.0%-3.5% equity risk per trade.
- Existing hard margin cap: 35% equity.

Live-only hard ceilings therefore use:

```env
BYBIT_LIVE_ASSET_ALLOWLIST=BTC,ETH
BYBIT_LIVE_MAX_LEVERAGE=20
BYBIT_LIVE_MAX_POSITIONS=3
BYBIT_LIVE_MAX_RISK_PER_TRADE_PCT=0.035
BYBIT_LIVE_MAX_TOTAL_RISK_PCT=0.105
BYBIT_LIVE_MAX_SYMBOL_NOTIONAL_PCT=7.0
BYBIT_LIVE_MAX_TOTAL_NOTIONAL_PCT=21.0
BYBIT_LIVE_MAX_SIGNAL_AGE_S=30
BYBIT_LIVE_MAX_QUOTE_AGE_S=5
BYBIT_LIVE_MAX_SPREAD_PCT=0.0015
BYBIT_LIVE_MIN_DEPTH_RATIO=1.0
```

Derivation:

- 7x equity symbol notional equals existing 35% margin ceiling at 20x leverage.
- 21x total notional equals three current scalper positions at the symbol ceiling.
- 10.5% total open risk equals three positions at current 3.5% maximum risk tier.
- User settings can lower leverage/position limits; live hard caps cannot raise them.
- Paper sizing and paper asset universe are unchanged.

### Completed Controls

- Added one `BybitLiveRiskGate` in `BybitExecutor.open_position`; manual and automatic entry paths converge there.
- BTC/ETH initial live allowlist.
- Signal, user, live hard-cap, and Bybit metadata leverage caps all apply.
- Per-trade risk, total open risk, symbol notional, total notional, and live position caps.
- Signal timestamp and Bybit execution-quote freshness guards.
- Public Bybit ticker plus 50-level orderbook fetch.
- Best bid/ask spread guard, side-aware VWAP slippage estimate, and minimum depth guard.
- Market-data/orderbook errors fail closed before leverage change or order creation.
- Risk rejection count/reason and live limit values appear in secret-free telemetry and `/status`.
- Startup validates every Phase 10 setting fail-closed.
- Kill-switch auto-reset was removed; only explicit authorized reset remains.
- Fixed latent `_calculate_trade_risk` tuple-unpack bug.
- Exit, emergency close, reconciliation, and WS recovery bypass entry gate and remain active.

Important: these limits match current aggressive settings. They do not make mainnet conservative or ready. Mainnet remains blocked until Phase 14C evidence and Phase 16 audit.

## `/live` Dynamic Activation Fixes

Completed 2026-07-13:

- Added `core/bybit_session_lifecycle.py`.
- First live user after an all-Paper startup now lazily initializes public Bybit clock and instrument metadata.
- Bootstrap is lock-protected; concurrent `/live` activations create one public client only.
- Failed metadata bootstrap closes the temporary client and leaves no partial global client.
- Live reactivation stops the old private WS before closing the old private REST client.
- Failed private session initialization is cleaned and never inserted into the session registry.
- Activation rollback restores previous credentials, authorization, environment, and mode instead of always forcing Paper.
- If the previous mode was Live, rollback attempts to rebuild the previous live monitor session.
- Cleanup attempts both WS and REST even if one close operation fails.
- Added activation lifecycle and Telegram callback tests.

Verification after these fixes: `101 passed`.

### Original Required Controls

- Live leverage cap, aligned by user request to current 20x scalper cap.
- Per-trade risk cap, aligned by user request to current 3.5% maximum score tier.
- Total open risk cap.
- Total open notional cap.
- Per-symbol notional cap.
- Max live concurrent positions, initially one.
- Max correlated exposure.
- Entry block on stale Hyperliquid signal price.
- Entry block on stale Bybit mark price.
- Spread guard from Bybit orderbook/ticker.
- Estimated slippage guard.
- Minimum liquidity/notional guard.
- Price bridge max-gap enforcement telemetry.
- Telegram alert when circuit breaker opens.
- Telegram alert for unprotected position.
- Kill-switch must never auto-reset.
- Pause must block only new entries; exits and reconciliation continue.
- Initial mainnet asset allowlist: BTC and ETH only.

### Acceptance Criteria

- Unit tests for each cap and each rejection reason.
- Config values appear in `/status` or dashboard without exposing secrets.
- Risk rejection is logged with safe reason and no secret data.
- No path can bypass cap through Telegram manual action, auto signal, WS event, or restart.

## Phase 15 - Observability and Alerting

Implement after Phase 13 or in parallel with testnet drills.

### Phase 15 Progress

Phase 15A-D completed 2026-07-13:

- Added `core/bybit_observability.py` with one secret-free `BybitTelemetry` per live user.
- Snapshot schema contains no API key, API secret, signature, auth payload, or order identifier fields.
- REST instrumentation records health, last success/error, error count, and latency.
- Private WS instrumentation records connected/stale state, stale duration, last message, and reconnect count.
- Executor instrumentation records reconciliation time/mismatches, hard-SL health per symbol, entry/fill/close latency, price bridge gap, actual fill slippage, fee, circuit state, unknown recovered positions, and emergency-close outcomes.
- `/status` exposes `BYBIT TESTNET`/`BYBIT MAINNET`, REST, private WS, reconciliation, mismatch, hard SL, latency, price gap, and circuit state without credentials.
- Added per-user Telegram alert sink with key-based deduplication and a default five-minute cooldown.
- Alerts cover hard-SL failure, emergency-close failure, missing/reinstalled SL, unexpected exchange position, WS stale, reconciliation failure, circuit breaker opening, incomplete close-all, and startup exchange positions.
- Alert delivery failure is isolated and cannot block exit or reconciliation.
- Added `tests/test_bybit_observability.py`; focused suite now reports `77 passed`.
- Compile checks and `git diff --check` pass. Existing Windows LF-to-CRLF warnings remain informational.

Phase 15 residual work tied to Deferred Phase 10:

- `estimated_slippage_pct` remains zero until Bybit orderbook/depth guard is implemented.
- Minimum liquidity and spread telemetry require the same orderbook feed.
- Real alert delivery and latency values require Phase 14C testnet execution evidence.

### Required Telemetry

- Venue and environment badge: `BYBIT TESTNET` or `BYBIT MAINNET`.
- REST health.
- Private WS connected/stale duration.
- Last successful reconciliation timestamp.
- Reconciliation mismatch count.
- Native hard-SL health per position.
- Entry latency.
- Fill latency.
- Close latency.
- Price bridge gap.
- Estimated and actual slippage.
- Actual fill fee.
- Circuit breaker state and remaining cooldown.
- Count of unknown recovered positions.
- Count of emergency close attempts and outcomes.

### Alerts

- Hard SL setup fails.
- Emergency close fails.
- Reconciliation finds unexpected position.
- Reconciliation finds missing hard SL.
- WS stale beyond threshold.
- Circuit breaker opens.
- Close-all incomplete.
- Startup finds positions on exchange.

### Acceptance Criteria

- Alert text never includes secrets.
- Dashboard/API never returns credential fields.
- Alerts have rate limiting/deduplication.

## Phase 16 - Mainnet Readiness Audit

Do not write mainnet activation code before a review of entire flow.

Audit this exact path:

```text
/live
credential encryption
testnet preflight
session creation
Hyperliquid signal
Bybit symbol resolution
price bridge
risk gate
entry order
ambiguous order lookup
fill
hard SL
TP1
TP2
trailing
manual close
native SL close
WS disconnect
restart
reconciliation
close-all
/paper
shutdown
```

Required audit findings policy:

- P0/P1 finding: block mainnet.
- No silent fallback to paper or Hyperliquid.
- No unknown exchange position can remain unprotected.
- No mainnet test until testnet evidence complete.

## Phase 17 - Controlled Mainnet Rollout

Only after Phases 10, 13, 14, 15, and 16 pass.

### Initial Mainnet Settings

```env
BYBIT_TESTNET_ONLY=false
BYBIT_TESTNET=false
BYBIT_MAINNET_ACK=I_UNDERSTAND_BYBIT_MAINNET_RISK
KARA_FULL_AUTO=false
```

Operational limits:

- BTCUSDT and ETHUSDT only.
- One position maximum.
- 3x-5x leverage maximum.
- 0.25%-0.5% risk per trade.
- Manual confirmation only.
- Smallest meaningful capital.
- Daily operator review.
- At least one week before enabling any increase.

### Rollout Steps

1. Read-only mainnet preflight.
2. Manual tiny BTC test order.
3. Confirm hard SL with exchange REST and UI.
4. Manual full close.
5. Repeat LONG and SHORT lifecycle.
6. Restart recovery drill with tiny protected position.
7. Keep auto execution disabled.
8. Review logs, fills, fee, reconciliation, and alerts daily.
9. Decide whether to enable one-position controlled auto only after evidence review.

## Final Completion Definition

Migration is complete only when:

- Hyperliquid has no execution path.
- Bybit is sole execution path.
- Testnet drills have passed with evidence.
- Phase 10 risk hardening is complete.
- Integration test suite passes.
- Mainnet audit has no P0/P1 findings.
- Mainnet rollout has completed controlled manual lifecycle.
- Observability and alerts work.
- No unprotected, orphan, duplicate, or unreconciled position remains.
