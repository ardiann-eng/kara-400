# Bybit Live Migration - Phases 1-9

Date: 2026-07-12

## Goal

KARA architecture is being changed to this fixed venue split:

```text
Hyperliquid = read-only market data, scanning, candles, funding/OI, scoring, signals
Bybit       = only live execution venue
```

Hyperliquid live execution is not allowed. Bybit mainnet is not enabled yet.

## Historical Audit

Old Bybit code was found in Git history, including commit `95d91d3`:

- `data/bybit_client.py`
- `data/bybit_ws_client.py`
- `execution/bybit_executor.py`

Old implementation was not restored because its client calls, risk-manager signatures,
position schema, TP/SL calls, PnL math, price routing, and restart behavior were incompatible
with current code. New implementation was written from scratch.

Old Hyperliquid live executor was removed:

- Deleted `execution/live_executor.py`.
- Removed application imports and runtime construction of `LiveExecutor`.
- Legacy Telegram Agent Wallet callbacks remain blocked only to safely handle old messages.

## Phase 1 - Execution Contracts

Added `execution/base_executor.py`:

- Common account-state interface.
- Open-position interface.
- Open, monitor, partial/full close, close-all, and reconciliation hooks.

Added `execution/exchange_client.py`:

- Venue-neutral account, order, instrument, and position models.
- Stable `ExecutionClient` contract.
- Order lifecycle statuses.
- Live position lifecycle statuses:
  - `PENDING_ENTRY`
  - `PARTIALLY_FILLED`
  - `OPEN_UNPROTECTED`
  - `OPEN_PROTECTED`
  - `PENDING_CLOSE`
  - `CLOSED`
  - `RECONCILIATION_REQUIRED`

`PaperExecutor` now implements `BaseExecutor`.

Root `__init__.py` was fixed. It previously imported a nonexistent `.schemas` module and
caused pytest collection failure.

## Phase 2 - Fail-Closed Configuration

Configuration changes:

- `KARA_EXECUTION_EXCHANGE` defaults to and only accepts `bybit`.
- Hyperliquid execution is rejected by startup validation.
- `KARA_FULL_AUTO` reads environment and defaults to `false`.
- Bybit execution parameters added:
  - `BYBIT_TESTNET`
  - `BYBIT_TESTNET_ONLY`
  - `BYBIT_ACCOUNT_TYPE`
  - `BYBIT_CATEGORY`
  - `BYBIT_SETTLE_COIN`
  - `BYBIT_RECV_WINDOW`
  - `BYBIT_MAX_PRICE_GAP_PCT`
  - `BYBIT_MAX_SLIPPAGE_PCT`
  - `BYBIT_MAINNET_ACK`

Added `core/startup_validation.py`:

- Rejects invalid trade mode or data source.
- Rejects execution venue other than Bybit.
- Requires Unified, linear, USDT configuration.
- Validates receive window, price-gap limit, and slippage limit.
- Requires `FERNET_KEY` for live execution.
- Mainnet requires exact acknowledgement and testnet-only gate disabled.

Mainnet remains locked while:

```env
BYBIT_TESTNET_ONLY=true
```

## Phase 3 - Symbol Registry and Price Bridge

Added `execution/symbol_registry.py`:

- Loads Bybit instrument metadata.
- Accepts active `LinearPerpetual` USDT contracts only.
- Stores tick size, quantity step, minimum quantity, minimum notional, and max leverage.
- Rounds quantity down to avoid increasing risk.
- Rounds price to exchange tick size.
- Rejects unknown assets instead of guessing `asset + USDT`.
- Supports explicit aliases only when target symbol exists in active metadata.

Added `execution/price_bridge.py`:

- Compares Hyperliquid reference price with Bybit execution price.
- Rejects trades above configured venue gap.
- Validates LONG/SHORT SL and TP ordering.
- Preserves strategy percentage distances.
- Rebases SL, TP1, and TP2 to actual Bybit execution/fill price.

## Phase 4 - Bybit V5 REST Client

Added `data/bybit_client.py` from scratch.

Public support:

- Server time.
- Instrument metadata with pagination.
- Mark price.

Private support:

