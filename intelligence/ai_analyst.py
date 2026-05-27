"""
KARA AI Market Analyst — Mimo v2.5 Pro
Evaluates trade setups and provides confidence-based score adjustment.
AI = EVALUATOR only. Never blocks trades. Adds/subtracts bounded pts to score.

Usage:
    from intelligence.ai_analyst import ai_analyst
    verdict = await ai_analyst.evaluate_signal(signal_context)
    score += verdict.score_adj  # bounded ±8/-5
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("kara.intelligence")


@dataclass
class AIVerdict:
    """Result from AI evaluation."""
    confidence: float = 0.5
    score_adj: int = 0
    fake_breakout_risk: float = 0.0
    momentum_quality: str = "medium"
    market_state: str = "unknown"
    risk_note: str = ""
    reasoning: str = ""
    latency_ms: float = 0.0
    error: Optional[str] = None


class AIMarketAnalyst:
    """
    Non-blocking market analysis using Mimo v2.5 Pro (OpenAI-compatible).
    Temperature 0.2 for stable, deterministic output.
    """

    def __init__(self):
        self.api_key = os.getenv("MIMO_API_KEY", "")
        self.api_key_fallback = os.getenv("MIMO_API_KEY_FALLBACK", "")
        self.base_url = os.getenv("MIMO_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1")
        self.model = os.getenv("MIMO_MODEL", "mimo-v2.5-pro")
        self.temperature = 0.2
        self.timeout = 2.0
        self.enabled = bool(self.api_key)
        self._client = None
        self._client_fallback = None
        self._using_fallback = False
        self._primary_rate_limited = False
        self._cache: dict = {}
        self._cache_ttl = 60  # cache per asset for 60s
        self._daily_calls = 0
        self._daily_reset = 0
        self._max_daily = 200
        self._connected = False

        if self.enabled:
            log.info(f"[AI] Mimo analyst initialized (model={self.model}, url={self.base_url})")
        else:
            log.warning("[AI] Mimo analyst DISABLED (no MIMO_API_KEY)")

    async def health_check(self) -> bool:
        """
        Test AI connection on startup. Logs clear status.
        Called once at boot to confirm API reachable.
        """
        if not self.enabled:
            log.info("[AI-CONNECT] MIMO AI: DISABLED (no API key configured)")
            return False
        try:
            client = self._get_client()
            if not client:
                log.error("[AI-CONNECT] MIMO AI: FAILED (openai package not installed)")
                return False
            response = await client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
                temperature=0.1,
                max_tokens=5,
            )
            if response and response.choices:
                log.info(
                    f"[AI-CONNECT] MIMO AI: CONNECTED "
                    f"(model={self.model}, latency OK, key valid)"
                )
                self._connected = True
                return True
            else:
                log.error("[AI-CONNECT] MIMO AI: FAILED (empty response)")
                return False
        except Exception as e:
            err_type = type(e).__name__
            if "429" in str(e) or "RateLimit" in err_type:
                # Primary rate limited → try fallback key immediately
                if self.api_key_fallback and self._client_fallback:
                    try:
                        resp2 = await self._client_fallback.chat.completions.create(
                            model=self.model,
                            messages=[{"role": "user", "content": "ping"}],
                            temperature=0.1, max_tokens=5,
                        )
                        if resp2 and resp2.choices:
                            self._using_fallback = True
                            self._primary_rate_limited = True
                            self._connected = True
                            log.info(
                                f"[AI-CONNECT] MIMO AI: CONNECTED via FALLBACK KEY "
                                f"(primary rate limited, fallback OK)"
                            )
                            return True
                    except Exception:
                        pass
                log.warning(
                    f"[AI-CONNECT] MIMO AI: RATE LIMITED (key valid, but throttled). "
                    f"Will retry on next signal."
                )
                self._connected = True  # key is valid, just throttled
                return True
            elif "401" in str(e) or "Auth" in err_type:
                log.error(f"[AI-CONNECT] MIMO AI: AUTH FAILED (invalid API key)")
                self.enabled = False
                return False
            else:
                log.error(f"[AI-CONNECT] MIMO AI: CONNECTION ERROR ({err_type}: {e})")
                return False

    def _get_client(self):
        """Lazy-init OpenAI client. Returns fallback client if primary rate limited."""
        try:
            from openai import AsyncOpenAI
        except ImportError:
            log.warning("[AI] openai package not installed. AI disabled.")
            self.enabled = False
            return None

        if self._using_fallback and self._client_fallback:
            return self._client_fallback

        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=0,  # disable SDK auto-retry — we handle fallback ourselves
            )
        if self._client_fallback is None and self.api_key_fallback:
            self._client_fallback = AsyncOpenAI(
                api_key=self.api_key_fallback,
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=0,  # same — no auto-retry
            )
        return self._client

    def _check_rate_limit(self) -> bool:
        """Check daily call limit."""
        now = time.time()
        if now - self._daily_reset > 86400:
            self._daily_calls = 0
            self._daily_reset = now
        if self._daily_calls >= self._max_daily:
            return False
        return True

    def _confidence_to_score_adj(self, confidence: float) -> int:
        """Convert AI confidence to bounded score adjustment."""
        if confidence >= 0.7:
            return +8
        elif confidence >= 0.5:
            return +4
        elif confidence >= 0.3:
            return 0
        else:
            return -5

    async def evaluate_signal(self, context: dict) -> AIVerdict:
        """
        Evaluate signal quality. Returns AIVerdict with score_adj.
        On timeout/error → neutral verdict (score_adj=0).
        Max latency: 2s. Never blocks trade execution.
        """
        if not self.enabled:
            return AIVerdict(reasoning="AI disabled")

        if not self._check_rate_limit():
            return AIVerdict(reasoning="Daily limit reached")

        # Cache check (same asset within 60s = same verdict)
        cache_key = f"{context.get('asset')}_{context.get('side')}_{int(time.time() // self._cache_ttl)}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        start = time.time()
        try:
            verdict = await asyncio.wait_for(
                self._call_mimo(context),
                timeout=self.timeout
            )
            verdict.latency_ms = (time.time() - start) * 1000
            self._daily_calls += 1
            self._cache[cache_key] = verdict
            return verdict
        except asyncio.TimeoutError:
            return AIVerdict(error="timeout", reasoning="AI timeout — neutral")
        except Exception as e:
            return AIVerdict(error=str(e), reasoning=f"AI error: {e}")

    async def _call_mimo(self, context: dict) -> AIVerdict:
        """Call Mimo via OpenAI-compatible SDK. Auto-fallback to key 2 on rate limit."""
        client = self._get_client()
        if not client:
            return AIVerdict(reasoning="Client not available")

        prompt = self._build_prompt(context)
        _system = (
            "You are an institutional crypto scalping microstructure analyst. "
            "Evaluate trade setups and return confidence scores. "
            "Be precise, data-driven, no speculation. "
            "Confidence calibration: 0.50=neutral, 0.60=moderate edge, "
            "0.70+=strong confluence, above 0.85=extremely rare. "
            "Always respond in valid JSON only."
        )
        _messages = [{"role": "system", "content": _system}, {"role": "user", "content": prompt}]

        try:
            response = await client.chat.completions.create(
                model=self.model, messages=_messages,
                temperature=self.temperature, max_tokens=300,
            )
            raw = response.choices[0].message.content.strip()
            return self._parse_response(raw)

        except Exception as e:
            # Rate limit on primary → auto-switch to fallback key
            if ("429" in str(e) or "RateLimit" in type(e).__name__) and not self._using_fallback and self._client_fallback:
                self._using_fallback = True
                self._primary_rate_limited = True
                log.info("[AI] Primary key rate limited → switching to fallback key automatically")
                try:
                    response = await self._client_fallback.chat.completions.create(
                        model=self.model, messages=_messages,
                        temperature=self.temperature, max_tokens=300,
                    )
                    raw = response.choices[0].message.content.strip()
                    return self._parse_response(raw)
                except Exception as e2:
                    return AIVerdict(error=str(e2), reasoning=f"Both keys failed: {e2}")
            raise  # re-raise for caller to handle

    def _build_prompt(self, ctx: dict) -> str:
        """Build structured prompt from signal context."""
        return f"""Evaluate this crypto scalping setup:

