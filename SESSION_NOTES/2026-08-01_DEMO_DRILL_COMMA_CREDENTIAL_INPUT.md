# Demo Drill Comma Credential Input

Date: 2026-08-01

## Change

- `tools/bybit_testnet_drill.py` now accepts one hidden prompt in exact form `API_KEY,API_SECRET` when no Demo environment variables exist.
- The prompt requires exactly one comma and rejects empty key or secret.
- Credential is neither passed in command arguments nor written into drill evidence.

## Safety Boundary

- Normal Demo drill opens the smallest valid Demo position, installs native hard SL, then closes it reduce-only and verifies final exchange size zero.
- This is a real Demo order, not a preflight-only check. It must not be run with an unexpected open BTCUSDT position.
- No order was submitted in this session. Interactive operator input is unavailable in this execution environment.

## Verification

```text
33 passed
python -m pytest tests/test_bybit_testnet_drill.py -q

python -m py_compile tools/bybit_testnet_drill.py tests/test_bybit_testnet_drill.py
git diff --check
```

## Deployment and Next Measurement

- Not deployed, restarted, committed, or pushed.
- Run controlled Demo BTC long drill. Record only masked key, fill prices, fee, native SL presence, and final exchange position size. Stop if final size is not zero or native SL is absent.
