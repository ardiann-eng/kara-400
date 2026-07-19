# Demo Capital Onboarding and Paper Deprecation Plan

Date: 2026-07-19

## Decision Context

Operator requests:

- Bybit Demo as normal execution environment, separate from Mainnet.
- Per-user Telegram onboarding after registration/access code.
- Per-user capital allocation entered in IDR.
- Scalper strategy/profile matching current Paper behavior.
- Full-auto Demo execution through normal bot path.
- Hyperliquid top-100 candidate universe, executed only when exact Bybit contract is active and eligible.
- Remove redundant Paper files only after Demo execution is proven and dependency audit is clean.

No implementation, deployment, restart, credential access, or order occurred for this plan.

## Target Model

Separate these concepts:

```text
strategy profile      = shared scalper logic
execution environment = paper | demo | mainnet
capital allocation    = per-user sizing limit
venue equity          = actual virtual/real balance held at Bybit
```

Normal Demo flow:

```text
Hyperliquid top-100 candidate
→ shared scalper strategy
→ exact Bybit active USDT linear contract resolution
→ per-user allocation sizing
→ Bybit quote spread/slippage/depth risk gate
→ Bybit Demo order
→ actual fill, fee, native SL, REST/WS reconciliation
```

## Environment Design

Do not create duplicate executors.

```text
BybitExecutor
├── Demo:    https://api-demo.bybit.com
└── Mainnet: https://api.bybit.com or valid regional endpoint
```

Use explicit environment values:

```text
paper
demo
mainnet
```

Do not call Demo Testnet. Do not mix Demo/Mainnet credentials, telemetry, fills, fees, PnL, or evidence cohorts.

## Per-User Capital Allocation

User enters allocation in IDR during onboarding.

Example:

```text
allocation_idr = Rp1.000.000
USD_TO_IDR    = 16.000
allocation_usd = $62.50
venue_equity   = $999.00 Demo balance
sizing_equity  = min($999.00, $62.50) = $62.50
```

Formula:

```python
sizing_equity = min(venue_equity, capital_allocation_idr / capital_fx_rate)
```

Semantic requirements:

```text
venue_equity       = actual Bybit balance
capital_allocation = user-selected maximum sizing capital
sizing_equity      = effective capital used by sizing/risk gates
available_margin   = venue execution constraint
```

Do not display allocation as Bybit account balance.

### Demo Virtual Fund Request

Bybit Demo supports a signed virtual-fund request endpoint:

```text
POST /v5/account/demo-apply-money
```

During Demo onboarding, after user confirms allocation, bot may request equivalent Demo USDT:

```text
Rp1.000.000
→ rate Rp16.000/USD
→ request 62.50 USDT Demo
```

Required safe flow:

```text
user inputs IDR allocation
→ bot calculates USD/USDT amount
→ bot displays amount and requests explicit confirmation
→ bot calls Demo endpoint once
→ bot re-reads authenticated wallet balance
→ bot persists allocation separately
→ sizing uses allocation, not entire wallet balance
```

Rules:

- Demo endpoint only; never call it for Mainnet or Testnet.
- Use stored Demo credential only after encrypted persistence, or protected in-memory onboarding state.
- Never include key, secret, signature, or auth payload in Telegram, logs, evidence, command arguments, or reports.
- Respect Bybit documented request rate limit; no blind retry.
- Validate USDT amount against Bybit documented request maximum and serialize decimals deterministically.
- If authenticated wallet readback is insufficient, block Demo activation and report a non-secret error.
- Lowering allocation changes only `capital_allocation`; do not automatically reduce virtual wallet balance.
- Each Telegram user must use separate Bybit Demo account/subaccount and credentials. Shared Demo keys destroy user sizing, telemetry, and order isolation.

Validation proposal:

- positive integer IDR amount;
- minimum Rp100.000;
- allocation cannot exceed venue equity equivalent;
- allocation modification blocked while exchange position is open;
- FX rate stored with allocation for audit/reproducibility.

## Persisted User Fields

Additive and idempotent migration:

```text
bybit_environment         = demo | mainnet | legacy_testnet
bybit_api_key             = Fernet encrypted
bybit_api_secret          = Fernet encrypted
capital_allocation_idr
capital_allocation_usd
capital_fx_rate
capital_updated_at
```

Existing `bybit_testnet` data must remain compatible during migration. Never infer or overwrite credential environment. Credentials remain encrypted.

For existing Paper users, default allocation proposal:

```text
paper_balance_usd × USD_TO_IDR
```

Do not backfill missing actual fills, fees, slippage, or stop telemetry.

## Telegram Onboarding

Target flow:

```text
/start
→ access code
→ choose Demo or Mainnet
→ input capital allocation IDR
→ allocation confirmation
→ environment-specific API key tutorial
→ API key message deleted
→ API secret message deleted
→ exact environment preflight
→ show venue equity and sizing equity separately
→ final environment confirmation
→ encrypted credential persistence and session creation
```

Demo API guidance:

1. Login to `www.bybit.com`.
2. Switch to Demo Trading.
3. Create Demo API key from Demo account.
4. Grant account read and contract trading only.
5. Do not grant withdrawal permission.
6. Fund Demo account.
7. Submit credentials only through Telegram `/live` flow.

Mainnet API guidance must retain explicit warnings and server acknowledgement gates.

## Shared Scalper Strategy

Do not copy Paper strategy config into Demo.

