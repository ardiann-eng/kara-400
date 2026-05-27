# KARA AI Intelligence Layer — Proposal
## Mimo v2.5 Pro Integration

**Prinsip Utama:** AI = REASONING & EVALUATOR only. Bukan decision maker. Bot tetap trade berdasarkan scoring engine. AI memberikan context, confidence, dan post-trade analysis.

---

## Module 1: AI Market Analyst

### Fungsi
AI menganalisis market condition SEBELUM signal dieksekusi. Output = **advisory score** yang di-log tapi TIDAK memblokir trade (kecuali confidence sangat rendah).

### Input ke AI (per signal)
```json
{
  "asset": "CFX",
  "side": "LONG",
  "score": 62,
  "components": {"OB": 10, "EMA": 8, "RSI": 5, "FUND": 0, "XAM": 0},
  "regime": "ranging",
  "htf_regime": "CHOPPY",
  "momentum_move_pct": 0.35,
  "atr_pct": 0.0018,
  "funding_rate": -0.0001,
  "recent_candles": [0.0549, 0.0550, 0.0551, 0.0550, 0.0552],
  "volume_profile": [1200, 800, 1500, 900, 2100],
  "ob_imbalance": 0.35,
  "btc_move_5m": 0.08,
  "time_utc": "2026-05-28T14:30:00Z"
}
```

### Output dari AI
```json
{
  "confidence": 0.72,
  "regime_assessment": "Low-vol ranging. BTC flat. Altcoin momentum weak.",
  "fake_breakout_risk": 0.35,
  "momentum_quality": "medium",
  "volatility_condition": "compressed — breakout possible but direction unclear",
  "recommendation": "PROCEED_WITH_CAUTION",
  "reasoning": "OB wall present but in ranging regime = potential trap. EMA fresh but momentum only 0.35% — borderline. Suggest tighter SL or reduced size.",
  "suggested_adjustments": {
    "size_multiplier": 0.7,
    "sl_tighter": true
  }
}
```

### Bagaimana AI Digunakan (SCORE CONTRIBUTION)

```
Signal Generated (score calculated by scoring engine)
    │
    ├─→ AI Analyst evaluates (async, <500ms)
    │       │
    │       ├─ confidence >= 0.7 → ADD +8 to score (strong conviction)
    │       ├─ confidence 0.5-0.7 → ADD +4 to score (moderate)
    │       ├─ confidence 0.3-0.5 → ADD +0 (neutral — no contribution)
    │       └─ confidence < 0.3 → ADD -5 to score (AI sees red flags)
    │
    └─→ Final score = base_score + ai_adjustment
         │
         ├─ If final score >= threshold → EXECUTE
         └─ If final score < threshold → SKIP (AI helped filter bad setup)
```

**Mekanisme:**
- AI menjadi **komponen scoring tambahan** (seperti OB, EMA, RSI, dll)
- Max contribution: **+8 / -5 pts** (bounded — AI tidak bisa dominate)
- Stored di DB → displayed on Dashboard (AI Intelligence section)
- **TIDAK di-log ke console** — no spam

**Kenapa bounded ±8/-5:**
- +8 max = bisa bantu marginal signal (score 44) lolos threshold (52)
- -5 max = bisa block bad signal (score 50) jadi below threshold (45)
- Tapi AI TIDAK bisa override strong signal (score 70 - 5 = 65, masih lolos)
- Ini menjaga AI sebagai **evaluator**, bukan decision maker

**PENTING:** 
- AI TIDAK pernah single-handedly block atau approve trade
- AI hanya NUDGE score up/down dalam range kecil
- Semua AI output masuk ke **Dashboard section khusus** ("AI Intelligence")
- Dashboard menampilkan: confidence history, accuracy tracking, pattern insights

### Implementation Architecture

```
intelligence/
├── __init__.py
├── ai_analyst.py          # Main analyst class
├── market_context.py      # Gather market data for AI input
├── prompt_templates.py    # Structured prompts for Mimo
└── confidence_tracker.py  # Track AI confidence vs actual outcome
```

