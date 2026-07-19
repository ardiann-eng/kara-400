# Phase 10 Risk Audit Fixes

Date: 2026-07-19

## Scope

Re-audited current live risk path after conflicting historical notes said Phase 10 was both skipped and completed. Code confirms adjusted Phase 10 controls exist: Bybit allowlist, leverage/position/risk/notional caps, stale signal/quote, spread, VWAP slippage, depth, fail-closed market data, circuit breaker, native SL recovery, and secret-free telemetry.

## Findings and Fixes

### P1: Mainnet could start with full automation

`validate_startup_config()` required mainnet acknowledgement and testnet lock removal but did not reject `FULL_AUTO=true`.

Fix: mainnet Live startup now fails with `Bybit mainnet requires KARA_FULL_AUTO=false` whenever `FULL_AUTO=true`. Testnet behavior is unchanged. This enforces manual-only staged mainnet even after acknowledgement.

### P1: Kill-switch reset authorization/persistence was shadowed

`RiskManager.reset_kill_switch` had two definitions. The later no-argument definition overrode the earlier admin-authorized, persistent reset.

Fix: removed later duplicate. Remaining method requires `requester_id`, compares it to `ADMIN_CHAT_ID` or `TELEGRAM_CHAT_ID`, clears switch, and persists state. Non-admin reset raises `PermissionError`.

## Verification

```text
62 passed
python -m py_compile core/startup_validation.py risk/risk_manager.py
git diff --check passed
```

New regression assertions:

- acknowledged mainnet with testnet lock disabled and `FULL_AUTO=true` fails startup;
- non-admin cannot reset kill switch;
- admin reset persists `kill_switch: false`;
- only one `reset_kill_switch` definition remains.

## Deployment Status

No deploy, restart, commit, full-auto activation, Testnet order, or mainnet order.

## Remaining Risk

- Current Phase 10 limits remain aggressive historical scalper limits: 20x leverage, 3.5% risk/trade, three positions, and 21x total notional. They are not suitable proposed limits for a mainnet micro-pilot.
- Mainnet remains prohibited pending Testnet evidence, Phase 16 whole-flow audit, and explicit user request for staged rollout.
- Local config introspection could not run under Windows `cp1252` because `config.py` emits emoji at import. This is a local diagnostics portability defect, not evidence that startup validation is bypassed.
