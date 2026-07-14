# Session Note - 2026-07-15 Bull Exhaustion Short

## Scope

- Native scalper SHORT above configured +3% 24h trend now requires closed-candle bull-exhaustion confirmation.
- No deploy, restart, database schema change, or production data change.

## Contract

- Applies only when `trend_pct > config.SIGNAL.bull_exhaustion_short_min_trend_pct`; exact +3% retains native SHORT path under default config.
- Requires 15m MTF state exactly `bear`, 21 closed 1m candles, bearish latest closed candle, and close below rejected level.
- Retest scans prior three closed candles for EMA21 or prior resistance (`high[-16:-4]`) with `0.0015` tolerance.
- Missing, malformed, or open candle timestamps fail closed.
- Trend context is fetched only after existing score, meta, and concentration gates, preserving prior scan cost for low-score rejects.

## Telemetry And Verification

- `TradeSignal` and `Position` include `strategy_source`; valid setup stores `bull_exhaustion_short`.
- Paper and Bybit opened positions copy source from signal.
- Focused runtime tests cover valid resistance retest, stale retest, neutral MTF, unavailable latest closed candle, and serialization.
- Focused suite: 23 passed. `py_compile` and `git diff --check` passed; one existing `requests` dependency-version warning only.

## Monitoring And Rollback

- Monitor count and outcomes by `strategy_source`, especially `bull_exhaustion_short` versus `native_scalper`.
- Roll back by setting `bull_exhaustion_short_enabled = False`; no positions or schema migration require reversal.