TRADE SETUP
-----------
Asset: {ctx.get('asset', 'UNKNOWN')}
Direction: {ctx.get('side', 'UNKNOWN')}
Signal Score: {ctx.get('score', 0)}/100
Market Regime: {ctx.get('regime', 'unknown')}
HTF Regime: {ctx.get('htf_regime', 'unknown')}

SIGNAL COMPONENTS
-----------------
Orderbook: {ctx.get('components', {}).get('OB', 0)}
EMA: {ctx.get('components', {}).get('EMA', 0)}
RSI: {ctx.get('components', {}).get('RSI', 0)}
Funding: {ctx.get('components', {}).get('FUND', 0)}
XAM: {ctx.get('components', {}).get('XAM', 0)}

MICROSTRUCTURE DATA
-------------------
5m Momentum: {ctx.get('momentum_move_pct', 0)*100:.3f}%
ATR Volatility: {ctx.get('atr_pct', 0)*100:.3f}%
Funding Rate: {ctx.get('funding_rate', 0)*100:.4f}%
OB Imbalance: {ctx.get('ob_imbalance', 0):.2f}
BTC 7m Move: {ctx.get('btc_move', 0)*100:.3f}%
Volume Trend: {ctx.get('volume_trend', 'unknown')}

EVALUATION RULES
----------------
- Favor momentum continuation during strong trend regimes.
- Penalize weak momentum with high ATR volatility.
- Penalize setups likely caused by liquidity sweeps or exhaustion spikes.
- Reduce confidence if BTC movement conflicts with trade direction.
- High funding + overextended momentum = crowded positioning risk.
- In CHOPPY/ranging regime, OB walls are often traps (reduce confidence).

