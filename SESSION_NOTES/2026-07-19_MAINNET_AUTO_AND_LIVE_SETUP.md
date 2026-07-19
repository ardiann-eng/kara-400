# Mainnet Auto and `/live` Setup

Date: 2026-07-19

## Operator Decision

Operator explicitly requested mainnet automatic scalper execution. Demo Trading evidence is accepted as rollout-policy substitute for unavailable usable Testnet funding. Demo records remain labelled Demo and do not prove mainnet equivalence.

## Implementation

- Mainnet full-auto requires all of these exact server settings:

```env
KARA_TRADE_MODE=live
BYBIT_TESTNET=false
BYBIT_TESTNET_ONLY=false
BYBIT_MAINNET_ACK=I_UNDERSTAND_BYBIT_MAINNET_RISK
KARA_FULL_AUTO=true
BYBIT_MAINNET_AUTO_ACK=I_UNDERSTAND_BYBIT_MAINNET_AUTO_RISK
```

- Missing or wrong `BYBIT_MAINNET_AUTO_ACK` blocks startup.
- Mainnet acknowledgement and auto acknowledgement are separate, preventing accidental auto activation from a normal mainnet configuration.
- `/live` now selects Testnet/Mainnet from immutable server config during credential preflight, labels its prompt/confirmation with that exact environment, and persists the preflight environment.
- Confirm callback rejects if server environment changes between preflight and confirmation.
- `UserSession` rejects a stored credential environment differing from server `BYBIT_TESTNET`; a Testnet key cannot silently route to mainnet or vice versa.

## Credential Rules

- Create API key in the same environment selected by server config.
- Permissions: account read and contract trading only.
- No withdrawal permission.
- Use an IP whitelist if available and ensure server public IP is included.
- Never submit key/secret in command-line arguments, `.env`, Git, screenshots, or chat.
- `/live` accepts key/secret through Telegram, deletes messages, preflights credentials, then stores encrypted credentials only after final confirmation.

## Verification

```text
65 passed
python -m py_compile config.py core/startup_validation.py core/user_session.py notify/telegram.py
git diff --check passed
```

Tests cover separate mainnet auto acknowledgement, user/server environment mismatch rejection, `/live` reactivation, and preflight/confirmation environment mutation rejection.

## Deployment Status

No `.env` change, deployment, restart, commit, mainnet credential submission, or mainnet order.

## Residual Risk

- Current live hard caps retain user-selected scalper settings: 20x, three positions, 3.5% per trade, 10.5% total open risk, and 21x total notional.
- `KARA_FULL_AUTO=true` can now send real mainnet orders after deployment/restart and `/live` confirmation. This is operator-requested behavior.
- Mainnet fill, fees, slippage, outages, and credential/account behavior remain unobserved until real use.
- Immediate stop conditions: duplicate create, no native SL after entry, failed emergency close, unresolved position, missing credential isolation, or reconciliation failure.