- Unified wallet balance.
- USDT linear positions.
- Set leverage.
- Create market order.
- Realtime order lookup.
- Order-history fallback.
- Cancel order.
- Exchange-native trading stop.
- API permission/account preflight.

Signing rules:

- GET signs sorted URL-encoded query.
- POST signs exact compact JSON body.
- Signature payload is `timestamp + apiKey + recvWindow + queryOrBody`.
- HMAC SHA-256.

Order safety:

- Every order uses `orderLinkId`.
- Create-order POST is never blindly retried.
- Network timeout raises `BybitAmbiguousOrderError`.
- Caller must lookup same `orderLinkId` before any replacement decision.
- GET and rate-limit retries use bounded backoff.

## Phase 5 - Bybit Executor

Added `execution/bybit_executor.py`.

Open lifecycle:

1. Resolve active Bybit symbol.
2. Read Bybit account state.
3. Run risk pre-trade check.
4. Read Bybit mark price.
5. Validate Hyperliquid/Bybit price gap.
6. Calculate size from Bybit equity.
7. Cap leverage by exchange metadata.
8. Normalize quantity and minimum notional.
9. Set leverage.
10. Submit entry with unique `orderLinkId`.
11. Confirm fill.
12. Rebase levels to actual fill.
13. Install exchange-native hard SL.
14. Record position only after hard SL succeeds.

Hard-SL invariant:

- Position is not considered normal/open until hard SL succeeds.
- Hard-SL failure triggers reduce-only emergency close.
- Failed emergency close becomes `RECONCILIATION_REQUIRED`.

Close lifecycle:

- Reads actual remaining position size from Bybit.
- Uses reduce-only order.
- Confirms fill.
- Uses actual exit fill and fee.
- PnL is not multiplied by leverage.
- Partial and full close state are handled separately.

## Phase 6 - Persistence and Recovery

Added SQLite table `bybit_positions`, separate from paper state.

Persisted fields include:

- Position model.
- Bybit symbol.
- Lifecycle status.
- Entry `orderLinkId`.
- Telegram user ID.

Restart recovery:

- Loads persisted strategy metadata.
- Reconciles against actual Bybit positions.
- Reinstalls valid persisted hard SL when exchange stop is missing.
- Unknown exchange position without a safe persisted stop is emergency-closed.
- Local position absent on exchange is marked closed and removed from persistence.

Added entry circuit breaker:

- Default opens after three consecutive execution failures.
- Default cooldown is 60 seconds.
- Blocks new entries only.
- Close, emergency close, monitoring, and reconciliation remain enabled.

Added `reconcile_if_due()` with default 30-second interval.

## Phase 7 - Application Wiring

`UserSession` now creates only:

- `PaperExecutor` for paper mode.
- `BybitExecutor` for live mode.

No live fallback to paper exists.

Startup wiring:

- Hyperliquid connects for market data.
- Public Bybit client syncs clock and loads metadata.
- Live user sessions load persistence and force reconciliation.

Price monitoring is venue-separated:

```text
Paper position -> Hyperliquid price
Bybit position -> Bybit mark price
```

Shared assets cannot leak one venue's price into another executor.

## Phase 8 - Telegram Live Setup and Credential Security

User model fields added:

- `bybit_api_key`
- `bybit_api_secret`
- `bybit_authorized`
- `bybit_testnet`

`/live` flow:

1. Requires server `FERNET_KEY`.
2. Accepts Bybit testnet API key.
3. Deletes key message immediately.
4. Accepts API secret.
5. Deletes secret message immediately.
6. Holds credentials temporarily in Telegram conversation memory.
7. Runs clock sync and read-only preflight.
8. Shows testnet/account summary.
9. Requires final button confirmation.
10. Saves encrypted credentials.
11. Builds fresh live session and reconciles.

Preflight checks:

- Credential validity.
- Read-account access.
- Contract-trade permission.
- No withdrawal permission.
- Unified account.
- One-way position mode.
- Testnet environment.

Credential storage:

- API key and secret are Fernet-encrypted in `users.json`.
- Missing Fernet key blocks saving; plaintext fallback is forbidden.
- Decryption failure clears unusable credential fields.
- Global `BYBIT_API_KEY` and `BYBIT_SECRET_KEY` configuration was removed.
- Each user gets a private Bybit REST client.

