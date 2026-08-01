# Duplicate User Session Unknown Position Race

Date: 2026-08-01

## Incident Evidence

- One Demo user activated while the five-way scanner was running.
- TRUMP LONG signal at 15:08:16 was followed by one `defers in-flight entry` log, four identical `unexpected_position` alerts, three failed extra emergency closes (`110017 current position is zero`), four vanished-position records, and no readable close PnL.
- HYPE LONG at 15:09:14 produced one in-flight deferral and four identical unknown-position alerts. Later five vanished-position records appeared.
- AAVE LONG at 15:27:16 again produced one in-flight deferral and four identical unknown-position alerts.
- Stable 1+4 pattern across three symbols disproves a single-executor alert-cooldown explanation. One executor owned the entry; four additional executors independently reconciled the same Bybit account.

## Root Cause

- `KaraBot.get_session()` used check-then-create without a per-user lock.
- Demo activation called `close_user_session()` and then `get_session()` as separate operations.
- `close_user_session()` removed the Paper session from `self.sessions` before the new Live session was initialized.
- Scanner tasks run with `Semaphore(5)` and call `get_session()` concurrently. During the empty registry gap, multiple tasks each created a `UserSession`, `BybitExecutor`, REST client, private WebSocket, alert manager, and reconciliation loop for the same user credential.
- Only the last completed session remained in `self.sessions`; earlier sessions became unreachable but their private WebSocket tasks stayed alive.
- The registry-owned executor marked its entry symbol in flight. Orphan executors had no such local state, classified the exchange position as unknown, and emergency-closed it.
- Regression entered in commit `6d54bfb` (2026-07-14), which added separate close/recreate on activation and per-user Bybit private sessions while retaining unlocked `get_session()`.

## Why Earlier Behavior Differed

- Unlocked `get_session()` existed before Bybit Demo, but Paper sessions did not own independent authenticated Bybit private WebSockets or unknown-position emergency-close logic.
- The destructive effect appeared only after mid-scan Paper-to-Demo activation created multiple live session owners for one exchange account.

## Local Fix

- Add `SessionRegistry` with per-user asyncio lock.
- Make get-or-create idempotent under concurrency.
- Add atomic replace operation so old session close and new session creation expose no empty registry gap.
- Demo/Mainnet activation and rollback use atomic replacement.
- Startup persistence loading deduplicates same symbol+side rows, preferring original KARA position over `exchange_recovery_unknown` copies and deleting duplicate recovery rows.

## Verification

```text
117 passed
python -m pytest tests/test_session_registry_concurrency.py tests/test_bybit_telegram_safety.py tests/test_demo_capital_onboarding.py tests/test_user_session_bybit.py tests/test_bybit_executor.py tests/test_bybit_observability.py -q

python -m py_compile core/session_registry.py main.py notify/telegram.py execution/bybit_executor.py tests/test_session_registry_concurrency.py tests/test_bybit_executor.py
git diff --check
```

- Regression test launches five concurrent session reads and proves exactly one build.
- Atomic replacement test holds old-session close open while four readers arrive; all readers receive the single new session.
- Persistence test proves one original position survives and duplicate recovery rows are removed.

## Deployment and Monitoring

- Not deployed, restarted, committed, or pushed.
- Production remains unsafe for new Demo entries until deployment restart kills orphan WebSocket sessions.
- After deployment, required evidence: one session build per user, zero repeated `unexpected_position` for KARA entries, zero `110017` emergency-close race, zero duplicate persisted symbol+side rows, and readable close PnL.
- Roll back if startup cannot create the user session or if atomic replacement prevents Telegram activation recovery.
