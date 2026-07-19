# Bybit Execution Universe Policy

Date: 2026-07-19

## Symptom and Evidence

- Railway logged `2026-07-19 10:46:57,131 [kara.bybit_exec] WARNING: Bybit live entry rejected for ARB: asset_not_allowlisted`.
- Scanner processes the Hyperliquid top-100 candidate universe, while Bybit live risk config defaulted to `BYBIT_LIVE_ASSET_ALLOWLIST=BTC,ETH`.
- The live risk gate rejected assets outside that list before quote validation or order submission. ARB therefore could never execute despite being scanned.

## Change

- Removed `BYBIT_LIVE_ASSET_ALLOWLIST` config, startup validation, session telemetry, and `asset_not_allowlisted` gate.
- Demo and Mainnet may execute any scanner candidate only when exact active Bybit linear-USDT metadata resolves. Demo has explicit exact metadata gating; execution still resolves venue metadata before order creation.
- Retained all non-universe protections: signal/quote freshness, spread, VWAP slippage, order-book depth, leverage, position count, per-trade risk, symbol and total notional, total open risk, circuit breaker, native hard SL, and REST reconciliation.

## Verification

```text
67 passed
python -m pytest tests/test_bybit_live_risk.py tests/test_bybit_executor.py tests/test_startup_validation.py tests/test_user_session_bybit.py tests/test_demo_capital_onboarding.py -q

python -m py_compile config.py core/startup_validation.py core/user_session.py execution/live_risk_gate.py execution/bybit_executor.py main.py
git diff --check
```

## Deployment and Monitoring

- No deployment, restart, commit, config change on Railway, credential access, or order.
- Measure executed/rejected candidates by asset, metadata rejection, spread/slippage/depth rejection, fills, native-SL presence, and final reconciliation separately by Demo/Mainnet cohort.
- Stop automatic entries if unsupported-symbol resolution reaches order path, native SL is absent, duplicate order occurs, or reconciliation fails.
