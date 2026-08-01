# Demo Fund Request Accepted Without Balance Mutation

Date: 2026-08-01

## Scope

- Railway production log audit, 2026-08-01 14:11:38 server log time.
- One authenticated Bybit Demo capital-onboarding attempt. `n=1`; this proves the observed failure mechanism only, not its frequency across accounts.

## Evidence

```text
target_usdt=62.5
before_wallet_usdt=32.65415543
before_equity_usdt=32.6225462
before_available_usdt=32.6225462
before_used_margin_usdt=0.0
adjustment=add
amount_usdt=29.85
readback_wallet_usdt=32.65415543 (six reads over five seconds)
open_position_count=0
POST /v5/account/demo-apply-money: HTTP 200, ret_code=0, ret_msg="", ret_ext_info={}
```

## Root Cause

- KARA computed `62.50 - 32.65415543 = 29.84584457`, rounded the documented USDT request amount to `29.85`, and sent documented `adjustType=0` add payload.
- Expected post-request wallet was `62.50415543`, within KARA's `$0.01` target tolerance.
- Six authenticated wallet reads stayed exactly at pre-request wallet value. No position or used margin could explain the missing amount.
- Authentication succeeded and Bybit accepted the funding request syntactically, but Bybit Demo did not apply the requested wallet mutation within five seconds.
- Root cause boundary: exchange-side Demo fund application non-mutation or delayed processing beyond observed window. Existing data cannot distinguish those two mechanisms. It is not credential failure, active-position usage, local target arithmetic, repeated rate-limited request, or KARA accepting a stale balance.

## Decision

- Keep onboarding fail-closed. Do not save credential or start trading with `$32.62` when operator selected `$62.50`.
- Do not retry fund endpoint before one minute. Official Demo documentation specifies one request per minute.
- Do not widen delay or bypass exact balance check without a later authenticated read proving whether mutation appears after five seconds.

## Follow-up Diagnostic Change

- On future mismatch only, KARA makes one read-only `GET /v5/account/transaction-log` for USDT around request time.
- It logs `DEMO_FUNDING_ROOT_CAUSE` with one evidence-bound classification:
  - `ledger_records_requested_demo_fund_wallet_readback_stale`
  - `ledger_records_different_usdt_change`
  - `ledger_has_no_requested_demo_fund_credit`
  - `bybit_accepted_fund_request_without_usdt_ledger_record`
  - `insufficient_evidence_transaction_log_unavailable`
- It logs only transaction time/type/currency/change/cash-flow/fee/bonus/cash balance. Transaction IDs, order IDs, credential, signature, and auth headers remain excluded.
- Classification identifies observable API state only. It cannot label Bybit's internal cause such as region policy or backend defect without exchange evidence.

## Confirmed Bybit Rejection and Local Fix

Production attempt after diagnostic deployment, 2026-08-01 14:46:24:

```text
HTTP 200 / retCode=0
result.orderStatus=FAIL
result.resultCode=3410020
result.retMsg=Your quantity of deposit request for USDT has error ... precision 1
requested amount=29.85 USDT
transaction-log USDT rows=[]
wallet readback unchanged for six reads
```

- Root cause confirmed: Bybit Demo requires USDT `amountStr` at one decimal precision. KARA sent `29.85`, which Bybit rejected in nested `result` despite outer `retCode=0`.
- Local source fix changes Demo fund step from `$0.01` to `$0.10`, target tolerance from `$0.01` to `$0.05`, and fails immediately on nested `result.orderStatus=FAIL` with Bybit `resultCode` and safe message.
- Focused regression suite: `73 passed`. Local fix is not deployed as of this note update.

## Deployment and Next Measurement

- Deployed Railway production deployment `7aff4a88-33a9-4adb-baa5-7839f64a992b`, status `SUCCESS`, at 2026-08-01 14:33:44 UTC. Startup completed and Telegram started.
- Deployment archive used clean `HEAD` plus only `data/bybit_client.py` Demo diagnostic overlay. Unrelated local edits and local database were excluded.
- No order was made. No commit or push.
- Next safe diagnostic: wait one minute, submit one Demo onboarding attempt, then inspect `DEMO_WALLET_READBACK_MISMATCH`, `DEMO_FUNDING_RESPONSE`, `DEMO_FUNDING_TRANSACTION_LOG`, and `DEMO_FUNDING_ROOT_CAUSE`. If wallet never changes, send Bybit support trace ID plus timestamp; do not include credential.