Configuration loading bug fixed:

- Removed second `load_dotenv(override=True)` call.
- Process/Railway environment now has priority over local `.env`.

## Phase 9 - Private WebSocket

Added `data/bybit_private_ws.py`.

Private topics:

- `order`
- `execution`
- `position`
- `wallet`

Authentication signs:

```text
GET/realtime + expires
```

Behavior:

- Authenticates before subscribing.
- Caches order events by `orderLinkId`.
- Wakes waiting executor fills.
- Detects stale/disconnected stream.
- Falls back to REST immediately when stale.
- Reconnects with exponential backoff up to 30 seconds.
- Reauthenticates and resubscribes.
- Forces REST reconciliation after reconnect.

REST remains source of truth. WebSocket only improves latency.

## Safety Invariants

- Hyperliquid cannot execute live orders.
- Bybit is only live execution venue.
- Mainnet remains locked.
- Full auto defaults off.
- Live credential plaintext storage is forbidden.
- Order create is never blindly retried.
- Every order has idempotent `orderLinkId` lookup path.
- Entry is not accepted without exchange-native hard SL.
- Close is reduce-only and uses exchange size.
- PnL uses actual fill and fee, not leverage multiplication.
- Exchange state wins over local state.
- Circuit breaker blocks entries, never exits.
- Paper and Bybit prices are routed separately.

## Verification

Focused tests after Phase 9:

```text
37 passed
```

Covered areas:

- Execution contracts.
- Startup validation.
- Mainnet lock.
- Symbol metadata and normalization.
- Hyperliquid/Bybit price bridge.
- REST signing and order mapping.
- Ambiguous-order reconciliation.
- Entry, protection, emergency close, and PnL lifecycle.
- Persistence and restart recovery.
- Circuit breaker.
- Per-user credential encryption.
- User-session isolation.
- Private WebSocket auth, cache, waiter, and stale fallback.

Compile checks and `git diff --check` passed.

## Not Yet Completed

- No real Bybit testnet end-to-end run has been executed.
- No mainnet activation.
- Live-risk hardening phase still pending.
- Spread/orderbook slippage guard still pending.
- Total open-risk and total-notional caps still pending.
- Native testnet order, SL trigger, restart, and close-all drills still pending.
- `/paper` behavior with active Bybit positions still needs explicit choice flow.
- Telegram close preview still has legacy Hyperliquid price reads in some manual UI paths.
- Mainnet rollout must remain blocked until all testnet drills pass.

## Next Phase

Phase 10: live-risk hardening.

- Leverage caps.
- Total open-risk cap.
- Total notional cap.
- Stale market-data entry guard.
- Spread/slippage guard.
- Consecutive failure alerting.
- Kill-switch must not auto-reset.
- Fix remaining manual Telegram price routes to Bybit.

## Phase 11 - Paper, Close-All, and Shutdown Safety

- `/paper` refuses to switch while Bybit positions remain open.
- User can close all positions and switch only after forced exchange reconciliation confirms zero positions.
- User can cancel and remain live.
- Live session is never discarded while exchange positions remain unresolved.
- Bybit close-all now returns an explicit `close_all_failed` result with unresolved assets.
- Telegram never reports close-all success when any exchange position remains.
- Manual close preview, close, and close-all use Bybit mark price for Bybit positions.
- Shutdown audits native hard stops and sends a critical alert for unprotected positions.

## Phase 12 - Private WS State Events

- Execution events are deduplicated by `execId`.
- Position events are cached by symbol and position index.
- Latest wallet event is cached.
- Execution, position, and wallet events trigger forced REST reconciliation.
- Native SL closure and manual Bybit changes are therefore reflected without waiting for periodic reconciliation.
- WS payload remains advisory; REST remains source of truth.

## Phase 13A-13C - Integration Test Hardening

Added controlled transport and state-machine tests without real Bybit credentials:

