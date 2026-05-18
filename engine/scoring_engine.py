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

        # [RAILWAY TELEMETRY] Signal skip counters for observability
        self.skip_counters: Dict[str, int] = {
            "score_below_threshold": 0,
            "short_score_below_min": 0,
            "short_funding_too_low": 0,
            "squeeze_guard": 0,
            "blocked_hour": 0,
            "no_mark_price": 0,
            "other": 0,
        }
        self._skip_count_since_summary = 0
        self._last_signal_time: Optional[float] = None

    def dump_oi_state(self):
        """Persist OI snapshots to database to prevent amnesia on restart."""
        from core.db import user_db
        user_db.save_oi_snapshots_batch(self._oi_snapshots)

    def log_skip_summary(self):
        """[RAILWAY TELEMETRY] Log aggregated skip reasons. Called from main.py every 5 min."""
        total_skips = sum(self.skip_counters.values())
        if total_skips == 0:
            return
        parts = [f"{k}={v}" for k, v in self.skip_counters.items() if v > 0]
        log.info(f"[SKIP-SUMMARY] total={total_skips} | {' | '.join(parts)}")
        # Reset counters after logging
        self.skip_counters = {k: 0 for k in self.skip_counters}

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
        Returns -1 as max_score if ALL active modes were blocked by schedule.
        """
        import config
        # KARA runs exclusively in Scalper Mode — active_modes parameter kept for signature compat
        signals = {}
        max_score_found = 0
        now_ts = time.monotonic()

        try:
            cooldown_secs = config.SCALPER.signal_cooldown_minutes * 60
            last_ts = self._last_signal_ts.get(f"{asset}_scalper", 0)

            sig, score_scl = await self._run_scalper(asset, meta_data)

            if score_scl == -1:
                return signals, -1

            if score_scl > max_score_found:
                max_score_found = score_scl

            if sig and (now_ts - last_ts >= cooldown_secs):
                signals["scalper"] = sig
                self._last_signal_ts[f"{asset}_scalper"] = now_ts

                # OI delta 1m dari snapshot cache
                _snaps = self._oi_snapshots.get(asset, [])
                _oi_str = ""
                if len(_snaps) >= 2:
                    _snap_now  = [v for t, v in _snaps if t >= now_ts - 90]
                    _snap_prev = [v for t, v in _snaps if t < now_ts - 90 and t >= now_ts - 180]
                    if _snap_now and _snap_prev and _snap_prev[-1] > 0:
                        _oi_delta = (_snap_now[-1] - _snap_prev[-1]) / _snap_prev[-1] * 100
                        _oi_str = f" | OI_1m={_oi_delta:+.2f}%"

                _fr_str = f" | FR={sig.funding_rate*100:+.4f}%" if sig.funding_rate is not None else ""
                _reasons = ", ".join(sig.breakdown.reasons) if sig.breakdown.reasons else "-"
                log.info(
                    f"⚡ SCALPER SIGNAL: {asset} {sig.side.value.upper()} score={score_scl}"
                    f" | entry={sig.entry_price} | SL={sig.stop_loss} | TP1={sig.tp1} | TP2={sig.tp2}"
                    f" | {sig.suggested_leverage}x | regime={sig.regime.value}"
                    f"{_fr_str}{_oi_str}"
                    f" | [{_reasons}]"
                )
            elif sig:
                log.debug(f"{asset} [SCALPER]: cooldown active (score={score_scl} blocked)")
        except RuntimeError as e:
            if "backoff" in str(e):
                log.debug(f"[SCAN] {asset}: skipped (API backoff)")
                return {}, 0
            log.error(f"Error in scalper: {e}")
        except Exception as e:
            log.error(f"Error in scalper: {e}")

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
            log.info(f"[SKIP] {asset} | reason=blocked_hour | context=hour={current_hour}")
            self.skip_counters["blocked_hour"] = self.skip_counters.get("blocked_hour", 0) + 1
            return None, -1  # sentinel: blocked by schedule, not an error

        # 1. Get mark price
        mark_price = await self.client.get_mark_price(asset, meta=meta_data)
        if mark_price <= 0:
            log.info(f"[SKIP] {asset} | reason=no_mark_price | context=price={mark_price}")
            self.skip_counters["no_mark_price"] = self.skip_counters.get("no_mark_price", 0) + 1
            return None, 0

        # 2. Fetch 1-minute candles (last 30 for EMA/RSI)
        # Hybrid: REST gives closed candles, WS trades cache patches the current (in-progress) minute.
        # Eliminates 0-60s lag on the live bar (critical for EMA8/RSI/CVD on 20-min scalping).
        candles = []
        try:
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
            log.info(f"[SKIP] {asset} | reason=candle_fetch_fail | context={e}")

        # Patch in the LIVE in-progress minute from WS trades cache (real-time, no API call).
        try:
            import time as _time
            current_min_ms = (int(_time.time()) // 60) * 60 * 1000
            live_candle = self._build_live_candle_from_trades(asset, current_min_ms)
            if live_candle:
                _replaced = False
                if candles and isinstance(candles[-1], dict):
                    last_t = int(candles[-1].get("t", 0) or candles[-1].get("T", 0))
                    if last_t == current_min_ms:
                        candles[-1] = live_candle
                        _replaced = True
                    else:
                        candles.append(live_candle)
                else:
                    candles = [live_candle]
                log.info(
                    f"[LIVE-CANDLE] {asset} | o={live_candle['o']:.6f} h={live_candle['h']:.6f} "
                    f"l={live_candle['l']:.6f} c={live_candle['c']:.6f} v={live_candle['v']:.2f} | "
                    f"replaced={_replaced}"
                )
        except Exception as e:
            log.info(f"[LIVE-CANDLE] {asset} | overlay FAILED | reason={e}")

        # 3. Fetch 15m MTF trend
        mtf_trend = await self._fetch_15m_mtf_data(asset)

        # 3b. Fundamental data (OI, Funding, Liquidation) — from cache, zero extra API calls
        #     [FIX 2026-05-09] Sebelumnya hanya dipakai di standard mode.
        #     Sekarang scalper juga pakai agar sinyal diperkaya dengan data fundamental.
        try:
            funding = await self.client.get_funding_data(asset, meta=meta_data)
            oi = await self.client.get_oi_data(asset, meta=meta_data)
        except Exception:
            funding = FundingData(asset=asset, funding_rate=0, premium=0, predicted_rate=0, hourly_trend=[])
            oi = OIData(asset=asset, open_interest=0, oi_change_pct=0.0, oi_change_24h=0.0)

        # Spot price for basis calculation (same as standard mode)
        now_mono = time.monotonic()
        if not self._spot_prices or (now_mono - self._spot_cache_time) > 10:
            self._spot_prices = await self.client.get_all_mids()
            self._spot_cache_time = now_mono
        spot_price = self._spot_prices.get(f"@{asset}") or getattr(oi, 'oracle_price', 0) or mark_price

        # Price history + OI snapshot tracking (same as standard mode)
        self._update_price_history(asset, mark_price)
        price_change_1h = self._get_price_change(asset, minutes=60)
        _now_ts = time.time()
        if asset not in self._oi_snapshots:
            self._oi_snapshots[asset] = []
        self._oi_snapshots[asset].append((_now_ts, oi.open_interest))
        # [SCALPER FIX] Use 5-min OI delta — 1h delta is irrelevant for 20-min hold window.
        # Falls back to oldest available snapshot if 5min history not yet built up.
        five_min_ago = _now_ts - 300
        recent_old = [v for t, v in self._oi_snapshots[asset] if t <= five_min_ago]
        if recent_old:
            oi.oi_change_pct = (oi.open_interest - recent_old[-1]) / max(recent_old[-1], 1)
        elif len(self._oi_snapshots[asset]) >= 2:
            # Bootstrap: use oldest snapshot we have (better than zero)
            oldest_v = self._oi_snapshots[asset][0][1]
            oi.oi_change_pct = (oi.open_interest - oldest_v) / max(oldest_v, 1)
        # Retain 2h of snapshots for other consumers / debugging
        self._oi_snapshots[asset] = [
            (t, v) for t, v in self._oi_snapshots[asset] if t > _now_ts - 7200
        ]

        # Run fundamental analyzers (same analyzers as standard mode)
        funding_history = self.cache.funding_history.get(asset, []) if hasattr(self.cache, 'funding_history') else []
        oi_bull, oi_bear, oi_reasons, oi_warns = self.oi_funding_analyzer.analyze(
            asset, funding, oi, funding_history, price_change_1h, mark_price, spot_price
        )
        recent_liqs = self.cache.liquidations if hasattr(self.cache, 'liquidations') else []
        oi_usd = oi.open_interest * mark_price
        liq_bull, liq_bear, liq_reasons, liq_warns, liq_map = self.liq_analyzer.analyze(
            asset, mark_price, recent_liqs, oi_usd,
            funding_rate=funding.funding_rate
        )

        # Short Squeeze Detection: price spike + OI drop dalam 1m = squeeze risk → blok SHORT
        _squeeze_detected = False
        _squeeze_reason = ""
        _snaps = self._oi_snapshots.get(asset, [])
        if len(_snaps) >= 2:
            _snap_1m_ago = [v for t, v in _snaps if t >= _now_ts - 90]
            _snap_prev = [v for t, v in _snaps if t < _now_ts - 90 and t >= _now_ts - 180]
            if _snap_1m_ago and _snap_prev:
                _oi_now = _snap_1m_ago[-1]
                _oi_prev = _snap_prev[-1]
                if _oi_prev > 0:
                    _oi_delta_pct = (_oi_now - _oi_prev) / _oi_prev
                    _price_1m_ago = None
                    if len(candles) >= 2:
                        try:
                            _price_1m_ago = float(candles[-2].get("c", 0)) if isinstance(candles[-2], dict) else None
                        except (TypeError, ValueError, AttributeError):
                            _price_1m_ago = None
                    if _price_1m_ago and _price_1m_ago > 0:
                        _price_chg = (mark_price - _price_1m_ago) / _price_1m_ago
                        if _price_chg > 0.010 and _oi_delta_pct < -0.05:
                            _squeeze_detected = True
                            _squeeze_reason = (
                                f"price +{_price_chg*100:.1f}% + OI {_oi_delta_pct*100:.1f}%"
                            )

        # 4a. Fetch regime BEFORE scoring for regime_multiplier [AUDIT Phase 1]
        vol_regime, realized_vol, trend_pct = await self._fetch_vol_regime(asset)

        # 4a2. [4H REGIME FILTER 2026-05-18] Fetch 4h market regime.
        # Aligns 1m scalp direction with higher-timeframe trend.
        # TRENDING_UP:   only LONG allowed at normal threshold; SHORT needs +8 extra score
        # TRENDING_DOWN: only SHORT allowed at normal threshold; LONG needs +8 extra score
        # CHOPPY:        both directions allowed but threshold raised +2 (lower edge)
        htf_regime = await self._fetch_4h_regime(asset)

        # 4b. Compute scalper indicators (enriched with fundamental data)
        # [F1 FIX 2026-05-18] Capture per-analyzer signed contributions
        _scalper_components: dict = {}
        score, side, reasons = self._calculate_scalper_score(
            asset, mark_price, candles, mtf_trend,
            oi_bull=oi_bull, oi_bear=oi_bear,
            liq_bull=liq_bull, liq_bear=liq_bear,
            fund_reasons=oi_reasons[:2], liq_reasons=liq_reasons[:2],
            out_components=_scalper_components,
            trend_pct=trend_pct,
        )

        # 4b2. Apply 4h regime adjustment to score and leverage
        htf_threshold_adj = 0
        htf_leverage_adj  = 0
        if htf_regime == "TRENDING_UP":
            if side == Side.LONG:
                htf_threshold_adj = -3   # easier entry — aligned with 4h trend
                htf_leverage_adj  = +2   # slightly more conviction
                reasons.append(f"📈 4H TRENDING_UP — LONG aligned (+lev, -threshold)")
            else:  # SHORT against 4h trend
                htf_threshold_adj = +8   # need much stronger signal to fade 4h trend
                htf_leverage_adj  = -3
                reasons.append(f"⚠️ 4H TRENDING_UP — SHORT counter-trend (+8 threshold)")
        elif htf_regime == "TRENDING_DOWN":
            if side == Side.SHORT:
                htf_threshold_adj = -3
                htf_leverage_adj  = +2
                reasons.append(f"📉 4H TRENDING_DOWN — SHORT aligned (+lev, -threshold)")
            else:  # LONG against 4h trend
                htf_threshold_adj = +8
                htf_leverage_adj  = -3
                reasons.append(f"⚠️ 4H TRENDING_DOWN — LONG counter-trend (+8 threshold)")
        else:  # CHOPPY
            htf_threshold_adj = +2   # choppy 4h = lower edge, need stronger 1m signal
            htf_leverage_adj  = -2
            reasons.append(f"〰️ 4H CHOPPY — threshold +2, leverage reduced")

        # 4c. [QUANT AGGRESSION] Regime multiplier — trending = boost, late trend flagged.
        # Original AUDIT logic penalized late trend (×0.7) but data shows this just kills
        # valid entries. Now: trending = boost, late_trend = flag for exit tuning.
        late_trend = False
        if vol_regime in (MarketRegime.HIGH_VOL, MarketRegime.EXTREME):
            _regime_cat = "volatile"
            _regime_mult = 0.9
        elif abs(trend_pct) >= 0.030:
            _regime_cat = "late_trend"
            _regime_mult = 1.15
            late_trend = True
            reasons.append(f"Late trend chase: {trend_pct*100:.2f}%/1h — tighter SL, wider TP")
        elif abs(trend_pct) >= 0.015:
            _regime_cat = "trending"
            _regime_mult = 1.2
        else:
            _regime_cat = "ranging"
            _regime_mult = 1.0
        score_pre = score
        score = int(score * _regime_mult)
        score = max(0, min(score, 100))
        reasons.append(f"🌐 Regime: {_regime_cat} (×{_regime_mult}, {score_pre}→{score})")

        # [SESSION FIX 2026-05-18]: Split session bonus — small portion to score,
        # majority to threshold. Old: +14 all to score → inflated top decile (21% WR).
        # New: +4 to score (NY+3, London+1), rest lowers threshold.
        # Rationale: session IS a mild edge (more liquidity, tighter spreads),
        # but should not be the primary driver of a high score.
        session_bonus, session_reasons, session_threshold_delta = self._get_session_bonus()
        SESSION_SCORE_RATIO = 0.30  # 30% of bonus goes to score, 70% to threshold
        session_score_add = int(round(session_bonus * SESSION_SCORE_RATIO))  # NY=+3, London=+1
        session_threshold_add = session_bonus - session_score_add            # NY=+7, London=+3
        score += session_score_add
        score = max(0, min(score, 100))
        reasons.extend(session_reasons)

        # Effective threshold: session lowers bar proportionally + 4h regime adjustment
        effective_threshold = (
            config.SCALPER.min_score_to_enter
            - session_threshold_add         # NY: threshold drops by 7
            + session_threshold_delta       # Asia: threshold rises
            + htf_threshold_adj             # 4h regime: aligned=-3, counter=+8, choppy=+2
        )
        if score < effective_threshold:
            log.info(
                f"[SKIP] {asset} | score={score} | side={side.value} | "
                f"reason=score_below_threshold | context=threshold={effective_threshold},regime={_regime_cat}"
            )
            self.skip_counters["score_below_threshold"] = self.skip_counters.get("score_below_threshold", 0) + 1
            self._skip_count_since_summary += 1
            return None, score

        # SHORT-specific filters: higher threshold + funding rate confirmation + squeeze guard
        if side == Side.SHORT:
            short_min = getattr(config.SIGNAL, 'min_score_short_signal', 62)
            if score < short_min:
                log.info(
                    f"[SKIP] {asset} | score={score} | side=SHORT | "
                    f"reason=short_score_below_min | context=min={short_min}"
                )
                self.skip_counters["short_score_below_min"] = self.skip_counters.get("short_score_below_min", 0) + 1
                self._skip_count_since_summary += 1
                return None, score

            cached_funding = self.cache.funding_history.get(asset, []) if hasattr(self.cache, 'funding_history') else []
            if cached_funding:
                fr = cached_funding[-1] if isinstance(cached_funding[-1], float) else float(cached_funding[-1].get('fundingRate', 0) if isinstance(cached_funding[-1], dict) else cached_funding[-1])
                min_fr = getattr(config.SIGNAL, 'short_min_funding_rate', -0.0001)
                if fr < min_fr:
                    log.info(
                        f"[SKIP] {asset} | score={score} | side=SHORT | "
                        f"reason=short_funding_too_low | context=fr={fr:.6f},min={min_fr}"
                    )
                    self.skip_counters["short_funding_too_low"] = self.skip_counters.get("short_funding_too_low", 0) + 1
                    self._skip_count_since_summary += 1
                    return None, score

            if _squeeze_detected:
                log.info(
                    f"[SKIP] {asset} | score={score} | side=SHORT | "
                    f"reason=squeeze_guard | context={_squeeze_reason}"
                )
                self.skip_counters["squeeze_guard"] = self.skip_counters.get("squeeze_guard", 0) + 1
                self._skip_count_since_summary += 1
                return None, score

            # [FIX 2026-05-14] Technical minimum gate for SHORT
            # Block SHORT jika OI+Liq fundamental score terlalu rendah.
            # Mencegah sinyal "session-only" lolos — misal score=59 tapi OI=0,Liq=0.
            # OB score tidak tersedia di scope ini, threshold pakai nilai lebih kecil (6).
            _tech_fundamental = (oi_bear or 0) + (liq_bear or 0)
            _min_tech_short = max(getattr(config.SIGNAL, 'min_technical_score_short', 10) - 4, 6)
            if _tech_fundamental < _min_tech_short:
                log.info(
                    f"[SKIP] {asset} | score={score} | side=SHORT | "
                    f"reason=weak_technical | context=fundamental_pts={_tech_fundamental:.1f}<{_min_tech_short} "
                    f"(OI_bear={oi_bear:.1f} Liq_bear={liq_bear:.1f})"
                )
                self.skip_counters["weak_technical_short"] = self.skip_counters.get("weak_technical_short", 0) + 1
                self._skip_count_since_summary += 1
                return None, score

        # ── [QUANT AGGRESSION 2026] Funding extreme = CONTRARIAN OPPORTUNITY, bukan veto.
        # Sebelumnya veto membuang 15-20% sinyal. Sekarang: flagkan sebagai "fade_mode".
        fade_mode = False
        try:
            veto_threshold = getattr(config.SIGNAL, 'funding_extreme_threshold', 0.0003)
            fr_now = funding.funding_rate if funding else 0.0
            if side == Side.LONG and fr_now > veto_threshold:
                fade_mode = True
                reasons.append(
                    f"🎯 FADE mode: extreme positive funding {fr_now*100:.4f}% — crowded longs, contrarian entry"
                )
            elif side == Side.SHORT and fr_now < -veto_threshold:
                fade_mode = True
                reasons.append(
                    f"🎯 FADE mode: extreme negative funding {fr_now*100:.4f}% — crowded shorts, contrarian entry"
                )
        except Exception as _fade_err:
            log.debug(f"[SCALPER] {asset}: funding fade check skipped: {_fade_err}")

        # 4. Build signal with scalper TP/SL (vol_regime already fetched in step 4a)
        # Compute ATR% from the same candles used for scoring — avoids re-fetch.
        _highs, _lows, _closes = [], [], []
        for _c in candles:
            if isinstance(_c, dict):
                try:
                    _highs.append(float(_c.get("h", 0)))
                    _lows.append(float(_c.get("l", 0)))
                    _closes.append(float(_c.get("c", 0)))
                except (TypeError, ValueError):
                    pass
        atr_pct_now = self._compute_atr_pct(_highs, _lows, _closes, period=14)
        signal = self._build_scalper_signal(
            asset, side, score, mark_price, reasons, vol_regime,
            session_bonus, realized_vol, trend_pct, atr_pct=atr_pct_now,
            fade_mode=fade_mode,
            late_trend=late_trend,
            # [F1 FIX 2026-05-18] Pass per-analyzer signed scores for breakdown telemetry
            oi_signed=_scalper_components.get("oi_signed", 0),
            liq_signed=_scalper_components.get("liq_signed", 0),
            ob_signed=_scalper_components.get("ob_signed", 0),
        )

        # Apply 4h regime leverage adjustment
        if signal and htf_leverage_adj != 0:
            scfg = config.SCALPER
            new_lev = max(3, min(scfg.max_leverage, signal.suggested_leverage + htf_leverage_adj))
            signal.suggested_leverage = new_lev

        # Attach last-known funding rate for Telegram warning
        _fr_cache = self.cache.funding_history.get(asset, []) if hasattr(self.cache, 'funding_history') else []
        if _fr_cache:
            _last_fr = _fr_cache[-1]
            signal.funding_rate = _last_fr if isinstance(_last_fr, float) else float(_last_fr.get('fundingRate', 0) if isinstance(_last_fr, dict) else _last_fr)

        # [RAILWAY TELEMETRY] Signal Decision Trace — end-to-end log for accepted signals
        if signal:
            self._last_signal_time = time.time()
            log.info(
                f"[SIGNAL-TRACE] {asset} | FINAL | score={score} | side={side.value} | "
                f"sl={signal.stop_loss:.4f} | tp1={signal.tp1:.4f} | tp2={signal.tp2:.4f} | "
                f"atr_used={atr_pct_now > 0} | fade={fade_mode} | late_trend={late_trend} | "
                f"reasons={' | '.join(reasons[-5:])}"
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

    @staticmethod
    def _compute_atr_pct(highs: list, lows: list, closes: list, period: int = 14) -> float:
        """
        Compute ATR as percentage of last close. Returns 0.0 if insufficient data.
        TR = max(high-low, |high-prev_close|, |low-prev_close|)
        """
        n = min(len(highs), len(lows), len(closes))
        if n < period + 1:
            return 0.0
        try:
            trs = []
            for i in range(n - period, n):
                if i == 0:
                    continue
                h = float(highs[i])
                l = float(lows[i])
                pc = float(closes[i - 1])
                tr = max(h - l, abs(h - pc), abs(l - pc))
                trs.append(tr)
            if not trs:
                return 0.0
            atr = sum(trs) / len(trs)
            last_close = float(closes[-1])
            if last_close <= 0:
                return 0.0
            return atr / last_close
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0

    def _build_live_candle_from_trades(self, asset: str, minute_ms: int) -> dict | None:
        """
        Build a single 1m candle for the current (in-progress) minute from
        the WS trades cache. Returns None if no trades available.
        """
        trades = self.cache.trades.get(asset, []) if hasattr(self.cache, 'trades') else []
        if not trades:
            return None
        # Filter trades belonging to the requested minute bucket
        bucket_start = minute_ms
        bucket_end   = minute_ms + 60_000
        bucket = []
        for t in trades:
            try:
                ts = float(t.get("time", 0))
            except (TypeError, ValueError):
                continue
            if bucket_start <= ts < bucket_end:
                bucket.append(t)
        if not bucket:
            return None
        try:
            prices = [float(t.get("px", 0)) for t in bucket if float(t.get("px", 0)) > 0]
            sizes  = [float(t.get("sz", 0)) for t in bucket]
        except (TypeError, ValueError):
            return None
        if not prices:
            return None
        return {
            "t": bucket_start,
            "o": prices[0],
            "h": max(prices),
            "l": min(prices),
            "c": prices[-1],
            "v": sum(sizes),
        }

    def _calculate_scalper_score(
        self, asset: str, mark_price: float, candles: list, mtf_trend: str = "neutral",
        oi_bull: float = 0, oi_bear: float = 0,
        liq_bull: float = 0, liq_bear: float = 0,
        fund_reasons: list = None, liq_reasons: list = None,
        out_components: dict = None,
        trend_pct: float = 0.0,
    ) -> Tuple[int, Side, List[str]]:
        """
        Fast scalper scoring using technical indicators on 1m candles
        + fundamental data (OI, Funding, Liquidation) from standard pipeline.
        Returns (score: int, side: Side, reasons: List[str])

        out_components (optional): if provided, will be populated with signed
        per-analyzer contributions for breakdown telemetry:
          ob_signed:  positive = bid-heavy (bullish), negative = ask-heavy (bearish)
          oi_signed:  oi_bull - oi_bear (passed through)
          liq_signed: liq_bull - liq_bear (passed through)

        trend_pct: 1h price change. If |trend_pct| >= 0.015 (1.5%) we treat the
        market as TRENDING and invert RSI level + divergence signals
        (momentum continuation). Otherwise we keep classic mean-reversion logic.
        """
        score = 0
        bull_pts = 0
        bear_pts = 0
        reasons = []

        # Component trackers for SCORE-DEBUG log
        _c_ob = 0
        _c_fund = 0
        _c_liq = 0
        _c_ema = 0
        _c_rsi = 0
        _c_div = 0
        _c_wick = 0
        _c_cvd = 0
        _c_vol = 0
        _c_mtf = 0
        # [F1 FIX 2026-05-18] Signed component for breakdown telemetry
        _ob_signed = 0

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
                    if spread_pct > 0.0015:
                        log.debug(f"[{asset}] SCALPER REJECT: Spread too wide ({spread_pct*100:.2f}%)")
                        return 0, Side.LONG, ["REJECT: Spread too wide"]
            except Exception:
                imb = 0.0

            if imb > 0.60:
                bull_pts += 20; _c_ob = 20; _ob_signed = 20
                reasons.append(f"📗 Strong bid wall (imbalance {imb:.2f}) → LONG")
            elif imb < -0.60:
                bear_pts += 20; _c_ob = 20; _ob_signed = -20
                reasons.append(f"📕 Strong ask wall (imbalance {imb:.2f}) → SHORT")
            elif imb > 0.40:
                bull_pts += 8; _c_ob = 8; _ob_signed = 8
                reasons.append(f"🟢 Mild bid pressure ({imb:.2f})")
            elif imb < -0.40:
                bear_pts += 8; _c_ob = 8; _ob_signed = -8
                reasons.append(f"🔴 Mild ask pressure ({imb:.2f})")

        # ── OI + Funding Analysis (from standard pipeline — zero API calls) ──────
        # [FIX 2026-05-09] Sebelumnya scalper hanya pakai teknikal.
        # Sekarang fundamental data (OI rising+funding, basis, liquidation) juga
        # diperhitungkan — sama seperti standard mode, di-scale max ±12 pts.
        if oi_bull > 0 or oi_bear > 0:
            fund_delta = oi_bull - oi_bear
            fund_pts = max(-25, min(25, int(fund_delta)))  # [AUDIT] Fundamental 40%: OI/Funding max ±25
            _c_fund = abs(fund_pts)
            if fund_pts > 0:
                bull_pts += fund_pts
                reasons.append(f"📊 OI/Funding bullish (+{fund_pts})")
            elif fund_pts < 0:
                bear_pts += abs(fund_pts)
                reasons.append(f"📊 OI/Funding bearish ({fund_pts})")
            if fund_reasons:
                reasons.extend(fund_reasons)

        # ── Liquidation Analysis (from standard pipeline) ────────────────────────
        if liq_bull > 0 or liq_bear > 0:
            liq_delta = liq_bull - liq_bear
            liq_pts = max(-15, min(15, int(liq_delta)))  # [AUDIT] Fundamental 40%: Liquidation max ±15
            _c_liq = abs(liq_pts)
            if liq_pts > 0:
                bull_pts += liq_pts
                reasons.append(f"💥 Liq bias bullish (+{liq_pts})")
            elif liq_pts < 0:
                bear_pts += abs(liq_pts)
                reasons.append(f"💥 Liq bias bearish ({liq_pts})")
            if liq_reasons:
                reasons.extend(liq_reasons)

        if len(candles) < 10:
            # Cannot compute EMA/RSI without data — use OB + fundamental score
            total = bull_pts + bear_pts
            side = Side.LONG if bull_pts >= bear_pts else Side.SHORT
            bull_candles = 0
            bear_candles = 0
            score = min(total, 100)
            # [F1 FIX 2026-05-18] Populate breakdown telemetry on early return
            if out_components is not None:
                out_components["ob_signed"]  = int(_ob_signed)
                out_components["oi_signed"]  = int(oi_bull - oi_bear)
                out_components["liq_signed"] = int(liq_bull - liq_bear)
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
            # [F1 FIX 2026-05-18] Populate breakdown telemetry on early return
            if out_components is not None:
                out_components["ob_signed"]  = int(_ob_signed)
                out_components["oi_signed"]  = int(oi_bull - oi_bear)
                out_components["liq_signed"] = int(liq_bull - liq_bear)
            return min(bull_pts + bear_pts, 100), side, reasons

        # ── [AUDIT Phase 1] Momentum Candles REMOVED — redundant with EMA cross ──
        # Triple-counting direction (EMA + momentum + structure) inflated scores.
        # EMA cross retained as best noise-filtered trend indicator.
        bull_candles = 0  # kept for backward compat (consensus filter removed below)
        bear_candles = 0

        # ── EMA 8 vs EMA 21 ──────────────────────────────────────────
        # [FIX 2026-05-10] Dikurangi dari 15→10 pts karena redundan dengan
        # momentum candles dan structure — semuanya mengukur arah harga terbaru.
        def ema(data: list, period: int) -> float:
            k = 2 / (period + 1)
            e = data[0]
            for v in data[1:]:
                e = v * k + e * (1 - k)
            return e

        ema8  = ema(closes[-21:], 8)  if len(closes) >= 8  else closes[-1]
        ema21 = ema(closes[-21:], 21) if len(closes) >= 21 else closes[-1]

        if ema8 > ema21 * 1.0005:   # EMA8 clearly above EMA21
            bull_pts += 12; _c_ema = 12
            reasons.append(f"📈 EMA8 ({ema8:.4f}) > EMA21 ({ema21:.4f}) → bullish")
        elif ema8 < ema21 * 0.9995:
            bear_pts += 12; _c_ema = 12
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

            # ── RSI regime-gated weight ──────────────────────────────
            # [F5 FIX 2026-05-18] Audit: RSI overbought/divergence had r=-0.22 with PnL
            # in production (all NY session = trending). Root cause: mean-reversion RSI
            # logic fails in trending markets.
            # Solution: invert RSI level + divergence in TRENDING regime (|trend_pct|>=1.5%),
            # keep classic mean-reversion in RANGING regime.
            _is_trending = abs(trend_pct) >= 0.015

            if _is_trending:
                # TRENDING: RSI overbought = momentum continuation = BULL
                #           RSI oversold  = momentum continuation = BEAR
                if rsi > 70:
                    bull_pts += 10; _c_rsi = 10
                    reasons.append(f"📊 RSI overbought ({rsi:.1f}) + trending → momentum LONG +10")
                elif rsi > 60:
                    bull_pts += 5; _c_rsi = 5
                    reasons.append(f"📊 RSI high ({rsi:.1f}) + trending → momentum LONG +5")
                elif rsi < 30:
                    bear_pts += 10; _c_rsi = 10
                    reasons.append(f"📊 RSI oversold ({rsi:.1f}) + trending → momentum SHORT +10")
                elif rsi < 40:
                    bear_pts += 5; _c_rsi = 5
                    reasons.append(f"📊 RSI low ({rsi:.1f}) + trending → momentum SHORT +5")
                else:
                    reasons.append(f"📊 RSI neutral ({rsi:.1f}) trending")
            else:
                # RANGING: classic mean-reversion
                if rsi < 30:
                    bull_pts += 10; _c_rsi = 10
                    reasons.append(f"📊 RSI extreme oversold ({rsi:.1f}) → strong buy")
                elif rsi < 40:
                    bull_pts += 5; _c_rsi = 5
                    reasons.append(f"📊 RSI oversold ({rsi:.1f}) → buy signal")
                elif rsi > 70:
                    bear_pts += 10; _c_rsi = 10
                    reasons.append(f"📊 RSI extreme overbought ({rsi:.1f}) → strong sell")
                elif rsi > 60:
                    bear_pts += 5; _c_rsi = 5
                    reasons.append(f"📊 RSI overbought ({rsi:.1f}) → sell signal")
                else:
                    reasons.append(f"📊 RSI neutral ({rsi:.1f})")

            # ── RSI Divergence (regime-gated) ────────────────────────
            # In TRENDING: divergence is noise on 1m — skip entirely.
            # In RANGING:  divergence is valid mean-reversion signal.
            if len(closes) >= 20 and not _is_trending:
                def _rsi_at(cl, window=14):
                    g = [max(cl[i] - cl[i-1], 0) for i in range(1, len(cl))]
                    l = [max(cl[i-1] - cl[i], 0) for i in range(1, len(cl))]
                    ag = sum(g[-window:]) / window
                    al = sum(l[-window:]) / window
                    return (100 - 100 / (1 + ag / al)) if al > 0 else 100.0

                rsi_now = rsi
                rsi_5ago = _rsi_at(closes[:-5])
                price_now = closes[-1]
                price_5ago = closes[-6]

                if price_now > price_5ago * 1.002 and rsi_now < rsi_5ago - 3:
                    bear_pts += 5; _c_div = 5
                    reasons.append(f"📉 Bearish RSI divergence (price ↑ tapi RSI {rsi_5ago:.0f}→{rsi_now:.0f}) → SHORT")
                elif price_now < price_5ago * 0.998 and rsi_now > rsi_5ago + 3:
                    bull_pts += 5; _c_div = 5
                    reasons.append(f"📈 Bullish RSI divergence (price ↓ tapi RSI {rsi_5ago:.0f}→{rsi_now:.0f}) → LONG")

        # ── Bearish Rejection Wick Detection (SHORT signal) ──────────────
        if len(closes) >= 3 and len(opens) >= 3 and len(candles) >= 1:
            last_c = candles[-1] if isinstance(candles[-1], dict) else {}
            try:
                c_high = float(last_c.get("h", closes[-1]))
                c_open = opens[-1]
                c_close = closes[-1]
                body = abs(c_close - c_open)
                upper_wick = c_high - max(c_close, c_open)
                # [FIX 2026-05-10] Wick detection dikurangi 8→5 pts — single candle
                # pattern pada 1m terlalu noisy untuk bobot besar.
                if body > 0 and upper_wick > 1.5 * body and c_close < c_open:
                    bear_pts += 3; _c_wick = 3
                    reasons.append(f"🕯️ Rejection wick (wick {upper_wick/body:.1f}× body) → SHORT signal")
            except (TypeError, ValueError):
                pass

        # ── Short-term CVD (last 80 trades from cache) ────────────────
        recent_trades = self.cache.trades.get(asset, []) if hasattr(self.cache, 'trades') else []
        # [FIX 2026-05-10] CVD: threshold dinaikkan 0.20→0.25 dan poin dikurangi 12→8.
        # CVD dari 80 trades bisa bias oleh 1-2 large order, bukan indikator kuat.
        if len(recent_trades) >= 20:
            sample = recent_trades[-80:]
            buy_vol = sum(float(t.get('sz', 0)) for t in sample if t.get('side', '') in ('B', 'buy', 'Ask'))
            sell_vol = sum(float(t.get('sz', 0)) for t in sample if t.get('side', '') in ('S', 'sell', 'Bid'))
            cvd_total = buy_vol + sell_vol
            if cvd_total > 0:
                cvd_ratio = (buy_vol - sell_vol) / cvd_total
                if cvd_ratio > 0.25:
                    bull_pts += 10; _c_cvd = 10
                    reasons.append(f"💚 CVD bullish ({cvd_ratio*100:.0f}% net buy pressure)")
                elif cvd_ratio < -0.25:
                    bear_pts += 10; _c_cvd = 10
                    reasons.append(f"❤️ CVD bearish ({cvd_ratio*100:.0f}% net sell pressure)")

        # ── Volume Surge (2min vs 10min average) ──────────────────────
        # [FIX 2026-05-10] Threshold diturunkan 2.5→2.0x agar lebih sering berguna
        # sebagai konfirmasi. Max pts dikurangi 10→8.
        if len(volumes) >= 10:
            total_vol = sum(volumes[-10:])
            avg_10m = total_vol / 10
            avg_2m  = sum(volumes[-2:])  / 2
            if avg_10m > 0:
                surge = avg_2m / avg_10m
                if surge > 2.0:
                    # High volume surge — follow the direction
                    extra = min(int((surge - 2.0) * 4), 10) # [BOOST] Increase surge max to 10
                    _c_vol += extra
                    reasons.append(f"🔥 Volume surge {surge:.1f}x avg (+{extra} pts momentum)")
                    if bull_pts >= bear_pts:
                        bull_pts += extra
                    else:
                        bear_pts += extra
            
            # --- [ADAPTIVE BOOST] Baseline Volume Points (0-5 pts) ---
            # Coins with high absolute volume relative to market cap / average get a tiny boost
            # to favor liquid assets where scalping is safer.
            if total_vol > 500000: # Example $500k/min threshold
                bull_pts += 3
                bear_pts += 3
                _c_vol += 3
                reasons.append("💎 High liquidity asset (+3 baseline)")

        # ── [AUDIT Phase 1] HH/HL Structure REMOVED — redundant with EMA cross ──

        # ── Final tally & Direction ────────────────────────────────
        side = Side.LONG if bull_pts >= bear_pts else Side.SHORT
        raw = (bull_pts if side == Side.LONG else bear_pts)
        
        # ── MTF Confirmation (15m Trend Alignment) ──────────────────
        import config
        scfg = config.SCALPER
        if mtf_trend != "neutral":
            if (side == Side.LONG and mtf_trend == "bull") or (side == Side.SHORT and mtf_trend == "bear"):
                raw += scfg.mtf_score_bonus; _c_mtf = scfg.mtf_score_bonus
                reasons.append(f"📡 15m MTF Align ({mtf_trend}) → +{scfg.mtf_score_bonus}")
            else:
                raw += scfg.mtf_score_penalty; _c_mtf = scfg.mtf_score_penalty
                reasons.append(f"📡 15m MTF Discord ({mtf_trend}) → {scfg.mtf_score_penalty}")
                # Hard reject for scalper if 15m trend is strongly against us
                if abs(scfg.mtf_score_penalty) > 10 and raw < 60:
                    log.debug(f"[{asset}] SCALPER REJECT: Against 15m MTF trend")
                    return 0, side, reasons + ["REJECT: Counter-trend MTF"]

        # ── [LATE ENTRY FIX 2026-05-18] Price Displacement Filter ──────────
        # Problem: bot enters LONG after pump already happened, SHORT after dump.
        # Root cause: EMA cross + CVD + volume surge are all lagging indicators.
        # Fix: if price has already moved > threshold from the 5-candle-ago close,
        # the move is "stale" — penalize score to discourage chasing.
        #
        # Threshold: 0.8% displacement = roughly 1× ATR on 1m for most assets.
        # Above this, the entry is likely at exhaustion, not at the start.
        if len(closes) >= 6:
            price_5ago = closes[-6]
            if price_5ago > 0:
                displacement = (closes[-1] - price_5ago) / price_5ago
                DISP_THRESHOLD = 0.008  # 0.8% in 5 candles = stale move
                if side == Side.LONG and displacement > DISP_THRESHOLD:
                    penalty = min(int((displacement - DISP_THRESHOLD) * 1000), 20)
                    raw = max(0, raw - penalty)
                    reasons.append(f"⚠️ Late LONG: price already +{displacement*100:.2f}% in 5m (-{penalty} pts)")
                    log.info(f"[LATE-ENTRY] {asset} LONG displacement={displacement*100:.2f}% penalty={penalty}")
                elif side == Side.SHORT and displacement < -DISP_THRESHOLD:
                    penalty = min(int((abs(displacement) - DISP_THRESHOLD) * 1000), 20)
                    raw = max(0, raw - penalty)
                    reasons.append(f"⚠️ Late SHORT: price already {displacement*100:.2f}% in 5m (-{penalty} pts)")
                    log.info(f"[LATE-ENTRY] {asset} SHORT displacement={displacement*100:.2f}% penalty={penalty}")

        # ── [QUANT AGGRESSION] Mean-Reversion Guard REMOVED ───────────────
        # Previously capped score to 60 when RSI>60 + price>EMA21.
        # Data analysis shows: high scores aren't the problem, BAD EXITS are.
        # Score 65+ now gets runner-mode exit treatment (wider TP, longer hold)
        # instead of being artificially suppressed.
        _mr_capped = False  # kept for backward compat in logs

        score = min(raw, 100)

        # ── Per-coin SCORE-DEBUG log (info level for Railway visibility) ──
        log.info(
            f"[SCORE-DEBUG] {asset} | {side.value.upper()} score={score} (pre-adj) | "
            f"OB={_c_ob} EMA={_c_ema} RSI={_c_rsi} DIV={_c_div} WICK={_c_wick} "
            f"CVD={_c_cvd} VOL={_c_vol} FUND={_c_fund} LIQ={_c_liq} MTF={_c_mtf} | "
            f"bull={bull_pts} bear={bear_pts}"
        )

        # [F1 FIX 2026-05-18] Populate breakdown telemetry for downstream persistence
        if out_components is not None:
            out_components["ob_signed"]  = int(_ob_signed)
            out_components["oi_signed"]  = int(oi_bull - oi_bear)
            out_components["liq_signed"] = int(liq_bull - liq_bear)

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
        trend_pct: float = 0.0,
        atr_pct: float = 0.0,
        fade_mode: bool = False,
        late_trend: bool = False,
        # [F1 FIX 2026-05-18] Per-analyzer signed contributions for breakdown telemetry
        oi_signed: int = 0,
        liq_signed: int = 0,
        ob_signed: int = 0,
    ) -> TradeSignal:
        """Build a TradeSignal with scalper-specific dynamic TP/SL levels."""
        from models.schemas import SignalStrength, MarketRegime, ScoreBreakdown

        import config
        scfg = config.SCALPER

        # ── SL Computation: ATR-adaptive with vol-based fallback ─────────────
        # [AUDIT FIX 2026] Use real ATR(14) from 1m candles when available.
        # Fallback: vol-based proxy (realized_vol / sqrt(24)) — same as before.
        if getattr(scfg, 'atr_sl_enabled', True) and atr_pct > 0 and atr_pct >= 0.001:
            mult = getattr(scfg, 'atr_sl_multiplier', 1.5)
            sl_min = getattr(scfg, 'sl_pct_min', 0.006)
            sl_max = getattr(scfg, 'sl_pct_max', 0.020)
            raw_sl = atr_pct * mult
            sl_pct = max(min(raw_sl, sl_max), sl_min)
            log.info(
                f"[ATR-SL] {asset} | atr_pct={atr_pct*100:.4f}% | mult={mult} | "
                f"raw_sl={raw_sl*100:.4f}% | clamped_sl={sl_pct*100:.4f}% | "
                f"min={sl_min*100:.4f}% | max={sl_max*100:.4f}% | fallback=False"
            )
        else:
            # Fallback: vol-based proxy (original logic)
            _fb_reason = "atr_disabled" if not getattr(scfg, 'atr_sl_enabled', True) else f"atr_too_low={atr_pct*100:.4f}%"
            if regime in (MarketRegime.HIGH_VOL, MarketRegime.EXTREME):
                SL_FLOOR = max(scfg.sl_pct, 0.0150)   # min 1.5% di high/extreme vol
            else:
                SL_FLOOR = scfg.sl_pct

            SL_CEILING = getattr(scfg, 'sl_pct_max', 0.0200)
            ATR_MULT   = getattr(scfg, 'atr_sl_multiplier', 1.5)

            if realized_vol > 0:
                atr14_pct = realized_vol / (24 ** 0.5)
                sl_pct = max(SL_FLOOR, min(atr14_pct * ATR_MULT, SL_CEILING))
            else:
                sl_pct = SL_FLOOR
            log.warning(
                f"[ATR-SL] {asset} | FALLBACK to fixed sl_pct={sl_pct*100:.4f}% | reason={_fb_reason}"
            )

        # TP pakai nilai fixed dari config — level realistis untuk hold time 20 menit.
        tp1_pct = scfg.tp1_pct
        tp2_pct = scfg.tp2_pct

        # ── RR Enforcement: TP1/TP2 must be at least N× SL distance, BUT capped ──
        # [AUDIT FIX 2026 PHASE 3] Original RR enforcement could push TP2 to 3.0%
        # when SL=2.0% (×1.5 RR), well above the 20-min hold window's reachable target.
        # Cap TP2 absolute at 2.0% (TP1 at 1.2%) so partial-close ladder stays
        # achievable within max_hold_minutes; widen TP only when configured TP is
        # actually below the RR floor.
        tp1_min_rr = getattr(scfg, 'tp1_min_rr_to_sl', 0.6)
        tp2_min_rr = getattr(scfg, 'tp2_min_rr_to_sl', 1.5)
        TP1_ABS_CAP = 0.008   # [F4 FIX 2026-05-18] 1.2%→0.8%: keep TP1 reachable, trailing activates sooner
        TP2_ABS_CAP = 0.020   # 2.0% — keep TP2 reachable in 20m hold
        tp1_floor = min(sl_pct * tp1_min_rr, TP1_ABS_CAP)
        tp2_floor = min(sl_pct * tp2_min_rr, TP2_ABS_CAP)
        tp1_pct = max(tp1_pct, tp1_floor)
        tp2_pct = max(tp2_pct, tp2_floor)
        log.debug(
            f"[RR-CAP] {asset} sl={sl_pct*100:.2f}% → tp1_floor={tp1_floor*100:.2f}% "
            f"tp2_floor={tp2_floor*100:.2f}% (final tp1={tp1_pct*100:.2f}% tp2={tp2_pct*100:.2f}%)"
        )

        # ── [QUANT AGGRESSION] Score-driven exit matrix ─────────────────────
        # High score = runner, low score = quick scalp. time_exit is VARIABLE.
        # sl_pct_max dari ScalperConfig SELALU di-enforce — tidak boleh dilewati.
        _sl_max = getattr(scfg, 'sl_pct_max', 0.015)
        time_exit_min = 20  # default
        if score >= 66 or (fade_mode and score >= 60):
            # HIGH CONVICTION → TP multiplier dikecilkan agar realistis dalam 25m
            sl_pct = max(sl_pct, 0.010)
            sl_pct = min(sl_pct, _sl_max)
            tp1_pct = max(tp1_pct, sl_pct * 1.0)   # was 1.5×
            tp2_pct = max(tp2_pct, sl_pct * 1.8)   # was 2.5×
            time_exit_min = 25
            reasons.append(f"🏃 RUNNER mode: score={score}, SL={sl_pct*100:.2f}%, TP2={tp2_pct*100:.2f}%, hold={time_exit_min}m")
        elif score >= 61:
            # MEDIUM-HIGH
            sl_pct = max(sl_pct, 0.008)
            sl_pct = min(sl_pct, _sl_max)
            tp1_pct = max(tp1_pct, sl_pct * 0.8)   # was 1.2×
            tp2_pct = max(tp2_pct, sl_pct * 1.5)   # was 2.0×
            time_exit_min = 20
        elif score >= 56:
            # SWEET SPOT → TP lebih dekat agar tercapai dalam 15m
            sl_pct = max(sl_pct, 0.007)
            sl_pct = min(sl_pct, _sl_max)
            tp1_pct = max(tp1_pct, sl_pct * 0.7)   # was 1.0×
            tp2_pct = max(tp2_pct, sl_pct * 1.2)   # was 1.5×
            time_exit_min = 15
        else:
            # LOW CONVICTION → sangat ketat
            sl_pct = max(sl_pct, 0.006)
            sl_pct = min(sl_pct, _sl_max)
            tp1_pct = max(tp1_pct, sl_pct * 0.5)   # was 0.8×
            tp2_pct = max(tp2_pct, sl_pct * 1.0)   # was 1.2×
            time_exit_min = 10
            reasons.append(f"⚡ QUICK SCALP: score={score}, SL={sl_pct*100:.2f}%, time_exit={time_exit_min}m")

        # Late trend chase override: turunkan time_exit tapi naikkan TP
        if late_trend:
            time_exit_min = max(time_exit_min - 5, 8)
            tp2_pct = tp2_pct * 1.3
            reasons.append("Late-trend: -5min time, +30% TP2")

        # Fade mode override: contrarian entry = tighter SL, wider TP (asymmetric RR)
        if fade_mode:
            sl_pct = sl_pct * 0.8
            tp2_pct = tp2_pct * 1.5
            time_exit_min = min(time_exit_min, 12)
            reasons.append("FADE: tight SL 0.8×, wide TP 1.5×, max 12min")

        # Leverage hanya dari config — HL exchange cap dihapus.
        # User cap diterapkan di executor (paper/bitget), bukan di signal.
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
            # [F1 FIX 2026-05-18] Persist signed analyzer contributions
            # so per-component edge can be audited via DB.
            oi_funding_score=oi_signed,
            liquidation_score=liq_signed,
            orderbook_score=ob_signed,
            raw_score=score,
            final_score=score,
            session_bonus=session_bonus,
            reasons=reasons
        )

        # [AUDIT FIX 2026 PHASE 3] Propagate realized_vol + entry_atr to signal
        # so RiskManager.calculate_position_size can apply vol-scaling and
        # downstream consumers (trailing stop, dashboards) see real values.
        # Without this, signal.realized_vol falls back to default 0.02 = baseline,
        # making vol-adjusted sizing a no-op.
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
            realized_vol=realized_vol if realized_vol > 0 else 0.02,
            entry_atr=atr_pct if atr_pct > 0 else 0.0,
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

        # Step 4: Session context (FIX #5: threshold adjuster, not score inflator)
        session_bonus, session_reasons, session_threshold_delta = self._get_session_bonus()

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
            # FIX: fr=0.000000 is neutral — fine for SHORT.
            # Only block when funding is strongly negative (shorts paying longs heavily).
            fr = getattr(funding, 'funding_rate', 0.0)
            min_fr = getattr(config.SIGNAL, 'short_min_funding_rate', -0.0001)
            if fr < min_fr:
                log.debug(f"[{asset}] SHORT BLOCKED: Funding {fr:.6f} < {min_fr} (terlalu bearish/short-biased)")
                return None, 0

            # Solusi 3: Anti-Trend Filter
            # Jangan SHORT jika market sedang uptrend kuat (> +2% dalam 24h).
            max_up = getattr(config.SIGNAL, 'short_max_uptrend_pct', 0.02)
            if trend_pct > max_up:
                log.debug(f"[{asset}] SHORT BLOCKED: 24h uptrend {trend_pct*100:.1f}% > {max_up*100:.0f}% (jangan lawan trend)")
                return None, 0

            # [FIX 2026-05-14] Minimum technical gate: OI_bear + Liq_bear >= threshold
            # Blok SHORT jika 3 komponen teknikal utama terlalu lemah (pure session score).
            _tech_bear = (oi_bear or 0) + (liq_bear or 0) + (ob_bear or 0)
            _min_tech = getattr(config.SIGNAL, 'min_technical_score_short', 10)
            if _tech_bear < _min_tech:
                log.debug(
                    f"[{asset}] SHORT BLOCKED: technical score {_tech_bear:.1f} < {_min_tech} "
                    f"(OI_bear={oi_bear:.1f} Liq_bear={liq_bear:.1f} OB_bear={ob_bear:.1f})"
                )
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

        # Session bonus applied later with diminishing returns (line ~1546)
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

        # Session bonus dengan diminishing returns di score tinggi.
        # Skor sudah tinggi (≥72) berarti sinyal genuinely kuat — session bonus
        # tidak perlu mendorong lebih jauh dan justru menciptakan false 80+.
        # Taper: <62 → full bonus, 62-71 → 60%, ≥72 → 30%
        if session_bonus > 0:
            if final_score >= 72:
                effective_session = int(session_bonus * 0.30)
            elif final_score >= 62:
                effective_session = int(session_bonus * 0.60)
            else:
                effective_session = session_bonus
        else:
            effective_session = session_bonus  # penalty tetap penuh
        final_score += effective_session
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
            oi_funding_score=oi_bull + oi_bear,
            liquidation_score=liq_bull + liq_bear,
            orderbook_score=ob_bull + ob_bear,
            session_bonus=session_threshold_delta,
            regime_multiplier=final_multiplier,
            total_bull=int(total_bull),
            total_bear=int(total_bear),
            raw_score=int(raw_score),
            final_score=int(final_score),
            reasons=all_reasons,
            warnings=all_warnings,
        )

        # ── Format Combat Report ────────────────────────────────────────
        breakdown_str = (
            f"(OI:{oi_bull+oi_bear:+} Liq:{liq_bull+liq_bear:+} "
            f"OB:{ob_bull+ob_bear:+} SesThresh:{session_threshold_delta:+})"
        )

        # ── UNIFORM SCORING LOG ──
        if final_score >= 20:
            bias_emoji = "🟢 LONG " if total_bull > total_bear else "🔴 SHORT"
            log.info(
                f"🎯 [SCORE] {asset:6} | {bias_emoji} | {final_score:2d}/100 | "
                f"Pts: {total_bull:.1f} vs {total_bear:.1f} | "
                f"OI:{oi_bull:.1f}/{oi_bear:.1f} Liq:{liq_bull:.1f}/{liq_bear:.1f} OB:{ob_bull:.1f}/{ob_bear:.1f} | "
                f"SesThresh:{session_threshold_delta:+d} Mult:{final_multiplier:.2f}x ({vol_regime.value})"
            )

        breakdown.final_score = final_score

        # ── FIX #9: OI-tier threshold adjustment ──────────────────────
        # Large-cap assets (BTC/ETH) are more efficient — need stronger signal.
        # Small-cap assets are more explosive — can enter on weaker signal.
        # This replaces the old magnitude_bonus (+2..+8) that inflated scores.
        oi_usd = oi.open_interest
        if oi_usd > 1_000_000_000:      # > $1B  (BTC, ETH)
            oi_threshold_delta = 3
        elif oi_usd > 200_000_000:      # > $200M (SOL, HYPE)
            oi_threshold_delta = 1
        elif oi_usd > 50_000_000:       # > $50M
            oi_threshold_delta = 0
        elif oi_usd > 10_000_000:       # > $10M
            oi_threshold_delta = -2
        else:                           # micro-cap
            oi_threshold_delta = -3

        # ── Check threshold ────────────────────────────────────────────
        # SHORT needs higher threshold (audit: 57.6% WR, net -$12.55).
        # session_threshold_delta adjusts by session liquidity (FIX #5).
        # oi_threshold_delta adjusts by market efficiency (FIX #9).
        if side == Side.SHORT:
            base_threshold = config.SIGNAL.min_score_short_signal
        else:
            base_threshold = 30  # LONG: internal capture threshold
        threshold = base_threshold + session_threshold_delta + oi_threshold_delta
        if final_score < threshold:
            log.debug(
                f"[{asset}] {side.value.upper()} score {final_score} < threshold {threshold} "
                f"(base={base_threshold} sess={session_threshold_delta:+d} oi={oi_threshold_delta:+d}), skip"
            )
            return None, final_score

        # ── R:R Quality Gate ───────────────────────────────────────────
        sl_pct_check, tp1_pct_check, tp2_pct_check = self.risk_mgr.calculate_tp_levels(
            asset, mark_price, side, realized_vol
        )
        rr_ratio = tp2_pct_check / max(sl_pct_check, 0.001)
        if rr_ratio < 1.5:
            log.info(
                f"[{asset}] R:R GATE: {tp2_pct_check*100:.2f}%/{sl_pct_check*100:.2f}% "
                f"= {rr_ratio:.2f}x < 1.5x minimum. Signal rejected."
            )
            return None, final_score

        # ── Build signal ───────────────────────────────────────────────
        signal = self._build_signal(
            asset, side, final_score, log_regime, breakdown, mark_price,
            realized_vol=realized_vol,
            oi_usd=oi_usd,
            funding_rate=funding.funding_rate,
            trend_pct=trend_pct,
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
    # 4H REGIME DETECTION
    # ──────────────────────────────────────────

    async def _fetch_4h_regime(self, asset: str) -> str:
        """
        Classify 4h market regime for an asset.
        Returns: "TRENDING_UP" | "TRENDING_DOWN" | "CHOPPY"
        Cached 4 hours per asset.

        Logic:
          - Fetch last 20 × 4h candles (~3.3 days)
          - EMA10 vs EMA20 on 4h closes → direction
          - ADX proxy (avg true range vs avg body) → trend strength
          - TRENDING_UP:   EMA10 > EMA20 and trend strong
          - TRENDING_DOWN: EMA10 < EMA20 and trend strong
          - CHOPPY:        EMAs close or trend weak
        """
        cache_key = f"4h_{asset}"
        cached = self._vol_cache.get(cache_key)
        if cached and (time.monotonic() - cached[0]) < 14400:  # 4h cache
            return cached[1]

        try:
            async with self.candle_sem:
                await asyncio.sleep(0.2)
                now_ms   = int(time.time() * 1000)
                start_ms = now_ms - (20 * 4 * 3600 * 1000)  # 20 × 4h candles
                resp, succ = await self.client._call_info_endpoint(
                    "candleSnapshot",
                    {"req": {"coin": asset, "interval": "4h",
                             "startTime": start_ms, "endTime": now_ms}}
                )

            if not succ or not isinstance(resp, list) or len(resp) < 8:
                self._vol_cache[cache_key] = (time.monotonic(), "CHOPPY")
                return "CHOPPY"

            closes = []
            highs  = []
            lows   = []
            for c in resp:
                if isinstance(c, dict):
                    try:
                        closes.append(float(c["c"]))
                        highs.append(float(c["h"]))
                        lows.append(float(c["l"]))
                    except (ValueError, TypeError):
                        pass

            if len(closes) < 8:
                self._vol_cache[cache_key] = (time.monotonic(), "CHOPPY")
                return "CHOPPY"

            # EMA helper
            def _ema(data, p):
                k = 2 / (p + 1)
                e = data[0]
                for v in data[1:]:
                    e = v * k + e * (1 - k)
                return e

            ema10 = _ema(closes[-10:], 10) if len(closes) >= 10 else closes[-1]
            ema20 = _ema(closes[-20:], 20) if len(closes) >= 20 else closes[-1]

            # Trend strength: ratio of directional move vs total range
            # High ratio = trending, low ratio = choppy
            n = min(len(closes), 10)
            total_range = sum(highs[-n:][i] - lows[-n:][i] for i in range(n))
            net_move    = abs(closes[-1] - closes[-n])
            strength    = net_move / total_range if total_range > 0 else 0

            STRENGTH_THRESHOLD = 0.30  # net move must be >30% of total range

            if ema10 > ema20 * 1.002 and strength >= STRENGTH_THRESHOLD:
                regime = "TRENDING_UP"
            elif ema10 < ema20 * 0.998 and strength >= STRENGTH_THRESHOLD:
                regime = "TRENDING_DOWN"
            else:
                regime = "CHOPPY"

            self._vol_cache[cache_key] = (time.monotonic(), regime)
            log.info(f"[4H-REGIME] {asset}: {regime} | EMA10={ema10:.4f} EMA20={ema20:.4f} strength={strength:.2f}")
            return regime

        except Exception as e:
            log.debug(f"[4H-REGIME] {asset}: fetch failed ({e}), defaulting CHOPPY")
            self._vol_cache[cache_key] = (time.monotonic(), "CHOPPY")
            return "CHOPPY"

    # ──────────────────────────────────────────
    # SESSION BIAS
    # ──────────────────────────────────────────

    def _get_session_bonus(self):
        """Session adjusts entry threshold (not score). Returns (bonus, reasons, threshold_delta)."""
        hour = datetime.now(timezone.utc).hour
        ny_start = SIGNAL.ny_session_start_utc
        ny_end   = SIGNAL.ny_session_end_utc
        lon_start= SIGNAL.london_start_utc
        lon_end  = SIGNAL.london_end_utc

        bonus = 0
        threshold_delta = 0
        reasons = []

        is_ny  = ny_start  <= hour < ny_end
        is_lon = lon_start <= hour < lon_end

        if is_ny:
            ny_bonus = getattr(SIGNAL, 'ny_session_bonus', 10)
            bonus += ny_bonus
            reasons.append(f"🗽 NY session (+{ny_bonus} pts)")

        if is_lon:
            lon_bonus = getattr(SIGNAL, 'london_session_bonus', 4)
            bonus += lon_bonus
            reasons.append(f"🇬🇧 London session (+{lon_bonus} pts)")

        if not is_ny and not is_lon:
            if hour >= 22 or hour < 7:
                asia_pen = getattr(SIGNAL, 'asia_session_penalty', -10)
                threshold_delta += abs(asia_pen)  # raise threshold (need stronger signal)
                reasons.append(f"🌏 Asia session (threshold +{abs(asia_pen)}, need stronger signal)")
            else:
                reasons.append("⏰ Off-session neutral")

        return bonus, reasons, threshold_delta

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

        # Leverage hanya dari config — HL exchange cap dihapus.
        leverage = min(RISK.default_leverage, RISK.max_leverage)
        if score >= 75:
            leverage = min(RISK.default_leverage + 2, RISK.max_leverage)

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

