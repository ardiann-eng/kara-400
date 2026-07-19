# PnL Card and TP Lifecycle Messages

Date: 2026-07-19

## Symptom and Root Cause

- Operator received fallback text `PENGU LONG closed` with `+13.40 USD` after requesting a PnL card.
- That exact text is emitted only by `KaraTelegram.send_pnl_card()` exception fallback, not by normal close notification.
- `notify/pnl_card.py` passed undefined variable `pnl` to `_exit_reason_color`; generator parameter is `pnl_usd`. This raises `NameError`, card generation fails, and Telegram receives only fallback text.
- Railway log query after CLI token recovery returned no retained `PnLCard` match. Root cause is nevertheless directly proven by exact fallback source and undefined variable in generator.

## Changes

- Replaced undefined `pnl` with `pnl_usd` in PnL card generator.
- Added source-contract regression for exact call and PNG rendering tests when Pillow is installed.
- Rewrote TP1 and TP2 notifications as KARA status updates: realized profit, remaining position, confirmed protection, and next lifecycle state. No copied external wording.
- TP1 only states stop moved to entry when executor confirms native Bybit protection update or Paper state update. It no longer infers protection solely from price proximity.
- Operator selected concise bilingual target-update wording. TP1/TP2 now use `KARA UPDATE: Target Reached` / `Second Target Reached`, partial realized profit, remaining percentage, and confirmed SL/trailing state. PnL Card remains final-close only.

## Verification

```text
59 passed
python -m pytest tests/test_bybit_telegram_safety.py tests/test_bybit_executor.py tests/test_bybit_client.py tests/test_user_session_bybit.py -q

python -m py_compile notify/pnl_card.py notify/telegram.py execution/paper_executor.py execution/bybit_executor.py main.py
git diff --check
```

- Two PNG render tests skipped because local interpreter lacks `PIL`/Pillow, though `requirements.txt` requires `Pillow>=10.0.0`.
- This is local test-environment gap. Production image must install requirements; no production render success claim before deployment.

## Deployment and Monitoring

- No deployment, restart, commit, credential access, Demo balance change, or order.
- After deployment, trigger one complete Demo lifecycle and tap `PnL Card`. Required result: image delivery, no `PnLCard Card generation failed` log, and no plain-text closed fallback.
- Roll back notification change if PnL image generation fails or TP1 reports moved stop without native protection confirmation.