- Scripted aiohttp-compatible HTTP transport validates signed GET and POST requests.
- Read rate-limit and timeout retries are deterministic.
- Create-order timeout proves one POST only and raises ambiguous-order state.
- Realtime/history lookup proves same `orderLinkId` reconciliation path.
- Entry tests cover full, partial, rejected, and cancelled outcomes.
- Hard-SL tests cover successful protection, emergency close, and unresolved emergency close.
- Close tests cover partial, full, and rejected reduce-only outcomes.
- Recovery tests cover present and missing exchange hard stops.
- WS tests cover duplicate/out-of-order events, REST fallback, reconnect auth/subscription, and exactly one forced reconnect reconciliation.
- Per-user client and credential isolation is asserted.
- `/paper` refusal with an open Bybit position is asserted without changing live state.

Verification after Phase 14B:

```text
70 passed
```

## Phase 13D - Full HTTP Lifecycle

- Added a stateful mock V5 HTTP exchange behind the real `BybitClient`.
- Full entry, fill confirmation, native hard SL, partial reduce-only close, and full reduce-only close run through HTTP request parsing.
- Mixed close-all reports unresolved assets when one close succeeds and another fails.
- Failed unknown-position emergency close remains visibly unresolved.
- Failed `/paper` close-all callback cannot discard live state.

Phase 13 controlled integration infrastructure is complete.

## Phase 14A-14B - Testnet Drill Tooling

- Added opt-in `tools/bybit_testnet_drill.py`.
- Runner is isolated from normal startup and refuses execution without exact testnet gates.
- Credentials use hidden prompts and never enter CLI arguments or evidence.
- BTC/ETH only, 1x leverage, smallest valid quantity, native hard SL verification, optional partial close, full close, and `finally` cleanup are implemented.
- Evidence is JSONL with masked account and order IDs.
- Safety, sizing, cleanup, unresolved-position, symbol, and confirmation tests pass.
- CLI help and missing-confirmation refusal smoke checks pass.

Phase 14 is not complete: no real Bybit testnet lifecycle has run. Phase 14C evidence targets remain pending.

## Phase 15A-15D - Observability and Alerts

- Added per-user secret-free Bybit telemetry shared by REST, private WS, executor, and session status.
- REST health and latency, WS state/stale duration/reconnects, reconciliation health, hard-SL state per symbol, execution latency, bridge gap, fill slippage/fee, circuit state, recovery counts, and emergency-close outcomes are tracked.
- `/status` shows environment and operational health without credential or order identifier fields.
- Telegram alerts are deduplicated and rate-limited per event key.
- Alert failures never block exits or reconciliation.
- Critical alerts cover protection, emergency close, reconciliation, unexpected positions, WS stale, circuit breaker, close-all, and startup positions.

Verification after Phase 15D:

```text
77 passed
```

Orderbook-derived estimated slippage, spread, and liquidity telemetry remains coupled to mandatory Deferred Phase 10 risk hardening. Real alert behavior still requires Phase 14C testnet drills.

## Adjusted Phase 10 - Current-Setting Live Risk Hardening

Phase 10 was completed after explicit instruction to match current settings:

- Current forced-scalper profile remains 20x user leverage cap, three positions, and up to 3.5% risk per trade.
- Live hard caps mirror those settings instead of earlier generic 3x-5x and 0.25%-0.5% proposals.
- Per-symbol notional cap is 7x equity, derived from 35% existing margin cap at 20x.
- Total notional cap is 21x equity and total open risk cap is 10.5%, matching three maximum-sized current positions.
- BTC/ETH remain the initial live allowlist.
- Unified entry gate covers leverage, positions, risk, notional, signal age, quote age, spread, VWAP slippage, and depth.
- Bybit ticker/orderbook failures block entry before any order.
- Kill-switch no longer auto-resets when drawdown improves.
- Paper behavior is unchanged; exits and reconciliation remain available under every entry rejection.

Verification after adjusted Phase 10:

```text
94 passed
```

These are aggressive current-setting limits, not a mainnet-readiness claim. Phase 14C and Phase 16 remain mandatory.

## Dynamic `/live` Session Lifecycle Fixes

- Public Bybit metadata now lazy-loads for the first live user after an all-Paper startup.
- Concurrent bootstrap is serialized and failed bootstrap closes its temporary client.
- Reactivation closes old private WS and REST resources before constructing the new session.
- Failed session initialization is never cached.
- Rollback restores previous user mode and credentials; previous live monitoring is rebuilt when possible.
- Resource cleanup attempts both WS and REST even if one operation fails.

Verification:

```text
101 passed
```
