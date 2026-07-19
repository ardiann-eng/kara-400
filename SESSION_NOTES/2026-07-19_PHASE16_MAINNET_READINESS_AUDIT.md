# Phase 16 Mainnet Readiness Audit

Date: 2026-07-19

## Scope

Audited configured source-to-exchange path:

```text
/live → encrypted credential storage → session → scalper signal → risk sizing
→ Bybit metadata/price bridge → live risk gate → order → fill → native SL
→ persistence/recovery → TP/partial/trailing/manual close → WS/reconciliation
→ close-all → /paper → shutdown
```

No deployment, restart, environment change, Testnet order, or mainnet order occurred.

## Evidence

### Code and Tests

- Startup rejects non-Bybit execution, invalid live config, missing Fernet, mainnet without exact acknowledgement, mainnet while testnet-only is enabled, and mainnet with `KARA_FULL_AUTO=true`.
- Bybit user credentials are Fernet-encrypted at rest; raw key/secret are removed from Telegram messages during setup.
- `/live` flow is explicitly Testnet-only: preflight creates `BybitClient(testnet=True)` and confirmation persists `user.bybit_testnet = True`.
- Live entry executes `RiskManager.pre_trade_check`, Bybit price bridge, normalized quantity/notional, then `BybitLiveRiskGate` before leverage change/order submission.
- Live gate rejects allowlist, leverage, position, stale signal/quote, spread, VWAP slippage, depth, per-trade risk, symbol/total notional, and total risk violations. Market-quote errors fail closed.
- Create timeout reconciles same `orderLinkId`; no blind replacement create.
- Filled position is persisted only after native hard SL succeeds. SL failure triggers reduce-only emergency close; failed emergency close remains reconciliation-required.
- Persisted protected position reloads and reconciles on session initialization. Missing valid recovery stop is reinstalled; unknown unprotected venue position is emergency-closed.
- Private WS is advisory; reconnect/state events force REST reconciliation. Demo live evidence confirms this reconnect path.
- `/paper` forces reconciliation, blocks when venue position remains, and refuses Paper activation after close-all failure.
- Shutdown audits native SL for live positions and alerts on missing protection; it does not silently close or discard live positions.

Verification after Phase 16 audit fixes:

```text
122 passed  # whole-flow suite before Telegram label fix
53 passed   # affected Phase 10/16 suites after Telegram label fix
python -m py_compile notify/telegram.py core/startup_validation.py risk/risk_manager.py
git diff --check passed
```

### Real Exchange Evidence

Demo evidence in `SESSION_NOTES/bybit_demo_drills.jsonl` covers 14 records including BTC/ETH long/short lifecycle, partial protected remainder, protected recovery, missing-SL detection/emergency close, private WS reconnect/REST reconciliation, and two-position close-all. Every successful lifecycle ended exchange-zero where intended.

Testnet real lifecycle evidence: **0**.

Mainnet real lifecycle evidence: **0**.

## Findings

### P0 — Testnet acceptance evidence absent

Phase 16 acceptance policy requires Testnet evidence. Existing real evidence is Demo only. The repository's Phase 17 plan also requires Testnet drills before mainnet.

Impact: mainnet rollout remains blocked by process gate. Demo results are integration evidence, not Testnet/mainnet deployment proof.

## Operator Rollout Policy Decision

On 2026-07-19, operator explicitly accepted Bybit Demo Trading evidence as the practical substitute for unavailable usable Testnet funding. This changes the **rollout evidence policy only**:

- Demo evidence may satisfy the Testnet-evidence prerequisite for deciding whether to prepare a manual mainnet micro-pilot.
- All evidence labels remain `BYBIT DEMO TRADING`; Demo is never renamed or reported as Testnet.
- This does not enable mainnet, change `BYBIT_TESTNET_ONLY`, set `BYBIT_TESTNET=false`, restart a service, or enable `KARA_FULL_AUTO`.
- It does not establish mainnet fill, fee, latency, account-permission, regional-routing, or outage equivalence.
- Mainnet full-auto remains startup-blocked.

### P1 fixed — Mainnet full-auto startup

Startup now rejects mainnet when `KARA_FULL_AUTO=true`. See `2026-07-19_PHASE10_RISK_AUDIT_FIXES.md`.

### P1 fixed — Kill-switch reset shadowing

Unauthorized/non-persistent duplicate reset method removed. Remaining reset is admin-authorized and persists state.

### P1 fixed — Telegram scalper warning contradicted live caps

`/scalper` previously displayed `25-35x` and `13%` risk. It now displays `BYBIT_LIVE_MAX_LEVERAGE` and `BYBIT_LIVE_MAX_RISK_PER_TRADE_PCT` values. It also states that caps do not guarantee losses under gaps, slippage, or outage.

## Current Runtime Config Evidence

Local non-secret config snapshot:

```text
trade_mode=paper
full_auto=true
bybit_testnet=true
bybit_testnet_only=true
allowlist=BTC,ETH
live leverage cap=20
live positions cap=3
live risk/trade=3.5%
live total risk=10.5%
live total notional=21x equity
```

This is not a mainnet micro-pilot configuration. Mainnet startup now rejects its `full_auto=true` state.

## Non-Recommendations

- Do not change strategy, score, scalper levels, SL/TP, or paper behavior from this audit.
- Do not enable mainnet or automatic mainnet entry.
- Do not call Demo evidence Testnet/mainnet proof.

## Conditions Before Mainnet Micro-Pilot

1. Keep `KARA_FULL_AUTO=false`.
2. Select explicit mainnet micro-pilot limits and obtain operator request to activate mainnet. Current paper-like live caps are 20x, three positions, and 3.5% risk/trade.
3. Complete a read-only mainnet preflight before any mainnet order.
4. Run only manually confirmed BTC/ETH micro-pilot lifecycles after explicit activation.
5. Stop on first P0 failure: duplicate create, missing hard SL, failed emergency close, unresolved position, credential leak, or reconciliation failure.
