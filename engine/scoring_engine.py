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

        # Recent accepted signal timestamps per asset/mode for concentration control.
        self._asset_signal_history: Dict[str, List[float]] = {}

    @staticmethod
    def _ema(data: list, period: int) -> float:
        if not data:
            return 0.0
        k = 2 / (period + 1)
        value = data[0]
        for item in data[1:]:
            value = item * k + value * (1 - k)
        return value

    def dump_oi_state(self):
        """Persist OI snapshots to database to prevent amnesia on restart."""
        from core.db import user_db
        user_db.save_oi_snapshots_batch(self._oi_snapshots)

    def _asset_concentration_threshold_add(self, asset: str, mode: str) -> int:
        if not getattr(config.SIGNAL, "asset_concentration_enabled", True):
            return 0

        window_sec = getattr(config.SIGNAL, "asset_concentration_window_minutes", 60) * 60
        max_signals = getattr(config.SIGNAL, "asset_concentration_max_signals", 2)
        step = getattr(config.SIGNAL, "asset_concentration_threshold_step", 4)
        max_add = getattr(config.SIGNAL, "asset_concentration_max_threshold_add", 12)
        now = time.time()
        key = f"{mode.lower()}_{asset}"
        recent = [ts for ts in self._asset_signal_history.get(key, []) if now - ts <= window_sec]
        self._asset_signal_history[key] = recent

        excess = max(0, len(recent) - max_signals + 1)
        return min(excess * step, max_add)

    def _record_asset_signal(self, asset: str, mode: str):
        key = f"{mode.lower()}_{asset}"
        self._asset_signal_history.setdefault(key, []).append(time.time())

    def _infer_setup_type(self, signal: TradeSignal) -> str:
        reasons = " | ".join(getattr(signal.breakdown, "reasons", []) or []).lower()
        if signal.side == Side.SHORT:
            if "cascade short" in reasons or "liq cascade" in reasons:
                return "short_cascade"
            if (
                "failed-rally" in reasons
                or "failed rally" in reasons
                or "crowded-long reversal" in reasons
            ):
                return "short_crowded_reversal"
            if "breakdown continuation" in reasons or "downtrend" in reasons or "bearish" in reasons:
                return "short_breakdown"
            return "no_trade_unclear"

        if "reversal" in reasons or "oversold" in reasons or "support" in reasons or "reclaim" in reasons:
            return "long_reversal"
        return "long_continuation"

    def _log_signal_marker(
        self,
        mode: str,
        signal: TradeSignal,
        *,
        entry_location_quality: str = "na",
        concentration_delta: int = 0,
    ):
        setup_type = self._infer_setup_type(signal)
        log.info(
            "[SIGNAL-MARKER] "
            f"mode={mode} asset={signal.asset} side={signal.side.value.upper()} "
            f"score={signal.score} setup_type={setup_type} "
            f"meta_pattern_key={signal.meta_pattern_key or '-'} "
            f"meta_delta={getattr(signal, 'meta_score_delta', 0):+d} "
            f"entry_location_quality={entry_location_quality} "
            f"concentration_delta={concentration_delta:+d}"
        )

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
            raw_score = 0

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
        Returns -1 as max_score if ALL active modes were blocked by schedule.
        """
        import config
        if active_modes is None:
            active_modes = ["standard"]
            
        signals = {}
        max_score_found = 0
        all_mode_scores = []  # track per-mode scores for sentinel propagation
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
                all_mode_scores.append(score_scl)

                if score_scl > max_score_found: max_score_found = score_scl

                if sig and (now_ts - last_ts >= cooldown_secs):
                    signals["scalper"] = sig
                    self._last_signal_ts[f"{asset}_scalper"] = now_ts
                    self._record_asset_signal(asset, "scalper")
                    log.info(f"⚡ SCALPER SIGNAL: {asset} {sig.side.value.upper()} score={score_scl} (Meta: {getattr(sig, 'meta_score_delta', 0):+d})")
                elif sig:
                    log.debug(f"{asset} [SCALPER]: cooldown active (score={score_scl} blocked)")
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
                all_mode_scores.append(score_std)

                if score_std > max_score_found: max_score_found = score_std

                if sig and (now_ts - last_ts >= cooldown_secs):
                    signals["standard"] = sig
                    self._last_signal_ts[f"{asset}_standard"] = now_ts
                    self._record_asset_signal(asset, "standard")
            except RuntimeError as e:
                if "backoff" in str(e):
                    log.debug(f"[SCAN] {asset}: skipped (API backoff)")
                    return {}, 0
                log.error(f"Error in standard: {e}")
            except Exception as e:
                log.error(f"Error in standard: {e}")

        # Propagate blocked-by-schedule sentinel:
        # If ALL active modes returned -1, this asset was entirely blocked.
        if all_mode_scores and all(s == -1 for s in all_mode_scores):
            return signals, -1

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
            return None, -1  # sentinel: blocked by schedule, not an error

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

        if score < config.SCALPER.min_score_to_enter:
            log.debug(
                f"[SCALPER] {asset}: score {score} < {config.SCALPER.min_score_to_enter} after meta-learning"
            )
            return None, score

        concentration_add = self._asset_concentration_threshold_add(asset, "scalper")
        if concentration_add > 0:
            required_score = min(100, config.SCALPER.min_score_to_enter + concentration_add)
            reasons.append(
                f"Asset concentration guard: {asset} requires {required_score}+ after recent repeats"
            )
            if score < required_score:
                log.debug(
                    f"[SCALPER] {asset}: score {score} < concentration threshold {required_score}"
                )
                return None, score

        # 4. Build signal with scalper TP/SL (now dynamic based on Volatility)
        vol_regime, realized_vol, trend_pct = await self._fetch_vol_regime(asset)
        signal = self._build_scalper_signal(asset, side, score, mark_price, reasons, vol_regime, session_bonus, realized_vol, trend_pct)

        entry_location_quality = "disabled"
        if getattr(config.SCALPER, "entry_location_gate_enabled", True):
            loc = self._validate_entry_location(signal, candles, vol_regime, realized_vol)
            entry_location_quality = loc["quality"]
            loc_reason = (
                f"Entry location {loc['quality']}: {loc['location_type']} "
                f"(risk={loc['distance_pct']*100:.2f}%, room/risk={loc['room_risk']:.2f}) - {loc['reason']}"
            )
            if loc["quality"] == "invalid":
                log.debug(f"[SCALPER] {asset}: REJECT {loc_reason}")
                return None, score
            if loc["quality"] == "weak":
                weak_min = getattr(config.SCALPER, "entry_location_weak_min_score", 72)
                if score < weak_min:
                    log.debug(f"[SCALPER] {asset}: REJECT weak location score {score} < {weak_min} | {loc_reason}")
                    return None, score
                penalty = getattr(config.SCALPER, "entry_location_weak_penalty", 8)
                score = max(0, score - penalty)
                signal.score = score
                signal.breakdown.raw_score = score
                signal.breakdown.final_score = score
                signal.breakdown.failure_risk_score += penalty
                signal.breakdown.reasons.append(f"{loc_reason}; score -{penalty}")
            elif loc["quality"] == "excellent":
                bonus = getattr(config.SCALPER, "entry_location_excellent_bonus", 3)
                score = min(100, score + bonus)
                signal.score = score
                signal.breakdown.raw_score = score
                signal.breakdown.final_score = score
                signal.breakdown.trade_quality_score += bonus
                signal.breakdown.reasons.append(f"{loc_reason}; score +{bonus}")
            else:
                signal.breakdown.reasons.append(loc_reason)

            # Carry this level into position management. It is more useful for a
            # 1m scalp than waiting for the wider ATR/fixed stop to be hit.
            signal.micro_invalidation_price = loc["invalidation_price"]

        signal.meta_pattern_key = pattern_key
        signal.meta_score_delta = meta_delta
        signal.entry_location_quality = entry_location_quality
        if getattr(config, "ENABLE_INTELLIGENCE", True):
            micro_risk = abs((signal.micro_invalidation_price or signal.stop_loss) - signal.entry_price) / signal.entry_price
            signal.expected_edge = intelligence_model.predict_edge(extract_live_features(
                score=signal.score,
                meta_delta=signal.meta_score_delta,
                bd=signal.breakdown,
                funding_rate=signal.funding_rate,
                realized_vol=signal.realized_vol,
                trend_pct=signal.trend_pct,
                micro_risk_pct=micro_risk,
                entry_location_quality=signal.entry_location_quality,
                trade_mode="scalper",
            ))
        self._log_signal_marker(
            "scalper",
            signal,
            entry_location_quality=entry_location_quality,
            concentration_delta=concentration_add,
        )
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
        bull_pts = 0
        bear_pts = 0
        quality_pts = 0
        failure_risk_pts = 0
        reasons = []
        ob_imb = 0.0
        ob_signal = "neutral"
        ob_raw_pts = 0

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
                    ob_imb = imb
                
                # Spread Filter (Institutional Filter)
                if bids and asks and bids[0][0] > 0:
                    spread_pct = (asks[0][0] - bids[0][0]) / asks[0][0]
                    if spread_pct > 0.0015:
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

        if ob:
            # Audit fix: raw OB imbalance was inverse/noisy in futures.
            # Neutralize the legacy direct points; OB becomes pending evidence
            # until candle price reaction confirms support/resistance.
            if ob_imb > 0.60:
                bull_pts = max(0, bull_pts - 20)
                ob_signal = "strong_bid"
                ob_raw_pts = 6
                reasons.append(f"Orderbook strong bid wall ({ob_imb:.2f}) -> pending price reaction")
            elif ob_imb < -0.60:
                bear_pts = max(0, bear_pts - 20)
                ob_signal = "strong_ask"
                ob_raw_pts = 6
                reasons.append(f"Orderbook strong ask wall ({ob_imb:.2f}) -> pending price reaction")
            elif ob_imb > 0.40:
                bull_pts = max(0, bull_pts - 8)
                ob_signal = "mild_bid"
                ob_raw_pts = 2
                reasons.append(f"Orderbook mild bid pressure ({ob_imb:.2f}) -> context only")
            elif ob_imb < -0.40:
                bear_pts = max(0, bear_pts - 8)
                ob_signal = "mild_ask"
                ob_raw_pts = 2
                reasons.append(f"Orderbook mild ask pressure ({ob_imb:.2f}) -> context only")

        if len(candles) < 21:
            log.debug(f"[{asset}] SCALPER REJECT: insufficient 1m candles for EMA21/RSI14 ({len(candles)}/21)")
            return 0, Side.LONG, reasons + ["REJECT: insufficient candle data for EMA21/RSI14"]

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

        if len(closes) < 21:
            log.debug(f"[{asset}] SCALPER REJECT: insufficient parsed candles for EMA21/RSI14 ({len(closes)}/21)")
            return 0, Side.LONG, reasons + ["REJECT: insufficient parsed candle data for EMA21/RSI14"]

        # ── Momentum Confirmation (Institutional Filter) ─────────────────
        bull_candles = 0
        bear_candles = 0
        price_move_3m = 0.0
        price_move_5m = 0.0
        if len(closes) >= 3:
            bull_candles = sum(1 for c, o in zip(closes[-3:], opens[-3:]) if c > o)
            bear_candles = sum(1 for c, o in zip(closes[-3:], opens[-3:]) if c < o)
            price_move_3m = (closes[-1] - closes[-3]) / closes[-3] if closes[-3] > 0 else 0.0
            
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

        if len(closes) >= 5 and closes[-5] > 0:
            price_move_5m = (closes[-1] - closes[-5]) / closes[-5]

        if ob_signal in ("strong_bid", "mild_bid"):
            if price_move_3m > 0.0008 and bull_candles >= 2:
                bull_pts += ob_raw_pts
                quality_pts += 2 if ob_signal == "strong_bid" else 1
                reasons.append(f"Orderbook bid confirmed by price reaction ({price_move_3m*100:.2f}%) -> LONG +{ob_raw_pts}")
            elif price_move_3m < -0.0008:
                bear_pts += 2
                failure_risk_pts += 6 if ob_signal == "strong_bid" else 3
                reasons.append(f"Orderbook bid failed; price fell {price_move_3m*100:.2f}% -> absorption risk")
            else:
                quality_pts -= 1
                reasons.append("Orderbook bid unconfirmed -> no direction boost")
        elif ob_signal in ("strong_ask", "mild_ask"):
            if price_move_3m < -0.0008 and bear_candles >= 2:
                bear_pts += ob_raw_pts
                quality_pts += 2 if ob_signal == "strong_ask" else 1
                reasons.append(f"Orderbook ask confirmed by price reaction ({price_move_3m*100:.2f}%) -> SHORT +{ob_raw_pts}")
            elif price_move_3m > 0.0008:
                bull_pts += 2
                failure_risk_pts += 6 if ob_signal == "strong_ask" else 3
                reasons.append(f"Orderbook ask failed; price rose {price_move_3m*100:.2f}% -> squeeze/absorption risk")
            else:
                quality_pts -= 1
                reasons.append("Orderbook ask unconfirmed -> no direction boost")

        ema8  = ema(closes[-21:], 8)  if len(closes) >= 8  else closes[-1]
        ema21 = ema(closes[-21:], 21) if len(closes) >= 21 else closes[-1]

        if ema8 > ema21 * 1.0005:   # EMA8 clearly above EMA21
            bull_pts += 15
            reasons.append(f"📈 EMA8 ({ema8:.4f}) > EMA21 ({ema21:.4f}) → bullish")
        elif ema8 < ema21 * 0.9995:
            bear_pts += 15
            reasons.append(f"📉 EMA8 ({ema8:.4f}) < EMA21 ({ema21:.4f}) → bearish")

        # ── RSI 14 (1m) ──────────────────────────────────────────────
        rsi = None
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
                # Audit: oversold LONG was negative EV; confirm after CVD/orderbook.
                reasons.append(f"📊 RSI oversold ({rsi:.1f}) → buy signal")
            elif rsi > 65:
                # Audit: overbought often worked as LONG continuation, not blind SHORT.
                reasons.append(f"📊 RSI overbought ({rsi:.1f}) → sell signal")
            else:
                reasons.append(f"📊 RSI neutral ({rsi:.1f})")

        # ── Short-term CVD (last 80 trades from cache) ────────────────
        recent_trades = self.cache.trades.get(asset, []) if hasattr(self.cache, 'trades') else []
        cvd_ratio = None
        if len(recent_trades) >= 20:
            sample = recent_trades[-80:]
            buy_vol = sum(float(t.get('sz', 0)) for t in sample if t.get('side', '') in ('B', 'buy', 'Ask'))
            sell_vol = sum(float(t.get('sz', 0)) for t in sample if t.get('side', '') in ('S', 'sell', 'Bid'))
            total_vol = buy_vol + sell_vol
            if total_vol > 0:
                cvd_ratio = (buy_vol - sell_vol) / total_vol
                price_move_5m = (closes[-1] - closes[-5]) / closes[-5] if len(closes) >= 5 and closes[-5] > 0 else 0.0
                if cvd_ratio > 0.20:
                    if price_move_5m > 0.001 and bull_candles >= 2:
                        bull_pts += 10
                        quality_pts += 3
                        reasons.append(f"CVD bullish + price follow-through ({cvd_ratio*100:.0f}%, {price_move_5m*100:.2f}%)")
                    elif price_move_5m < -0.001:
                        bear_pts += 4
                        failure_risk_pts += 5
                        reasons.append(f"CVD bullish but price falling ({price_move_5m*100:.2f}%) -> sell absorption risk")
                    else:
                        failure_risk_pts += 2
                        reasons.append(f"CVD bullish without follow-through ({cvd_ratio*100:.0f}%) -> no boost")
                elif cvd_ratio < -0.20:
                    if price_move_5m < -0.001 and bear_candles >= 2:
                        bear_pts += 10
                        quality_pts += 3
                        reasons.append(f"CVD bearish + price follow-through ({cvd_ratio*100:.0f}%, {price_move_5m*100:.2f}%)")
                    elif price_move_5m > 0.001:
                        bull_pts += 4
                        failure_risk_pts += 5
                        reasons.append(f"CVD bearish but price rising ({price_move_5m*100:.2f}%) -> buy absorption risk")
                    else:
                        failure_risk_pts += 2
                        reasons.append(f"CVD bearish without follow-through ({cvd_ratio*100:.0f}%) -> no boost")

        # Audit-driven RSI handling:
        # Oversold LONG was the worst RSI bucket, so it needs orderflow confirmation.
        # Overbought worked better as continuation context than as automatic SHORT.
        if rsi is not None and rsi < 35:
            reversal_checks = 0
            if cvd_ratio is not None and cvd_ratio > 0.20:
                reversal_checks += 1
            if ob_imb > 0.60:
                reversal_checks += 1
            if bull_candles >= 2 and closes[-1] > closes[-2]:
                reversal_checks += 1

            if reversal_checks >= 2:
                bull_pts += 8
                quality_pts += 2
                reasons.append(f"RSI oversold confirmed by orderflow ({reversal_checks}/3) -> cautious LONG +8")
            else:
                bull_pts = max(0, bull_pts - 10)
                failure_risk_pts += 8
                reasons.append(f"RSI oversold unconfirmed ({reversal_checks}/3) -> LONG catch-knife penalty -10")
        elif rsi is not None and rsi > 65:
            trend_continuation = ema8 > ema21 * 1.0005 and bull_candles >= 2
            short_exhaustion = (
                ema8 < ema21 * 0.9995
                and bear_candles >= 2
                and (ob_imb < -0.40 or (cvd_ratio is not None and cvd_ratio < -0.20))
            )

            if trend_continuation:
                bull_pts += 10
                quality_pts += 2
                reasons.append("RSI overbought + bullish EMA/momentum -> LONG continuation +10")
            elif short_exhaustion:
                bear_pts += 10
                quality_pts += 2
                reasons.append("RSI overbought + bearish orderflow -> SHORT exhaustion +10")
            else:
                reasons.append("RSI overbought without exhaustion -> no SHORT boost")

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
                    if bull_pts > bear_pts:
                        bull_pts += extra
                    elif bear_pts > bull_pts:
                        bear_pts += extra
                    else:
                        reasons.append("Volume surge ignored on bull/bear tie")

        # ── Market Structure HH/HL (cache-first: reuse existing 1m candles) ──
        trend_state = self._infer_hh_hl_structure(closes)
        import config
        if trend_state == "bull":
            bull_pts += config.SIGNAL.structure_scalper_bonus
            reasons.append(f"🧩 1m bullish break + follow-through (+{config.SIGNAL.structure_scalper_bonus})")
        elif trend_state == "bear":
            bear_pts += config.SIGNAL.structure_scalper_bonus
            reasons.append(f"🧩 1m bearish break + follow-through (+{config.SIGNAL.structure_scalper_bonus})")
        else:
            reasons.append("🧩 1m structure neutral")

        # ── Final tally & Consensus Filter ────────────────────────────
        if bull_pts == bear_pts:
            return 0, Side.LONG, reasons + ["REJECT: bull/bear tie - no directional edge"]
        side = Side.LONG if bull_pts > bear_pts else Side.SHORT

        if side == Side.LONG and trend_state != "bull":
            log.debug(f"[{asset}] SCALPER REJECT: LONG requires 1m bullish structure (got {trend_state})")
            return 0, side, reasons + ["REJECT: LONG requires 1m bullish structure"]
        if side == Side.SHORT and trend_state != "bear":
            log.debug(f"[{asset}] SCALPER REJECT: SHORT requires 1m bearish structure (got {trend_state})")
            return 0, side, reasons + ["REJECT: SHORT requires 1m bearish structure"]

        direction_score = (bull_pts if side == Side.LONG else bear_pts)
        raw = direction_score
        
        # ── MTF Confirmation (15m Trend Alignment) ──────────────────
        import config
        scfg = config.SCALPER
        if mtf_trend != "neutral":
            if (side == Side.LONG and mtf_trend == "bull") or (side == Side.SHORT and mtf_trend == "bear"):
                if raw < getattr(scfg, "mtf_bonus_floor_score", 65):
                    mtf_bonus = 0
                elif raw < getattr(scfg, "mtf_bonus_high_score", 72):
                    mtf_bonus = min(getattr(scfg, "mtf_mid_bonus", 4), scfg.mtf_score_bonus)
                else:
                    mtf_bonus = min(getattr(scfg, "mtf_high_bonus", 6), scfg.mtf_score_bonus)
                quality_pts += mtf_bonus
                reasons.append(f"📡 15m MTF Align ({mtf_trend}) -> +{mtf_bonus} context bonus")
            else:
                failure_risk_pts += abs(scfg.mtf_score_penalty)
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
            
        trade_quality_score = max(-20, min(25, quality_pts))
        failure_risk_score = max(0, min(35, failure_risk_pts))
        score = min(max(direction_score + trade_quality_score - failure_risk_score, 0), 100)
        reasons.append(
            f"Score split: direction={direction_score}, quality={trade_quality_score:+d}, "
            f"failure_risk=-{failure_risk_score}, final={score}"
        )
        return score, side, reasons

    def _infer_hh_hl_structure(self, closes: list) -> str:
        """
        Lightweight structure inference from close sequence.
        Requires break of prior range plus follow-through, not just many up/down closes.
        Returns: bull | bear | neutral
        """
        if len(closes) < 16:
            return "neutral"

        prior = closes[-16:-4]
        recent = closes[-4:]
        if not prior or not recent:
            return "neutral"

        prior_high = max(prior)
        prior_low = min(prior)
        recent_change = (recent[-1] - recent[0]) / recent[0] if recent[0] > 0 else 0.0
        broke_up = recent[-1] > prior_high * 1.0005
        broke_down = recent[-1] < prior_low * 0.9995
        follow_up = recent_change > 0.001 and recent[-1] > recent[-2]
        follow_down = recent_change < -0.001 and recent[-1] < recent[-2]

        if broke_up and follow_up:
            return "bull"
        if broke_down and follow_down:
            return "bear"
        return "neutral"

    def _validate_entry_location(
        self,
        signal: TradeSignal,
        candles: list,
        regime: MarketRegime,
        realized_vol: float = 0.0,
    ) -> dict:
        """
        Adaptive entry-location gate for scalping.
        It validates whether entry has a nearby market-structure invalidation and enough room to TP1.
        """
        fallback = {
            "quality": "valid",
            "location_type": "insufficient_data",
            "invalidation_price": signal.stop_loss,
            "distance_pct": abs(signal.entry_price - signal.stop_loss) / max(signal.entry_price, 1e-12),
            "room_to_tp1_pct": abs(signal.tp1 - signal.entry_price) / max(signal.entry_price, 1e-12),
            "room_risk": 1.0,
            "reason": "location gate skipped; not enough candle structure",
        }

        if not candles or len(candles) < 16 or signal.entry_price <= 0:
            return fallback

        highs, lows, closes = [], [], []
        for c in candles:
            if not isinstance(c, dict):
                continue
            try:
                highs.append(float(c.get("h", c.get("c", 0))))
                lows.append(float(c.get("l", c.get("c", 0))))
                closes.append(float(c.get("c", 0)))
            except (TypeError, ValueError):
                continue

        if len(closes) < 16:
            return fallback

        entry = signal.entry_price
        side = signal.side
        prior_high = max(highs[-16:-4])
        prior_low = min(lows[-16:-4])
        recent_high = max(highs[-8:])
        recent_low = min(lows[-8:])
        ema21 = self._ema(closes[-21:], 21) if len(closes) >= 21 else closes[-1]

        # Regime-aware thresholds. Wider in trend/high vol, stricter in range/extreme.
        thresholds = {
            MarketRegime.RANGING:  (0.0025, 0.0055, 0.0025, 1.00),
            MarketRegime.TRENDING: (0.0025, 0.0080, 0.0040, 0.75),
            MarketRegime.HIGH_VOL: (0.0035, 0.0095, 0.0035, 0.90),
            MarketRegime.EXTREME:  (0.0045, 0.0110, 0.0025, 1.20),
            MarketRegime.VOLATILE: (0.0035, 0.0095, 0.0035, 0.90),
            MarketRegime.NORMAL:   (0.0025, 0.0080, 0.0035, 0.85),
            MarketRegime.LOW_VOL:  (0.0020, 0.0060, 0.0030, 0.85),
            MarketRegime.UNKNOWN:  (0.0025, 0.0080, 0.0035, 0.85),
        }
        min_dist, max_dist, max_extension, min_room_risk = thresholds.get(
            regime, thresholds[MarketRegime.UNKNOWN]
        )

        # Let realized volatility widen distance a bit, but keep scalper bounds controlled.
        if realized_vol > 0.06:
            max_dist = min(max_dist + 0.0015, 0.0120)
            min_dist = min_dist + 0.0005

        candidates = []

        def add_candidate(location_type: str, level: float, extension: float):
            if level > 0:
                candidates.append((extension, location_type, level))

        if side == Side.LONG:
            if entry > prior_high:
                add_candidate("breakout_retest", prior_high, (entry - prior_high) / entry)
            if entry > ema21:
                add_candidate("ema_reclaim", ema21, abs(entry - ema21) / entry)
            if entry > recent_low:
                add_candidate("support_reclaim", recent_low, (entry - recent_low) / entry)
        else:
            if entry < prior_low:
                add_candidate("breakdown_retest", prior_low, (prior_low - entry) / entry)
            if entry < ema21:
                add_candidate("ema_rejection", ema21, abs(entry - ema21) / entry)
            if entry < recent_high:
                add_candidate("resistance_rejection", recent_high, (recent_high - entry) / entry)

        candidates = [c for c in candidates if c[0] >= 0]
        if not candidates:
            quality = "weak" if regime == MarketRegime.TRENDING else "invalid"
            return {
                "quality": quality,
                "location_type": "unclear",
                "invalidation_price": signal.stop_loss,
                "distance_pct": abs(signal.entry_price - signal.stop_loss) / entry,
                "room_to_tp1_pct": abs(signal.tp1 - entry) / entry,
                "room_risk": 0.0,
                "reason": "no nearby support/rejection/breakout level",
            }

        extension, location_type, level = min(candidates, key=lambda x: x[0])
        buffer_pct = max(0.0015, min_dist * 0.50)
        if side == Side.LONG:
            invalidation = level * (1 - buffer_pct)
            distance_pct = (entry - invalidation) / entry
            room_to_tp1_pct = max((signal.tp1 - entry) / entry, 0.0)
        else:
            invalidation = level * (1 + buffer_pct)
            distance_pct = (invalidation - entry) / entry
            room_to_tp1_pct = max((entry - signal.tp1) / entry, 0.0)

        room_risk = room_to_tp1_pct / max(distance_pct, 1e-9)
        problems = []

        if extension > max_extension:
            problems.append(f"entry extended {extension*100:.2f}% from {location_type}")
        if distance_pct < min_dist:
            problems.append(f"invalidation too close {distance_pct*100:.2f}%")
        if distance_pct > max_dist:
            problems.append(f"invalidation too far {distance_pct*100:.2f}%")
        if room_risk < min_room_risk:
            problems.append(f"room/risk {room_risk:.2f} below {min_room_risk:.2f}")

        if not problems:
            excellent = extension <= max_extension * 0.60 and room_risk >= min_room_risk * 1.25
            quality = "excellent" if excellent else "valid"
            reason = "clean entry location"
        else:
            severe_room = room_risk < min_room_risk * 0.55
            severe_extension = extension > max_extension * 1.75
            severe_distance = distance_pct > max_dist * 1.35
            too_close_extreme = distance_pct < min_dist * 0.60
            hard_regime = regime in (MarketRegime.RANGING, MarketRegime.EXTREME)
            if hard_regime and (severe_room or severe_extension or severe_distance or too_close_extreme):
                quality = "invalid"
            elif len(problems) >= 3 and regime != MarketRegime.TRENDING:
                quality = "invalid"
            else:
                quality = "weak"
            reason = "; ".join(problems)

        return {
            "quality": quality,
            "location_type": location_type,
            "invalidation_price": invalidation,
            "distance_pct": distance_pct,
            "room_to_tp1_pct": room_to_tp1_pct,
            "room_risk": room_risk,
            "reason": reason,
        }

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
        trend_pct: float = 0.0,
    ) -> TradeSignal:
        """Build a TradeSignal with scalper-specific dynamic TP/SL levels."""
        from models.schemas import SignalStrength, MarketRegime, ScoreBreakdown
        
        import config
        scfg = config.SCALPER

        # Dynamic SL: scale with realized_vol, floor at config sl_pct, ceiling at 1.5%
        # TP ladder calibrated for ~12m hold (tp1~0.45%, tp2~0.75% base).
        SL_FLOOR   = scfg.sl_pct          # 0.80%
        SL_CEILING = 0.0150               # 1.50% max — beyond this scalper isn't viable
        VOL_MULT   = 1.20                 # SL = vol * 1.2 (noise buffer)

        if realized_vol > 0:
            sl_pct = max(SL_FLOOR, min(realized_vol * VOL_MULT, SL_CEILING))
        else:
            sl_pct = SL_FLOOR

        # TP: keep config RR, but cap TP2 so it stays inside typical 12m MFE
        rr1 = scfg.tp1_pct / max(scfg.sl_pct, 1e-9)   # ~0.56x base
        rr2 = scfg.tp2_pct / max(scfg.sl_pct, 1e-9)   # ~0.94x base
        tp1_pct = sl_pct * rr1
        tp2_pct = sl_pct * rr2
        # Soft caps: TP1 ≤ 0.70%, TP2 ≤ 1.10% even if vol inflates SL
        tp1_pct = min(tp1_pct, 0.0070)
        tp2_pct = min(max(tp2_pct, tp1_pct * 1.40), 0.0110)

        leverage = min(scfg.default_leverage, scfg.max_leverage)

        if side == Side.LONG:
            stop_loss = round(mark_price * (1 - sl_pct), 8)
            tp1       = round(mark_price * (1 + tp1_pct), 8)
            tp2       = round(mark_price * (1 + tp2_pct), 8)
        else:
            stop_loss = round(mark_price * (1 + sl_pct), 8)
            tp1       = round(mark_price * (1 - tp1_pct), 8)
            tp2       = round(mark_price * (1 - tp2_pct), 8)

        direction_score = score
        trade_quality_score = 0
        failure_risk_score = 0
        for reason in reversed(reasons):
            if reason.startswith("Score split:"):
                try:
                    parts = {
                        item.split("=")[0].strip(): item.split("=")[1].strip()
                        for item in reason.replace("Score split:", "").split(",")
                        if "=" in item
                    }
                    direction_score = int(parts.get("direction", direction_score))
                    trade_quality_score = int(parts.get("quality", "0").replace("+", ""))
                    failure_risk_score = abs(int(parts.get("failure_risk", "0").replace("+", "")))
                except Exception:
                    pass
                break

        strength = SignalStrength.STRONG if score >= 70 else SignalStrength.MODERATE
        breakdown = ScoreBreakdown(
            raw_score=score,
            final_score=score,
            direction_score=direction_score,
            trade_quality_score=trade_quality_score,
            failure_risk_score=failure_risk_score,
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
                funding_rate=0.0,   # scalper tidak fetch funding — pakai 0.0 konsisten
                realized_vol=realized_vol,
                trend_pct=trend_pct
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
            expected_edge=edge,
            funding_rate=0.0,
            trend_pct=trend_pct,
            realized_vol=realized_vol
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
            return None, -1  # sentinel: blocked by schedule, not an error

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
        if ob_snap is not None and ob_snap.spread_pct > 0.0025:
            log.info(f"[{asset}] REJECT: Bid-Ask Spread too wide ({ob_snap.spread_pct*100:.2f}% > 0.25%)")
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
        # oi.open_interest adalah dalam contracts — konversi ke USD dulu
        oi_usd = oi.open_interest * mark_price
        liq_bull, liq_bear, liq_reasons, liq_warns, liq_map = self.liq_analyzer.analyze(
            asset, mark_price, recent_liqs, oi_usd,
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

        # Fetch 1m (swing + micro mom) and 5m (soft HTF for SHORT).
        # Keep LONG quantity: 1m mom still votes. SHORT: 1m votes only if 5m not strongly bull.
        mom_dir = None
        mom_reason = "Momentum: None"
        candles_1m: list = []
        htf_bull_veto = False
        htf_note = "htf=na"
        try:
            async with self.candle_sem:
                now_ms = int(time.time() * 1000)
                # ~60m of 1m for failed-rally swing high
                start_1m = now_ms - 60 * 60 * 1000
                resp_1m, succ_1m = await self.client._call_info_endpoint(
                    "candleSnapshot",
                    {"req": {"coin": asset, "interval": "1m", "startTime": start_1m, "endTime": now_ms}}
                )
                if succ_1m and isinstance(resp_1m, list):
                    candles_1m = resp_1m

                start_5m = now_ms - 40 * 60 * 1000
                resp_5m, succ_5m = await self.client._call_info_endpoint(
                    "candleSnapshot",
                    {"req": {"coin": asset, "interval": "5m", "startTime": start_5m, "endTime": now_ms}}
                )
                if succ_5m and isinstance(resp_5m, list) and len(resp_5m) >= 4:
                    c5 = [float(c["c"]) for c in resp_5m[-4:] if isinstance(c, dict) and "c" in c]
                    o5 = [float(c["o"]) for c in resp_5m[-4:] if isinstance(c, dict) and "o" in c]
                    if len(c5) >= 4 and len(o5) >= 4:
                        green5 = sum(1 for o, c in zip(o5, c5) if c > o)
                        move5 = (c5[-1] - c5[0]) / c5[0] if c5[0] > 0 else 0.0
                        need_g = getattr(config.SIGNAL, "short_htf_bull_candles", 3)
                        need_m = getattr(config.SIGNAL, "short_htf_bull_move_pct", 0.004)
                        htf_bull_veto = green5 >= need_g or move5 >= need_m
                        htf_note = f"5m green={green5}/4 move={move5*100:+.2f}%"
        except Exception as e:
            log.debug(f"[{asset}] Momentum/HTF fetch failed: {e}")

        mom_1m_long = False
        mom_1m_short = False
        if len(candles_1m) >= 3:
            try:
                closes = [float(c["c"]) for c in candles_1m[-3:]]
                opens = [float(c["o"]) for c in candles_1m[-3:]]
                bull_candles = sum(1 for o, c in zip(opens, closes) if c > o)
                bear_candles = sum(1 for o, c in zip(opens, closes) if c < o)
                mom_1m_long = bull_candles >= 2
                mom_1m_short = bear_candles >= 2
                if mom_1m_long:
                    mom_reason = f"Momentum: 1m Bullish ({bull_candles}/3 Green) | {htf_note}"
                elif mom_1m_short:
                    mom_reason = f"Momentum: 1m Bearish ({bear_candles}/3 Red) | {htf_note}"
                else:
                    mom_reason = f"Momentum: 1m Neutral | {htf_note}"
            except (TypeError, ValueError, KeyError):
                pass

        # LONG vote: classic 1m (preserve long fill rate)
        # SHORT vote: 1m bear counts only if 5m is NOT in hard bull (soft HTF filter)
        if mom_1m_long:
            mom_dir = Side.LONG
        elif mom_1m_short:
            if htf_bull_veto and getattr(config.SIGNAL, "short_htf_veto_enabled", True):
                mom_dir = None  # abstain — don't let 1m dump vote short into 5m uptrend
                mom_reason += " | SHORT mom abstain (5m bull veto soft)"
            else:
                mom_dir = Side.SHORT
        else:
            mom_dir = None

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

            # P0-A: Mirror anti-trend hard filter — block LONG in strong 24h dump
            max_down = getattr(config.SIGNAL, "long_max_downtrend_pct", -0.03)
            if trend_pct < max_down:
                log.debug(
                    f"[{asset}] LONG BLOCKED: 24h downtrend {trend_pct*100:.1f}% < "
                    f"{max_down*100:.0f}% (jangan catch knife)"
                )
                return None, 0
        elif short_count >= 3:
            if not config.ALLOW_SHORT:
                log.debug(f"[{asset}] SHORT signal blocked (ALLOW_SHORT=False). WR=31.4% data.")
                return None, 0
            # Soft HTF veto only when 5m is strongly bull AND 1m was the 3rd vote.
            # If OI+Liq+OB already 3 short without mom, still allow (keep quantity).
            core_short = [oi_dir, liq_dir, ob_dir].count(Side.SHORT)
            if (
                getattr(config.SIGNAL, "short_htf_veto_enabled", True)
                and htf_bull_veto
                and core_short < 3
            ):
                log.debug(
                    f"[{asset}] SHORT BLOCKED soft HTF: {htf_note} core_short={core_short}/3"
                )
                return None, 0

            side = Side.SHORT
            raw_score = total_bear + confidence_bonus

            # SHORT thesis (moderate tightness — preserve fill rate):
            # 1) Breakdown: price 1h < -0.25% + (OI expand OR CVD OR structure OR mom red)
            # 2) Failed-rally: real up-leg + rejection from swing high + flow lean
            # 3) Cascade: long-liq cluster + red 1m
            fr = getattr(funding, "funding_rate", 0.0)
            min_fr = getattr(config.SIGNAL, "short_min_funding_rate", 0.00003)
            oi_thr = getattr(config.SIGNAL, "oi_change_threshold_pct", 0.008)
            oi_chg = getattr(oi, "oi_change_pct", 0.0)
            oi_expanding = oi_chg > oi_thr
            price_down_hard = price_change_1h < -0.0025
            cvd_bearish = cvd_val < 0
            # Mild OB lean ok for quantity; strong ask helps quality tag
            ob_bearish = ob_bear > ob_bull
            structure_bear = trend_pct < -0.010 or mom_dir == Side.SHORT
            mom_red = mom_1m_short

            long_liq_cascade = False
            if liq_map is not None and getattr(liq_map, "levels", None):
                long_liq_above = sum(
                    l.notional_usd
                    for l in liq_map.levels
                    if l.side == Side.LONG and l.distance_pct < 0.03
                )
                short_liq_below = sum(
                    l.notional_usd
                    for l in liq_map.levels
                    if l.side == Side.SHORT and l.distance_pct < 0.03
                )
                long_liq_cascade = long_liq_above > short_liq_below * 1.5 and long_liq_above > 0
            if not long_liq_cascade:
                long_liq_cascade = liq_bear >= liq_bull + 4

            breakdown_confirm = oi_expanding or cvd_bearish or structure_bear or mom_red
            breakdown_short = price_down_hard and breakdown_confirm

            # Real failed-rally geometry from 1m swing high (not flat price_1h)
            failed_rally_short = False
            rally_detail = "no_swing"
            lookback = getattr(config.SIGNAL, "short_rally_lookback_mins", 45)
            min_up = getattr(config.SIGNAL, "short_rally_min_up_pct", 0.0025)
            min_rej = getattr(config.SIGNAL, "short_rally_reject_pct", 0.0012)
            max_1h = getattr(config.SIGNAL, "short_rally_max_1h_pct", 0.0020)
            if candles_1m and mark_price > 0:
                try:
                    # last `lookback` 1m bars
                    window = candles_1m[-(lookback + 1):] if len(candles_1m) > lookback else candles_1m
                    highs = [float(c["h"]) for c in window if isinstance(c, dict) and "h" in c]
                    if highs:
                        swing_high = max(highs)
                        up_leg = (swing_high - mark_price) / mark_price
                        # up-leg from an earlier low in window
                        lows = [float(c["l"]) for c in window if isinstance(c, dict) and "l" in c]
                        swing_low = min(lows) if lows else mark_price
                        rally_span = (swing_high - swing_low) / swing_low if swing_low > 0 else 0.0
                        rejected = up_leg >= min_rej and rally_span >= min_up
                        stalled_1h = price_change_1h <= max_1h
                        flow_ok = cvd_bearish or ob_bearish
                        failed_rally_short = rejected and stalled_1h and flow_ok
                        rally_detail = (
                            f"up_leg={up_leg*100:.2f}% span={rally_span*100:.2f}% "
                            f"1h={price_change_1h*100:.2f}% flow={flow_ok}"
                        )
                except (TypeError, ValueError, KeyError) as e:
                    rally_detail = f"parse_err={e}"

            # Base cascade: long-liq cluster + 1m red
            cascade_raw = long_liq_cascade and mom_red
            # P1: don't cascade-short into a pump — need 1h red OR 5m not hard-bull
            cascade_ctx_ok = True
            if cascade_raw and getattr(config.SIGNAL, "short_cascade_require_red_context", True):
                price_1h_red = price_change_1h < 0.0
                five_m_not_bull = not htf_bull_veto
                cascade_ctx_ok = price_1h_red or five_m_not_bull
                if not cascade_ctx_ok:
                    log.debug(
                        f"[{asset}] SHORT cascade blocked (P1 pump context): "
                        f"price_1h={price_change_1h*100:+.2f}% {htf_note} — need red 1h or 5m not bull"
                    )
            cascade_short = cascade_raw and cascade_ctx_ok

            if breakdown_short:
                conf_bits = []
                if oi_expanding:
                    conf_bits.append(f"OI+{oi_chg*100:.2f}%")
                if cvd_bearish:
                    conf_bits.append("cvd_bear")
                if structure_bear:
                    conf_bits.append("structure_bear")
                if mom_red:
                    conf_bits.append("mom_1m_red")
                oi_reasons.append(
                    f"SHORT setup: breakdown continuation "
                    f"(price_1h={price_change_1h*100:.2f}%, confirm={'+'.join(conf_bits) or 'n/a'})"
                )
            elif failed_rally_short:
                fund_note = (
                    f"funding+={fr:.6f}"
                    if fr >= min_fr
                    else f"funding={fr:.6f} (optional)"
                )
                oi_reasons.append(
                    f"SHORT setup: failed-rally short "
                    f"({rally_detail}, {fund_note})"
                )
            elif cascade_short:
                oi_reasons.append(
                    f"SHORT setup: cascade short "
                    f"(long_liq_cluster=True, mom_red=True, liq_b/s={liq_bull}/{liq_bear}, "
                    f"ctx_1h={price_change_1h*100:+.2f}%, {htf_note})"
                )
            else:
                log.debug(
                    f"[{asset}] SHORT BLOCKED: no valid thesis "
                    f"(price_1h={price_change_1h:.4f}, oi_chg={oi_chg:.4f}, "
                    f"cvd_bear={cvd_bearish}, ob_bear={ob_bearish}, "
                    f"structure_bear={structure_bear}, long_liq_cascade={long_liq_cascade}, "
                    f"mom_red={mom_red}, cascade_raw={cascade_raw}, cascade_ctx={cascade_ctx_ok}, "
                    f"rally={rally_detail}, fr={fr:.6f}, {htf_note})"
                )
                return None, 0

            max_up = getattr(config.SIGNAL, "short_max_uptrend_pct", 0.03)
            if trend_pct > max_up:
                log.debug(
                    f"[{asset}] SHORT BLOCKED: 24h uptrend {trend_pct*100:.1f}% > "
                    f"{max_up*100:.0f}% (jangan lawan trend)"
                )
                return None, 0
        else:
            return None, 0

        # ── Bull-Bear gap filter (P0-C: SHORT gap == LONG gap until data says otherwise) ──
        bull_bear_gap = abs(total_bull - total_bear)
        if side == Side.SHORT:
            min_gap = getattr(config.SIGNAL, "min_bull_bear_gap_short", 18)
        else:
            min_gap = getattr(config.SIGNAL, "min_bull_bear_gap", 18)
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

        # Session bonus TIDAK dimasukkan ke raw_score sebelum multiplier.
        # Sebelumnya session +10 ikut dikalikan trend_multiplier 1.10x = efektif +11.
        # Ini menyebabkan skor 75+ banyak berisi sinyal lemah yang tertiup session bonus.
        # Session bonus ditambahkan SETELAH multiplier agar nilainya tetap flat.
        raw_score = max(0, min(raw_score, 92))

        # ── Apply regime multiplier ────────────────────────────────────
        vol_multiplier = {
            MarketRegime.LOW_VOL:  0.90,
            MarketRegime.NORMAL:   1.00,
            MarketRegime.HIGH_VOL: 0.85,
            MarketRegime.EXTREME:  0.70,
        }.get(vol_regime, 1.00)

        # Trend multiplier — hanya diperhitungkan jika trend > 1.5%
        trend_multiplier = 1.10 if abs(trend_pct) > 0.015 else 0.95

        final_multiplier = vol_multiplier * trend_multiplier
        final_score = int(raw_score * final_multiplier)

        # Session bonus after multiplier (flat, not multiplied).
        # P1: counter-trend SHORT only gets a fraction of session bonus
        # (was free +14 into pumps → false high scores).
        applied_session = int(session_bonus)
        if side == Side.SHORT and session_bonus > 0:
            counter_trend = (trend_pct > 0.0) or bool(htf_bull_veto)
            if counter_trend:
                damp = float(getattr(config.SIGNAL, "short_countertrend_session_mult", 0.35))
                damp = max(0.0, min(damp, 1.0))
                applied_session = int(round(session_bonus * damp))
                session_reasons = list(session_reasons) + [
                    f"Session short counter-trend damp ×{damp:.2f} "
                    f"({session_bonus}→{applied_session}; trend_24h={trend_pct*100:+.1f}%, {htf_note})"
                ]
        final_score += applied_session
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
            session_bonus=int(applied_session),
            regime_multiplier=final_multiplier,
            total_bull=int(total_bull),
            total_bear=int(total_bear),
            raw_score=int(raw_score),
            final_score=int(final_score),
            direction_score=int(raw_score),
            trade_quality_score=int(final_score - raw_score) if final_score >= raw_score else 0,
            failure_risk_score=int(raw_score - final_score) if final_score < raw_score else 0,
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
        concentration_add = self._asset_concentration_threshold_add(asset, "standard")
        if concentration_add > 0:
            threshold = min(100, threshold + concentration_add)
            all_reasons.append(f"Asset concentration guard: {asset} requires {threshold}+ after recent repeats")
        if final_score < threshold:
            log.debug(f"[{asset}] STANDARD: score {final_score} < threshold {threshold}, skipping")
            return None, final_score

        # ── Build signal ───────────────────────────────────────────────
        signal = self._build_signal(
            asset, side, final_score, log_regime, breakdown, mark_price,
            realized_vol=realized_vol,
            oi_usd=oi_usd,
            funding_rate=funding.funding_rate,
            trend_pct=trend_pct,
        )

        self._log_signal_marker(
            "standard",
            signal,
            entry_location_quality="na",
            concentration_delta=concentration_add,
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
        now_utc = datetime.now(timezone.utc)
        hour = now_utc.hour
        ny_start = SIGNAL.ny_session_start_utc
        ny_end   = SIGNAL.ny_session_end_utc
        lon_start= SIGNAL.london_start_utc
        lon_end  = SIGNAL.london_end_utc

        score = 0
        reasons = []

        is_ny  = ny_start  <= hour < ny_end
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

        log.debug(
            f"[SESSION] UTC={now_utc.strftime('%H:%M')} hour={hour} "
            f"London={is_lon}({lon_start}-{lon_end}) NY={is_ny}({ny_start}-{ny_end}) "
            f"bonus={score:+d}"
        )
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
        funding_rate: float = 0.0,
        trend_pct: float = 0.0,
    ) -> TradeSignal:
        # Determine strength
        if score >= 75:
            strength = SignalStrength.STRONG
        elif score >= 60:
            strength = SignalStrength.MODERATE
        else:
            strength = SignalStrength.WEAK

        # Placeholder SL/TP — akan di-override oleh calculate_levels() di main.py
        # sebelum sinyal dieksekusi. Nilai di sini hanya untuk mengisi field TradeSignal.
        sl_pct  = max(realized_vol * 1.00, 0.030)   # minimal 3%
        tp1_pct = sl_pct * 0.65 * 2.3
        tp2_pct = sl_pct * 2.3
        sl_pct  = min(sl_pct, 0.080)

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
                funding_rate=funding_rate,
                realized_vol=realized_vol,
                trend_pct=trend_pct,
                micro_risk_pct=abs(stop_loss - mark_price) / mark_price,
                entry_location_quality="unknown",
                trade_mode="standard",
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
            expected_edge=expected_edge,
            funding_rate=funding_rate,
            trend_pct=trend_pct,
            realized_vol=realized_vol
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
        if raw_score >= 72:
            score_bucket = "s72p"
        elif raw_score >= 65:
            score_bucket = "s65_71"
        elif raw_score >= 60:
            score_bucket = "s60_64"
        else:
            score_bucket = "sub60"

        asset_side_key = f"{mode.lower()}_{asset}_{side.value}"
        pattern_key = f"{asset_side_key}_{score_bucket}"
        side_bucket_key = f"{mode.lower()}_{side.value}_{score_bucket}"
        side_key = f"{mode.lower()}_{side.value}"

        def is_low_ev(stats: Optional[Dict]) -> bool:
            return bool(stats) and (
                float(stats["winrate_ema"]) <= config.SIGNAL.meta_penalty_threshold
                or float(stats.get("pnl_ema", 0.0)) < config.SIGNAL.meta_penalty_pnl_ema
            )

        specific = user_db.get_meta_pattern_stats(pattern_key)
        if specific and specific["samples"] >= config.SIGNAL.meta_boost_samples:
            wr = float(specific["winrate_ema"])
            pnl = float(specific.get("pnl_ema", 0.0))
            if wr >= config.SIGNAL.meta_boost_threshold and pnl > config.SIGNAL.meta_min_pnl_ema_for_boost:
                delta = config.SIGNAL.meta_specific_boost
                return delta, (
                    f"Meta specific n={specific['samples']} WR {wr*100:.0f}% EV {pnl:+.2f} -> {delta:+d}"
                ), pattern_key
            if is_low_ev(specific):
                delta = config.SIGNAL.meta_specific_penalty
                return delta, (
                    f"Meta specific low-EV n={specific['samples']} WR {wr*100:.0f}% EV {pnl:+.2f} -> {delta:+d}"
                ), pattern_key

        # Broader keys never boost. They only reduce repeated loss exposure while
        # preserving 1m scorer as primary source of conviction.
        for level, key, min_samples, delta in (
            ("asset-side", asset_side_key, config.SIGNAL.meta_penalty_samples, config.SIGNAL.meta_asset_side_penalty),
            ("side-bucket", side_bucket_key, config.SIGNAL.meta_side_bucket_penalty_samples, config.SIGNAL.meta_side_bucket_penalty),
            ("side", side_key, config.SIGNAL.meta_side_penalty_samples, config.SIGNAL.meta_side_penalty),
        ):
            stats = user_db.get_meta_pattern_stats(key)
            if stats and stats["samples"] >= min_samples and is_low_ev(stats):
                return delta, (
                    f"Meta {level} low-EV n={stats['samples']} WR {stats['winrate_ema']*100:.0f}% "
                    f"EV {stats.get('pnl_ema', 0.0):+.2f} -> {delta:+d}"
                ), pattern_key

        return 0, "", pattern_key
