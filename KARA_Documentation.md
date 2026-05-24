# KARA Bot — Dokumentasi Teknis Lengkap

**Versi**: 8.4.0 (Post-Audit #11 — EMA 13/34 + DVI Disabled + Regime Retuned + Liq Cluster)  
**Tanggal Dokumen**: 24 Mei 2026 (Malam)  
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

### Status Saat Ini (24 Mei 2026 Malam — Post-Audit #11)
- **Mode**: Scalper only, paper trading
- **Users**: 4 users, ~$70/user (dari $62.50 start)
- **Edge**: Trailing stop (100% WR, 47% firing rate)
- **Deploy**: Railway service `rare-youthfulness`
- **Data**: Hyperliquid WS + **Binance WS liquidation stream**
- **Last Audit (#11)**: 15 trades post-deploy, WR 46.7%, PnL +$3.98, PF 1.697
- **Deployed**: EMA 13/34, DVI disabled, regime 10%/18%, liq cluster

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
│       ├── Liq Cluster (Binance+HL real events, fallback OI proxy)
│       ├── Orderbook Analyzer (score only, NOT direction)
│       ├── EMA 13/34 (confirmation, direction vote)
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

### 5.1 Komponen Aktif (Post-Audit #11)

| Komponen | Max Pts | Role | Status |
|---|---|---|---|
| OI/Funding (contrarian) | **±8** | Setup + Direction vote (weight 3) | ✅ Capped, r=-0.013 netral |
| Orderbook Imbalance | ±18 | **Score only** (NOT direction) | ✅ Best predictor (r=+0.293) |
| **Liq Cluster** | ±12 | Setup (real cascade detection) | 🆕 Binance+HL events, replaces OI proxy |
| Cross-Asset Momentum (XAM) | ±12 | Setup | ⚠️ Barely fires (0%) |
| **EMA Cross (13/34)** | ±10 | Confirmation + Direction vote (weight 2) | 🔧 Period naik (was 8/21), gap 0.04% |
| RSI (14) | ±8 | Confirmation + Direction vote (weight 1) | ✅ Best confirmation (r=+0.587) |

### Disabled

| Komponen | Alasan | Audit |
|---|---|---|
| **DVI** | r=-0.126 inverse, 60% fire = noise, redundant dgn momentum gate, measures exhaustion bukan initiation | #11 |
| **CVD** | r=-0.21 INVERSE. Fire 74% = lagging constant bias. | #10 |
| OB Absorption | Reversal signal, not trend-following | #4 |
| MTF 15m | r=-0.68 inverse predictor | #6 |
| Bybit L/S | Blocked 403 | #1 |

### 5.2 Score Formula (Post-Audit #8 Fix)

```
bull_setup = OI_bull + OB_bull + Liq_bull + XAM_bull + EMA_boost + RSI_momentum
bear_setup = OI_bear + OB_bear + Liq_bear + XAM_bear + EMA_boost + RSI_momentum
confirm_pts = EMA_freshness + RSI_neutral (range -15 to +18)

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

### 5.3 Regime Detection (Post-Audit #11 Fix)

| Regime | Volatilitas/hari | Scalper Effect |
|---|---|---|
| LOW_VOL | < 2% | ×0.90 |
| NORMAL | 2–**10%** | ×1.00 |
| HIGH_VOL | **10–18%** | ×0.90 (volatile category) |
| EXTREME | > **18%** | **Threshold +15** (hanya score 71+ lolos) |

**[AUDIT #11 FIX]** Threshold dinaikkan dari 6%/12% → 10%/18%. Data: realized vol altcoin (GRASS 14.4%, GMT 12.8%, NEAR 9.4%, JTO 6.3%) = semua kena HIGH_VOL di threshold lama. Crypto altcoin 6-10% daily vol = NORMAL. Hanya true spike >10% yang patut di-penalti.

### 5.4 Liq Cluster Score (Post-Audit #11 — Replaces OI Proxy)

```python
# Scan liquidation events (Binance + HL) for this asset, last 10 minutes
recent = [e for e in cache.liquidations if e.coin == asset and e.time > now - 10min]

# Split by direction
long_liqs  = events where side="long"/"SELL"   # longs rekt → bearish
short_liqs = events where side="short"/"BUY"   # shorts squeezed → bullish

# Score by total notional (sum px × sz for each cluster)
$2k+  → 4 pts
$8k+  → 8 pts
$20k+ → 12 pts

# Only fires if ≥2 events in 10 min window
# Falls back to OI proxy if no cluster detected
```

**Kenapa Liq Cluster > OI Proxy:**
- OI proxy = tebakan (OI besar + funding → "mungkin ada liquidation")
- Liq Cluster = **real forced orders** dari Binance stream (actual cascade happening)
- Forced liquidation = momentum continuation (bukan voluntary, jadi predictive)
- Min 2 events + $2k notional = filter noise tanpa terlalu ketat untuk altcoin

### 5.5 EMA Cross 13/34 (Post-Audit #11 Fix)

```python
ema13 = ema(closes[-34:], 13)
ema34 = ema(closes[-34:], 34)

# Gap 0.04% — period sudah jadi filter, gap hanya cegah flicker
ema_bullish = ema13 > ema34 × 1.0004
ema_bearish = ema13 < ema34 × 0.9996

# Fresh cross (≤3 candles ago) → +10 pts
# Medium (4-7 candles) → +4 pts
# Stale (≥8 candles) → penalty
```

**Kenapa 13/34 > 8/21:**
- EMA 8/21 di 1m candle = cross setiap 2-3 menit (noise) → 93% fire rate = constant = not signal
- EMA 13/34 = cross setiap ~15-20 menit → hanya fire pada real trend formation
- Period sendiri sudah jadi filter, jadi gap bisa kecil (0.04% vs 0.08% sebelumnya)

### 5.6 4H HTF Regime (Post-Audit #8 Fix)

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
| 2 | EMA13/34 | 2 | EMA13 > EMA34×1.0004 → bull |
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
| **#11** | **24 Mei PM** | **15 (post-deploy)** | **46.7%** | **+$3.98** | **1.697** | **47%** | **-0.012** |

### Audit #11 Findings (24 Mei 2026 Malam)

**Data:** 15 trades post-deploy (06:25–15:48 UTC, 9.4 jam), 1.6/hr

**Wins:**
- PF naik ke 1.697 (dari 1.430)
- Score↔PnL r membaik: -0.177 → -0.012 (hampir netral, CVD disable bekerja)
- Trailing tetap konsisten 47%, 100% WR, avg +$1.38/trade
- LONG solid: 10 trades, WR 60%, PnL +$4.39

**Problems Found + Fixes:**

| # | Problem | Root Cause | Fix |
|---|---|---|---|
| 1 | EMA fire 93% + r=-0.33 inverse | Period 8/21 terlalu pendek untuk 1m → constant | EMA 13/34, gap 0.04% |
| 2 | DVI fire 60% + r=-0.126 inverse | Measures exhaustion (siapa agresif SEKARANG), bukan continuation. Redundant dengan momentum gate. | **Disabled** |
| 3 | Regime masih ×0.9 ALL coins | Threshold 6% terlalu rendah — altcoin normal 6-10% vol | Threshold → 10%/18% |
| 4 | LIQ proxy = tebakan, 13% fire | OI proxy tidak punya real cascade data | **Liq Cluster** dari Binance+HL events |

**Per-Component Correlation (n=15, post-deploy):**

| Komponen | r | Fire% | Verdict |
|---|---|---|---|
| RSI | +0.587 | 73% | ✅ Best confirmation |
| OB | +0.293 | 100% | ✅ Best predictor |
| FUND | -0.013 | 53% | ✅ Netral |
| EMA | -0.330 | 93% | ❌ Over-firing → FIXED (13/34) |
| DVI | -0.126 | 60% | ❌ Inverse → DISABLED |
| LIQ | -0.315 | 13% | ⚠️ n=2 → Liq Cluster replaces |

### Current Edge (Post-Audit #11)
- **Trailing stop** = sole profit source. 100% WR, 47% fire rate.
- **OB + RSI** = best predictors (r=+0.29, r=+0.59).
- **Regime fix** = expected frequency boost (×0.9 removed for most coins).
- **Liq Cluster** = untested but theoretically sound (real cascade > proxy).

---

*Dokumen ini sinkron dengan Audit #11 (24 Mei 2026 malam). Next audit: 25 Mei 2026 23:00 WIB (16:00 UTC).*
