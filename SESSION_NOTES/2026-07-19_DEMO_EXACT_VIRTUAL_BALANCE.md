# Demo Exact Virtual Balance

Date: 2026-07-19

## Decision and Evidence

- Operator requires Demo input `Rp1.000.000` to mean Demo trading balance around Rp1.000.000, not a separate sizing cap while the Demo wallet holds a larger balance.
- Bybit Demo API documents `POST /v5/account/demo-apply-money` with `adjustType: 0` to add and `adjustType: 1` to reduce virtual funds. It permits USDT and limits the endpoint to one request per minute.
- Existing client only added virtual funds. It could not make an existing larger Demo wallet equal the requested capital.

## Change

- Demo onboarding now treats entered IDR as the target virtual USDT balance.
- Client reads current Demo wallet, issues exactly one add or reduce request when balance differs, then requires authenticated wallet readback within $0.01 of target.
- Demo execution uses full actual Demo equity. It no longer applies legacy allocation fields as a second cap.
- Demo activation clears legacy allocation fields. Mainnet remains unchanged: it cannot modify real venue balance and keeps allocation as a sizing limit.
- Telegram wording now calls this `saldo Demo untuk trading` and states virtual funds only; no real-money transfer.

## Verification

```text
77 passed
python -m pytest tests/test_demo_capital_onboarding.py tests/test_bybit_client.py tests/test_bybit_executor.py tests/test_bybit_telegram_safety.py tests/test_user_session_bybit.py tests/test_bybit_live_risk.py -q

python -m py_compile data/bybit_client.py execution/bybit_executor.py main.py notify/telegram.py
git diff --check
```

Tests cover reducing an existing $100 Demo wallet to $62.50, exact readback, Demo full-equity sizing, Mainnet allocation sizing, and no virtual-fund endpoint outside Demo.

## Deployment and Monitoring

- No deployment, restart, commit, credential access, virtual-fund request, or order.
- Before activation after deploy, require Demo wallet readback equal target. Do not retry automatic fund adjustment because Bybit documents one request per minute.
- Stop Demo setup on failed readback, open position, or API adjustment failure. No credential persistence or execution activation occurs before confirmation.
