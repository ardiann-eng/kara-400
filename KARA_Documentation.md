# KARA Bot — Dokumentasi Teknis Lengkap

**Versi**: 8.1.1 (Post-Audit #7)  
**Tanggal Dokumen**: 22 Mei 2026 (malam)  
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

### Status Saat Ini (22 Mei 2026 Malam)
- **Mode**: Scalper only, paper trading
- **Users**: 4 users, ~$70/user (dari $62.50 start)
- **Edge**: Trailing stop (100% WR, 33% firing rate)
- **Deploy**: Railway service `rare-youthfulness`
- **Data**: Hyperliquid WS only (Bybit/Binance BLOCKED 403)
- **Last Audit (#7)**: 57 trades, WR 52.6%, PnL +$8.23, PF 1.115

### Filosofi
- **Data > intuisi.** Metric kontradiksi hipotesis → metric menang.
- **Edge yang tidak terukur = tidak ada.** Komponen yang tidak bisa di-validate → disable.
- **Exit system = the edge.** Entry quality secondary; trailing stop catches trend continuation.
- **Direction voting > single indicator.** OB snapshot volatile (r=-0.098); OI/Funding stabil (r=+0.091).

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
│       ├── OI/Funding Analyzer
│       ├── Liquidation Analyzer
│       ├── Orderbook Analyzer (score only, NOT direction)
│       ├── Direction Voting (7 voters)
│       ├── Filters (ATR, momentum, trend veto, threshold)
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
| All Mids | `allMids` | 10s |

### 4.3 Bybit — BLOCKED
Railway IP mendapat 403 dari Bybit/Binance/OKX. L/S ratio, Bybit funding, Bybit OI = **dead code**.

---

## 5. Sistem Scoring

### 5.1 Komponen Aktif (Post-Audit #6)

| Komponen | Max Pts | Role | Status |
|---|---|---|---|
| OI/Funding (contrarian) | ±28 | Setup + **Direction vote (weight 3)** | ✅ Active |
| Orderbook Imbalance | ±18 | **Score only** (NOT direction) | ✅ Active |
| Liquidation | ±12 | Setup | ⚠️ Barely fires (8%) |
| Cross-Asset Momentum (XAM) | ±12 | Setup | ✅ Re-enabled |
| EMA Cross (8/21) | ±10 | Confirmation + **Direction vote (weight 2)** | ✅ Active |
| RSI (14) | ±8 | Confirmation + Direction vote (weight 1) | ✅ Active |
| CVD Confirms | ±10 | Confirmation (threshold 0.25 + price confirm) | ✅ Active (bug fixed Audit #7) |
| RSI Momentum (1m vs 5m) | ±8 | Setup | ✅ Active |

**Disabled:** DVI (0% firing), OB Absorption (reversal), MTF 15m (r=-0.68), Bybit L/S (blocked).

### 5.2 Score Formula

```
bull_setup = OI_bull + OB_bull + Liq_bull + XAM_bull + EMA_boost + RSI_momentum
bear_setup = OI_bear + OB_bear + Liq_bear + XAM_bear + EMA_boost + RSI_momentum
confirm_pts = EMA_freshness + RSI_neutral + CVD_confirms (range -15 to +25)

dominant_setup = max(bull_setup, bear_setup)
raw = max(0, dominant_setup + confirm_pts)
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

4H HTF regime (TRENDING_UP/DOWN/CHOPPY) juga mempengaruhi threshold (±3/+8).

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
| 7 | 🐋 Whale Trade Imbalance | 2 | Large trades (>3× median) imbalance >30% |

**Decision:** `bull_votes > bear_votes` → LONG. Tie → fallback `bull_setup >= bear_setup`.

### 6.2 Trend Structure Veto (replaces old trend-flip)

Setelah direction ditentukan:
- LONG + price >0.2% below EMA21 + EMA8<EMA21 → **SKIP** (jangan entry)
- SHORT + price >0.2% above EMA21 + EMA8>EMA21 → **SKIP**

Tidak flip ke sisi lain (data: flip 0% WR, SHORT structural WR 20%).

### 6.3 Whale Trade Imbalance (LTI)

- Ambil 200 trades terakhir dari WS cache
- Hitung median trade size (USD)
- Filter trades > 3× median = "whale"
- Whale buy vol vs sell vol → imbalance ratio
- If |imbalance| > 30% → vote +2 ke sisi dominan
- Zero extra API calls
- **Bug fix (Audit #7):** Sell side detection pakai `'A'` (HL format), bukan `'S'`. Sebelumnya sell vol selalu 0 → 100% buy bias.

---

## 7. Filter Entry

Urutan filter (scoring engine → signal handler → pre_trade_check):

| # | Filter | Kondisi Skip |
|---|---|---|
| 1 | Spread | > 0.15% |
| 2 | Score threshold | < base 45 + CHOPPY +8 + session + HTF adj + **EXTREME +15** + vote margin + OI gate + funding bonus |
| 3 | SHORT-specific | score < 52, funding < -0.0003, squeeze, tech_min < 6 |
| 4 | Funding crowded | LONG fr>0.05%, SHORT fr<-0.05% |
| 5 | ATR gate | LONG < **0.0013**, SHORT < 0.0015 |
| 6 | Min momentum | LONG < 0.15%, SHORT < 0.25% |
| 7 | Momentum confirm | Leading: 2/5 candles. Standard: 3/5 + 0.04% net |
| 8 | Trend structure veto | Direction vs EMA21 trend |
| 9 | Direction voting | 7-voter system determines LONG/SHORT |
| 10 | **Vote margin gate** | margin < 4 → threshold +5 |
| 11 | **OI conviction gate** | abs(OI score) < 6 → threshold +3 |
| 12 | **Funding negative bonus** | FR < 0 + LONG → threshold -3 (easier entry) |
| 13 | Displacement penalty | Regime-aware multiplier |
| 14 | Signal cooldown | 5 min per asset |
| 15 | Max positions | 3 concurrent (scalper) |
| 16 | Kill switch / pause | Drawdown > 95% or daily loss > 90% |

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
- **Performance:** 100% WR, 33% firing rate, avg PnL +$1.31/trade
- Avg fire time: minute 13.4

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

### Reasoning Display (via button)
Shows all `reasons` from scoring engine including:
- Direction votes: `🧭 Direction: LONG (votes: bull=7 bear=2)`
- Whale detection: `🐋 Whale buy flow 45% imbalance`
- All component contributions

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

### Bot Brain Section
- Live reasoning flow (step-by-step per asset)
- Pattern memory ranking (top winners/losers)
- ML stats (when active)

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
| **#7** | **22 Mei PM** | **57** | **52.6%** | **+$8.23** | **1.12** | **33.3%** | **+0.098** |

### Key Milestones
- **Audit #4:** First net profitable. Edge = trailing stop (100% WR).
- **Audit #5:** ATR gate deployed. Trailing fire rate 22%.
- **Audit #6:** Root cause found (OB counter-predictive). Direction voting implemented. Trailing 33%.
- **Audit #7:** Whale/CVD sell-side bug found & fixed. OI = best predictor (r=+0.211). Funding negative = WR 89%.

### Current Edge Analysis
- **Trailing stop** = sole profit source. 100% WR, 33% fire rate, +$32.36 in 57 trades.
- **time_exit** = sole loss source. 59.6% of trades, 29.4% WR, -$17.73.
- **ATR + Vol** = real predictor of trailing fire. Winners avg ATR 0.0026 vs losers 0.0017.
- **OI score** = best component predictor. OI≥6 = WR 69.6%, OI<6 = WR 41.2%.
- **Funding negative** = strongest edge signal. FR<0 + LONG = WR 88.9% (9 trades).
- **Whale/CVD** = were broken (sell side = 0). Fixed in Audit #7. Needs validation.

### Audit #7 Findings (22 Mei 2026 Malam)

**Bugs Found:**
1. Whale detection: HL uses `"A"` for sell, code checked `"S"` → sell vol always 0 → 100% buy bias (89.6% constant fire)
2. CVD calculation: same bug → always bullish → constant noise
3. Both fixed by adding `'A'` to sell side detection

**Data-Driven Additions:**
4. ATR gate LONG raised: 0.0010 → 0.0013 (dead zone elimination)
5. Vote margin gate: margin < 4 → threshold +5 (low consensus = coin flip)
6. OI conviction gate: abs(OI) < 6 → threshold +3 (no fundamental backing)
7. Funding negative bonus: FR < 0 + LONG → threshold -3 (contrarian edge)

**Key Data Points:**
- EXTREME regime = best performer (WR 88.9%, +$14.95) — don't over-filter
- Vote margin 8+ = WR 66.7%, +$11.87
- Leverage 30x underperforms 20x (WR 40% vs 55%)
- Hour 07 UTC (London open) = WR 86%

---

*Dokumen ini sinkron dengan Audit #7 (22 Mei 2026 malam). Untuk detail per-audit, lihat `KARA_SYSTEM_DOCUMENT.md`.*