Return JSON only:
{{"confidence": 0.0, "fake_breakout_probability": 0.0, "momentum_quality": "weak|medium|strong", "market_state": "trend_continuation|exhaustion|chop|breakout|squeeze", "risk_note": "", "reasoning": ""}}"""

    def _parse_response(self, raw: str) -> AIVerdict:
        """Parse AI JSON response into AIVerdict."""
        try:
            # Handle markdown code blocks
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw.strip())
            confidence = float(data.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))

            return AIVerdict(
                confidence=confidence,
                score_adj=self._confidence_to_score_adj(confidence),
                fake_breakout_risk=float(data.get("fake_breakout_probability", 0)),
                momentum_quality=data.get("momentum_quality", "medium"),
                market_state=data.get("market_state", "unknown"),
                risk_note=data.get("risk_note", ""),
                reasoning=data.get("reasoning", ""),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            return AIVerdict(error=f"parse_error: {e}", reasoning="Invalid AI response")


# Singleton instance
ai_analyst = AIMarketAnalyst()


def _ensure_ai_table():
    """Create ai_verdicts table if not exists."""
    try:
        from core.db import user_db
        conn = user_db._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_verdicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                score_before INTEGER,
                score_after INTEGER,
                confidence REAL,
                score_adj INTEGER,
                fake_breakout_risk REAL,
                momentum_quality TEXT,
                market_state TEXT,
                reasoning TEXT,
                latency_ms REAL,
                pnl REAL,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            )
        """)
        conn.commit()
    except Exception as e:
        log.debug(f"[AI] Table creation: {e}")


def save_ai_verdict(asset: str, side: str, score_before: int, verdict: AIVerdict, trade_id: str = None):
    """Save AI verdict to DB for dashboard display."""
    try:
        _ensure_ai_table()
        from core.db import user_db
        conn = user_db._get_conn()
        conn.execute(
            """INSERT INTO ai_verdicts 
               (trade_id, asset, side, score_before, score_after, confidence, 
                score_adj, fake_breakout_risk, momentum_quality, market_state, 
                reasoning, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade_id, asset, side, score_before,
                score_before + verdict.score_adj,
                verdict.confidence, verdict.score_adj,
                verdict.fake_breakout_risk, verdict.momentum_quality,
                verdict.market_state, verdict.reasoning, verdict.latency_ms,
            )
        )
        conn.commit()
    except Exception as e:
        log.debug(f"[AI] Save verdict failed: {e}")


def update_verdict_pnl(asset: str, side: str, pnl: float):
    """Update PnL for the most recent verdict matching asset+side (for accuracy tracking)."""
    try:
        from core.db import user_db
        conn = user_db._get_conn()
        conn.execute(
            """UPDATE ai_verdicts SET pnl = ? 
               WHERE id = (
                   SELECT id FROM ai_verdicts 
                   WHERE asset = ? AND side = ? AND pnl IS NULL
                   ORDER BY created_at DESC LIMIT 1
               )""",
            (pnl, asset, side)
        )
        conn.commit()
    except Exception as e:
        log.debug(f"[AI] Update PnL failed: {e}")
