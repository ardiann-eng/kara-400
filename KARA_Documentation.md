# KARA Bot — Dokumentasi Teknis Lengkap

**Versi**: 8.2.0 (Post-Audit #9 — Pump Timing Gate)  
**Tanggal Dokumen**: 24 Mei 2026  
**Platform**: Hyperliquid Futures (Mainnet only — Railway blocked Bybit/Binance/OKX)  
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

### Status Saat Ini (24 Mei 2026)
- **Mode**: Scalper only, paper trading
- **Users**: 4 users, ~$70/user (dari $62.50 start)
- **Edge**: Trailing stop (100% WR, 33% firing rate)
- **Deploy**: Railway service `rare-youthfulness`
- **Data**: Hyperliquid WS only (Bybit/Binance BLOCKED 403)
- **Last Audit (#9)**: 134 trades, WR 44.8%, PnL +$1.70, PF 1.027
- **Critical Fix Deployed (23 Mei malam)**: Pump Timing Gate, OI cap, CVD/EMA threshold, CHOPPY penalty off

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
| WebSocket | `websockets` | Reconnect exponential backoff |
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
│       ├── Liquidation Analyzer
│       ├── Orderbook Analyzer (score only, NOT direction)
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

| Channel | Data | Dipakai Untuk |
|---|---|---|
| `l2Book` | Orderbook L2 (20 levels) | OB imbalance scoring, spread filter |
| `trades` | Setiap transaksi | CVD, momentum, **Whale Trade Imbalance** |
| `activeAssetCtx` | Funding + OI | OI/Funding scoring, funding history |
| `liquidations` | Liquidation events | Liq analyzer (jarang fire) |

Cache: `cache.trades[asset]` = 500 trades terakhir per asset.

### 4.2 REST API

| Data | Endpoint | Cache |
|---|---|---|
| Mark Price + OI + Funding | `metaAndAssetCtxs` | 30s |
| Candles 1m (30) | `candleSnapshot` | Per scan |
| Candles 1h (24) | `candleSnapshot` | 60min (vol regime) |
| Candles 4h (20) | `candleSnapshot` | 4h (HTF regime) |
| All Mids | `allMids` | 10s |

### 4.3 Bybit — BLOCKED
Railway IP mendapat 403 dari Bybit/Binance/OKX. L/S ratio, Bybit funding, Bybit OI = **dead code**.

---

## 5. Sistem Scoring

### 5.1 Komponen Aktif (Post-Audit #8)

| Komponen | Max Pts | Role | Status |
|---|---|---|---|
| OI/Funding (contrarian) | **±8** | Setup + **Direction vote (weight 3)** | ✅ Capped (was ±35, inflated score) |
| Orderbook Imbalance | ±18 | **Score only** (NOT direction) | ✅ Best predictor (OB=18 WR 55.9%) |
| Liquidation | ±12 | Setup | ⚠️ Barely fires (22%) |
| Cross-Asset Momentum (XAM) | ±12 | Setup | ⚠️ Barely fires (0.8%) |
| EMA Cross (8/21) | ±10 | Confirmation + **Direction vote (weight 2)** | ✅ Gap raised 0.03%→0.1% (target fire ~35%) |
| RSI (14) | ±8 | Confirmation + Direction vote (weight 1) | ✅ Active (67% fire) |
| CVD Confirms | ±10 | Confirmation (threshold **0.40** + price **0.2%** confirm) | ✅ Threshold raised (target fire ~40%) |
| RSI Momentum (1m vs 5m) | ±8 | Setup | ✅ Active |

**Disabled:** DVI (0% firing), OB Absorption (reversal), MTF 15m (r=-0.68), Bybit L/S (blocked).

### 5.2 Score Formula (Post-Audit #8 Fix)

```
bull_setup = OI_bull + OB_bull + Liq_bull + XAM_bull + EMA_boost + RSI_momentum
bear_setup = OI_bear + OB_bear + Liq_bear + XAM_bear + EMA_boost + RSI_momentum
confirm_pts = EMA_freshness + RSI_neutral + CVD_confirms (range -15 to +25)

# [AUDIT #8 FIX] Score = conviction in CHOSEN direction only
# Before: max(bull, bear) → high score from OPPOSING setup = inverse predictive
# After: aligned setup only
aligned_setup = bull_setup if direction == LONG else bear_setup
raw = max(0, aligned_setup + confirm_pts)
scaled = int(raw × 1.6)
score = int(scaled × displacement_mult)  # regime-aware anti-chase
score = clamp(0, 100)

# In _run_scalper():
score × regime_mult (ranging=1.0, trending=0.85, late_trend=0.70, volatile=0.90)
score + session_bonus (30% to score, 70% to threshold)
score + learning_engine adjustment (−20 to +12)
```

### 5.3 Regime Detection

| Regime | Volatilitas/hari | Scalper Effect |
|---|---|---|
| LOW_VOL | < 1.5% | ×0.90 |
| NORMAL | 1.5–4% | ×1.00 |
| HIGH_VOL | 4–8% | ×0.90 (volatile category) |
| EXTREME | > 8% | **Threshold +15** (hanya score 71+ lolos) |

### 5.4 4H HTF Regime (Post-Audit #8 Fix)

```
# [AUDIT #8 FIX] EMA uses full candle array for proper convergence
# Before: _ema(closes[-10:], 10) → EMA10≈EMA20 always → CHOPPY 91.6%
# After: _ema(closes, 10) → proper warm-up → realistic detection
ema10 = _ema(closes, 10)  # full 20 candles
ema20 = _ema(closes, 20)  # full 20 candles
strength = net_move / total_range  # must be > 0.30

TRENDING_UP:   EMA10 > EMA20×1.002 AND strength ≥ 0.30
TRENDING_DOWN: EMA10 < EMA20×0.998 AND strength ≥ 0.30
CHOPPY:        otherwise
```

Effect: TRENDING aligned → threshold -3, lev +2. Counter-trend → threshold +8, lev -3. CHOPPY → threshold +0 (penalty disabled, detector unreliable 91.9%), lev -2.

---

## 6. Direction Decision

### 6.1 Voting System (Audit #6, 22 Mei 2026)

**Kenapa:** Data 115 trades menunjukkan OB imbalance counter-predictive (r=-0.098) untuk direction. OB dominates → WR 38.8%. OI dominates → WR 54.2%.

**Fix:** Direction ditentukan oleh 7 voters. OB excluded dari direction, hanya masuk score.

| # | Voter | Weight | Kondisi |
|---|---|---|---|
| 1 | OI/Funding | 3 | `oi_signed > 3` → bull, `< -3` → bear |
| 2 | EMA8/21 | 2 | EMA8 > EMA21×1.0003 → bull |
| 3 | Price momentum 5m | 1 | net_move > 0.1% → bull |
| 4 | RSI momentum | 1 | RSI accelerating + price direction |
| 5 | 4H HTF regime | 2 | TRENDING_UP → bull, DOWN → bear |
| 6 | Momentum strength | 1 | Only fires if |mom| > 0.5% |
| 7 | 🐋 Whale Trade Imbalance | 2 | Large trades (≥5 whales) imbalance >50% |

**Decision:** `bull_votes > bear_votes` → LONG. Tie → fallback `bull_setup >= bear_setup`.

### 6.2 Whale Trade Imbalance (Post-Audit #8 Fix)

- Ambil 200 trades terakhir dari WS cache
- Hitung median trade size (USD)
- Filter trades > 3× median = "whale"
- **[AUDIT #8 FIX] Minimum 5 whale trades required** (sebelumnya 1 trade = 100% imbalance)
- **[AUDIT #8 FIX] Threshold raised to 50%** (sebelumnya 30% = selalu fire)
- If whale_count ≥ 5 AND |imbalance| > 50% → vote +2 ke sisi dominan
- Sell side detection: `'A'` (HL format) + `'S'` + `'sell'` + `'Bid'`

**Before fix:** 78% fire rate, 219/299 = "100% imbalance" (noise)  
**Expected after:** ~20-40% fire rate (meaningful signal)

---

## 7. Filter Entry

Urutan filter (scoring engine → signal handler → pre_trade_check):

| # | Filter | Kondisi Skip |
|---|---|---|
| 1 | Spread | > 0.15% |
| 2 | Score threshold | < base 45 + session + HTF adj + EXTREME +15 + vote margin + OI gate + funding bonus |
| 3 | SHORT-specific | score < 52, funding < -0.0003, squeeze, tech_min < 6 |
| 4 | Funding crowded | LONG fr>0.05%, SHORT fr<-0.05% |
| 5 | ATR gate | LONG < 0.0013, SHORT < 0.0015 |
| 6 | Min momentum (fast reject) | LONG < 0.15%, SHORT < 0.25% |
| 7 | Momentum confirm | Leading: 2/5 candles. Standard: 3/5 + 0.04% net |
| 8 | **★ PUMP TIMING GATE** | vol_surge < 1.5×(LONG)/2.0×(SHORT) OR accel < 1.2× OR move > 0.7% OR direction wrong OR coin dead |
| 9 | Signal cooldown | 5 min per asset |
| 10 | Max positions | 3 concurrent (scalper) |
| 11 | Kill switch / pause | Drawdown > 95% or daily loss > 90% |

**Removed (Audit #9):** Trend structure veto — redundant dengan pump gate (3/5 candle direction + move < 0.7% sudah cover).

### 7.1 Pump Timing Gate (Audit #9, 24 Mei 2026)

**Problem:** 62% trades = time_exit karena entry SETELAH pump selesai (lagging indicators).

**Solution:** Hanya entry saat pump BARU MULAI:

```python
# Volume: median baseline (robust vs spikes)
vol_baseline = median(volumes[-35:-5])  # 30 candle
vol_recent = mean(volumes[-5:])          # 5 candle
vol_surge = vol_recent / vol_baseline

# Price acceleration
avg_candle = mean(|close[i] - close[i-1]| / close[i-1] for last 10)
last_candle = |close[-1] - close[-2]| / close[-2]

# Gate conditions (ALL must be true)
pump_starting = (
    vol_surge >= 1.5 (LONG) / 2.0 (SHORT) and  # volume expanding
    last_candle >= avg_candle × 1.2 and          # price accelerating
    move_5m < 0.7% and                           # not too late
    3/5 candles in trade direction and            # directional
    avg_candle > 0.04%                            # coin alive
)
```

**Expected impact:** time_exit 62% → <40%, trailing fire 33% → >45%

---

## 8. Manajemen Posisi & Exit

### 8.1 Entry
- Paper: mark_price + 0.03% spread + noise
- Leverage: 20x default (max 35x), ATR-adaptive SL

### 8.2 SL/TP (Scalper — ATR-adaptive)

```
sl_pct = clamp(ATR(14) × 1.5, min=0.6%, max=2.0%)
tp1 = entry ± sl_pct × 0.7
tp2 = entry ± sl_pct × 1.0
```

Score-driven max_hold: score 70+ = 25min, score 60+ = 20min, score 50+ = 15min.

### 8.3 Exit Rules (in order)

| Rule | Trigger | Action |
|---|---|---|
| Early loss cut | floating ≤ -0.2% after 5min | Close 100% |
| Quick profit (F0) | floating ≥ 0.25-0.35% + retrace | Close 100% |
| TP1 | Price hits TP1 | Close 60%, SL → BE+0.1% |
| TP2 | Price hits TP2 (after TP1) | Close 40% remaining |
| **Trailing stop** | After TP1, trail from peak | Close remaining — **THIS IS THE EDGE** |
| Hard time limit | Hold > max_hold | Close 100% |

### 8.4 Trailing Stop (Edge Source)
- Activates after TP1 hit
- Trail distance: `max(realized_vol × 50%, 0.5%)` pre-TP2, `max(vol × 30%, 0.3%)` post-TP2
- **Performance (Audit #8):** 100% WR, 40% firing rate (14/35), avg PnL +$2.03/trade
- Avg fire time: minute 4-8

---

## 9. Manajemen Risiko

### 9.1 Position Sizing

```
risk_pct = 2.0-3.5% (score-based)
size_usd = (equity × risk_pct) / (sl_pct × leverage)
size_usd = min(size_usd, equity × 35%)  # hard cap
min_margin = $8 (floor)
```

Drawdown guards: equity ≤ 80% start → ×0.50. Drawdown ≥ 15% peak → ×0.50 again.

### 9.2 Limits

| Parameter | Value |
|---|---|
| Max concurrent positions | 3 |
| Daily loss pause | 90% of session balance |
| Kill switch | Drawdown > 95% |
| Post-loss cooldown | 5 hours (after daily loss > 50%) |
| Paper balance start | $62.50 |

---

## 10. Learning Engine

### 10.1 Pattern Memory (Layer 1)
- Key: `{asset}_{side}_{regime}`
- EMA win rate (alpha 0.15)
- After 5 trades: WR < 25% → score -20 or FLIP. WR > 65% → score +8.
- Persisted to SQLite `pattern_memory` table.

### 10.2 ML Model (Layer 2) — DORMANT
- HistGradientBoosting, needs 200 samples (currently ~170)
- Retrain every 50 new trades
- Output: P(win) → size multiplier (0.5x to 1.3x)

---

## 11. Telegram Bot

### Commands
`/start`, `/status`, `/pos`, `/history`, `/stats`, `/pause`, `/resume`, `/scalper`, `/standard`, `/whatsnew`, `/setleverage`, `/setrisk`, `/signal`, `/export`, `/resetml`

### Notifications
- Signal card (score, entry/SL/TP, leverage, R:R)
- "📝 Mengapa Sinyal Ini?" button → full reasoning breakdown
- TP1/TP2/trailing/SL/time_exit notifications
- Daily summary card (PNG)

---

## 12. Dashboard Web

FastAPI + Tailwind + WebSocket real-time.

### Endpoints
| Endpoint | Function |
|---|---|
| `/api/overview` | Balance, PnL, positions |
| `/api/history` | Trade history |
| `/api/admin/reasoning/decisions` | Decision traces |
| `/api/admin/learning/patterns` | Pattern memory |
| `/ws/admin/reasoning` | Real-time reasoning feed |

---

## 13. Deployment

### Railway
- Service: `rare-youthfulness`, project `precious-integrity`
- Auto-deploy from `main` branch
- Persistent volume for SQLite
- JSON structured logging (auto-enabled)

### Environment Variables
| Variable | Purpose |
|---|---|
| `HL_WALLET_ADDRESS` | Hyperliquid wallet |
| `HL_PRIVATE_KEY` | Wallet key |
| `TELEGRAM_BOT_TOKEN` | Telegram bot |
| `ALLOWED_CHAT_IDS` | Authorized users |
| `KARA_ACCESS_CODE` | New user gate |
| `KARA_TRADE_MODE` | paper/live |

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
| **#9** | **23 Mei PM** | **134** | **44.8%** | **+$1.70** | **1.027** | **33%** | **-0.11** |

### Audit #9 Findings (23 Mei 2026 Malam)

**Fundamental Problem Identified:** Bot masuk SETELAH pump selesai (lagging indicators), bukan saat pump DIMULAI.

**Data:**
- 134 trades, 32.8h, 4.1/hr, WR 44.8%, PnL +$1.70, PF 1.027
- time_exit 62% (83 trades, -$42.89) — harga tidak gerak setelah entry
- Score MASIH inverse r=-0.11 (high score = high vol = bigger time_exit loss)
- CVD fire 81% = constant bias (CVD=0 WR 52% > CVD=10 WR 43%)
- EMA fire 77% = noise on 1m (EMA=10 WR 39.6% < EMA≤0 WR 65%)
- OB=18 = best predictor (WR 55.9%, trailing 38%)
- OB≥10 + CVD=0 = WR 80% (10 trades)

**5 Fixes Deployed:**

| # | Fix | Root Cause |
|---|---|---|
| 1 | OI/Funding cap ±35 → ±8 | OI inflate score tanpa improve trailing rate |
| 2 | HTF CHOPPY penalty +8 → 0 | Detector broken (91.9% CHOPPY), penalty meaningless |
| 3 | CVD threshold 25% → 40% | Fire 81% = constant bias, not signal |
| 4 | EMA gap 0.03% → 0.1% | 1m cross too frequent = noise |
| 5 | **★ Pump Timing Gate** | Entry setelah pump = time_exit. Entry saat pump mulai = trailing fire. |

### Audit #9 Cutoff
- Timestamp: `1748001600` (23 Mei 2026 06:40 UTC / 13:40 WIB)

### Current Edge Analysis
- **Trailing stop** = sole profit source. 100% WR, 33% fire rate, +$54.99 in 134 trades.
- **time_exit** = sole loss source. 62% of trades, 18% WR, -$42.89.
- **OB=18** = best entry predictor. WR 55.9%, trailing 38%, PnL +$7.02.
- **Pump gate** = expected to increase trailing rate from 33% to >45% by filtering dead-market entries.

---

*Dokumen ini sinkron dengan Audit #9 (23 Mei 2026 malam). Next audit: 24 Mei 2026.*
