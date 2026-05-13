# KARA — Hyperliquid Futures Trading Bot

**KARA** is an automated futures trading bot for [Hyperliquid](https://hyperliquid.xyz) with a multi-factor scoring engine, adaptive risk management, Telegram integration, and a real-time web dashboard.

> Current version: **8.0.1** | Mode: Scalper | Exchange: Hyperliquid Perpetuals

---

## Features

- **Multi-Factor Signal Scoring (0–100)** — Three independent analyzers (OI+Funding, Liquidation, Orderbook) combined with session bias and market regime multipliers
- **Two Trading Modes** — Standard (swing, up to 10 concurrent positions) and Scalper (ultra-short, up to 3 positions, fixed SL/TP)
- **Self-Learning AI Layer** — `HistGradientBoosting` model trained on live trade history; adjusts position size and blocks low-edge trades (activates after 300 trades)
- **Meta Score System** — EMA-based win rate tracker per `{mode}_{asset}_{side}` pattern; adds ±8–12 pts after 5 samples
- **Layered Exit Engine** — TP1/TP2 partial closes, breakeven SL move, ATR-trailing stop, momentum reversal exit, and hard time limits
- **Multi-User Support** — Each user connects their own Hyperliquid wallet; private keys encrypted with Fernet AES at rest
- **Telegram Bot** — Real-time signal alerts, entry/exit notifications, daily P&L reports, `/pause`, `/resume`, `/scalper`, `/standard`, `/whatsnew`, and more
- **Web Dashboard** — FastAPI + Tailwind glassmorphism UI; live position monitor, market scanner, decision feed
- **Rule-Based Autopsy Engine** — 16 deterministic templates that explain every trade outcome in plain language
- **Railway-Ready** — Docker + `railway.toml` included; JSON structured logging auto-enabled on Railway

---

## Architecture

```
kara-bot/
├── config.py                    # Central config (env loading, all strategy params)
├── main.py                      # Bot orchestrator, event loop
│
├── models/schemas.py            # Pydantic models: TradeSignal, Position, Order
│
├── data/
│   ├── hyperliquid_client.py    # REST API wrapper (3-layer fallback)
│   └── ws_client.py             # WebSocket client (orderbook, trades, funding, liquidations)
│
├── engine/
│   ├── scoring_engine.py        # Signal orchestrator — runs Standard & Scalper pipelines
│   └── analyzers/
│       ├── oi_funding_analyzer.py    # OI + Funding Rate (max ±45 pts)
│       ├── liquidation_analyzer.py  # Liquidation cascade (max ~12 pts)
│       └── orderbook_analyzer.py    # Bid/ask imbalance, VWAP, CVD, walls (max ±30 pts)
│
├── risk/risk_manager.py         # Position sizing, SL/TP calc, drawdown guards
│
├── execution/
│   ├── paper_executor.py        # Simulated trades (paper mode)
│   └── live_executor.py         # Real execution on Hyperliquid mainnet
│
├── backtest/backtester.py       # Vectorized backtesting engine
├── dashboard/app.py             # FastAPI web UI
├── notify/telegram.py           # Telegram bot integration
└── utils/helpers.py             # Utility functions
```

---

## Signal Scoring

Every asset is scored 0–100 each scan cycle. A trade is only opened if the score clears the threshold **and** all pre-execution filters pass.

| Component | Max Points | Description |
|---|---|---|
| OI + Funding Analyzer | ±45 | Funding rate extremes, OI change direction, spot-perp basis, funding trend slope |
| Liquidation Analyzer | ~±12 | Cascade probability, cluster imbalance (uses OI proxy when live data is absent) |
| Orderbook Analyzer | ±30 | Bid/ask imbalance, VWAP deviation, CVD, dollar depth, bid/ask wall detection |
| Session Bonus | ±10 | NY +10, London +4, Asia −10, overlap cumulative |
| Volatility Multiplier | ×0.85–1.10 | Regime: LOW_VOL ×0.90, NORMAL ×1.0, HIGH_VOL ×0.85, EXTREME → skip |
| Meta Score | ±12 | EMA win rate per `{mode}_{asset}_{side}` pattern (needs ≥5 samples) |

**Direction filter (Standard):** 3-of-4 consensus required across OI, Liquidation, Orderbook, and 1m momentum.

**Thresholds:** Standard ≥ 62 | Scalper ≥ 60 | Both blocked during 08:00–09:00 UTC (London open spike hours).

---

## Risk Management

| Parameter | Standard | Scalper |
|---|---|---|
| Risk per trade | 2.5–3.5% of equity (score-based) | 4% baseline |
| Leverage | 10× default (max 10×) | 25× default (max 35×) |
| Stop Loss | Vol-aware 1.2–3.5% | Fixed 0.65% |
| TP1 / TP2 | Vol-aware ~3–4.8% / ~7–9% | Fixed 0.85% / 1.50% |
| Close at TP1 | 40% of position | 60% of position |
| Close at TP2 | 30% of remaining | 40% of remaining |
| Max positions | 10 concurrent | 3 concurrent |
| Max hold time | 6 h hard limit | 12 min + 6 min grace |
| Daily loss pause | > 90% of session balance | same |
| Kill switch | Drawdown > 95% from peak | same |
| Post-loss cooldown | 5 h after daily loss > 50% | same |

**Drawdown guards:** position size ×0.50 when equity ≤ 80% of start; ×0.50 again if drawdown ≥ 15% from peak. Hard cap: no single position > 35% of balance.

---

## Setup

### Requirements

- Python 3.11+
- Hyperliquid account (testnet or mainnet)
- Telegram Bot token (optional but recommended)

### Environment Variables

Create a `.env` file:

```env
# Trading Mode
KARA_TRADE_MODE=paper          # paper | live
KARA_FULL_AUTO=true            # true | false

# Hyperliquid
HL_WALLET_ADDRESS=0x...
HL_PRIVATE_KEY=0x...
HL_FERNET_KEY=                 # required for multi-user encrypted key storage

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
ALLOWED_CHAT_IDS=id1,id2       # comma-separated for multi-user

# Access Gate
KARA_ACCESS_CODE=KARA2026      # required for new users joining via Telegram

# Database
DATABASE_URL=                  # SQLite default; set for Railway Postgres
```

### Local Run

```bash
pip install -r requirements.txt
python main.py
```

### Docker

```bash
docker compose up --build
```

### Railway Deployment

```bash
railway login
railway link       # select your KARA project
railway up
```

JSON structured logging activates automatically when `RAILWAY_ENVIRONMENT=true` is detected.

---

## Telegram Commands

| Command | Description |
|---|---|
| `/start` | Register and enter access code |
| `/status` | Current positions, equity, daily P&L |
| `/pause` / `/resume` | Pause or resume trading |
| `/scalper` / `/standard` | Switch trading mode |
| `/whatsnew` | Show latest changelog |
| `/history` | Recent closed trades |
| `/stats` | Win rate, Sharpe, drawdown summary |
| `/setleverage` | Adjust max leverage |
| `/setrisk` | Adjust risk per trade |

---

## Changelog Summary

| Version | Date | Highlight |
|---|---|---|
| 8.0.1 | 2026-05-12 | Railway telemetry, rule-based autopsy engine, dynamic git changelog |
| 8.0.0 | 2026-05-12 | Quant Aggression Protocol — layered partial exits, score-driven time exits, funding contrarianism |
| 7.1.0 | 2026-05-08 | SHORT signal improvements, RSI divergence, rejection wick detection, double-PnL bugfix |
| 7.0.0 | 2026-04-13 | Self-learning AI engine (HistGradientBoosting), dynamic risk sizing, EV gate |
| 6.2.0 | 2026-04-09 | Multi-user architecture, Fernet-encrypted private keys |
| 5.0.0 | 2026-04-06 | Web dashboard (FastAPI + Glassmorphism) |

Full changelog: [CHANGELOG.md](CHANGELOG.md)

---

## Disclaimer

This software is provided for educational purposes. Automated trading involves substantial risk of loss. Never trade with money you cannot afford to lose. The authors are not responsible for financial losses incurred through use of this software.
