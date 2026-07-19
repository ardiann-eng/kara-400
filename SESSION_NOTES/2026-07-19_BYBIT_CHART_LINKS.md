# Bybit Chart Links in Telegram Position Messages

Date: 2026-07-19

## Requirement and Evidence

- Operator requires every Demo/Mainnet position message to provide a direct Bybit chart link.
- Position messages previously linked to Hyperliquid even when execution was on Bybit.
- Scanner asset names are not always Bybit symbols. Example alias `kBONK` resolves to `1000BONKUSDT`; URL construction from asset name would be incorrect.

## Change

- Added a Bybit chart URL helper that resolves the exact active venue symbol through `BybitSymbolRegistry` before producing `https://www.bybit.com/en/trade/usdt/<symbol>`.
- Added environment-labelled chart buttons: `Chart Bybit Demo` and `Chart Bybit Live`.
- Added chart links to Bybit position-open messages, active `/pos` monitoring messages, TP1/TP2 lifecycle messages, and final-close messages beside PnL Card.
- The Bybit public market chart URL is shared between Demo and Mainnet. Label identifies execution environment; the link itself does not expose account state or submit an order.
- No link is generated when exact Bybit metadata cannot resolve; code never guesses a symbol.

## Verification

```text
68 passed
python -m pytest tests/test_bybit_telegram_safety.py tests/test_demo_capital_onboarding.py tests/test_bybit_executor.py tests/test_bybit_client.py -q

python -m py_compile notify/telegram.py
git diff --check
```

Regression tests cover exact alias resolution and Demo/Mainnet chart labels.

## Deployment and Monitoring

- No deployment, restart, commit, credential access, or order.
- After deploy, verify one Demo and one Mainnet-labelled notification opens the exact Bybit symbol chart. Check an alias asset separately.
