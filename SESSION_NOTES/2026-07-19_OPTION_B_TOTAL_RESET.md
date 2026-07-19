# Option B Total Production Reset

Date: 2026-07-19

## Authorization

- Operator explicitly selected `Opsi B` after read-only Railway audit.
- Scope: delete every user, Telegram authorization state, encrypted Bybit credentials, user configuration, persisted positions, trading journals, signal history, ML experience/model, caches, snapshots, candidate telemetry, weak-confirmation telemetry, and Excel export.

## Pre-reset Evidence

- Railway production database identity verified: `/data/kara_data.db` 6,594,560 bytes, valid SQLite header, `PRAGMA integrity_check=ok`.
- ML DB `/data/kara_ml.db` 454,656 bytes, `PRAGMA integrity_check=ok`.
- Before reset inventory: 2,129 closed trades, 2,151 signals, 2,129 ML experiences, zero persisted Paper positions, zero persisted Bybit positions, three Paper states, five risk states, and two Demo candidate rejections.
- Historical data was audited read-only before authorized deletion.

## Implementation

- Added exact confirmation token `KARA_TOTAL_RESET_CONFIRMATION=WIPE_ALL_KARA_PRODUCTION_DATA`.
- Reset runs only before user/session/Telegram startup.
- Added on-volume `.kara_total_reset_done` marker. If process restarts while token remains, it skips wipe.
- Reset removes `execution_candidates`, `trade_history.xlsx`, every user record and credential, in addition to legacy reset tables, ML DB, and ML model.
- Startup blocks if reset fails or marker/Telegram-state cleanup fails.

## Verification

```text
17 passed
python -m pytest tests/test_total_reset.py tests/test_startup_validation.py tests/test_bybit_persistence.py tests/test_bybit_credentials.py -q

python -m py_compile config.py core/db.py main.py
git diff --check
```

## Deployment Plan

1. Deploy reset-capable code.
2. Set exact confirmation token to trigger one startup wipe.
3. Verify all tables/users/Telegram state are empty and no persisted exchange positions existed before reset.
4. Delete confirmation token from Railway.
5. Verify marker prevents repeated wipe until operator intentionally removes it for a future separately approved reset.

## Deployment Result

- Deployed Railway production deployment `ad1733fe-4230-4489-9726-098bb1351800` at 2026-07-19 11:21 UTC.
- Reset log recorded deletion counts: 4 users, 2,129 trades, 2,151 signals, 2,129 ML experiences through deleted ML DB, 408 meta rows, 140 volatility-cache rows, 138 OI rows, 210 snapshots, 5 risk states, 2 execution candidates, 384 weak events, 381 weak outcomes, and Excel export.
- Pre-reset persisted Paper and Bybit position counts were both zero.
- Post-reset Railway verification: `users.json={}`, `paper_positions=0`, `bybit_positions=0`, `trade_history=0`, `signals_history=0`, `execution_candidates=0`, `risk_state=0`, weak-confirmation tables zero, `kara_ml.db` absent, ML model absent, and Excel export absent.
- Runtime then created one fresh system snapshot and 10 fresh volatility-cache rows. These are new cache/system state, not preserved user or trading history.
- Confirmation token was deleted from Railway after verification. One-shot marker `/data/.kara_total_reset_done` remains.

## Residual Risk

- User credentials and all historical data are irreversibly deleted.
- Demo/Mainnet exchange accounts are not modified. Persisted positions were audited zero before reset; if exchange state changes before deployment, reset does not close or alter venue positions.
