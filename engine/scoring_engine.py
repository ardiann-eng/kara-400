"""
KARA Bot - Scoring Engine (Heart of KARA)
Orchestrates all analyzers, detects market regime, applies session bias.
Produces a final 0-100 score with full explanation.

BUGFIX 2026-04-05:
- Bug #1: Wrap data fetches in try/except, return None on failure (don't score with zeros)
- Bug #2: Removed pre-determined side_bias. Each analyzer returns bull/bear evidence.
  Direction is decided AFTER all evidence is tallied.
"""

from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple, List
import uuid

import config
from config import SIGNAL, RISK
from models.schemas import (
    TradeSignal, ScoreBreakdown, SignalStrength, Side, MarketRegime,
    FundingData, OIData, OrderbookSnapshot, LiquidationMap
)
from engine.analyzers.oi_funding_analyzer  import OIFundingAnalyzer
from engine.analyzers.liquidation_analyzer import LiquidationAnalyzer
from engine.analyzers.orderbook_analyzer   import OrderbookAnalyzer
from config import SIGNAL, SCALPER

# -- Intelligence Layer --
from intelligence.feature_engine import extract_live_features
from intelligence.intelligence_model import intelligence_model
from risk.risk_manager import RiskManager
from data.hyperliquid_client     import HyperliquidClient
from data.ws_client              import MarketDataCache

log = logging.getLogger("kara.scoring")