### API Call Design (Mimo v2.5 Pro — OpenAI-compatible SDK)

```python
from openai import AsyncOpenAI

class AIMarketAnalyst:
    """Non-blocking market analysis using Mimo v2.5 Pro (OpenAI-compatible)."""
    
    def __init__(self, api_key: str):
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.mimo-v2.com/v1"
        )
        self.model = "mimo-v2.5-pro"
        self.timeout = 2.0  # max 2 detik — jangan delay trade
        self.temperature = 0.2  # LOW: stable, deterministic output
        self.enabled = True
        self._cache = {}  # cache per asset per 60s
    
    async def evaluate_signal(self, signal_context: dict) -> AIVerdict:
        """
        Evaluate signal quality. Returns in <500ms.
        On timeout/error → return neutral verdict (score +0).
        """
        try:
            response = await self._call_mimo(signal_context)
            return self._parse_verdict(response)
        except Exception:
            # AI down = neutral verdict, no score adjustment
            return AIVerdict(confidence=0.5, score_adj=0, reasoning="AI unavailable")
    
    async def _call_mimo(self, context: dict) -> dict:
        """
        Call Mimo via OpenAI-compatible SDK.
        Temperature 0.2 = deterministic, stable confidence scores.
        """
        prompt = self._build_prompt(context)
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a crypto scalping microstructure analyst. "
                        "Evaluate trade setups and return confidence scores. "
                        "Be precise, data-driven, no speculation. "
                        "Always respond in valid JSON only."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0.2,      # LOW: stable, no hallucination
            max_tokens=300,       # Short response only
        )
        import json
        return json.loads(response.choices[0].message.content)
```

**Kenapa temperature 0.2:**
- Output stabil — same market condition → same confidence score
- Tidak "kreatif" — AI tidak invent patterns yang tidak ada
- Confidence tidak liar — 0.72 hari ini = 0.72 besok untuk setup yang sama
- Reproducible — bisa di-audit dan di-backtest
```

### Prompt Template (untuk Mimo)

```
You are an institutional crypto scalping analyst specialized in market microstructure, momentum continuation, and liquidity behavior.

Your task is to evaluate whether the following setup has a statistically favorable short-term expectancy for a leveraged scalping trade.

TRADE SETUP
-----------
Asset: {asset}
Direction: {side}

Signal Score: {score}/100
Market Regime: {regime}
HTF Regime: {htf_regime}

SIGNAL COMPONENTS
-----------------
Order Block: {ob}
EMA Alignment: {ema}
RSI State: {rsi}
Funding Bias: {fund}
Cross-Asset Momentum: {xam}

MICROSTRUCTURE DATA
-------------------
5m Momentum: {momentum_pct}%
ATR Volatility: {atr_pct}%
Funding Rate: {funding_rate}%
Orderbook Imbalance: {ob_imb}
BTC 5m Move: {btc_move}%
Volume Trend: {vol_trend}

EVALUATION RULES
----------------
- Favor momentum continuation during strong trend regimes.
- Penalize weak momentum with high ATR volatility.
- Penalize setups likely caused by liquidity sweeps or exhaustion spikes.
- Reduce confidence if BTC movement conflicts with trade direction.
- High funding + overextended momentum may indicate crowded positioning.
- Confidence must be calibrated realistically:
  - 0.50 = neutral / unclear edge
  - 0.60 = moderate edge
  - 0.70+ = strong confluence
  - Above 0.85 should be extremely rare

OUTPUT
------
Return JSON only with this exact schema:

