# Demo Wallet Readback Delay Fix

Date: 2026-07-30

## Symptom and Root Cause

- Demo setup stopped after `POST /v5/account/demo-apply-money` because the immediate authenticated wallet readback differed from the requested virtual USDT target by more than $0.01.
- Credential was not persisted and no order was made. That behavior was correct fail-closed behavior.
- The prior client read the wallet exactly once after the fund request. Bybit documentation limits the fund endpoint to one request per minute. Retrying the fund request would risk duplicate adjustment or rate-limit rejection.
- Bybit official Demo documentation confirms the endpoint and one-request-per-minute limit. It does not guarantee immediate wallet readback consistency.

## Change

- After one successful Demo fund request, `BybitClient.set_demo_usdt_balance()` now reads the wallet at most six times: immediately, then once per second for up to five seconds.
- It returns only when wallet balance is within $0.01 of target.
- It never repeats `POST /v5/account/demo-apply-money` during polling.
- If readback remains different, setup stays blocked. Operator text says to wait at least one minute and resend `API_KEY,API_SECRET`, not only an API key.

## Verification

```text
65 passed
python -m pytest tests/test_demo_capital_onboarding.py tests/test_bybit_client.py tests/test_bybit_credential_input.py tests/test_bybit_telegram_safety.py -q

python -m py_compile data/bybit_client.py notify/telegram.py tests/test_demo_capital_onboarding.py
git diff --check
```

- Regression test simulates stale first readback followed by matching second readback. It proves one one-second wait and exactly one fund endpoint call.

## Deployment and Monitoring

- Not deployed, restarted, committed, pushed, or tested against a real Demo account.
- After deployment, measure count of Demo funding attempts, readback attempt number, elapsed readback time, final target difference, and failures. Do not record credentials.
- Keep setup blocked when balance differs after five seconds. Investigate Bybit response, wallet state, and active positions before widening wait or changing target logic.
