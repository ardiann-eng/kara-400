# Bybit Entry/Reconciliation Race and Order-Link Limit

Date: 2026-07-19

## Incident Evidence

- Railway log recorded LDO LONG signal at 12:16:19 UTC and executor return at 12:16:21 UTC.
- Telegram then reported unknown exchange `LDOUSDT` and missing native hard SL before position-open notification.
- Detached private-WS reconciliation tasks raised `BybitAPIError 10001: order link id is longer than 45` while attempting emergency close of the unknown position.
- A third detached reconciliation task raised `BybitAPIError 34040: not modified` during native SL update.
- Mechanism: private WS fill/position callbacks scheduled concurrent forced reconciliations before entry had registered local position state and installed its initial native SL. Reconciliation classified known in-flight venue position as unknown, then attempted emergency close.
- Post-incident persisted DB check showed no LDO row. It currently contains `ARBUSDT` LONG and `LTCUSDT` LONG as `open_protected`; this does not prove LDO exchange state. Operator must check Demo UI directly.

## Local Change

- Mark entry symbol in-flight before order handling and defer reconciliation for that symbol until local registration/native SL lifecycle completes.
- Serialize reconciliation calls with one async lock.
- Generate compact emergency-close link IDs independent of arbitrary reason text.
- Reject any client order link ID longer than Bybit documented 45-character cap before network request.
- Treat Bybit `34040 not modified` as idempotent success only for `set_protection`.
- Catch/log detached private-WS callback reconciliation failures rather than leave unhandled task exceptions.

## Verification

```text
52 passed
python -m pytest tests/test_bybit_client.py tests/test_bybit_executor.py tests/test_bybit_executor_http_lifecycle.py tests/test_user_session_bybit.py tests/test_bybit_private_ws.py -q

python -m py_compile data/bybit_client.py execution/bybit_executor.py core/user_session.py
git diff --check
```

Regression tests cover in-flight entry reconciliation deferral, compact emergency link IDs, 46-character link-ID rejection, and protection code 34040 idempotency.

## Deployment Status and Stop Conditions

- No deployment or restart for this incident fix.
- Do not open new Demo orders until operator confirms LDOUSDT, ARBUSDT, and LTCUSDT exchange UI state plus native SL presence.
- Deployment requires operator approval. Post-deploy proof requires a minimum protected Demo lifecycle with WS callbacks enabled: no unknown-position alert during entry, native SL verified, and no unhandled task exception.
