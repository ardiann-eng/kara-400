# Telegram Demo Onboarding UX and Preflight Recovery

Date: 2026-07-19

## Symptom and Evidence

- Operator Demo onboarding reached `Bybit returned non-JSON HTTP 403` after API secret capture.
- Prior Telegram message exposed only the raw exception and ended the flow. Operator had no clear distinction between credential failure and a server-to-Bybit access block.
- Bybit official Demo documentation requires switching a mainnet account into Demo Trading and creating an independent Demo API key. It specifies `https://api-demo.bybit.com` and private stream `wss://stream-demo.bybit.com`.
- Source audit found Demo setup labels used `Live`, omitted official API documentation link, and did not preserve allocation/retry state after preflight failure.

## Changes

- `/demo` now presents numbered onboarding steps and explicitly states allocation is a KARA sizing cap, not Bybit balance or a real-money transfer.
- Credential tutorial links to Bybit homepage and official Demo API documentation. It names required permissions, forbids withdrawal permission, and rejects Testnet/Mainnet key use for Demo.
- API key/secret capture states state deletion behavior and prohibit password, OTP, recovery code, and seed phrase submission.
- Preflight announces its checks before network calls.
- `HTTP 401/403` non-JSON preflight errors now state that endpoint access was blocked before credential validation. It does not assert key/secret are wrong, clears temporary key/secret, keeps allocation/environment, and returns to API-key capture.
- Successful confirmation button removes false `Live` label for Demo.
- `/status` renders Bybit environment and, when configured, allocation and active sizing equity without credential fields.

## Verification

```text
31 passed
python -m pytest tests/test_demo_capital_onboarding.py tests/test_bybit_telegram_safety.py -q

python -m py_compile notify/telegram.py
git diff --check
```

Regression coverage includes Demo official guide link, `HTTP 403` action text, retry state retaining allocation, deleted secret message, and Demo status allocation display.

## Deployment and Monitoring

- No deployment, restart, commit, credential access, virtual-fund request, or order.
- After deployment, retry one Demo onboarding with one Demo key only. Observe server log category and status code without credential material.
- If Railway still receives non-JSON 403, block Demo activation and treat server-to-Bybit endpoint access as unresolved. Do not ask operator to regenerate credentials without a Bybit API auth/permission response.
