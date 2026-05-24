# KARA Bot — Dokumentasi Teknis Lengkap

**Versi**: 8.3.0 (Post-Audit #10 — CVD→DVI Swap + Regime Fix + Binance Liq)  
**Tanggal Dokumen**: 24 Mei 2026  
**Platform**: Hyperliquid Futures (Mainnet only — Railway blocked Bybit/Binance/OKX REST, WS OK)  
**Mode**: Scalper only, Paper Trading  
**Bahasa**: Python 3.10+

---

## Daftar Isi

1. [Gambaran Umum](#1-gambaran-umum)
2. [Tech Stack](#2-tech-stack)
3. [Arsitektur Sistem](#3-arsitektur-sistem)
4. [Sumber Data](#4-sumber-data)
5. [Sistem Scoring](#5-sistem-scoring)
6. [Direction Decision (Voting System)](#6-direction-decision)
7. [Filter Entry](#7-filter-entry)
8. [Manajemen Posisi & Exit](#8-manajemen-posisi--exit)
9. [Manajemen Risiko](#9-manajemen-risiko)
10. [Learning Engine](#10-learning-engine)
11. [Telegram Bot](#11-telegram-bot)
12. [Dashboard Web](#12-dashboard-web)
13. [Deployment](#13-deployment)
14. [Audit History & Performance](#14-audit-history--performance)

---

## 1. Gambaran Umum

KARA adalah bot scalping futures otomatis untuk **Hyperliquid** (DEX on-chain perpetual futures). Menggunakan **multi-factor scoring** + **direction voting system** untuk entry, dan **trailing stop** sebagai primary edge source.

### Status Saat Ini (24 Mei 2026 Siang)
- **Mode**: Scalper only, paper trading
- **Users**: 4 users, ~$70/user (dari $62.50 start)
- **Edge**: Trailing stop (100% WR, 47% firing rate — post pump gate)
- **Deploy**: Railway service `rare-youthfulness`
- **Data**: Hyperliquid WS + **Binance WS liquidation stream** (baru)
- **Last Audit (#10)**: 19 trades, WR 52.6%, PnL +$3.92, PF 1.430
- **Pending Deploy**: CVD→DVI swap, EMA fix, regime fix, Binance liq stream

### Filosofi
- **Data > intuisi.** Metric kontradiksi hipotesis → metric menang.
- **Root cause first.** "Disable" bukan solusi — trace bug, fix root cause.
- **Entry timing > entry score.** Masuk saat pump BARU MULAI, bukan setelah pump selesai.
- **Exit system = the edge.** Trailing stop catches trend continuation.
- **Direction voting > single indicator.** OI/Funding stabil untuk direction, OB untuk score.

---

## 2. Tech Stack

| Komponen | Library | Catatan |
|---|---|---|
| Runtime | Python 3.10+ / asyncio | |
| Exchange | `hyperliquid-python-sdk` + raw HTTP | 3-layer fallback |
| WebSocket | `websockets` | HL + Binance liq stream |
| Telegram | `python-telegram-bot` v21+ | Multi-user |
| Web | FastAPI + uvicorn | Dashboard + API |
| DB | SQLite (sync, thread-locked) | `kara_data.db` |
| ML | scikit-learn `HistGradientBoosting` | Dormant (needs 200 samples) |
| Config | `python-dotenv` + pydantic | |
| Deploy | Docker + Railway | Auto-deploy from `main` |

---

## 3. Arsitektur Sistem

```
main.py (orchestrator)
├── scan_loop (15s interval)
│   └── ScoringEngine._run_scalper()
│       ├── OI/Funding Analyzer (capped ±8)
│       ├── Liquidation Analyzer (HL + Binance data)
│       ├── Orderbook Analyzer (score only, NOT direction)
│       ├── DVI - Delta Volume Imbalance (2min, dollar-weighted)
│       ├── Direction Voting (7 voters)
│       ├── Filters:
│       │   ├── Score threshold
│       │   ├── ATR gate
│       │   ├── Momentum gate (fast reject)
│       │   └── ★ Pump Timing Gate (vol surge + accel + not late)
│       └── _build_scalper_signal() → TradeSignal
├── position_monitor_loop (5s interval)
│   └── RiskManager.check_tp_trail()
│       ├── Early loss cut (-0.2% / 5min)
│       ├── TP1 → partial close 60%, SL→BE
│       ├── TP2 → partial close 40% remaining
│       ├── Trailing stop (THE EDGE)
│       └── Hard time limit (15-25min)
├── BinanceLiquidationStream (background)
│   └── wss://fstream.binance.com/ws/!forceOrder@arr
└── ws_watchdog_loop
    └── KaraWebSocketClient (reconnect, health check)
```

### Multi-User
- Setiap user = independent `UserSession` (balance, positions, config)
- Signal broadcast ke semua users, execute per-user slot availability
- Ranked execution: signals sorted by score descending, top-N per user

---

## 4. Sumber Data

### 4.1 WebSocket (Real-time)

| Channel | Source | Data | Dipakai Untuk |
|---|---|---|---|
| `l2Book` | HL | Orderbook L2 (20 levels) | OB imbalance scoring, spread filter |
| `trades` | HL | Setiap transaksi | DVI, momentum, Whale Trade Imbalance |
| `activeAssetCtx` | HL | Funding + OI | OI/Funding scoring, funding history |
| `liquidations` | HL | Liquidation events | Liq analyzer (sparse) |
| **`!forceOrder@arr`** | **Binance** | **ALL futures liquidations** | **Liq analyzer (10-50x more data)** |

Cache: `cache.trades[asset]` = 500 trades terakhir per asset.  
Cache: `cache.liquidations` = 1000 events (HL + Binance combined).

### 4.2 Binance Liquidation Stream (Baru - Audit #10)

```
URL: wss://fstream.binance.com/ws/!forceOrder@arr
Format: {"e":"forceOrder","o":{"s":"BTCUSDT","S":"SELL","p":"9910","q":"0.014"}}
```

- Free, no API key required
- Binance REST = 403 blocked, tapi WS = **WORKS** dari Railway
- Normalized ke format KARA: `{coin, px, sz, side, source:"binance"}`
- S="SELL" → long position liquidated. S="BUY" → short position liquidated.
- Auto-reconnect dengan 5s backoff

### 4.3 REST API

| Data | Endpoint | Cache |
|---|---|---|
| Mark Price + OI + Funding | `metaAndAssetCtxs` | 30s |
| Candles 1m (30) | `candleSnapshot` | Per scan |
| Candles 1h (24) | `candleSnapshot` | 60min (vol regime) |
| Candles 4h (20) | `candleSnapshot` | 4h (HTF regime) |
| All Mids | `allMids` | 10s |

### 4.4 Bybit — BLOCKED (REST only)
Railway IP mendapat 403 dari Bybit/Binance/OKX REST. L/S ratio = dead code. Binance WS tetap works.

---

## 5. Sistem Scoring

### 5.1 Komponen Aktif (Post-Audit #10)

| Komponen | Max Pts | Role | Status |
|---|---|---|---|
| OI/Funding (contrarian) | **±8** | Setup + Direction vote (weight 3) | ✅ Capped |
| Orderbook Imbalance | ±18 | **Score only** (NOT direction) | ✅ Best predictor (r=+0.205) |
| Liquidation | ±12 | Setup | ⚠️ Expected improve with Binance data |
| Cross-Asset Momentum (XAM) | ±12 | Setup | ⚠️ Barely fires (0.8%) |
| EMA Cross (8/21) | ±10 | Confirmation + Direction vote (weight 2) | ✅ Gap **0.06%** (was 0.1% = dead) |
| RSI (14) | ±8 | Confirmation + Direction vote (weight 1) | ✅ Active (r=+0.191) |
| **DVI (Delta Volume Imbalance)** | **±10** | **Confirmation (2min dollar-weighted)** | ✅ **Re-enabled** (side bug fixed) |
| RSI Momentum (1m vs 5m) | ±8 | Setup | ✅ Active |

### Disabled

| Komponen | Alasan | Audit |
|---|---|---|
| **CVD** | r=-0.21 INVERSE. Fire 74% = lagging constant bias. DVI replaces. | #10 |
| DVI (old) | Was disabled for 0% fire — root cause was side bug ('A' not in sell list) | #7-9 |
| OB Absorption | Reversal signal, not trend-following | #4 |
| MTF 15m | r=-0.68 inverse predictor | #6 |
| Bybit L/S | Blocked 403 | #1 |

### 5.2 Score Formula (Post-Audit #8 Fix)

```
bull_setup = OI_bull + OB_bull + Liq_bull + XAM_bull + EMA_boost + RSI_momentum
bear_setup = OI_bear + OB_bear + Liq_bear + XAM_bear + EMA_boost + RSI_momentum
confirm_pts = EMA_freshness + RSI_neutral + DVI_confirms (range -15 to +25)

# Score = conviction in CHOSEN direction only
aligned_setup = bull_setup if direction == LONG else bear_setup
raw = max(0, aligned_setup + confirm_pts)
scaled = int(raw × 1.6)
score = int(scaled × displacement_mult)
score = clamp(0, 100)

# In _run_scalper():
score × regime_mult (ranging=1.0, trending=0.85, late_trend=0.70, volatile=0.90)
score + session_bonus (30% to score, 70% to threshold)
score + learning_engine adjustment (−20 to +12)
```

### 5.3 Regime Detection (Post-Audit #10 Fix)

| Regime | Volatilitas/hari | Scalper Effect |
|---|---|---|
| LOW_VOL | < 1.5% | ×0.90 |
| NORMAL | 1.5–**6%** | ×1.00 |
| HIGH_VOL | **6–12%** | ×0.90 (volatile category) |
| EXTREME | > **12%** | **Threshold +15** (hanya score 71+ lolos) |

**[AUDIT #10 FIX]** Threshold dinaikkan dari 4%/8% → 6%/12%. Alasan: altcoins standar = 5-8% daily vol. Threshold lama (4%) = SEMUA altcoin di-label HIGH_VOL = permanent ×0.9 penalty yang tidak discriminate.

### 5.4 DVI - Delta Volume Imbalance (Post-Audit #10 — Replaces CVD)

```python
# Window: 2 menit terakhir (leading, bukan lagging)
# Metric: dollar-weighted buy/sell imbalance
recent = trades in last 120 seconds
buy_dollar = sum(px × sz for trades where side='B')
sell_dollar = sum(px × sz for trades where side='A')
imbalance = (buy_dollar - sell_dollar) / (buy_dollar + sell_dollar)

# Threshold: 45% (was 60% — too strict)
# Only scores when ALIGNED with trade direction
if imbalance > 0.45 and side == LONG: confirm_pts += pts
if imbalance < -0.45 and side == SHORT: confirm_pts += pts
pts = min(10, int(abs(imbalance) × 12))
```

**Kenapa DVI > CVD:**
- CVD: last 80 trades (no time window) = LAGGING, fire 74%
- DVI: last 2 minutes, dollar-weighted = LEADING, expected fire 20-40%
- CVD r=-0.21 (inverse). DVI expected neutral-to-positive.

**Root cause DVI 0% fire sebelumnya:** HL kirim `side='A'` untuk sell, tapi DVI cek `('S', 'sell', 'Bid')` — 'A' tidak match → sell volume = 0 → imbalance broken.

### 5.5 4H HTF Regime (Post-Audit #8 Fix)

```
ema10 = _ema(closes, 10)  # full 20 candles
ema20 = _ema(closes, 20)

TRENDING_UP:   EMA10 > EMA20×1.002 AND strength ≥ 0.30
TRENDING_DOWN: EMA10 < EMA20×0.998 AND strength ≥ 0.30
CHOPPY:        otherwise (threshold penalty = 0, detector unreliable)
```

---

## 6. Direction Decision

### 6.1 Voting System (7 voters)

| # | Voter | Weight | Kondisi |
|---|---|---|---|
| 1 | OI/Funding | 3 | `oi_signed > 3` → bull, `< -3` → bear |
| 2 | EMA8/21 | 2 | EMA8 > EMA21×1.0006 → bull |
| 3 | Price momentum 5m | 1 | net_move > 0.1% → bull |
| 4 | RSI momentum | 1 | RSI accelerating + price direction |
| 5 | 4H HTF regime | 2 | TRENDING_UP → bull, DOWN → bear |
| 6 | Momentum strength | 1 | Only fires if |mom| > 0.5% |
| 7 | 🐋 Whale Trade Imbalance | 2 | ≥5 whales, imbalance >50% |

**Decision:** `bull_votes > bear_votes` → LONG. Tie → fallback `bull_setup >= bear_setup`.

### 6.2 Whale Trade Imbalance
- 200 trades terakhir, median size → filter > 3× median = "whale"
- Min 5 whale trades, threshold 50% imbalance
- Sell side: `'A'` + `'S'` + `'sell'` + `'Bid'`

---

## 7. Filter Entry

| # | Filter | Kondisi Skip |
|---|---|---|
| 1 | Spread | > 0.15% |
| 2 | Score threshold | < base 45 + session + HTF adj |
| 3 | SHORT-specific | score < 52, funding < -0.0003 |
| 4 | Funding crowded | LONG fr>0.05%, SHORT fr<-0.05% |
| 5 | ATR gate | LONG < 0.0013, SHORT < 0.0015 |
| 6 | Min momentum (fast reject) | LONG < 0.15%, SHORT < 0.25% |
| 7 | Momentum confirm | Leading: 2/5 candles. Standard: 3/5 + 0.04% net |
| 8 | **★ PUMP TIMING GATE** | vol_surge < 1.5×/2.0× OR accel < 1.2× OR move > 0.7% |
| 9 | Signal cooldown | 5 min per asset |
| 10 | Max positions | 3 concurrent |
| 11 | Kill switch / pause | Drawdown > 95% or daily loss > 90% |

### 7.1 Pump Timing Gate

```python
pump_starting = (
    vol_surge >= 1.5 (LONG) / 2.0 (SHORT) and
    last_candle >= avg_candle × 1.2 and
    move_5m < 0.7% and
    3/5 candles in trade direction and
    avg_candle > 0.04%
)
```

---

## 8. Manajemen Posisi & Exit

### 8.1 Exit Rules

| Rule | Trigger | Action |
|---|---|---|
| Early loss cut | floating ≤ -0.2% after 5min | Close 100% |
| Quick profit (F0) | floating ≥ 0.25-0.35% + retrace | Close 100% |
| TP1 | Price hits TP1 | Close 60%, SL → BE+0.1% |
| TP2 | Price hits TP2 (after TP1) | Close 40% remaining |
| **Trailing stop** | After TP1, trail from peak | Close remaining — **THE EDGE** |
| Hard time limit | Hold > max_hold | Close 100% |

### 8.2 Trailing Stop (Edge Source)
- Activates after TP1 hit
- Trail distance: `max(realized_vol × 50%, 0.5%)` pre-TP2, `max(vol × 30%, 0.3%)` post-TP2
- **Performance (Audit #10):** 100% WR, **47% firing rate**, avg PnL +$1.44/trade
- Avg fire time: minute 4-8

---

## 9. Manajemen Risiko

### 9.1 Position Sizing

```
risk_pct = 2.0-3.5% (score-based)
size_usd = (equity × risk_pct) / (sl_pct × leverage)
size_usd = min(size_usd, equity × 35%)
min_margin = $8 (floor)
```

### 9.2 Limits

| Parameter | Value |
|---|---|
| Max concurrent positions | 3 |
| Daily loss pause | 90% of session balance |
| Kill switch | Drawdown > 95% |
| Post-loss cooldown | 5 hours |
| Paper balance start | $62.50 |

---

## 10. Learning Engine

### 10.1 Pattern Memory (Layer 1)
- Key: `{asset}_{side}_{regime}`
- EMA win rate (alpha 0.15)
- After 5 trades: WR < 25% → score -20 or FLIP. WR > 65% → score +8.

### 10.2 ML Model (Layer 2) — DORMANT
- HistGradientBoosting, needs 200 samples
- Output: P(win) → size multiplier (0.5x to 1.3x)

---

## 11. Telegram Bot

### Commands
`/start`, `/status`, `/pos`, `/history`, `/stats`, `/pause`, `/resume`, `/scalper`, `/standard`, `/whatsnew`, `/setleverage`, `/setrisk`, `/signal`, `/export`

---

## 12. Dashboard Web

FastAPI + Tailwind + WebSocket real-time.
- `/api/overview` — Balance, PnL, positions
- `/api/history` — Trade history
- `/api/admin/reasoning/decisions` — Decision traces
- `/ws/admin/reasoning` — Real-time feed

---

## 13. Deployment

### Railway
- Service: `rare-youthfulness`, project `precious-integrity`
- Auto-deploy from `main` branch
- Persistent volume for SQLite
- JSON structured logging

### Environment Variables
`HL_WALLET_ADDRESS`, `HL_PRIVATE_KEY`, `TELEGRAM_BOT_TOKEN`, `ALLOWED_CHAT_IDS`, `KARA_ACCESS_CODE`, `KARA_TRADE_MODE`

---

## 14. Audit History & Performance

### Progression

| Audit | Date | Trades | WR | PnL | PF | Trailing% | Score↔PnL r |
|---|---|---|---|---|---|---|---|
| #1 | 18 Mei | 338 | 48.8% | -$67.22 | 0.65 | — | +0.025 |
| #2 | 20 Mei | 260 | 47.7% | -$26.39 | 0.74 | 3.5% | -0.145 |
| #3 | 21 Mei AM | 21 | 47.6% | -$5.74 | 0.58 | — | -0.449 |
| #4 | 21 Mei PM | 72 | 37.5% | +$3.90 | 1.87 | 19.4% | +0.035 |
| #5 | 21 Mei night | 104 | 35.6% | -$0.63 | 1.79 | 22.1% | -0.023 |
| #6 | 22 Mei | 115 | 45.2% | +$0.58 | 1.01 | 33% | +0.085 |
| #7 | 22 Mei PM | 57 | 52.6% | +$8.23 | 1.12 | 33.3% | +0.098 |
| #8 | 23 Mei AM | 35 | 40% | +$1.56 | 1.587 | 40% | -0.18 |
| #9 | 23 Mei PM | 134 | 44.8% | +$1.70 | 1.027 | 33% | -0.11 |
| **#10** | **24 Mei AM** | **19** | **52.6%** | **+$3.92** | **1.430** | **47%** | **-0.177** |

### Audit #10 Findings (24 Mei 2026 Pagi)

**Data:** 19 trades, 8.4 jam, 2.3/hr (post-pump-gate deploy)

**Wins:**
- Pump gate BEKERJA: trailing 33% → 47%, time_exit 62% → 42%
- WR naik 44.8% → 52.6%, PF naik 1.027 → 1.430
- LONG dominant: 12t, WR 58%, trailing 7/12 (58%)
- RR positif: avg win $1.30, avg loss $1.01

**Problems Found + Fixes:**

| # | Problem | Root Cause | Fix |
|---|---|---|---|
| 1 | Score↔PnL r=-0.177 (still inverse) | **CVD r=-0.21** (fire 74%, lagging) | CVD disabled, DVI enabled |
| 2 | Bot berhenti trade (0 trades/hr) | **EMA gap 0.1% = impossible** on 1m candle | EMA gap → 0.06% |
| 3 | Bot berhenti trade | **Regime 4% = all altcoins "volatile"** → permanent ×0.9 | Threshold → 6%/12% |
| 4 | LIQ barely fires (8%) | HL liq data sparse | Binance forceOrder WS added |
| 5 | DVI was 0% fire (3 audits) | Side bug: HL sends 'A', DVI checked 'S'/'Bid' | Side fixed + threshold 60%→45% |

**Per-Component Correlation (n=19):**

| Komponen | r | Verdict |
|---|---|---|
| EMA | +0.226 | ✅ Best confirmation |
| OB | +0.205 | ✅ Best predictor |
| RSI | +0.191 | ✅ Working |
| FUND | +0.145 | ✅ Neutral-positive |
| XAM | -0.019 | Neutral |
| CVD | **-0.210** | ❌ INVERSE → disabled |
| LIQ | -0.350 | ⚠️ n=2, monitor |

### Current Edge
- **Trailing stop** = sole profit source. 100% WR, 47% fire rate post-pump-gate.
- **Pump gate** = quality filter. Reduces time_exit from 62% → 42%.
- **OB + EMA** = best predictors (r > +0.20).

---

*Dokumen ini sinkron dengan Audit #10 (24 Mei 2026 pagi). Next audit: 24 Mei 2026 23:00 WIB (16:00 UTC).*