class ScoringEngine:
    """
    Main scoring pipeline. For each asset:
    1. Fetch market data (with error handling — skip asset on failure)
    2. Detect market regime (TRENDING/RANGING/VOLATILE)
    3. Run OI+Funding analyzer   -> bull/bear evidence
    4. Run Liquidation analyzer  -> bull/bear evidence
    5. Run Orderbook analyzer    -> bull/bear evidence
    6. Tally total bull vs bear → determine direction
    7. Apply session bonus + regime multiplier
    8. Emit TradeSignal if score ≥ threshold
    """

    def __init__(
        self,
        hl_client: HyperliquidClient,
        cache: MarketDataCache,
        risk_mgr: Optional[RiskManager] = None,
        mode_manager=None,
    ):
        self.client    = hl_client
        self.cache     = cache
        self.risk_mgr  = risk_mgr or RiskManager()
        self.mode_mgr  = mode_manager  # injected from main.py
        self.oi_funding_analyzer  = OIFundingAnalyzer()
        self.liq_analyzer         = LiquidationAnalyzer()
        self.ob_analyzer          = OrderbookAnalyzer()

        # Semaphores for rate limiting (Task 4)
        self.candle_sem = asyncio.Semaphore(3)

        # Cooldown tracking: asset_mode -> last signal timestamp
        self._last_signal_ts: Dict[str, float] = {}

        # Price history for regime detection: asset -> [(ts, price)]
        self._price_history: Dict[str, list] = {}

        # OI history for change calculation: asset -> [(ts, oi_usd)]
        from core.db import user_db
        self._oi_snapshots = user_db.load_all_oi_snapshots()
        if self._oi_snapshots:
            log.info(f"💾 Loaded {len(self._oi_snapshots)} cached OI snapshot histories from DB.")

        # Volatility cache: asset -> (ts, regime, realized_vol, trend)
        # Load dari SQLite supaya tidak fetch 100 candles tiap restart
        self._vol_cache: Dict[str, tuple] = {}
        self._load_vol_cache_from_db()

        # Spot-Perp basis cache (global)
        self._spot_prices: Dict[str, float] = {}
        self._spot_cache_time: float = 0.0

        # MTF Trend Cache: asset -> (timestamp, trend_string)
        self._mtf_cache: Dict[str, Tuple[float, str]] = {}

    def dump_oi_state(self):
        """Persist OI snapshots to database to prevent amnesia on restart."""
        from core.db import user_db
        user_db.save_oi_snapshots_batch(self._oi_snapshots)

    def _load_vol_cache_from_db(self):
        """Load volatility cache dari SQLite saat startup — cegah 100 candleSnapshot sekaligus."""
        from core.db import user_db
        import sqlite3
        loaded = 0
        expired = 0
        now_ts = time.time()
        try:
            conn = user_db._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT asset, regime, realized_vol, trend, cached_at FROM vol_cache")
            rows = cursor.fetchall()
            for asset, regime_str, realized_vol, trend, cached_at in rows:
                age = now_ts - cached_at
                if age < 3600:
                    # Masih valid — masukkan ke memory cache
                    # Kurangi waktu sisa sesuai umur entry supaya TTL tetap akurat
                    self._vol_cache[asset] = (
                        time.monotonic() - age,
                        MarketRegime(regime_str),
                        float(realized_vol),
                        float(trend)
                    )
                    loaded += 1
                else:
                    expired += 1
            log.info(
                f"[VOL] Loaded {loaded} cached regimes from DB "
                f"({expired} expired, will fetch lazily)"
            )
        except Exception as e:
            log.warning(f"[VOL] Failed to load vol_cache from DB: {e}")

    # ──────────────────────────────────────────
    # PUBLIC
    # ──────────────────────────────────────────

    async def simulate_score(self, params: dict) -> dict:
        """
        Simulate what score would be given specific market params.
        Used for testing and calibration.
        Returns full breakdown.
        """
        asset = "BTC"
        mark_price = 80000.0

        # Build dummy structures
        funding = FundingData(
            asset=asset,
            funding_rate=params.get("funding_rate", 0),
            premium=0,
            predicted_rate=None,
            hourly_trend=[]
        )
        oi = OIData(
            asset=asset,
            open_interest=100000000.0,
            oi_change_pct=params.get("oi_change_1h", 0),
            oi_change_24h=0
        )

        history = [0.0] * 10

        ob = OrderbookSnapshot(
            asset=asset,
            bids=[[mark_price-10, 10]],
            asks=[[mark_price+10, 10]],
            mid_price=mark_price,
            spread_pct=0.0001,
            bid_ask_imbalance=params.get("imbalance", 0),
            vwap=mark_price * (1 - params.get("vwap_dev", 0)),
            vwap_deviation_pct=params.get("vwap_dev", 0)
        )

        regime = params.get("regime", MarketRegime.TRENDING)

        # Run analyzers — returns bull/bear evidence
        spot_price = mark_price # use mark_price as dummy spot for simulate_score
        oi_bull, oi_bear, oi_reasons, oi_warns = self.oi_funding_analyzer.analyze(
            asset, funding, oi, history, 0.005, mark_price, spot_price
        )

        liq_bull, liq_bear, liq_reasons, liq_warns, liq_map = self.liq_analyzer.analyze(
            asset, mark_price, [], oi.open_interest,
            funding_rate=funding.funding_rate
        )
        if "cascade_risk" in params:
            liq_map.cascade_risk = params["cascade_risk"]

        ob_bull, ob_bear, ob_reasons, ob_warns = self.ob_analyzer.analyze(ob, [])

        # Tally
        total_bull = oi_bull + liq_bull + ob_bull
        total_bear = oi_bear + liq_bear + ob_bear

        # Direction + margin scoring (matches run_asset)
        margin = abs(total_bull - total_bear)
        confidence_bonus = min(margin * 2, 15)

        if total_bull > total_bear:
            side = Side.LONG
            raw_score = total_bull + confidence_bonus
        elif total_bear > total_bull:
            side = Side.SHORT
            raw_score = total_bear + confidence_bonus
        else:
            side = Side.LONG
            raw_score = total_bull

        # Session bonus
        session_bonus = params.get("session_bonus", 0)
        raw_score += session_bonus
        raw_score = max(0, min(raw_score, 85))

        # Dynamic Multipliers (Sync with _calculate_score)
        vol_multiplier   = 1.0  # Assume normal for simulation
        trend_pct        = params.get("trend_pct", 0.0)
        trend_multiplier = 1.10 if abs(trend_pct) > 0.015 else 0.95
        
        final_score = int(raw_score * vol_multiplier * trend_multiplier)
        final_score = max(0, min(final_score, 100))

        return {
            "score": final_score,
            "raw": raw_score,
            "side": side.value,
            "total_bull": total_bull,
            "total_bear": total_bear,
            "session_bonus": session_bonus,
        }

    async def run_asset(self, asset: str, active_modes: List[str] = None, meta_data=None) -> Tuple[Dict[str, TradeSignal], int]:
        """
        Run full scoring pipeline for one asset.
        """
        import config
        if active_modes is None:
            active_modes = ["standard"]
            
        signals = {}
        max_score_found = 0
        now_ts = time.monotonic()

        # 1. SCALPER MODE
        score_scl = 0
        if "scalper" in active_modes:
            try:
                cooldown_secs = 1 * 60 # Default
                if hasattr(config, 'SCALPER'):
                    cooldown_secs = config.SCALPER.signal_cooldown_minutes * 60

                last_ts = self._last_signal_ts.get(f"{asset}_scalper", 0)
                sig, score_scl = await self._run_scalper(asset, meta_data)

                if score_scl > max_score_found: max_score_found = score_scl

                if sig and (now_ts - last_ts >= cooldown_secs):
                    signals["scalper"] = sig
                    self._last_signal_ts[f"{asset}_scalper"] = now_ts
                elif sig:
                    log.debug(f"{asset} [SCALPER]: cooldown active")
            except RuntimeError as e:
                if "backoff" in str(e):
                    log.debug(f"[SCAN] {asset}: skipped (API backoff)")
                    return {}, 0
                log.error(f"Error in scalper: {e}")
            except Exception as e:
                log.error(f"Error in scalper: {e}")

        # 2. STANDARD MODE
        score_std = 0
        if "standard" in active_modes:
            try:
                cooldown_secs = config.SIGNAL.signal_cooldown_minutes * 60
                last_ts = self._last_signal_ts.get(f"{asset}_standard", 0)

                sig, score_std = await self._run_standard(asset, meta_data)

                if score_std > max_score_found: max_score_found = score_std

                if sig and (now_ts - last_ts >= cooldown_secs):
                    signals["standard"] = sig
                    self._last_signal_ts[f"{asset}_standard"] = now_ts
            except RuntimeError as e:
                if "backoff" in str(e):
                    log.debug(f"[SCAN] {asset}: skipped (API backoff)")
                    return {}, 0
                log.error(f"Error in standard: {e}")
            except Exception as e:
                log.error(f"Error in standard: {e}")

        return signals, max_score_found

    # ──────────────────────────────────────────
    # SCALPER SCORING
    # ──────────────────────────────────────────

    async def _run_scalper(self, asset: str, meta_data=None) -> Tuple[Optional[TradeSignal], int]:
        """
        Ultra-fast scoring for Scalper Mode.
        Uses: Orderbook Imbalance, CVD, EMA8/21, RSI(14), Volume Surge.
        Targets score >= 45 (vs 56 for standard).
        """
        import config

        # [FIX 4] Block London open hours (08-09 UTC) - WR 7.1% di jam 08, 21.4% di jam 09
        current_hour = datetime.now(timezone.utc).hour
        if current_hour in config.BLOCKED_HOURS_UTC:
            log.debug(f"{asset} [SCALPER]: blocked hour {current_hour} UTC (London open spike)")
            return None, 0

        # 1. Get mark price
        mark_price = await self.client.get_mark_price(asset, meta=meta_data)
        if mark_price <= 0:
            return None, 0

        # 2. Fetch 1-minute candles (last 30 for EMA/RSI)
        candles = []
        try:
            # Use candle_semaphore (3) as requested
            async with self.candle_sem:
                import time as _time
                now_ms = int(_time.time() * 1000)
                start_ms = now_ms - 30 * 60 * 1000  # last 30 minutes
                resp, succ = await self.client._call_info_endpoint(
                    "candleSnapshot",
                    {"req": {"coin": asset, "interval": "1m", "startTime": start_ms, "endTime": now_ms}}
                )
                if succ and isinstance(resp, list):
                    candles = resp
        except Exception as e:
            log.debug(f"[SCALPER] {asset} candle fetch failed: {e}")

        # 3. Fetch 15m MTF trend
        mtf_trend = await self._fetch_15m_mtf_data(asset)

        # 4. Compute scalper indicators
        score, side, reasons = self._calculate_scalper_score(asset, mark_price, candles, mtf_trend)

        # [FIX 3] Block SHORT trades until scoring is fixed
        if side == Side.SHORT and not config.ALLOW_SHORT:
            log.debug(f"{asset} [SCALPER]: SHORT blocked (ALLOW_SHORT=False). WR=31.4% data proof.")
            return None, score

        # 3c. Apply session bonus/penalty so adaptive threshold can use it.
        session_bonus, session_reasons = self._get_session_bonus()
        score = max(0, min(score + session_bonus, 100))
        reasons.extend(session_reasons)

        if score < config.SCALPER.min_score_to_enter:
            log.debug(f"[SCALPER] {asset}: score {score} < {config.SCALPER.min_score_to_enter}")
            return None, score

        # 3d. Apply Meta-Learning adjustment
        meta_delta, meta_reason, pattern_key = self._apply_meta_learning(asset, "scalper", side, score)
        if meta_delta != 0:
            score += meta_delta
            reasons.append(meta_reason)
        score = max(0, min(score, 100))

        # 4. Build signal with scalper TP/SL (now dynamic based on Volatility)
        vol_regime, realized_vol, trend_pct = await self._fetch_vol_regime(asset)
        signal = self._build_scalper_signal(asset, side, score, mark_price, reasons, vol_regime, session_bonus, realized_vol)
        signal.meta_pattern_key = pattern_key
        signal.meta_score_delta = meta_delta
        
        log.info(f"⚡ SCALPER SIGNAL: {asset} {side.value.upper()} score={score} (Meta: {meta_delta:+d})")
        return signal, score

    async def _fetch_scalper_mtf_candles(self, asset: str) -> list:
        """Fetch higher-timeframe candles for scalper confirmation."""
        candles_15m = []
        try:
            async with self.candle_sem:
                now_ms = int(time.time() * 1000)
                lookback = max(16, int(getattr(config.SCALPER, "mtf_confirm_lookback", 32)))
                interval = getattr(config.SCALPER, "mtf_confirm_interval", "15m")
                start_ms = now_ms - lookback * 15 * 60 * 1000
                resp, succ = await self.client._call_info_endpoint(
                    "candleSnapshot",
                    {"req": {"coin": asset, "interval": interval, "startTime": start_ms, "endTime": now_ms}}
                )
                if succ and isinstance(resp, list):
                    candles_15m = resp
        except Exception as e:
            log.debug(f"[SCALPER] {asset} 15m fetch failed: {e}")
        return candles_15m

    def _scalper_mtf_confirm(self, side: Side, candles_15m: list) -> Tuple[bool, str, MarketRegime]:
        """
        Confirm 1m scalper signal with higher timeframe trend (15m).
        Returns (is_confirmed, reason, derived_regime).
        """
        import config
        if not getattr(config.SCALPER, "mtf_confirm_enabled", True):
            return True, "🧭 MTF confirm disabled", MarketRegime.UNKNOWN

        closes = []
        for c in candles_15m:
            if isinstance(c, dict):
                try:
                    closes.append(float(c.get("c", 0)))
                except (TypeError, ValueError):
                    pass

        if len(closes) < 21:
            return False, "🧭 MTF blocked: 15m data not enough", MarketRegime.UNKNOWN

        def ema(data: list, period: int) -> float:
            k = 2 / (period + 1)
            e = data[0]
            for v in data[1:]:
                e = v * k + e * (1 - k)
            return e

        window = closes[-32:] if len(closes) >= 32 else closes
        ema9 = ema(window, 9)
        ema21 = ema(window, 21)
        trend = (window[-1] - window[0]) / max(window[0], 1e-9)

        # Realized volatility proxy on 15m closes
        rets = []
        for i in range(1, len(window)):
            prev = window[i - 1]
            if prev > 0:
                rets.append((window[i] - prev) / prev)
        vol = 0.0
        if rets:
            mean_r = sum(rets) / len(rets)
            var = sum((r - mean_r) ** 2 for r in rets) / len(rets)
            vol = var ** 0.5

        if vol > 0.010:
            regime = MarketRegime.VOLATILE
        elif abs(trend) >= 0.006:
            regime = MarketRegime.TRENDING
        else:
            regime = MarketRegime.RANGING

        long_ok = ema9 > ema21 and trend > -0.002
        short_ok = ema9 < ema21 and trend < 0.002
        confirmed = long_ok if side == Side.LONG else short_ok

        if confirmed:
            return True, f"🧭 15m confirm OK (EMA9/21, trend {trend*100:.2f}%)", regime
        return False, f"🧭 MTF blocked: 15m trend not aligned ({trend*100:.2f}%)", regime

    def _calculate_scalper_score(self, asset: str, mark_price: float, candles: list, mtf_trend: str = "neutral") -> Tuple[int, Side, List[str]]:
        """
        Fast scalper scoring using technical indicators on 1m candles.
        Returns (score: int, side: Side, reasons: List[str])
        """
        score = 0
        bull_pts = 0
        bear_pts = 0
        reasons = []

        # ── Orderbook Imbalance (from cache) ─────────────────────────
        ob = self.cache.orderbook.get(asset) if hasattr(self.cache, 'orderbook') else None
        if ob:
            imb = 0.0
            try:
                # WS cache stores raw levels; compute imbalance like standard pipeline.
                levels = ob.get("levels", [[], []]) if isinstance(ob, dict) else [[], []]
                bids_raw = levels[0] if len(levels) > 0 else []
                asks_raw = levels[1] if len(levels) > 1 else []

                def parse_lvl(x):
                    if isinstance(x, dict):
                        return float(x.get("px", 0)), float(x.get("sz", 0))
                    try:
                        return float(x[0]), float(x[1])
                    except Exception:
                        return 0.0, 0.0

                bids = [parse_lvl(b) for b in bids_raw[:20]]
                asks = [parse_lvl(a) for a in asks_raw[:20]]
                bid_liq = sum(px * sz for px, sz in bids if px > 0 and sz > 0)
                ask_liq = sum(px * sz for px, sz in asks if px > 0 and sz > 0)
                if (bid_liq + ask_liq) > 0:
                    imb = (bid_liq - ask_liq) / (bid_liq + ask_liq)
                
                # Spread Filter (Institutional Filter)
                if bids and asks and bids[0][0] > 0:
                    spread_pct = (asks[0][0] - bids[0][0]) / asks[0][0]
                    if spread_pct > 0.0008:
                        log.info(f"[{asset}] SCALPER REJECT: Spread too wide ({spread_pct*100:.2f}%)")
                        return 0, Side.LONG, ["REJECT: Spread too wide"]
            except Exception:
                imb = 0.0

            if imb > 0.60:
                bull_pts += 20
                reasons.append(f"📗 Strong bid wall (imbalance {imb:.2f}) → LONG")
            elif imb < -0.60:
                bear_pts += 20
                reasons.append(f"📕 Strong ask wall (imbalance {imb:.2f}) → SHORT")
            elif imb > 0.40:
                bull_pts += 8
                reasons.append(f"🟢 Mild bid pressure ({imb:.2f})")
            elif imb < -0.40:
                bear_pts += 8
                reasons.append(f"🔴 Mild ask pressure ({imb:.2f})")

        if len(candles) < 10:
            # Cannot compute EMA/RSI without data — use only OB score
            total = bull_pts + bear_pts
            side = Side.LONG if bull_pts >= bear_pts else Side.SHORT
            # Skip setting bull_candles/bear_candles — default 0
            bull_candles = 0
            bear_candles = 0
            score = min(total, 100)
            return score, side, reasons

        # Extract OHLCV
        closes = []
        opens = []
        volumes = []
        for c in candles:
            if isinstance(c, dict):
                try:
                    closes.append(float(c.get("c", 0)))
                    opens.append(float(c.get("o", 0)))
                    volumes.append(float(c.get("v", 0)))
                except (ValueError, TypeError):
                    pass

        if len(closes) < 10:
            side = Side.LONG if bull_pts >= bear_pts else Side.SHORT
            return min(bull_pts + bear_pts, 100), side, reasons

        # ── Momentum Confirmation (Institutional Filter) ─────────────────
        bull_candles = 0
        bear_candles = 0
        if len(closes) >= 3:
            bull_candles = sum(1 for c, o in zip(closes[-3:], opens[-3:]) if c > o)
            bear_candles = sum(1 for c, o in zip(closes[-3:], opens[-3:]) if c < o)
            
            if bull_candles >= 2:
                bull_pts += 10
                reasons.append(f"🔥 Momentum 1m Bullish ({bull_candles}/3 Green)")
            elif bear_candles >= 2:
                bear_pts += 10
                reasons.append(f"🩸 Momentum 1m Bearish ({bear_candles}/3 Red)")
            else:
                reasons.append("⚖️ Momentum 1m Neutral")

        # ── EMA 8 vs EMA 21 ──────────────────────────────────────────
        def ema(data: list, period: int) -> float:
            k = 2 / (period + 1)
            e = data[0]
            for v in data[1:]:
                e = v * k + e * (1 - k)
            return e

        ema8  = ema(closes[-21:], 8)  if len(closes) >= 8  else closes[-1]
        ema21 = ema(closes[-21:], 21) if len(closes) >= 21 else closes[-1]

        if ema8 > ema21 * 1.0005:   # EMA8 clearly above EMA21
            bull_pts += 15
            reasons.append(f"📈 EMA8 ({ema8:.4f}) > EMA21 ({ema21:.4f}) → bullish")
        elif ema8 < ema21 * 0.9995:
            bear_pts += 15
            reasons.append(f"📉 EMA8 ({ema8:.4f}) < EMA21 ({ema21:.4f}) → bearish")

        # ── RSI 14 (1m) ──────────────────────────────────────────────
        if len(closes) >= 15:
            gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
            losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses[-14:]) / 14
            if avg_loss > 0:
                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))
            else:
                rsi = 100.0

            if rsi < 35:
                bull_pts += 15
                reasons.append(f"📊 RSI oversold ({rsi:.1f}) → buy signal")
            elif rsi > 65:
                bear_pts += 15
                reasons.append(f"📊 RSI overbought ({rsi:.1f}) → sell signal")
            else:
                reasons.append(f"📊 RSI neutral ({rsi:.1f})")

        # ── Short-term CVD (last 80 trades from cache) ────────────────
        recent_trades = self.cache.trades.get(asset, []) if hasattr(self.cache, 'trades') else []
        if len(recent_trades) >= 20:
            sample = recent_trades[-80:]
            buy_vol = sum(float(t.get('sz', 0)) for t in sample if t.get('side', '') in ('B', 'buy', 'Ask'))
            sell_vol = sum(float(t.get('sz', 0)) for t in sample if t.get('side', '') in ('S', 'sell', 'Bid'))
            total_vol = buy_vol + sell_vol
            if total_vol > 0:
                cvd_ratio = (buy_vol - sell_vol) / total_vol
                if cvd_ratio > 0.20:
                    bull_pts += 12
                    reasons.append(f"💚 CVD bullish ({cvd_ratio*100:.0f}% net buy pressure)")
                elif cvd_ratio < -0.20:
                    bear_pts += 12
                    reasons.append(f"❤️ CVD bearish ({cvd_ratio*100:.0f}% net sell pressure)")

        # ── Volume Surge (2min vs 10min average) ──────────────────────
        if len(volumes) >= 10:
            avg_10m = sum(volumes[-10:]) / 10
            avg_2m  = sum(volumes[-2:])  / 2
            if avg_10m > 0:
                surge = avg_2m / avg_10m
                if surge > 2.5:
                    # High volume surge — follow the direction
                    extra = min(int((surge - 2.5) * 5), 10)
                    reasons.append(f"🔥 Volume surge {surge:.1f}x avg (+{extra} pts momentum)")
                    if bull_pts >= bear_pts:
                        bull_pts += extra
                    else:
                        bear_pts += extra

        # ── Market Structure HH/HL (cache-first: reuse existing 1m candles) ──
        trend_state = self._infer_hh_hl_structure(closes)
        import config
        if trend_state == "bull":
            bull_pts += config.SIGNAL.structure_scalper_bonus
            reasons.append(f"🧩 1m structure HH/HL (+{config.SIGNAL.structure_scalper_bonus})")
        elif trend_state == "bear":
            bear_pts += config.SIGNAL.structure_scalper_bonus
            reasons.append(f"🧩 1m structure LH/LL (+{config.SIGNAL.structure_scalper_bonus})")
        else:
            reasons.append("🧩 1m structure neutral")

        # ── Final tally & Consensus Filter ────────────────────────────
        side = Side.LONG if bull_pts >= bear_pts else Side.SHORT
        raw = (bull_pts if side == Side.LONG else bear_pts)
        
        # ── MTF Confirmation (15m Trend Alignment) ──────────────────
        import config
        scfg = config.SCALPER
        if mtf_trend != "neutral":
            if (side == Side.LONG and mtf_trend == "bull") or (side == Side.SHORT and mtf_trend == "bear"):
                raw += scfg.mtf_score_bonus
                reasons.append(f"📡 15m MTF Align ({mtf_trend}) → +{scfg.mtf_score_bonus}")
            else:
                raw += scfg.mtf_score_penalty
                reasons.append(f"📡 15m MTF Discord ({mtf_trend}) → {scfg.mtf_score_penalty}")
                # Hard reject for scalper if 15m trend is strongly against us
                if abs(scfg.mtf_score_penalty) > 10 and raw < 60:
                    log.debug(f"[{asset}] SCALPER REJECT: Against 15m MTF trend")
                    return 0, side, reasons + ["REJECT: Counter-trend MTF"]

        # Enforce Scalper Consensus: Must have momentum alignment
        # (Now uses function-level vars set above, not locals())
        if side == Side.LONG and bear_candles >= 2:
            log.debug(f"[{asset}] SCALPER REJECT: LONG signal blocked by 1m Bearish Momentum")
            return 0, side, reasons + ["REJECT: Momentum against trade"]
        if side == Side.SHORT and bull_candles >= 2:
            log.debug(f"[{asset}] SCALPER REJECT: SHORT signal blocked by 1m Bullish Momentum")
            return 0, side, reasons + ["REJECT: Momentum against trade"]
            
        score = min(raw, 100)
        return score, side, reasons

    def _infer_hh_hl_structure(self, closes: list) -> str:
        """
        Lightweight structure inference from close sequence.
        Returns: bull | bear | neutral
        """
        if len(closes) < 12:
            return "neutral"
        w = closes[-12:]
        hh = 0
        ll = 0
        for i in range(1, len(w)):
            if w[i] > w[i - 1]:
                hh += 1
            elif w[i] < w[i - 1]:
                ll += 1
        if hh >= 8:
            return "bull"
        if ll >= 8:
            return "bear"
        return "neutral"

    def _build_scalper_signal(
        self,
        asset: str,
        side: Side,
        score: int,
        mark_price: float,
        reasons: list,
        regime: MarketRegime,
        session_bonus: int,
        realized_vol: float,
    ) -> TradeSignal:
        """Build a TradeSignal with scalper-specific dynamic TP/SL levels."""
        from models.schemas import SignalStrength, MarketRegime, ScoreBreakdown
        
        # Scalper mode pakai SL/TP dari SCALPER config — bukan dynamic vol-based levels
        # (calculate_tp_levels() didesain untuk standard swing, SL 2-3% jauh untuk scalper)
        import config
        scfg = config.SCALPER
        sl_pct  = scfg.sl_pct    # 0.65%
        tp1_pct = scfg.tp1_pct   # 0.85%
        tp2_pct = scfg.tp2_pct   # 1.50%
        leverage = min(scfg.default_leverage, scfg.max_leverage)

        if side == Side.LONG:
            stop_loss = round(mark_price * (1 - sl_pct), 8)
            tp1       = round(mark_price * (1 + tp1_pct), 8)
            tp2       = round(mark_price * (1 + tp2_pct), 8)
        else:
            stop_loss = round(mark_price * (1 + sl_pct), 8)
            tp1       = round(mark_price * (1 - tp1_pct), 8)
            tp2       = round(mark_price * (1 - tp2_pct), 8)

        strength = SignalStrength.STRONG if score >= 70 else SignalStrength.MODERATE
        breakdown = ScoreBreakdown(
            raw_score=score,
            final_score=score,
            session_bonus=session_bonus,
            reasons=reasons
        )

        # Compute Expected Edge using ML (hanya saat ENABLE_INTELLIGENCE=True)
        import config as _cfg
        if _cfg.ENABLE_INTELLIGENCE:
            features = extract_live_features(
                score=score,
                meta_delta=getattr(breakdown, 'meta_score_delta', 0),
                bd=breakdown,
                funding_rate=0.0,
                realized_vol=realized_vol,
                trend_pct=0.0
            )
            edge = intelligence_model.predict_edge(features)
        else:
            edge = None

        return TradeSignal(
            signal_id=str(uuid.uuid4())[:8].upper(),
            asset=asset,
            side=side,
            score=score,
            strength=strength,
            regime=regime,
            breakdown=breakdown,
            entry_price=mark_price,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            suggested_leverage=leverage,
            meta_pattern_key=getattr(breakdown, 'meta_pattern_key', None),
            meta_score_delta=getattr(breakdown, 'meta_score_delta', 0),
            expected_edge=edge
        )

    # ──────────────────────────────────────────
    # STANDARD SCORING (existing pipeline)
    # ──────────────────────────────────────────

    async def _run_standard(self, asset: str, meta_data=None) -> Tuple[Optional[TradeSignal], int]:
        """Standard scoring pipeline (OI, Funding, Liquidation, Orderbook)."""
        import config
        
        # [FIX 4] Block London open hours (08-09 UTC)
        # Data 124 trades: 08:00 UTC WR=7.1% (-$7.81), 09:00 UTC WR=21.4% (-$7.85)
        # Total -$15.66 dalam 2 jam = hampir seluruh account loss karena opening spike
        current_hour = datetime.now(timezone.utc).hour
        if current_hour in config.BLOCKED_HOURS_UTC:
            log.debug(f"{asset} [STANDARD]: blocked hour {current_hour} UTC (London open - high spread/spike)")
            return None, 0

        # 1. Fetch allMids ONCE per cycle for Spot-Perp basis (cached 10s by client)
        now_mono = time.monotonic()
        if not self._spot_prices or (now_mono - self._spot_cache_time) > 10:
            self._spot_prices = await self.client.get_all_mids()
            self._spot_cache_time = now_mono

        mark_price = await self.client.get_mark_price(asset, meta=meta_data)
        if mark_price <= 0:
            log.warning(f"{asset}: invalid mark price, skipping")
            return None, 0

        # Funding data — REQUIRED
        try:
            funding = await self.client.get_funding_data(asset, meta=meta_data)
            log.debug(f"[{asset}] funding_rate={funding.funding_rate:.6f}")
        except Exception as e:
            log.error(f"[{asset}] Cannot fetch funding: {e}")
            return None, 0  # skip this asset entirely

        # OI data — REQUIRED
        try:
            oi = await self.client.get_oi_data(asset, meta=meta_data)
            log.debug(
                f"[{asset}] oi_usd={oi.open_interest:,.0f} "
                f"change={oi.oi_change_pct:.4f}"
            )
        except Exception as e:
            log.error(f"[{asset}] Cannot fetch OI: {e}")
            return None, 0

        # Orderbook — OPTIONAL (PROACTIVELY USE WS CACHE FIRST TO SAVE REST CALLS)
        ob_snap = None
        try:
            # Task: Use WS Cache for 100 markets efficiency
            ws_book = self.cache.orderbook.get(asset)
            if ws_book:
                # Convert raw WS book to OrderbookSnapshot
                from models.schemas import OrderbookSnapshot
                levels = ws_book.get("levels", [[], []])
                def parse_lvl(x):
                    if isinstance(x, dict): return float(x.get("px", 0)), float(x.get("sz", 0))
                    try: return float(x[0]), float(x[1])
                    except: return 0, 0
                
                bids = [[px, sz] for px, sz in (parse_lvl(b) for b in levels[0][:20]) if px > 0]
                asks = [[px, sz] for px, sz in (parse_lvl(a) for a in levels[1][:20]) if px > 0]
                
                if bids and asks:
                    mid = (bids[0][0] + asks[0][0]) / 2
                    bid_liq = sum(b[0] * b[1] for b in bids)
                    ask_liq = sum(a[0] * a[1] for a in asks)
                    imbalance = (bid_liq - ask_liq) / (bid_liq + ask_liq) if (bid_liq + ask_liq) else 0
                    vwap_val = mid # standard fallback
                    ob_snap = OrderbookSnapshot(
                        asset=asset, bids=bids, asks=asks, mid_price=mid,
                        spread_pct=(asks[0][0] - bids[0][0])/mid,
                        bid_ask_imbalance=imbalance, vwap=vwap_val, vwap_deviation_pct=0
                    )
                    log.debug(f"[{asset}] Using WS Orderbook Cache")
            
            if not ob_snap:
                # Limit REST fallback to avoid 429 spam
                try:
                    ob_snap = await self.client.get_orderbook(asset)
                    log.debug(f"[{asset}] Fetched REST Orderbook")
                except Exception as e:
                    # Silence common 429/timeout errors to keep logs clean
                    if "429" not in str(e):
                        log.debug(f"[{asset}] REST Orderbook unavailable: {repr(e)}")
                    ob_snap = None
        except Exception as e:
            # Final catch for any parsing logic errors
            log.debug(f"[{asset}] Orderbook logic error: {repr(e)}")
            ob_snap = None

        # ── Spread Filter (Institutional Filter) ──────────────────────
        if ob_snap is not None and ob_snap.spread_pct > 0.0008:
            log.info(f"[{asset}] REJECT: Bid-Ask Spread too wide ({ob_snap.spread_pct*100:.2f}% > 0.08%)")
            return None, 0

        # Store OI snapshot for change calculation
        now_ts = time.time()
        if asset not in self._oi_snapshots:
            self._oi_snapshots[asset] = []
        self._oi_snapshots[asset].append((now_ts, oi.open_interest))

        # Compute 1h OI change
        one_hour_ago = now_ts - 3600
        old = [v for t, v in self._oi_snapshots[asset] if t <= one_hour_ago]
        if old:
            oi.oi_change_pct = (oi.open_interest - old[-1]) / max(old[-1], 1)

        # Trim old data
        self._oi_snapshots[asset] = [
            (t, v) for t, v in self._oi_snapshots[asset]
            if t > now_ts - 7200
        ]

        # Price history tracking for regime (deprecated but kept for any old use)
        self._update_price_history(asset, mark_price)
        price_change_1h = self._get_price_change(asset, minutes=60)

        # ── Detect market regime (Volatility) ──────────────────────────
        vol_regime, realized_vol, trend_pct = await self._fetch_vol_regime(asset)

        if vol_regime == MarketRegime.EXTREME:
            log.debug(f"[VOL] {asset}: realized_vol={realized_vol*100:.2f}%/day regime=EXTREME multiplier=0.70x (SKIPPING SIGNAL)")
            return None, 0

        # ── Run analyzers — NO side_bias input ─────────────────────────
        funding_history = self.cache.funding_history.get(asset, [])
        recent_liqs     = self.cache.liquidations
        recent_trades   = self.cache.trades.get(asset, [])

        spot_price = self._spot_prices.get(f"@{asset}")
        if not spot_price or spot_price <= 0:
            spot_price = oi.oracle_price

        # Step 1: OI + Funding + Spot-Perp Basis
        oi_bull, oi_bear, oi_reasons, oi_warns = self.oi_funding_analyzer.analyze(
            asset, funding, oi, funding_history, price_change_1h, mark_price, spot_price
        )

        # Step 2: Liquidation map (pass funding_rate for OI proxy tilting)
        liq_bull, liq_bear, liq_reasons, liq_warns, liq_map = self.liq_analyzer.analyze(
            asset, mark_price, recent_liqs, oi.open_interest,
            funding_rate=funding.funding_rate
        )

        # Step 3: Orderbook (optional — use neutral if unavailable)
        if ob_snap is not None:
            ob_bull, ob_bear, ob_reasons, ob_warns = self.ob_analyzer.analyze(
                ob_snap, recent_trades
            )
        else:
            ob_bull, ob_bear, ob_reasons, ob_warns = 2, 2, ["Orderbook unavailable -- neutral"], []

        # Step 4: Session bonus
        session_bonus, session_reasons = self._get_session_bonus()

        # ── DIAGNOSTIC LOG (Bug 4 Fix) ─────────────────────────────────
        basis = mark_price - spot_price
        cvd_val = 0.0
        if recent_trades:
            buys = sum(float(t.get('sz', 0)) for t in recent_trades if t.get('side') == 'B')
            sells = sum(float(t.get('sz', 0)) for t in recent_trades if t.get('side') == 'S')
            cvd_val = buys - sells

        log.debug(
            f"[BEAR DEBUG] {asset}: funding={funding.funding_rate:.6f} "
            f"oi_chg={oi.oi_change_pct:.4f} price_1h={price_change_1h:.4f} "
            f"ob_imbal={ob_snap.bid_ask_imbalance if ob_snap else 0.0:.3f} cvd={cvd_val:.0f} "
            f"basis={basis:.4f}"
        )

        # ── Tally total bull vs bear evidence ──────────────────────────
        total_bull = oi_bull + liq_bull + ob_bull
        total_bear = oi_bear + liq_bear + ob_bear

        # ── Institutional Filter: 3-of-4 Consensus & Momentum ─────────
        oi_dir = Side.LONG if oi_bull > oi_bear else Side.SHORT if oi_bear > oi_bull else None
        liq_dir = Side.LONG if liq_bull > liq_bear else Side.SHORT if liq_bear > liq_bull else None
        ob_dir = Side.LONG if ob_bull > ob_bear else Side.SHORT if ob_bear > ob_bull else None
        
        dirs = [oi_dir, liq_dir, ob_dir]
        long_count = dirs.count(Side.LONG)
        short_count = dirs.count(Side.SHORT)
        
        # Fast fail if even with Momentum, we can't reach 3 of 4 consensus
        if max(long_count, short_count) < 2:
            log.debug(f"[{asset}] REJECT: No basic consensus (Longs:{long_count}, Shorts:{short_count})")
            return None, 0
            
        # Fetch 3 latest 1m candles for Momentum Confirmation
        mom_dir = None
        mom_reason = "Momentum: None"
        try:
            async with self.candle_sem:
                now_ms = int(time.time() * 1000)
                start_ms = now_ms - 5 * 60 * 1000
                resp, succ = await self.client._call_info_endpoint(
                    "candleSnapshot",
                    {"req": {"coin": asset, "interval": "1m", "startTime": start_ms, "endTime": now_ms}}
                )
                if succ and isinstance(resp, list) and len(resp) >= 3:
                    closes = [float(c["c"]) for c in resp[-3:]]
                    opens = [float(c["o"]) for c in resp[-3:]]
                    
                    bull_candles = sum(1 for o, c in zip(opens, closes) if c > o)
                    bear_candles = sum(1 for o, c in zip(opens, closes) if c < o)
                    
                    if bull_candles >= 2:
                        mom_dir = Side.LONG
                        mom_reason = f"Momentum: 1m Bullish ({bull_candles}/3 Green)"
                    elif bear_candles >= 2:
                        mom_dir = Side.SHORT
                        mom_reason = f"Momentum: 1m Bearish ({bear_candles}/3 Red)"
                    else:
                        mom_reason = "Momentum: Neutral"
        except Exception as e:
            log.debug(f"[{asset}] Momentum fetch failed: {e}")
            
        dirs.append(mom_dir)
        long_count = dirs.count(Side.LONG)
        short_count = dirs.count(Side.SHORT)
        
        if max(long_count, short_count) < 3:
            log.debug(f"[{asset}] REJECT: Dropped due to lack of 3-of-4 Consensus. L:{long_count} S:{short_count}")
            return None, 0

        # ── Direction decided HERE after all evidence is in ────────────
        # raw_score = winning side score + confidence margin
        # The margin between bull and bear represents conviction strength
        margin = abs(total_bull - total_bear)
        confidence_bonus = min(margin * 1.5, 12)  # calibrated V2: up to 12pts

        if long_count >= 3:
            side = Side.LONG
            raw_score = total_bull + confidence_bonus
        elif short_count >= 3:
            # [FIX 3] Block SHORT if disabled
            if not config.ALLOW_SHORT:
                log.debug(f"[{asset}] SHORT signal blocked (ALLOW_SHORT=False). WR=31.4% data.")
                return None, 0
            side = Side.SHORT
            raw_score = total_bear + confidence_bonus

            # ── SHORT Quality Filters (berlaku saat ALLOW_SHORT = True) ──────
            # Solusi 2: Funding Rate Confirmation
            # SHORT valid HANYA jika longs paying aggressively (crowded long → reversal).
            # Jika funding negatif = market sudah short-biased = SHORT sangat berbahaya.
            fr = getattr(funding, 'funding_rate', 0.0)
            min_fr = getattr(config.SIGNAL, 'short_min_funding_rate', 0.0002)
            if fr < 0:
                log.debug(f"[{asset}] SHORT BLOCKED: Funding negatif ({fr:.6f}) = shorts already paying, berbahaya")
                return None, 0
            elif fr < min_fr:
                log.debug(f"[{asset}] SHORT BLOCKED: Funding {fr:.6f} < {min_fr} (tidak cukup crowded long untuk reversal)")
                return None, 0

            # Solusi 3: Anti-Trend Filter
            # Jangan SHORT jika market sedang uptrend kuat (> +2% dalam 24h).
            max_up = getattr(config.SIGNAL, 'short_max_uptrend_pct', 0.02)
            if trend_pct > max_up:
                log.debug(f"[{asset}] SHORT BLOCKED: 24h uptrend {trend_pct*100:.1f}% > {max_up*100:.0f}% (jangan lawan trend)")
                return None, 0
        else:
            return None, 0

        # ── Bull-Bear gap filter (SHORT butuh gap lebih besar dari LONG) ──
        bull_bear_gap = abs(total_bull - total_bear)
        if side == Side.SHORT:
            min_gap = getattr(config.SIGNAL, 'min_bull_bear_gap_short', 28)
        else:
            min_gap = getattr(config.SIGNAL, 'min_bull_bear_gap', 18)
        if bull_bear_gap < min_gap:
            log.debug(f"[{asset}] REJECT: Bull-Bear gap {bull_bear_gap:.1f} < {min_gap} ({'SHORT' if side == Side.SHORT else 'LONG'} threshold)")
            return None, 0

        # ── Market Structure bonus/penalty (cache-first: trend_pct from vol cache)
        structure_delta = 0
        if trend_pct > 0.010:
            if side == Side.LONG:
                structure_delta = SIGNAL.structure_standard_bonus
                all_structure_reason = f"🧩 Structure align uptrend (+{structure_delta})"
            else:
                structure_delta = SIGNAL.structure_mismatch_penalty
                all_structure_reason = f"🧩 Structure mismatch uptrend ({structure_delta})"
        elif trend_pct < -0.010:
            if side == Side.SHORT:
                structure_delta = SIGNAL.structure_standard_bonus
                all_structure_reason = f"🧩 Structure align downtrend (+{structure_delta})"
            else:
                structure_delta = SIGNAL.structure_mismatch_penalty
                all_structure_reason = f"🧩 Structure mismatch downtrend ({structure_delta})"
        else:
            all_structure_reason = "🧩 Structure neutral"

        raw_score += structure_delta

        # Add session bonus
        raw_score += session_bonus
        raw_score = max(0, min(raw_score, 92))  # raised cap V2 to allow 100 final scores

        # ── Apply regime multiplier ────────────────────────────────────
        vol_multiplier = {
            MarketRegime.LOW_VOL:  0.90,
            MarketRegime.NORMAL:   1.00,
            MarketRegime.HIGH_VOL: 0.85,
            MarketRegime.EXTREME:  0.70,
        }.get(vol_regime, 1.00)

        # Trend multiplier - less punishing of range (0.95 instead of 0.85)
        trend_multiplier = 1.10 if abs(trend_pct) > 0.015 else 0.95

        final_multiplier = vol_multiplier * trend_multiplier
        final_score = int(raw_score * final_multiplier)
        final_score = max(0, min(final_score, 100))
        
        log.debug(f"[VOL] {asset}: realized_vol={realized_vol*100:.2f}%/day regime={vol_regime.value.upper()} multiplier={vol_multiplier}x")

        # ── Build breakdown ────────────────────────────────────────────
        all_reasons  = oi_reasons + liq_reasons + ob_reasons + [all_structure_reason, mom_reason] + session_reasons
        all_warnings = oi_warns + liq_warns + ob_warns

        # Determine actual 'regime' for TradeSignal logging backwards compatibility
        if vol_regime in (MarketRegime.HIGH_VOL, MarketRegime.EXTREME):
            log_regime = MarketRegime.VOLATILE
        elif abs(trend_pct) > 0.015:
            log_regime = MarketRegime.TRENDING
        else:
            log_regime = MarketRegime.RANGING

        # Add regime context
        regime_labels = {
            MarketRegime.TRENDING:  "🟢 TRENDING — higher confidence",
            MarketRegime.RANGING:   "🟡 RANGING — lower confidence",
            MarketRegime.VOLATILE:  "🔴 VOLATILE — high risk, score dampened",
            MarketRegime.UNKNOWN:   "⚪ UNKNOWN regime",
        }
        all_reasons.insert(0, f"📊 Market regime: {regime_labels.get(log_regime, '⚪ UNKNOWN')}")

        breakdown = ScoreBreakdown(
            oi_funding_score=int(oi_bull + oi_bear),
            liquidation_score=int(liq_bull + liq_bear),
            orderbook_score=int(ob_bull + ob_bear),
            session_bonus=int(session_bonus),
            regime_multiplier=final_multiplier,
            total_bull=int(total_bull),
            total_bear=int(total_bear),
            raw_score=int(raw_score),
            final_score=int(final_score),
            reasons=all_reasons,
            warnings=all_warnings,
        )

        # ── Format Combat Report ────────────────────────────────────────
        # Format: CC | Score: XX/52 | (OI+XX Liq+XX OB+XX Ses+XX) | Regime: XXX
        breakdown_str = (
            f"(OI:{oi_bull+oi_bear:+} Liq:{liq_bull+liq_bear:+} "
            f"OB:{ob_bull+ob_bear:+} Ses:{session_bonus:+})"
        )
        
        # ── UNIFORM SCORING LOG (User Request: "Satu jenis format log") ──
        if final_score >= 20:
            bias_emoji = "🟢 LONG " if total_bull > total_bear else "🔴 SHORT"
            log.info(
                f"🎯 [SCORE] {asset:6} | {bias_emoji} | {final_score:2d}/100 | "
                f"Pts: {total_bull:.1f} vs {total_bear:.1f} | "
                f"OI:{oi_bull:.1f}/{oi_bear:.1f} Liq:{liq_bull:.1f}/{liq_bear:.1f} OB:{ob_bull:.1f}/{ob_bear:.1f} | "
                f"Sess:{int(session_bonus):+d} Mult:{final_multiplier:.2f}x ({vol_regime.value})"
            )

        # ── Apply Meta-Learning adjustment ─────────────────────────────
        meta_delta, meta_reason, pattern_key = self._apply_meta_learning(asset, "standard", side, final_score)
        if meta_delta != 0:
            final_score += meta_delta
            all_reasons.append(meta_reason)
        final_score = max(0, min(final_score, 100))

        breakdown.meta_pattern_key = pattern_key
        breakdown.meta_score_delta = meta_delta
        breakdown.final_score = final_score

        # ── Check threshold ────────────────────────────────────────────
        # [FIX 2] Threshold dinaikkan dari 30 ke 62 berdasarkan data 124 trades
        # Score 55-59: WR 18.4% (-$19.80) | Score 60-64: WR 41.7% (+$10.75)
        threshold = getattr(config.SIGNAL, 'min_score_to_signal', 62)
        if final_score < threshold:
            log.debug(f"[{asset}] STANDARD: score {final_score} < threshold {threshold}, skipping")
            return None, final_score

        # ── Build signal ───────────────────────────────────────────────
        signal = self._build_signal(
            asset, side, final_score, log_regime, breakdown, mark_price,
            realized_vol=realized_vol,
            oi_usd=oi.open_interest
        )

        return signal, final_score

    # ──────────────────────────────────────────
    # REGIME DETECTION
    # ──────────────────────────────────────────

    async def _fetch_vol_regime(self, asset: str) -> Tuple[MarketRegime, float, float]:
        """
        Returns (vol_regime, realized_vol, trend_pct)
        Cached per asset for 60 minutes.
        """
        cached = self._vol_cache.get(asset)
        if cached and (time.monotonic() - cached[0]) < 3600:
            log.debug(f"[VOL] {asset}: using memory cached regime={cached[1].value.upper()}")
            return cached[1], cached[2], cached[3]
            
        # BUG 2 FIX: Check SQLite Cache first
        from core.db import user_db
        db_cache = user_db.get_vol_cache(asset)
        if db_cache:
            age = time.time() - db_cache["cached_at"]
            if age < 3600:
                regime = MarketRegime(db_cache["regime"])
                log.debug(f"[VOL] {asset}: Loaded from SQL Cache (age {int(age)}s)")
                # Warm up memory cache
                self._vol_cache[asset] = (time.monotonic() - age, regime, db_cache["realized_vol"], db_cache["trend"])
                return regime, db_cache["realized_vol"], db_cache["trend"]

        try:
            # Use candle_semaphore (3) + stagger 0.35s — max ~3 req/s saat cold start
            async with self.candle_sem:
                await asyncio.sleep(0.35)
                log.debug(f"[VOL] {asset}: Cache expired/empty, fetching candles...")
                now_ms = int(time.time() * 1000)
                start_ms = now_ms - (86400 * 1000)
                payload = {
                    "coin": asset,
                    "interval": "1h",
                    "startTime": start_ms,
                    "endTime": now_ms
                }
                resp, succ = await self.client._call_info_endpoint("candleSnapshot", {"req": payload})
            
            if not succ or not isinstance(resp, list) or len(resp) < 2:
                log.warning(f"[{asset}] candleSnapshot failed (likely 429), using NORMAL and CACHING 5m")
                self._vol_cache[asset] = (time.monotonic() - 3300, MarketRegime.NORMAL, 0.02, 0.0) # 3600-3300 = 300s remain
                return MarketRegime.NORMAL, 0.02, 0.0
                
            returns = []
            for c in resp:
                if isinstance(c, dict) and "o" in c and "c" in c:
                    try:
                        open_px = float(c["o"])
                        close_px = float(c["c"])
                        if open_px > 0:
                            returns.append((close_px - open_px) / open_px)
                    except (ValueError, TypeError):
                        continue
            
            if not returns:
                log.debug(f"[{asset}] No valid returns, using NORMAL and CACHING 5m")
                self._vol_cache[asset] = (time.monotonic() - 3300, MarketRegime.NORMAL, 0.02, 0.0)
                return MarketRegime.NORMAL, 0.02, 0.0
                
            mean_ret = sum(returns) / len(returns)
            variance = sum((r - mean_ret)**2 for r in returns) / len(returns)
            # Daily realized vol approx (std dev of 1h returns * sqrt(24))
            realized_vol = (variance ** 0.5) * (24 ** 0.5)
            
            if realized_vol < 0.015:
                regime = MarketRegime.LOW_VOL
            elif realized_vol < 0.04:
                regime = MarketRegime.NORMAL
            elif realized_vol < 0.08:
                regime = MarketRegime.HIGH_VOL
            else:
                regime = MarketRegime.EXTREME
                
            # Keep a simple trend to supply the old trending multiplier
            # Prices in resp are strings, but we already know resp is list of dicts
            try:
                first_px = float(resp[0].get("o", 0))
                last_px = float(resp[-1].get("c", 0))
                trend_pct = (last_px - first_px) / first_px if first_px > 0 else 0.0
            except (ValueError, TypeError, IndexError):
                trend_pct = 0.0
            
            self._vol_cache[asset] = (time.monotonic(), regime, realized_vol, trend_pct)
            
            # Persist to SQLite
            user_db.save_vol_cache(asset, regime.value, realized_vol, trend_pct)
            
            return regime, realized_vol, trend_pct
            
        except Exception as e:
            log.warning(f"[{asset}] _fetch_vol_regime error: {e}, using NORMAL and CACHING 5m")
            self._vol_cache[asset] = (time.monotonic() - 3300, MarketRegime.NORMAL, 0.02, 0.0)
            return MarketRegime.NORMAL, 0.02, 0.0

    def _update_price_history(self, asset: str, price: float):
        ts = time.monotonic()
        if asset not in self._price_history:
            self._price_history[asset] = []
        self._price_history[asset].append((ts, price))
        # Keep last 4 hours of data (approx 240 data points at 1/min)
        cutoff = ts - 4 * 3600
        self._price_history[asset] = [
            (t, p) for t, p in self._price_history[asset] if t > cutoff
        ]

    def _get_price_change(self, asset: str, minutes: int) -> float:
        history = self._price_history.get(asset, [])
        if len(history) < 2:
            return 0.0
        cutoff  = time.monotonic() - minutes * 60
        old_pts = [(t, p) for t, p in history if t <= cutoff]
        if not old_pts:
            old_price = history[0][1]
        else:
            old_price = old_pts[-1][1]
        current = history[-1][1]
        return (current - old_price) / old_price

    # ──────────────────────────────────────────
    # SESSION BIAS
    # ──────────────────────────────────────────

    def _get_session_bonus(self):
        """Apply trading session bonus/penalty based on UTC hour."""
        hour = datetime.now(timezone.utc).hour
        ny_start = SIGNAL.ny_session_start_utc
        ny_end   = SIGNAL.ny_session_end_utc
        lon_start= SIGNAL.london_start_utc
        lon_end  = SIGNAL.london_end_utc

        score = 0
        reasons = []

        is_ny = ny_start <= hour < ny_end
        is_lon = lon_start <= hour < lon_end

        if is_ny:
            score += SIGNAL.ny_session_bonus
            reasons.append(f"🗽 NY session (+{SIGNAL.ny_session_bonus})")

        if is_lon:
            score += SIGNAL.london_session_bonus
            reasons.append(f"🇬🇧 London session (+{SIGNAL.london_session_bonus})")

        if not is_ny and not is_lon:
            if hour >= 22 or hour < 7:
                score += SIGNAL.asia_session_penalty
                reasons.append(f"🌏 Asia session penalty ({SIGNAL.asia_session_penalty})")
            else:
                reasons.append("⏰ Off-session neutral")

        return score, reasons

    # ──────────────────────────────────────────
    # SIGNAL BUILDER
    # ──────────────────────────────────────────

    def _build_signal(
        self,
        asset: str,
        side: Side,
        score: int,
        regime: MarketRegime,
        breakdown: ScoreBreakdown,
        mark_price: float,
        realized_vol: float,
        oi_usd: float = 0.0,
    ) -> TradeSignal:
        # Determine strength
        if score >= 75:
            strength = SignalStrength.STRONG
        elif score >= 60:
            strength = SignalStrength.MODERATE
        else:
            strength = SignalStrength.WEAK

        # ── DYNAMIC TP/SL (Fix 10) ────────────────────────────────────
        # Get dynamic levels from RiskManager based on Volatility
        sl_pct, tp1_pct, tp2_pct = self.risk_mgr.calculate_tp_levels(asset, mark_price, side, realized_vol)

        if side == Side.LONG:
            stop_loss = round(mark_price * (1 - sl_pct), 8)
            tp1       = round(mark_price * (1 + tp1_pct), 8)
            tp2       = round(mark_price * (1 + tp2_pct), 8)
        else:
            stop_loss = round(mark_price * (1 + sl_pct), 8)
            tp1       = round(mark_price * (1 - tp1_pct), 8)
            tp2       = round(mark_price * (1 - tp2_pct), 8)

        # Leverage: scale with score
        leverage = RISK.default_leverage
        if score >= 75:
            leverage = min(RISK.default_leverage + 2, RISK.max_leverage)

        # Compute Expected Edge via Intelligence Model (sama seperti scalper mode)
        import config as _cfg
        if _cfg.ENABLE_INTELLIGENCE:
            features = extract_live_features(
                score=score,
                meta_delta=getattr(breakdown, 'meta_score_delta', 0),
                bd=breakdown,
                funding_rate=0.0,
                realized_vol=realized_vol,
                trend_pct=0.0
            )
            expected_edge = intelligence_model.predict_edge(features)
        else:
            expected_edge = None

        return TradeSignal(
            signal_id=str(uuid.uuid4())[:8].upper(),
            asset=asset,
            side=side,
            score=score,
            strength=strength,
            regime=regime,
            breakdown=breakdown,
            entry_price=mark_price,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            suggested_leverage=leverage,
            meta_pattern_key=getattr(breakdown, 'meta_pattern_key', None),
            meta_score_delta=getattr(breakdown, 'meta_score_delta', 0),
            expected_edge=expected_edge
        )

    # ──────────────────────────────────────────
    # MTF & META HELPERS
    # ──────────────────────────────────────────

    async def _fetch_15m_mtf_data(self, asset: str) -> str:
        """
        Fetch 15m candles to detect medium-term trend with 5-min caching.
        Returns 'bull', 'bear', or 'neutral'.
        """
        now = time.time()
        if asset in self._mtf_cache:
            ts, cached_trend = self._mtf_cache[asset]
            if now - ts < 300:  # 5 minutes TTL
                return cached_trend

        try:
            async with self.candle_sem:
                now_ms = int(time.time() * 1000)
                # Fetch ~8 hours of 15m candles (32 candles)
                start_ms = now_ms - (32 * 15 * 60 * 1000)
                payload = {
                    "coin": asset,
                    "interval": "15m",
                    "startTime": start_ms,
                    "endTime": now_ms
                }
                resp, succ = await self.client._call_info_endpoint("candleSnapshot", {"req": payload})
            
            if not succ or not isinstance(resp, list) or len(resp) < 10:
                return "neutral"
            
            closes = []
            for c in resp:
                try: closes.append(float(c["c"]))
                except: continue
                
            if len(closes) < 10: return "neutral"
            
            # Simple trend: EMA10 > EMA20
            def quick_ema(data, p):
                k = 2/(p+1)
                res = data[0]
                for v in data[1:]: res = v*k + res*(1-k)
                return res
            
            ema10 = quick_ema(closes[-20:], 10)
            ema20 = quick_ema(closes[-20:], 20)
            
            trend = "neutral"
            if ema10 > ema20 * 1.001: trend = "bull"
            elif ema10 < ema20 * 0.999: trend = "bear"
            
            self._mtf_cache[asset] = (now, trend)
            return trend
        except Exception as e:
            log.debug(f"[{asset}] MTF fetch error: {e}")
            return "neutral"

    def _apply_meta_learning(self, asset: str, mode: str, side: Side, raw_score: int) -> Tuple[int, str, Optional[str]]:
        """
        Adjust score based on historical pattern winrate.
        Pattern key: {mode}_{asset}_{side}
        """
        if not config.SIGNAL.meta_learning_enabled:
            return 0, "", None
            
        from core.db import user_db
        pattern_key = f"{mode.lower()}_{asset}_{side.value}"
        stats = user_db.get_meta_pattern_stats(pattern_key)
        
        if not stats or stats["samples"] < config.SIGNAL.meta_min_samples:
            return 0, "", pattern_key
            
        wr = stats["winrate_ema"]
        delta = 0
        reason = ""
        
        if wr >= config.SIGNAL.meta_boost_threshold:
            delta = 8
            reason = f"🧠 Meta-learning: HI-WR pattern ({wr*100:.0f}%) → +8"
        elif wr <= config.SIGNAL.meta_penalty_threshold:
            delta = -12
            reason = f"🧠 Meta-learning: LO-WR pattern ({wr*100:.0f}%) → -12"
            
        # Clamp delta
        delta = max(-config.SIGNAL.meta_max_delta, min(config.SIGNAL.meta_max_delta, delta))
        return delta, reason, pattern_key
