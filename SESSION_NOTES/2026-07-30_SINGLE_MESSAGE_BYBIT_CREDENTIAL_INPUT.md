# Single-Message Bybit Credential Input

Date: 2026-07-30

## Requirement

- Replace two-message API Key then API Secret capture with one Telegram message.
- Accepted format: `API_KEY,API_SECRET`.

## Security Contract

- Incoming credential message is deleted before parsing or validation response.
- Parser requires exactly two comma-separated non-empty values, each at least eight
  characters after trimming.
- Missing comma, extra comma, empty part, or short part is rejected.
- Neither credential is logged.
- Both values remain only in conversation memory during preflight.
- Credentials are persisted only after preflight passes and operator presses the
  existing activation button.
- Password, OTP, recovery code, and seed phrase warning remains visible.

## Changes

- Added pure `parse_bybit_credentials()` parser.
- Replaced credential conversation handlers with `handle_bybit_credentials()`.
- Removed unused second credential conversation state.
- Extracted existing preflight flow into `_preflight_bybit_credentials()` without
  changing environment checks, Demo balance setup, permission validation, encryption,
  confirmation, or activation behavior.
- Updated Demo/Testnet/Mainnet onboarding copy and failure retry wording.

## Verification

- Focused credential/onboarding/Telegram safety tests: `48 passed`.
- Full suite: `273 passed`.
- `py_compile` passed.
- `git diff --check` passed; only existing Windows line-ending warnings emitted.

## Deployment

- Not deployed, restarted, committed, or pushed.
