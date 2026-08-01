# Demo Wallet Mismatch Diagnostics

Date: 2026-07-30

## Evidence Boundary

- A Demo setup reached authenticated wallet read, authenticated position read, and `POST /v5/account/demo-apply-money`, then failed because six wallet readbacks differed from target.
- This proves neither malformed credentials nor rejected authentication as root cause.
- It does not prove why the target and wallet differed. Prior logs omitted target, balances, margin, adjustment direction, and position count.

## Change

- On Demo wallet mismatch only, KARA logs safe numeric diagnostics: target, initial wallet/equity/available/margin, each wallet readback, final equity/available/margin, open-position count, and add/reduce amount.
- The diagnostic never logs API key, API secret, signed request, raw HTTP headers, chat ID, or raw exchange position data.
- The fund request remains exactly once. Five one-second wallet polls remain read-only.

## Verification

```text
66 passed
python -m pytest tests/test_demo_capital_onboarding.py tests/test_bybit_client.py tests/test_bybit_credential_input.py tests/test_bybit_telegram_safety.py -q

python -m py_compile data/bybit_client.py tests/test_demo_capital_onboarding.py
git diff --check
```

- Regression test asserts one fund request, one position-list request only after mismatch, required numeric fields, and absence of API key/secret strings in logs.

## Deployment and Next Measurement

- Deployed Railway production deployment `da983d5b-0147-4154-9f9e-06bb6d583b37`, status `SUCCESS`.
- Operator must wait at least one minute, verify/cancel any Demo open positions or orders in Bybit, then attempt `/demo` once using `API_KEY,API_SECRET`.
- Inspect one `DEMO_WALLET_READBACK_MISMATCH` log record. Determine root cause only from target, readbacks, margin, and position count. Do not alter balance rules before that evidence.
