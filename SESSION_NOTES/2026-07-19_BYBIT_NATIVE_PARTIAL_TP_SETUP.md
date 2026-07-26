# Bybit Native Partial TP Setup

Date: 2026-07-19

## Evidence and Decision

- Bybit Demo UI showed `TP / SL` as `-- / <price>` for open LDOUSDT and NEOUSDT positions: native SL existed, native TP did not.
- Entry code had calculated/persisted `tp1` and `tp2`, but called `set_protection()` with only `stop_loss`.
- KARA policy is TP1 25% initial quantity, TP2 50% of remainder, then trailing runner. Native full TP2 would change this lifecycle and was rejected.
- Bybit V5 Partial TP requires a paired partial SL with equal `tpSize`/`slSize`. Full-position SL remains installed separately as primary hard protection.

## Implementation

- Added `ExecutionClient.add_partial_tp_sl()` and Bybit V5 implementation using `tpslMode=Partial`, exact equal target quantities, MarkPrice triggers, and Market orders.
- New entries install full native SL first, then native TP1/SL pair and TP2/SL pair.
- Quantities derive from actual entry fill and venue quantity step: TP1 25%, TP2 50% of remainder. Invalid/zero target quantities fail closed by emergency-closing entry.
- `Position` persists native target state and target quantities. Existing recovered exchange positions remain `none`; no TP is invented for them.
- Local price polling cannot submit duplicate TP1/TP2 reduce-only closes when native targets are `armed` or require reconciliation.
- If exchange size falls while targets are armed, KARA marks `reconciliation_required` and alerts. It does not infer whether change was TP1, TP2, paired partial SL, or manual exchange action.

## Known Limitation / Deployment Gate

- Bybit `set trading stop` response provides no target order IDs. This change does not yet attribute native TP fills into KARA slice PnL, TP1/TP2 notification, break-even move, or trailing lifecycle.
- Therefore native target placement is implemented, but full target-fill lifecycle is **not ready to deploy**. Deploying now would create native TP protection but leave KARA lifecycle state unresolved after any native target fill.
- Next implementation: retrieve and persist exact conditional order IDs after install, reconcile exact execution fills after WS/restart, persist one actual-fill slice once, then update full SL only after confirmed TP1.

## Verification

```text
56 passed
python -m pytest tests/test_bybit_client.py tests/test_bybit_executor.py tests/test_bybit_executor_http_lifecycle.py tests/test_bybit_private_ws.py tests/test_user_session_bybit.py -q

python -m py_compile data/bybit_client.py execution/exchange_client.py execution/bybit_executor.py models/schemas.py tests/test_bybit_client.py tests/test_bybit_executor.py tests/test_bybit_executor_http_lifecycle.py
git diff --check
```

- No deployment, restart, commit, credential access, or order.
