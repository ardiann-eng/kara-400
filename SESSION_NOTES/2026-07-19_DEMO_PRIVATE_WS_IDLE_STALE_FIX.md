# Demo Private WebSocket Idle Stale Fix

Date: 2026-07-19

## Symptom and Evidence

- Operator received `WARNING BYBIT: private WebSocket stale/disconnected; REST fallback tetap aktif.` after switching to Demo.
- Railway production audit at 2026-07-19 10:18-10:33 UTC found deployment `9e696f77-c5ae-4538-b761-ca5aa7aa6c63` running `PAPER via BYBIT`, four Paper sessions, and public metadata `testnet=True`.
- No production log in available 24-hour window matched private WebSocket reconnect, private WS auth failure, stale/disconnected alert, or reconciliation failure.
- This deployment therefore cannot establish a Demo transport failure.

## Root Cause

- `BybitPrivateWebSocket.stale` classified a connection stale after 45 seconds with no private payload.
- Bybit private `order`, `execution`, `position`, and `wallet` topics are account-event streams. An idle account need not receive those payloads for longer than 45 seconds.
- A healthy idle socket was therefore falsely reported stale and executor bypassed its private order waiter for REST polling.
- This is observability and latency degradation, not evidence of a disconnected Demo socket or failed REST reconciliation.

## Change

- `stale` now means disconnected transport only.
- `aiohttp` WebSocket heartbeat detects dead peers. `_run` sets `_connected=False` on socket close/error/auth/reconnect failure, preserving immediate REST fallback for genuine disconnects.
- Added regression test for a connected private socket idle for one hour.

## Verification

```text
43 passed
python -m pytest tests/test_bybit_private_ws.py tests/test_bybit_observability.py tests/test_user_session_bybit.py tests/test_bybit_executor.py -q

python -m py_compile data/bybit_private_ws.py core/user_session.py main.py
git diff --check
```

## Deployment and Monitoring

- No deployment, restart, commit, credential access, funding request, or order.
- Before Demo auto-entry: deploy only with operator approval, onboard Demo credential, then observe private WS auth, REST wallet/position reconciliation, and a minimum-size protected Demo lifecycle.
- Roll back if a connected socket fails to detect an actual dead peer, a reconciliation fails, native SL is absent, or a duplicate order occurs.