{
  "confidence": 0.0,
  "fake_breakout_probability": 0.0,
  "momentum_quality": "weak|medium|strong",
  "market_state": "trend_continuation|exhaustion|chop|breakout|squeeze",
  "risk_note": "",
  "reasoning": ""
}
```

---

## Module 2: Trade Journaling AI

### Fungsi
Setelah trade CLOSED, AI menganalisis kenapa trade menang/kalah dan extract patterns. Output disimpan di DB untuk weekly review.

### Input ke AI (per closed trade)
```json
{
  "asset": "XPL",
  "side": "LONG",
  "score": 55,
  "entry_price": 0.090,
  "exit_price": 0.0895,
  "pnl_pct": -0.56,
  "pnl_usd": -1.39,
  "exit_reason": "momentum_death",
  "hold_minutes": 3,
  "max_favorable": 0.02,
  "max_adverse": -0.58,
  "components_at_entry": {"OB": 10, "EMA": 4, "RSI": 5, "FUND": 0},
  "regime_at_entry": "ranging",
  "htf_at_entry": "CHOPPY",
  "candles_during_trade": [0.090, 0.0899, 0.0898, 0.0899],
  "volume_during_trade": [500, 300, 200, 150],
  "ai_confidence_at_entry": 0.45,
  "similar_past_trades": [
    {"asset": "XPL", "pnl": -0.48, "reason": "time_exit"},
    {"asset": "XPL", "pnl": -0.62, "reason": "time_exit"}
  ]
}
```

### Output dari AI
```json
{
  "verdict": "LOSS — signal salah",
  "why_lost": "Entry di ranging market tanpa momentum. OB wall = trap (bid pulled). Volume declining = no buyer interest.",
  "pattern_detected": "XPL repeated loser in CHOPPY regime — 3 consecutive losses. Asset mungkin terlalu illiquid untuk scalping.",
  "exit_quality": "GOOD — momentum_death caught it early. Tanpa momentum_death, loss bisa -Rp15rb+.",
  "lesson": "Avoid XPL in CHOPPY regime. OB signal unreliable for low-liquidity alts.",
  "suggested_action": "ADD_TO_WATCHLIST",
  "confidence_calibration": "AI confidence was 0.45 (low) — correctly predicted weak setup. AI calibration: GOOD."
}
```

### Storage & Review — Dashboard Section

```python
# Setiap trade closed → AI journal entry → stored in DB → shown on Dashboard
class TradeJournal:
    async def analyze_trade(self, trade_data: dict) -> JournalEntry:
        """Generate AI analysis for closed trade. Stored in DB, shown on Dashboard."""
        response = await self.mimo.analyze(trade_data)
        entry = JournalEntry(
            trade_id=trade_data['trade_id'],
            ai_analysis=response,
            timestamp=utcnow(),
        )
        self.db.save_journal(entry)
        return entry
    
    async def weekly_summary(self) -> str:
        """Generate weekly pattern summary from all journal entries."""
        entries = self.db.get_entries_last_7_days()
        patterns = self._extract_patterns(entries)
        return await self.mimo.summarize_patterns(patterns)
```

### Dashboard AI Intelligence Section

Dashboard (`/dashboard`) mendapat tab/section baru: **"🧠 AI Intelligence"**

Layout:
```
┌─────────────────────────────────────────────────────┐
│ 🧠 AI INTELLIGENCE                                  │
├─────────────────────────────────────────────────────┤
│                                                     │
│ ┌─ AI Confidence Accuracy ────────────────────────┐ │
│ │ Confidence > 0.7: WR 62% (n=15)  ✅ Predictive │ │
│ │ Confidence 0.4-0.7: WR 44% (n=28)              │ │
│ │ Confidence < 0.4: WR 31% (n=11)  ✅ Predictive │ │
│ │ Calibration Score: 0.78 / 1.0                   │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ ┌─ Recent AI Verdicts ────────────────────────────┐ │
│ │ CFX LONG  | Conf: 0.72 | ✅ WIN  | "Strong OB" │ │
│ │ XPL LONG  | Conf: 0.38 | ❌ LOSS | "No momentum"│ │
│ │ AR SHORT  | Conf: 0.81 | ✅ WIN  | "Trend align"│ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ ┌─ Pattern Insights (AI Journal) ─────────────────┐ │
│ │ 🔴 XPL: 3 consecutive losses in CHOPPY         │ │
│ │ 🟢 AR: 100% WR when AI conf > 0.7              │ │
│ │ ⚠️ OB signal unreliable in ranging (AI agrees) │ │
│ │ 💡 Momentum death saved avg -$0.85/trade        │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ ┌─ AI Size Adjustments Today ─────────────────────┐ │
│ │ Full size (conf≥0.6): 35 trades                 │ │
│ │ Reduced ×0.7 (0.4-0.6): 18 trades              │ │
│ │ Reduced ×0.5 (conf<0.4): 7 trades              │ │
│ │ Impact: -$2.10 saved from reduced losers        │ │
│ └─────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

