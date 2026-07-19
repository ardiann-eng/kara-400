# Demo Capital Onboarding Implementation

Date: 2026-07-19

## Evidence and Root Cause

- User persistence represented Bybit environment only as `bybit_testnet: bool`; it could not distinguish Paper, Demo, Mainnet, or protect Demo/Mainnet credential routing.
- Live sizing passed full authenticated venue equity to `RiskManager` and `BybitLiveRiskGate`; a user allocation could not limit sizing.
- Existing Demo REST/WS support was available in `BybitClient` and `BybitPrivateWebSocket`, but normal Telegram onboarding remained server Testnet/Mainnet-only.

## Implementation

- Added `ExecutionEnvironment`: `paper`, `demo`, `mainnet`, `legacy_testnet`. Legacy boolean records remain readable without overwrite.
- Added persisted allocation fields: `capital_allocation_idr`, `capital_allocation_usd`, `capital_fx_rate`, `capital_updated_at`.
- Added deterministic IDR-to-USD conversion and reject rules: positive integer, minimum Rp100.000, allocation must not exceed authenticated venue equity, no change with open venue position.
- `/start` access-code success now requires Demo/Mainnet selection, allocation entry, allocation confirmation, environment API tutorial, deleted credential messages, exact preflight, encrypted persistence, and final environment confirmation.
- Demo fund request uses only `POST /v5/account/demo-apply-money`, compact deterministic decimal body, no retry, Demo-only fail-closed guard, then authenticated wallet readback.
- Demo uses `BybitExecutor`, `BybitLiveRiskGate`, `BybitClient(demo=True)`, and `BybitPrivateWebSocket(demo=True)`. No `DemoExecutor` added.
- Bybit sizing and live risk gate consume `sizing_equity=min(venue_equity, allocation_usd)` when allocation exists. Telemetry records venue equity, allocation, and sizing equity separately.
- Fixed live close persistence gap: final Bybit Demo/Mainnet lifecycle now writes `trade_history` with environment, venue, venue equity, allocation, sizing equity, actual final fill, aggregate entry+exit fee, planned stop, size/leverage, and strategy profile. Paper records remain separately labelled.
- Added exact Demo universe helper. It retains Hyperliquid candidates only when active Bybit linear-USDT metadata resolves through exact symbol/explicit alias mapping.
- Wired Demo-only exact metadata gate before per-user signal selection. Paper research scans and Mainnet universe policy remain unchanged.
- Paper retained. Source dependency audit confirms `core/user_session.py` still imports `PaperExecutor`; `notify/telegram.py` retains `/paper` handler.

## Verification

Focused regression suite:

```text
81 passed
python -m pytest tests/test_demo_capital_onboarding.py tests/test_bybit_client.py tests/test_bybit_executor.py tests/test_bybit_executor_http_lifecycle.py tests/test_bybit_live_risk.py tests/test_bybit_observability.py tests/test_bybit_persistence.py tests/test_bybit_private_ws.py tests/test_bybit_telegram_safety.py tests/test_user_session_bybit.py tests/test_paper_dependency_audit.py -q
```

Follow-up focused suite:

```text
84 passed
python -m pytest tests/test_demo_capital_onboarding.py tests/test_bybit_client.py tests/test_bybit_executor.py tests/test_bybit_executor_http_lifecycle.py tests/test_bybit_live_risk.py tests/test_bybit_observability.py tests/test_bybit_persistence.py tests/test_bybit_private_ws.py tests/test_bybit_telegram_safety.py tests/test_user_session_bybit.py tests/test_paper_dependency_audit.py -q
```

No deployment, restart, commit, credential access, virtual-fund request, or order.

## Monitoring and Deletion Gates

- Monitor cohort-separated Demo candidate count, exact metadata rejection, allocation/venue/sizing equity, risk rejection, fill, fee, native SL, and reconciliation results.
- Do not delete `execution/paper_executor.py`, `/paper`, `paper_positions`, `paper_state`, reset paths, or Paper history before 20 automatic Demo lifecycles meet stated plan gates and a full import/caller/database consumer audit reports zero required dependencies.
- Mocked Telegram Demo credential flow covers secret deletion, Demo client selection, single funding call, readback, final environment persistence, and allocation persistence. Remaining gap: deployment-process Telegram runtime and real Demo cohort.
- Operator decision: Demo replaces Paper as primary user execution onboarding. Access-code success now starts Demo allocation directly; `/demo` migrates existing Paper users only after positions are verified closed. Paper executor/tables/history remain retained as explicit shadow/research mode. Existing users are never silently switched because each Demo session needs separate Demo credentials.
- Operator follow-up: existing legacy Paper users are blocked from all new signal/manual entries and directed to `/demo` at `/start`. Existing Paper positions remain monitored for TP/SL/close; they are not discarded or force-closed. `/demo` refuses migration until positions are closed.
- Added database evidence for Demo rejection and partial exits. `execution_candidates` stores Demo unsupported-metadata and Bybit risk-gate rejections with observed reason/cohort inputs. Each actual partial close stores separate slice PnL/fee and cumulative PnL; final close retains original position ID and `fully_closed=true`.
- Full `KaraBot._handle_signals()` runtime test is blocked locally by missing `hyperliquid-python-sdk`; no dependency installation performed. Exact resolver and source-order contract remain covered. This is a test-environment gap, not a passing runtime claim.