Extract shared strategy profile for:

```text
score threshold
entry confirmation
SL/TP levels
trailing
time exit
score risk tiers
user leverage cap
user max positions
```

Execution differences only:

```text
Paper:   simulated fill/PnL
Demo:    Bybit actual Demo fill, fee, native SL, REST/WS state
Mainnet: Bybit actual Mainnet fill, fee, native SL, REST/WS state
```

## Top-100 Universe

Required intersection:

```text
Hyperliquid top-100 candidate
∩ active Bybit LinearPerpetual USDT contract metadata
∩ exact alias mapping where needed
∩ spread/slippage/depth/risk gate
```

Never infer `asset + USDT`.

Reject:

- unknown Bybit symbol;
- inactive/pre-launch contract;
- invalid metadata;
- insufficient quote/depth;
- spread/slippage breach;
- user/environment risk cap breach.

Demo can use dynamic eligible universe after controls pass. Mainnet universe remains independently policy-controlled until sufficient evidence exists per asset/regime.

## Required Telemetry

Every candidate/trade must separate cohorts:

```text
execution_environment
strategy_profile
venue
venue_equity
capital_allocation_idr
capital_allocation_usd
sizing_equity
planned_entry_price
quote_mark_price
estimated_fill_price
actual_fill_price
spread_pct
estimated_slippage_pct
actual_slippage_pct
fee
planned_stop_loss
observed_native_stop_loss
quantity
leverage
masked orderLinkId
reconciliation_result
```

Never aggregate Paper simulated PnL, Demo exchange PnL, and Mainnet realized PnL as one performance cohort.

## Paper Deprecation Plan

Do not delete Paper now. Paper code contains shared strategy/risk paths and useful shadow/counterfactual capability.

### Stage A — Extract shared logic

Move shared strategy/risk calculations out of `PaperExecutor` before any removal.

### Stage B — Prove normal Demo path

Run full-auto Demo through normal bot path, not isolated drill tool.

### Stage C — Reference audit

Audit every caller/import/consumer of:

```text
PaperExecutor
paper_positions
paper_state
/paper
dashboard paper controls
paper history labels
reset logic
```

### Stage D — Decide retention

Preferred: retain Paper as explicit shadow/research mode, not primary user execution mode.

If operator explicitly requests full removal, delete only after all gates below pass.

### Stage E — Removal gates

- no open Paper positions;
- no imports/callers remain;
- Demo execution cohort passes;
- migration/restart tests pass;
- dashboard and Telegram tests pass;
- historical records remain labelled and readable;
- no unrelated deletion in final diff.

## Implementation Sequence

1. Audit schema, registration, scanner, session, dashboard, and Paper dependencies.
2. Add explicit per-user environment and capital allocation fields with migration.
3. Add centralized allocation/sizing-equity calculation.
4. Add Demo virtual-fund request client method plus confirmation/readback onboarding step.
5. Route Demo environment through normal Bybit session/client/WS/executor.
6. Add Telegram registration/onboarding flow.
7. Extract shared scalper strategy profile.
8. Add top-100 to Bybit active-contract intersection.
9. Extend telemetry/persistence with allocation and environment fields.
10. Add unit/integration migration, environment, allocation, Demo fund request, and full-auto path tests.
11. Run controlled real Demo automatic cohort.
12. Complete Paper dependency audit and make separate removal decision.

## Test Requirements

Unit:

- IDR allocation conversion;
- allocation less than/equal to venue equity;
- zero/negative/invalid allocation rejection;
- allocation update blocked with open position;
- Demo USDT request body, signing, one-request behavior, and wallet readback;
- Mainnet/Testnet rejection for Demo fund request;
- Demo/Mainnet endpoint selection;
- credential-environment mismatch;
- top-100 exact contract intersection;
- unsupported symbol rejection;
- user leverage/risk caps;
- spread/slippage/depth rejection;
- full-auto route.

Integration:

- registration/access-code to Demo onboarding;
- deleted Telegram credential messages;
- encrypted persistence;
- Demo preflight;
- session restart;
- signal to supported Bybit symbol;
- fill/fee/native SL persistence;
- WS reconnect;
- close-all;
- environment switch blocked while position is open.

Real Demo acceptance proposal:

```text
20 automatic Demo lifecycles
0 duplicate create
0 entry left without native SL
0 unresolved final position for intentionally closed lifecycle
0 credential leak
100% environment-labeled evidence
```

## Risks and Controls

| Risk | Control |
| --- | --- |
| Top-100 includes illiquid asset | Bybit active metadata plus quote, spread, slippage, and depth gates |
| Hyperliquid/Bybit symbol mismatch | Exact registry resolution and explicit aliases only |
| Allocation confused with balance | Separate venue equity, allocation, and sizing-equity labels |
| Allocation changes mid-position | Reject update while position open |
| Demo/Mainnet key mix-up | Environment-persisted credential and preflight/confirm/session consistency checks |
| Paper removal deletes strategy behavior | Extract shared strategy first; delete only after dependency gates |
| Cohort mixing | Environment field required in telemetry and reports |
| Full-auto orders after deployment | Session still requires environment-specific credential preflight and final `/live` confirmation |

## Non-Goals

- No strategy threshold, SL/TP, or indicator parameter change in this plan.
- No Mainnet deployment, restart, credential submission, or order.
- No deletion of Paper code until Demo normal-path evidence and dependency audit pass.