**Tidak ada log spam.** Semua AI data di DB, ditampilkan di Dashboard saja.

### Telegram Integration

Setelah setiap trade close, kirim journal summary ke admin:

```
📓 TRADE JOURNAL — XPL LONG

Verdict: ❌ Signal salah
Why: Entry di ranging tanpa momentum. Volume declining.
Pattern: XPL 3× loss berturut di CHOPPY
Exit Quality: ✅ Momentum death saved -Rp12rb potential loss
Lesson: Avoid XPL in CHOPPY regime

AI Confidence was: 0.45 (correctly low) ✅
```

---

## Implementation Timeline

| Phase | Waktu | Deliverable |
|-------|-------|-------------|
| 1 | 29-30 Mei | AI Analyst — basic confidence score (log only, no action) |
| 2 | 30-31 Mei | AI Analyst — size adjustment based on confidence |
| 3 | 1-2 Juni | Trade Journal — post-trade analysis |
| 4 | 3+ Juni | Weekly pattern summary + auto-watchlist |

### Phase 1 Detail (MVP — 29 Mei)

Minimal viable:
1. `intelligence/ai_analyst.py` — call Mimo, get confidence
2. Log confidence di signal breakdown (alongside OB, EMA, etc.)
3. **NO action taken** — purely observational
4. After 50+ trades: correlate AI confidence vs actual PnL

Ini memungkinkan kita VALIDATE apakah AI confidence actually predictive sebelum kita beri power untuk adjust size.

---

## Cost & Rate Limit Considerations

| Metric | Estimate |
|--------|----------|
| Signals/day | ~60 (2.5/hr × 24hr) |
| AI calls/day (analyst) | ~60 |
| AI calls/day (journal) | ~60 |
| Total | ~120 calls/day |
| Token per call | ~500 input + 200 output = 700 |
| Daily tokens | ~84,000 |

Mimo v2.5 Pro pricing TBD — tapi 84K tokens/day = sangat manageable untuk most API pricing.

---

## Guardrails

1. **AI NEVER blocks trade** — hanya advisory
2. **Timeout 2s** — kalau AI lambat, trade jalan tanpa AI
3. **Fallback = neutral** — AI error = confidence 0.5 = no adjustment
4. **No hallucination risk** — AI output di-parse as JSON, invalid = discard
5. **Correlation tracking** — setiap minggu cek: apakah AI confidence > 0.7 = better WR? Kalau tidak = AI tidak berguna, disable.
6. **Cost cap** — max 200 calls/day. Kalau exceed = disable sampai reset.

---

## Kenapa Ini Bagus untuk KARA

1. **Scoring engine tetap king** — AI hanya second opinion
2. **Measurable** — kita bisa prove/disprove AI value dengan data
3. **Low risk** — AI down = bot tetap jalan normal
4. **High upside** — kalau AI confidence predictive, kita punya edge tambahan
5. **Trade journal** — ini yang bikin bot IMPROVE over time (bukan hanya trade)
6. **Regime detection** — AI bisa detect regime shift yang rule-based system miss

---

## Pertanyaan untuk Kamu

1. Mimo v2.5 Pro — apa endpoint/API format-nya? (REST? SDK?)
2. Budget per hari untuk AI calls?
3. Mau Phase 1 (log only) dulu atau langsung Phase 2 (size adjust)?
4. Trade journal — kirim ke semua user atau admin only?
