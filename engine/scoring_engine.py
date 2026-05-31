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

        # Bybit Long/Short Ratio cache: {symbol: ratio}, refreshed every 60s
        self._ls_ratio_cache: Dict[str, float] = {}
        self._ls_ratio_cache_time: float = 0

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
                start_ms = now_ms - 40 * 60 * 1000  # last 40 minutes (need 34+ for EMA34)
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

        # [FIX 2026-05-21] If funding_rate is 0 (Bybit down + HL API miss),
        # fall back to WS funding_history cache which is always populated from subscription.
        if funding.funding_rate == 0 and hasattr(self.cache, 'funding_history'):
            ws_history = self.cache.funding_history.get(asset, [])
            if ws_history:
                funding = FundingData(
                    asset=asset,
                    funding_rate=ws_history[-1],
                    premium=0,
                    predicted_rate=0,
                    hourly_trend=ws_history[-8:]
                )

        # Spot price for basis calculation (same as standard mode)
        now_mono = time.monotonic()
        if not self._spot_prices or (now_mono - self._spot_cache_time) > 10:
            self._spot_prices = await self.client.get_all_mids()
            self._spot_cache_time = now_mono

        # [AUDIT FIX 2026-05-20] Bybit Long/Short Ratio — contrarian crowd signal
        _ls_ratio = None
        if (now_mono - self._ls_ratio_cache_time) > 60:
            try:
                if not hasattr(self, '_bybit_ls_client'):
                    from data.bybit_client import BybitClient
                    import config as _cfg
                    self._bybit_ls_client = BybitClient(
                        api_key=_cfg.BYBIT_API_KEY, api_secret=_cfg.BYBIT_SECRET_KEY,
                        testnet=_cfg.BYBIT_TESTNET,
                    )
                # Fetch top 20 assets only (rate limit friendly)
                top_syms = [f"{a}USDT" for a in list(self._price_history.keys())[:20]]
                if top_syms:
                    self._ls_ratio_cache = await self._bybit_ls_client.get_long_short_ratios_batch(top_syms)
                    self._ls_ratio_cache_time = now_mono
            except Exception as _ls_err:
                log.debug(f"[LS-RATIO] Fetch failed: {_ls_err}")
        _ls_ratio = self._ls_ratio_cache.get(f"{asset}USDT")

        spot_price = self._spot_prices.get(f"@{asset}") or getattr(oi, 'oracle_price', 0) or mark_price

        # Price history + OI snapshot tracking (same as standard mode)
        self._update_price_history(asset, mark_price)
        price_change_1h = self._get_price_change(asset, minutes=60)
        price_change_5m = self._get_price_change(asset, minutes=5)  # [AUDIT #17] 5m momentum for OI confirmation (scalper-aligned)
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
            asset, funding, oi, funding_history, price_change_1h, mark_price, spot_price,
            price_change_5m=price_change_5m,
        )
        recent_liqs = self.cache.liquidations if hasattr(self.cache, 'liquidations') else []
        oi_usd = oi.open_interest * mark_price
        
        # [AUDIT #11] Liq Cluster — uses real Binance+HL events.
        # [AUDIT #12 FIX] OI proxy fallback DISABLED. Root cause:
        # 1. Binance forceOrder stream connects but sends 0 events (geo-blocked data)
        # 2. _calc_liq_cluster ALWAYS returns 0 (no real events ever arrive)
        # 3. OI proxy fallback fires on EVERY signal with funding-based direction
        # 4. Direction often CONTRADICTS trade direction (e.g. SHORT + bullish liq tilt)
        # 5. Result: 8 trades with liq, 7 LOSS, WR 12.5%, PnL -$7.37
        # 6. 100% of liq-tagged trades had INTERNAL CONTRADICTION (liq vs trade direction)
        #
        # Fix: Only use real liq cluster events. If none → liq_bull = liq_bear = 0.
        # OI proxy was a theoretical model that ACTIVELY HARMED performance.
        _liq_cluster_bull, _liq_cluster_bear, _liq_cluster_reason = self._calc_liq_cluster(asset)
        if _liq_cluster_bull > 0 or _liq_cluster_bear > 0:
            liq_bull, liq_bear = _liq_cluster_bull, _liq_cluster_bear
            liq_reasons = [_liq_cluster_reason]
            liq_warns = []
            liq_map = None
        else:
            # No real liq events → zero contribution. Don't guess with OI proxy.
            liq_bull, liq_bear = 0, 0
            liq_reasons = []
            liq_warns = []
            liq_map = None

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

        # 4a2. [1H REGIME FILTER — AUDIT #14 REDESIGN] Fetch 1h market regime.
        # Was 4H but always returned CHOPPY (98%). Now 1H with lower thresholds.
        # Aligns 1m scalp direction with higher-timeframe trend.
        # TRENDING_UP:   only LONG allowed at normal threshold; SHORT needs +8 extra score
        # TRENDING_DOWN: only SHORT allowed at normal threshold; LONG needs +8 extra score
        # CHOPPY:        both directions allowed but threshold raised +3 (lower edge)
        htf_regime = await self._fetch_1h_regime(asset)

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
            ls_ratio=_ls_ratio,
            htf_regime=htf_regime,
        )

        # 4b2. Apply 1H regime adjustment to score and leverage
        htf_threshold_adj = 0
        htf_leverage_adj  = 0
        if htf_regime == "TRENDING_UP":
            if side == Side.LONG:
                htf_threshold_adj = -3   # easier entry — aligned with 1h trend
                htf_leverage_adj  = +2   # slightly more conviction
                reasons.append(f"📈 1H TRENDING_UP — LONG aligned (+lev, -threshold)")
            else:  # SHORT against 1h trend
                htf_threshold_adj = +8   # need much stronger signal to fade 1h trend
                htf_leverage_adj  = -3
                reasons.append(f"⚠️ 1H TRENDING_UP — SHORT counter-trend (+8 threshold)")
        elif htf_regime == "TRENDING_DOWN":
            if side == Side.SHORT:
                htf_threshold_adj = -3
                htf_leverage_adj  = +2
                reasons.append(f"📉 1H TRENDING_DOWN — SHORT aligned (+lev, -threshold)")
            else:  # LONG against 1h trend
                htf_threshold_adj = +8
                htf_leverage_adj  = -3
                reasons.append(f"⚠️ 1H TRENDING_DOWN — LONG counter-trend (+8 threshold)")
        else:  # CHOPPY
            # [AUDIT #14] With 1H regime, CHOPPY now means genuinely no direction.
            # Keep +3 threshold — but now it only fires ~40-60% of time (was 98%).
            htf_threshold_adj = +3
            htf_leverage_adj  = -2
            reasons.append(f"〰️ 1H CHOPPY — threshold +3, leverage reduced")

        # 4c. [OPPORTUNITY SCORING] Regime multiplier — ranging = opportunity, trending = stale
        # Data audit: trending ×1.2 caused score inflation at exhaustion points.
        # New: trending = penalty (move already happened), ranging = neutral/slight boost.
        late_trend = False
        if vol_regime in (MarketRegime.HIGH_VOL, MarketRegime.EXTREME):
            _regime_cat = "volatile"
            _regime_mult = 0.90
        elif abs(trend_pct) >= 0.070:
            # [FIX 2026-05-25] Was 3% — too aggressive for crypto (3%/24h is NORMAL).
            # 7%/24h = actual parabolic move where exhaustion is real.
            _regime_cat = "late_trend"
            _regime_mult = 0.85  # [FIX] was 0.70 — 30% penalty killed ALL signals
            late_trend = True
            reasons.append(f"⚠️ Late trend {trend_pct*100:.2f}%/24h — score penalized (×0.85)")
        elif abs(trend_pct) >= 0.035:
            # [FIX 2026-05-25] Was 1.5% — 1.5%/24h is noise, not trend.
            _regime_cat = "trending"
            _regime_mult = 0.92  # [FIX] was 0.85
        else:
            _regime_cat = "ranging"
            _regime_mult = 1.0   # neutral — fresh move potential
        score_pre = score
        score = int(score * _regime_mult)
        score = max(0, min(score, 100))
        reasons.append(f"🌐 Regime: {_regime_cat} (×{_regime_mult}, {score_pre}→{score})")
        from dashboard.reasoning_logger import reasoning_logger
        reasoning_logger.log_regime_adjustment(
            asset,
            regime=_regime_cat,
            multiplier=_regime_mult,
            score_before=score_pre,
            score_after=score,
            htf_regime=htf_regime,
            htf_threshold_adj=htf_threshold_adj,
        )

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

        # ── SCORE-DEBUG final (post-regime, post-session) ─────────────
        log.info(
            f"[SCORE-DEBUG] {asset} | {side.value.upper()} score={score} | "
            f"regime={_regime_cat}(×{_regime_mult}) session_add={session_score_add}"
        )

        # ── [P0-2 FIX 2026-05-18] FUNDING RATE HARD GATE ──────────────
        # Data audit: funding_extreme adalah satu-satunya fitur dengan r>0 terhadap PnL.
        # Kalau funding crowded di sisi yang sama = market over-leveraged = mean reversion imminent.
        # HARD BLOCK kalau sangat crowded, half-size kalau moderate.
        _fr = funding.funding_rate if funding else 0.0
        _fr_crowded_hard = 0.0005   # 0.05%/8h — sangat crowded (was 0.03%, terlalu ketat)
        _fr_crowded_soft = 0.0001   # 0.01%/8h — moderate crowded
        if side == Side.LONG and _fr > _fr_crowded_hard:
            log.info(
                f"[P0-2 BLOCK] {asset} LONG: funding {_fr*100:.4f}% > {_fr_crowded_hard*100:.4f}% "
                f"(market over-leveraged long, mean reversion risk)"
            )
            self.skip_counters["other"] = self.skip_counters.get("other", 0) + 1
            return None, score
        elif side == Side.SHORT and _fr < -_fr_crowded_hard:
            log.info(
                f"[P0-2 BLOCK] {asset} SHORT: funding {_fr*100:.4f}% < -{_fr_crowded_hard*100:.4f}% "
                f"(market over-leveraged short, mean reversion risk)"
            )
            self.skip_counters["other"] = self.skip_counters.get("other", 0) + 1
            return None, score

        # ── [P0-3 FIX 2026-05-18] LIQUIDATION CASCADE ENTRY TRIGGER ───
        # ── [P0-3] LIQUIDATION CASCADE ENTRY TRIGGER ───
        # [AUDIT FIX 2026-05-20] Liq gate disabled — liquidation data is always 0
        # (WS liq events are extremely rare). This gate was blocking ALL SHORT signals.
        # Re-enable only when liq data source is fixed.
        if False:  # DISABLED
            # Score sangat rendah — butuh liquidation catalyst ATAU strong technical
            _target_liq_side = "short" if side == Side.LONG else "long"
            _now_ts_liq = time.time()
            _opposing_liqs = [
                e for e in recent_liqs
                if e.get("coin", e.get("asset", "")) == asset
                and e.get("side", "") in (
                    (_target_liq_side, "buy") if _target_liq_side == "long" else (_target_liq_side, "sell")
                )
            ]
            _filtered = []
            for e in _opposing_liqs:
                _evt_time = float(e.get("time", 0) or 0) / 1000
                if _evt_time > 0 and _evt_time > _now_ts_liq - 300:
                    _filtered.append(e)
                elif _evt_time == 0:
                    _filtered.append(e)
            _liq_notional = sum(
                float(e.get("sz", e.get("size", 0))) * float(e.get("px", e.get("price", 0)))
                for e in _filtered
            )
            # Block hanya kalau TIDAK ada liq trigger DAN score rendah
            if _liq_notional < 10_000:
                if not (hasattr(liq_map, 'cascade_risk') and liq_map.cascade_risk > 0.3):
                    log.info(
                        f"[P0-3 SKIP] {asset} {side.value.upper()} score={score}: "
                        f"no liq trigger (opposing=${_liq_notional:.0f}<$10k, "
                        f"cascade={getattr(liq_map, 'cascade_risk', 0):.2f})"
                    )
                    self.skip_counters["other"] = self.skip_counters.get("other", 0) + 1
                    return None, score

        # ── [LEARNING ENGINE] Evaluate pattern memory + ML model ──────────
        from engine.learning_engine import learning_engine
        _learn_regime = vol_regime.value  # [AUDIT #14 FIX] was _regime_cat — must match signal.regime.value used in record_outcome
        _learn_decision = learning_engine.evaluate(
            asset=asset,
            side=side.value.lower(),
            regime=_learn_regime,
            score=score,
            features={
                'oi_funding_score': _scalper_components.get("oi_signed", 0),
                'orderbook_score': _scalper_components.get("ob_signed", 0),
                'liquidation_score': _scalper_components.get("liq_signed", 0),
                'displacement_5m': trend_pct,
                'rsi': 50,  # placeholder — actual RSI computed inside _calculate_scalper_score
                'ema_freshness': 5,
                'atr_pct': realized_vol or 0.01,
                'regime_code': {'ranging': 0, 'trending': 1, 'late_trend': 2, 'volatile': 3}.get(_regime_cat, 0),
                'hour_utc': datetime.now(timezone.utc).hour,
                'score': score,
            }
        )
        if _learn_decision.score_adj != 0 or _learn_decision.flip_side:
            if _learn_decision.flip_side:
                side = Side.SHORT if side == Side.LONG else Side.LONG
                log.info(f"[LEARN] {asset}: FLIP to {side.value.upper()} | {_learn_decision.reason}")
            score += _learn_decision.score_adj
            score = max(0, min(100, score))
            if _learn_decision.reason:
                reasons.append(_learn_decision.reason)

        # ── [REASONING LOGGER] Emit decision trace for admin dashboard ──
        from dashboard.reasoning_logger import reasoning_logger
        _trace = reasoning_logger.start_trace(asset)
        reasoning_logger.log_signal(asset, score, side.value, _scalper_components)
        if _learn_decision.score_adj != 0 or _learn_decision.flip_side:
            reasoning_logger.log_learning(asset, {
                "score_adj": _learn_decision.score_adj,
                "flip_side": _learn_decision.flip_side,
                "size_mult": _learn_decision.size_mult,
                "reason": _learn_decision.reason,
            })

        # ── [AI INTELLIGENCE] MOVED to post-filter (Audit #14) ──
        # AI now evaluates EVERY signal that passes all filters.
        # Old position: before threshold → only 3% signals got AI.
        # New position: after all filters → AI evaluates every trade.
        _ai_score_adj = 0
        _ai_verdict = None

        # Effective threshold: overlap = market efisien, butuh sinyal LEBIH kuat
        # [P0-1 FIX 2026-05-18] Data: overlap WR=6.1% karena threshold terlalu rendah.
        # Sekarang: overlap NAIKKAN threshold (bukan turunkan).
        hour = datetime.now(timezone.utc).hour
        is_ny_lon_overlap = (
            config.SIGNAL.ny_session_start_utc <= hour < config.SIGNAL.london_end_utc
            and config.SIGNAL.london_start_utc <= hour < config.SIGNAL.ny_session_end_utc
        )
        if is_ny_lon_overlap:
            overlap_threshold_adj = 5  # overlap = efisien, butuh sinyal kuat
        elif config.SIGNAL.ny_session_start_utc <= hour < config.SIGNAL.ny_session_end_utc:
            overlap_threshold_adj = 2  # NY only
        else:
            overlap_threshold_adj = 0

        # [AUDIT #6 FIX 2026-05-22] EXTREME regime: don't block, raise threshold.
        # [FIX 2026-05-25] Reduced from +15 to +5. Score already gets ×0.9 penalty
        # for volatile — double penalty (×0.9 score AND +15 threshold) made it impossible.
        # Data: EXTREME score<60 = WR 33.3% (-$4.83), score 71+ = WR 44.4% (+$0.91).
        # Raise threshold +15 so only high-conviction signals pass in extreme vol.
        _vol_threshold_adj = 5 if vol_regime == MarketRegime.EXTREME else 0

        # [AUDIT #7] Vote margin gate: low consensus = raise threshold
        # Data: margin<4 WR 50% PnL -$1.70, margin 8+ WR 66.7% PnL +$11.87
        # [FIX 2026-05-25] Reduced from +5 to +3. Combined with regime penalty, +5 was too harsh.
        _vote_margin = _scalper_components.get("vote_margin", 99)
        _vote_margin_adj = 3 if _vote_margin < 4 else 0

        # [AUDIT #17] HARD VOTE-MARGIN GATE — system undecided = DON'T TRADE.
        # Root cause (AI flagged via LIT LONG, sig 4C9130D8): direction voter tied 4-4
        # but bot FORCED LONG via bull_setup tiebreaker (OB=18 dominates). Persona rule:
        # "Kontradiksi internal = stop trading." Tied/near-tied votes = max uncertainty.
        # Data (56 trades, vote margin parsed from signal):
        #   margin 0 → WR 25% PnL -$2.93 | margin 1 → WR 25% PnL -$1.14
        #   margin 2 → WR 100% +$2.15  | margin 3 → WR 62% +$2.46
        # Replay margin>1 gate: PF 0.59 → 0.67, blocks 8 losers, saves +$4.07.
        # This is NOT the soft +3 threshold above — it's a HARD skip when the 7-voter
        # system genuinely can't decide direction. CAVEAT: tie sample n=8, bug-detection
        # grade. Re-validate Audit #18. Threshold 1 (not 2) — margin 2 bucket is profitable.
        _MIN_VOTE_MARGIN = 2  # require |bull-bear| >= 2 (skip margin 0 and 1)
        if _vote_margin < _MIN_VOTE_MARGIN:
            log.info(
                f"[SKIP] {asset} | score={score} | side={side.value} | "
                f"reason=low_vote_consensus | context=vote_margin={_vote_margin}<{_MIN_VOTE_MARGIN} "
                f"(direction voters undecided — internal contradiction)"
            )
            self.skip_counters["low_vote_consensus"] = self.skip_counters.get("low_vote_consensus", 0) + 1
            self._skip_count_since_summary += 1
            return None, score

        # [AUDIT #7] OI score gate: low OI = no fundamental conviction
        # Data: OI<6 = WR 41.2% PnL -$7.05. OI>=6 = WR 69.6% PnL +$15.28
        # [FIX 2026-05-25] Reduced from +3 to +0. OI data from HL is often 0 (not
        # because there's no conviction, but because HL doesn't always return OI change).
        # This was penalizing ALL trades unconditionally.
        _oi_abs = abs(_scalper_components.get("oi_signed", 0))
        _oi_gate_adj = 0  # disabled — was always firing due to HL data gaps

        # [AUDIT #7] Funding negative bonus: contrarian LONG when shorts crowded
        # Data: funding<0 = WR 88.9% PnL +$11.09 (9 trades, 8 wins)
        _funding_bonus = -3 if (funding.funding_rate < 0 and side == Side.LONG) else 0

        effective_threshold = (
            config.SCALPER.min_score_to_enter
            + overlap_threshold_adj         # [P0-1] overlap: +8, NY-only: +3
            + session_threshold_delta       # Asia: threshold rises
            + htf_threshold_adj             # 1h regime: aligned=-3, counter=+8, choppy=+3
            + _vol_threshold_adj            # [AUDIT #6] EXTREME: +15 (only 71+ passes)
            + _vote_margin_adj              # [AUDIT #7] low consensus: +5
            + _oi_gate_adj                  # [AUDIT #7] low OI: +5
            + _funding_bonus                # [AUDIT #7] negative funding LONG: -3 (easier entry)
        )
        if score < effective_threshold:
            log.info(
                f"[SKIP] {asset} | score={score} | side={side.value} | "
                f"reason=score_below_threshold | context=threshold={effective_threshold},regime={_regime_cat}"
            )
            self.skip_counters["score_below_threshold"] = self.skip_counters.get("score_below_threshold", 0) + 1
            self._skip_count_since_summary += 1
            reasoning_logger.log_filters(asset, {"threshold": effective_threshold, "score": score, "passed": False})
            reasoning_logger.end_trace(asset, "skip", score, side.value, f"score {score} < threshold {effective_threshold}")
            return None, score

        # SHORT-specific filters: higher threshold + funding rate confirmation + squeeze guard
        if side == Side.SHORT:
            # [AUDIT #16] BLOCK SHORT in TRENDING_UP regime — counter-trend disaster.
            # POST-deploy 28 Mei sore - 29 Mei: 12 SHORTs, 1W/11L, $-12.17 (PF 0.010).
            # ~9-11 dari trades ini di HTF=TRENDING_UP atau CHOPPY (rally market).
            # Existing logic only RAISES threshold +8 — banyak score lolos tetap.
            # Hard block dibutuhkan: SHORT di market rally = no follow-through, all
            # SHORT setups (OI bear, EMA bear) di-buy-back oleh trend dominan.
            # Re-evaluate setelah HTF flip ke TRENDING_DOWN dominan (>50%).
            if htf_regime == "TRENDING_UP":
                log.info(
                    f"[SKIP] {asset} | score={score} | side=SHORT | "
                    f"reason=short_against_uptrend | context=htf={htf_regime} "
                    f"(audit#16: SHORT vs TRENDING_UP = $-12.17 PF 0.010)"
                )
                self.skip_counters["short_against_uptrend"] = self.skip_counters.get("short_against_uptrend", 0) + 1
                self._skip_count_since_summary += 1
                return None, score

            # [AUDIT #16] SHORT in CHOPPY needs higher threshold — range market
            # reverses fast on minor dump → setup mikro tidak survive macro.
            # Standard threshold + 5 untuk CHOPPY SHORT only.
            if htf_regime == "CHOPPY":
                _choppy_short_min = (getattr(config.SIGNAL, 'min_score_short_signal', 62)) + 5
                if score < _choppy_short_min:
                    log.info(
                        f"[SKIP] {asset} | score={score} | side=SHORT | "
                        f"reason=short_choppy_low_conviction | context=min={_choppy_short_min},htf={htf_regime}"
                    )
                    self.skip_counters["short_choppy_low_conviction"] = self.skip_counters.get("short_choppy_low_conviction", 0) + 1
                    self._skip_count_since_summary += 1
                    return None, score

            # [AUDIT #12 FIX] Block SHORT if OB is bullish (bid wall = support below).
            # Data: 3 SHORT trades with OB=+10 (bullish) → ALL LOSS.
            # Root cause: OB excluded from direction voting, so bot can SHORT
            # even when orderbook shows strong bid support. This is internal
            # contradiction — shorting into support = catching a bounce.
            _ob_score = _scalper_components.get("ob_signed", 0)
            if _ob_score > 0:
                log.info(
                    f"[SKIP] {asset} | score={score} | side=SHORT | "
                    f"reason=ob_bullish_contradiction | context=ob_signed={_ob_score} "
                    f"(bid wall = support, contradicts SHORT)"
                )
                self.skip_counters["ob_bullish_contradiction"] = self.skip_counters.get("ob_bullish_contradiction", 0) + 1
                self._skip_count_since_summary += 1
                return None, score

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
                min_fr = getattr(config.SIGNAL, 'short_min_funding_rate', -0.0003)  # [AUDIT FIX 2026-05-21] Use config value. -0.03%/8h allows normal negative funding.
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
            _min_tech_short = max(getattr(config.SIGNAL, 'min_technical_score_short', 10) - 4, 3)  # [FIX 2026-05-21] Was 6. OB not counted here, 6 blocks valid SHORTs with OB confirmation.
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

        # ── [AUDIT FIX 2026-05-21 P0] ATR MINIMUM GATE ─────────────────────
        # Data (104 trades): ATR < 0.0008 → WR 11%, PnL -$15.07 (dead money)
        #                    ATR >= 0.0012 → WR 51%, PnL +$18.17
        # SHORT-specific: winners avg ATR=0.00305, losers avg ATR=0.00157.
        # SHORT needs higher threshold due to structural upward bias + bounce risk.
        _min_atr = 0.0015 if side == Side.SHORT else 0.0013  # [AUDIT #7] LONG 0.0010→0.0013: ATR<0.0013 = dead zone, trailing never fires
        if atr_pct_now > 0 and atr_pct_now < _min_atr:
            log.info(
                f"[SKIP] {asset} | score={score} | side={side.value} | "
                f"reason=low_atr | context=atr={atr_pct_now:.5f} < {_min_atr} (need volatility for trailing)"
            )
            self.skip_counters["low_atr"] = self.skip_counters.get("low_atr", 0) + 1
            self._skip_count_since_summary += 1
            return None, score

        # ── [FIX 2026-05-21] MOMENTUM CONFIRMATION GATE ────────────────────
        # Defaults (used if candle data insufficient)
        _direction_ok = True; _net_move = 0.0; _bullish_candles = 0; _bearish_candles = 0
        if len(_closes) >= 6:
            _net_move = (_closes[-1] - _closes[-6]) / _closes[-6] if _closes[-6] > 0 else 0

            # [AUDIT FIX 2026-05-21 P1] MINIMUM MOMENTUM REQUIREMENT
            # Data: trades with pre-move >0.3% = WR 52.6%, PnL +$8.39
            #       trades with pre-move <0.15% = WR ~28%, PnL negative
            # SHORT-specific: winners entered AFTER strong downmove (avg vol 0.0958).
            #
            # [AUDIT #16] SHORT min raised 0.25% → 0.50%.
            # Root cause: 22 SHORT @ TRENDING_DOWN (PRE+POST), 18 reversal (82%).
            # Pattern: bot SHORT setelah dump kecil 0.3-0.4% → V-bottom catch → bounce.
            # Loser avg pre-move = 0.43% (just above 0.25% threshold). Winner avg = 0.54%.
            # 0.50% threshold filters mini-panic dumps yang langsung di-buy back.
            # Trade-off: blocks ~70% SHORT signals. Yang lolos = dump kuat dengan
            # follow-through probability lebih tinggi.
            # Re-evaluate setelah 50+ SHORT trades dengan threshold baru.
            _dir_move = _net_move if side == Side.LONG else -_net_move
            _min_momentum = 0.0050 if side == Side.SHORT else 0.0015  # 0.50% SHORT (was 0.25%), 0.15% LONG
            if _dir_move < _min_momentum:
                log.info(
                    f"[SKIP] {asset} | score={score} | side={side.value} | "
                    f"reason=low_momentum | context=dir_move={_dir_move*100:.3f}% < {_min_momentum*100:.2f}% (need trend)"
                )
                self.skip_counters["low_momentum"] = self.skip_counters.get("low_momentum", 0) + 1
                self._skip_count_since_summary += 1
                return None, score

            # Leading signal detection — these expect price HASN'T moved yet
            _has_leading_signal = (
                _scalper_components.get("cvd_pts", 0) >= 10 or
                _scalper_components.get("xam_pts", 0) != 0 or
                _scalper_components.get("abs_pts", 0) != 0
            )

            if _has_leading_signal:
                # Only require candle direction, no net move threshold
                # SHORT needs 3/5 (stricter) vs LONG 2/5 — structural upward bias
                _bullish_candles = sum(1 for i in range(-5, 0) if _closes[i] > _closes[i-1])
                _bearish_candles = 5 - _bullish_candles
                _direction_ok = (
                    (side == Side.LONG and _bullish_candles >= 2) or
                    (side == Side.SHORT and _bearish_candles >= 3)
                )
            else:
                # Standard: require net move + candle direction
                _min_confirm = 0.0004  # 0.04%
                _bullish_candles = sum(1 for i in range(-5, 0) if _closes[i] > _closes[i-1])
                _bearish_candles = 5 - _bullish_candles
                _direction_ok = (
                    (side == Side.LONG and _net_move > _min_confirm and _bullish_candles >= 3) or
                    (side == Side.SHORT and _net_move < -_min_confirm and _bearish_candles >= 3)
                )

            if not _direction_ok:
                log.info(
                    f"[SKIP] {asset} | score={score} | side={side.value} | "
                    f"reason=no_momentum_confirm | context=5m_move={_net_move*100:.4f}%,bull_candles={_bullish_candles}/5,leading={_has_leading_signal}"
                )
                self.skip_counters["no_micro_momentum"] = self.skip_counters.get("no_micro_momentum", 0) + 1
                self._skip_count_since_summary += 1
                from dashboard.reasoning_logger import reasoning_logger
                reasoning_logger.log_momentum_gate(
                    asset, passed=False,
                    move_pct=_net_move if side == Side.LONG else -_net_move,
                    bull_candles=_bullish_candles, total_candles=5,
                    is_leading=_has_leading_signal,
                )
                return None, score
            else:
                from dashboard.reasoning_logger import reasoning_logger
                reasoning_logger.log_momentum_gate(
                    asset, passed=True,
                    move_pct=_net_move if side == Side.LONG else -_net_move,
                    bull_candles=_bullish_candles, total_candles=5,
                    is_leading=_has_leading_signal,
                )

        # Capture momentum gate result for breakdown (default: no gate data)
        _mgp = None; _mmove = 0.0; _mcandles = ""
        if len(_closes) >= 6:
            _mgp = _direction_ok
            _mmove = _net_move if side == Side.LONG else -_net_move
            _dir_candles = _bullish_candles if side == Side.LONG else _bearish_candles
            _mcandles = f"{_dir_candles}/5"

        # ── [AUDIT #9 FIX 2026-05-24] PUMP TIMING GATE ─────────────────
        # Data: 62% trades = time_exit (entry AFTER pump, price diam).
        # Fix: Only enter when pump is STARTING (vol surge + price accel + not too late).
        # This is the fundamental shift: from "high score" to "pump beginning".
        if len(candles) >= 35:
            _volumes = []
            for _c in candles[-35:]:
                if isinstance(_c, dict):
                    try:
                        _volumes.append(float(_c.get("v", 0)))
                    except (TypeError, ValueError):
                        _volumes.append(0.0)

            if len(_volumes) >= 35 and len(_closes) >= 11:
                # Volume baseline: MEDIAN of 30 candles before recent 5 (robust vs spikes)
                _vol_baseline_arr = sorted(_volumes[:-5])
                _vol_baseline = _vol_baseline_arr[len(_vol_baseline_arr) // 2] if _vol_baseline_arr else 1e-10
                _vol_recent = sum(_volumes[-5:]) / 5
                _vol_surge = _vol_recent / max(_vol_baseline, 1e-10)

                # Price acceleration: last candle vs avg candle size (10 candles)
                _candle_sizes = [abs(_closes[i] - _closes[i-1]) / _closes[i-1]
                                 for i in range(-10, 0) if _closes[i-1] > 0]
                _avg_candle = sum(_candle_sizes) / max(len(_candle_sizes), 1)
                _last_candle = abs(_closes[-1] - _closes[-2]) / _closes[-2] if _closes[-2] > 0 else 0

                # Total move last 5 candles
                _move_5m = abs(_closes[-1] - _closes[-6]) / _closes[-6] if len(_closes) >= 6 and _closes[-6] > 0 else 0

                # Direction check: 3 of last 5 candles in trade direction (forgiving)
                _up_candles = sum(1 for i in range(-5, 0) if _closes[i] > _closes[i-1])
                _down_candles = 5 - _up_candles
                _direction_match = (_up_candles >= 3 if side == Side.LONG else _down_candles >= 3)

                # Min avg candle size: filter dead coins (< 0.04% per candle)
                _coin_alive = _avg_candle > 0.0004

                # Total displacement: don't buy at top, don't sell at bottom
                # LONG: how far is price from 30-candle low? If >2% = overextended
                # SHORT: how far is price from 30-candle high? If >2% = overextended
                _max_displacement = 0.02  # 2%
                if side == Side.LONG:
                    _low_30 = min(_closes[-30:]) if len(_closes) >= 30 else min(_closes)
                    _total_disp = (_closes[-1] - _low_30) / _low_30 if _low_30 > 0 else 0
                else:
                    _high_30 = max(_closes[-30:]) if len(_closes) >= 30 else max(_closes)
                    _total_disp = (_high_30 - _closes[-1]) / _closes[-1] if _closes[-1] > 0 else 0
                _not_overextended = _total_disp < _max_displacement

                # Thresholds differ by side:
                # LONG: need momentum starting (vol surge + acceleration)
                # SHORT: need momentum FADING (vol surge OR deceleration — crypto upward bias means shorts work when momentum dies)
                _max_move = 0.007  # 0.7%

                if side == Side.LONG:
                    # LONG: vol_surge OR accel (not AND — either confirms momentum starting)
                    _vol_surge_min = 1.5
                    _price_accel_min = 1.2
                    _pump_starting = (
                        (_vol_surge >= _vol_surge_min or _last_candle >= _avg_candle * _price_accel_min) and
                        _move_5m < _max_move and
                        _direction_match and
                        _coin_alive and
                        _not_overextended
                    )
                else:
                    # SHORT: vol confirms selling pressure, but accel should be LOW (momentum fading)
                    # Good short entry = high volume + decelerating price (exhaustion)
                    _vol_surge_min = 1.5  # lowered from 2.0 — shorts don't need extreme vol
                    _pump_starting = (
                        _vol_surge >= _vol_surge_min and
                        _move_5m < _max_move and
                        _direction_match and
                        _coin_alive and
                        _not_overextended
                    )
                    # Note: accel requirement REMOVED for shorts — shorts profit from
                    # momentum exhaustion, not acceleration

                if not _pump_starting:
                    _accel_ratio = _last_candle / _avg_candle if _avg_candle > 0 else 0
                    if side == Side.LONG:
                        _reason = (
                            f"vol_surge={_vol_surge:.1f}x(need {_vol_surge_min}x) "
                            f"accel={_accel_ratio:.1f}x(need {_price_accel_min}x) "
                            f"[need vol OR accel] "
                            f"move={_move_5m*100:.2f}%(max {_max_move*100:.1f}%) "
                            f"dir={'ok' if _direction_match else 'wrong'} "
                            f"alive={'ok' if _coin_alive else 'dead'} "
                            f"disp={_total_disp*100:.1f}%(max {_max_displacement*100:.0f}%)"
                        )
                    else:
                        _reason = (
                            f"vol_surge={_vol_surge:.1f}x(need {_vol_surge_min}x) "
                            f"move={_move_5m*100:.2f}%(max {_max_move*100:.1f}%) "
                            f"dir={'ok' if _direction_match else 'wrong'} "
                            f"alive={'ok' if _coin_alive else 'dead'} "
                            f"disp={_total_disp*100:.1f}%(max {_max_displacement*100:.0f}%)"
                        )
                    log.info(
                        f"[SKIP] {asset} | score={score} | side={side.value} | "
                        f"reason=pump_not_starting | context={_reason}"
                    )
                    self.skip_counters["pump_not_starting"] = self.skip_counters.get("pump_not_starting", 0) + 1
                    self._skip_count_since_summary += 1
                    return None, score

        # [AUDIT #9] Trend structure veto REMOVED — redundant with pump gate.
        # Pump gate already checks 3/5 candle direction + move < 0.7%.

        # ── [AI INTELLIGENCE — DISABLED FROM SCORING, AUDIT #17] ──
        # AI verdict masih dievaluasi & disimpan untuk DASHBOARD (konteks manusia),
        # TAPI tidak lagi mengubah score atau mem-veto trade.
        #
        # Root cause disable (data produksi, 377 verdict ber-PnL):
        #   1. TIMEOUT 67%: 252/377 verdict = fallback default (conf=0.50,
        #      market_state=unknown, 222 latency=0ms). Mimo API (api.xiaomimimo.com)
        #      terlalu lambat dari Railway SG. Audit #15 sudah coba 4s→10s, tetap gagal.
        #   2. INVERSE saat jalan (125 real evals):
        #        confidence  r(PnL) = -0.101
        #        score_adj   r(PnL) = -0.053
        #      Trade yang AI HUKUM (-5) → WR 44% (di atas rata-rata bot 34%).
        #      Trade yang AI PUJI (+4/+8) → rugi. AI persis terbalik.
        #   3. CONSTANT BIAS: 112/125 (90%) di-label "exhaustion" = noise floor.
        # Persona rule: komponen inverse + 100% satu output + 2 audit gagal fix → disable.
        # Path fix tersisa (ganti provider AI) tidak realistis untuk deadline live.
        # Re-enable HANYA jika: timeout < 20% DAN confidence r(PnL) > +0.10 di data baru.
        _AI_SCORING_ENABLED = False
        try:
            from intelligence.ai_analyst import ai_analyst
            if ai_analyst.enabled:
                _ai_context = {
                    "asset": asset,
                    "side": side.value.upper(),
                    "score": score,
                    "components": {
                        "OB": _scalper_components.get("ob_signed", 0),
                        "EMA": _scalper_components.get("ema_pts", 0),
                        "RSI": _scalper_components.get("rsi_pts", 0),
                        "FUND": _scalper_components.get("oi_signed", 0),
                        "XAM": _scalper_components.get("xam_pts", 0),
                    },
                    "regime": _regime_cat,
                    "htf_regime": htf_regime,
                    # [AUDIT #17 FIX] Was: momentum_move_pct=trend_pct (24h regime trend,
                    # cached 60min) → AI mistook long-term trend for 5m momentum and
                    # flagged "LONG opposes -4.318% momentum" while the GATE actually uses
                    # _net_move (real-time 5m, +ve for all executed LONGs). Send the SAME
                    # 5m value the momentum gate uses, plus trend_pct as a SEPARATE field.
                    "momentum_move_pct": _net_move,          # real-time 5m net move (gate value)
                    "trend_pct_24h": trend_pct,              # long-term regime trend (context only)
                    "atr_pct": realized_vol or 0.01,
                    "funding_rate": funding.funding_rate if funding else 0.0,
                    "ob_imbalance": _scalper_components.get("ob_signed", 0) / 18.0,
                    "btc_move": 0.0,
                    "volume_trend": "unknown",
                }
                _ai_verdict = await ai_analyst.evaluate_signal(_ai_context)
                # [AUDIT #17] score_adj NOT applied — AI is advisory-only now.
                if _AI_SCORING_ENABLED and _ai_verdict.score_adj != 0:
                    _ai_score_adj = _ai_verdict.score_adj
                    score += _ai_score_adj
                    score = max(0, min(100, score))
                    reasons.append(
                        f"🧠 AI: conf={_ai_verdict.confidence:.2f} ({_ai_verdict.market_state}) "
                        f"→ {_ai_score_adj:+d}pts"
                    )
                    if _ai_score_adj < 0 and score < effective_threshold:
                        log.info(
                            f"[AI-VETO] {asset} | score={score} (was {score - _ai_score_adj}) | "
                            f"AI penalty {_ai_score_adj} dropped below threshold {effective_threshold}"
                        )
                        self.skip_counters["ai_veto"] = self.skip_counters.get("ai_veto", 0) + 1
                        return None, score
                else:
                    # Advisory-only: annotate reasons without touching score
                    reasons.append(
                        f"🧠 AI (advisory): conf={_ai_verdict.confidence:.2f} "
                        f"({_ai_verdict.market_state}) — not applied to score"
                    )
                # Save to DB for dashboard (score unchanged → score_before == score_after)
                try:
                    from intelligence.ai_analyst import save_ai_verdict
                    save_ai_verdict(asset, side.value, score, _ai_verdict)
                except Exception:
                    pass
        except ImportError:
            pass
        except Exception as _ai_err:
            log.debug(f"[AI] {asset}: error {_ai_err}")

        signal = self._build_scalper_signal(
            asset, side, score, mark_price, reasons, vol_regime,
            session_bonus, realized_vol, trend_pct, atr_pct=atr_pct_now,
            fade_mode=fade_mode,
            late_trend=late_trend,
            oi_signed=_scalper_components.get("oi_signed", 0),
            liq_signed=_scalper_components.get("liq_signed", 0),
            ob_signed=_scalper_components.get("ob_signed", 0),
            bull_setup=_scalper_components.get("bull_setup", 0),
            bear_setup=_scalper_components.get("bear_setup", 0),
            sub_components={
                "OB":   _scalper_components.get("ob_signed", 0),
                "EMA":  _scalper_components.get("ema_pts", 0),
                "RSI":  _scalper_components.get("rsi_pts", 0),
                "MFI":  _scalper_components.get("mfi_pts", 0),
                "FUND": _scalper_components.get("oi_signed", 0),
                "LIQ":  _scalper_components.get("liq_signed", 0),
                "XAM":  _scalper_components.get("xam_pts", 0),
            },
            momentum_gate_passed=_mgp,
            momentum_move_pct=_mmove,
            momentum_candles=_mcandles,
            htf_regime=htf_regime,
            htf_threshold_adj=htf_threshold_adj,
        )

        # Apply 1H regime leverage adjustment
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
            reasoning_logger.log_execution(asset, {"entry": signal.entry_price, "sl": signal.stop_loss, "tp1": signal.tp1})
            reasoning_logger.end_trace(asset, "execute", score, side.value, "signal accepted")
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
        ls_ratio: float = None,
        htf_regime: str = "",
    ) -> Tuple[int, Side, List[str]]:
        """
        OPPORTUNITY SCORING v2 — measures UNTAPPED potential, not confirmation.

        Philosophy: high score = move HASN'T happened yet but conditions are ripe.
        Leading indicators (OI, funding, OB wall) get heavy weight.
        Lagging indicators (EMA, RSI) get small weight or PENALIZE if stale.
        Displacement (price already moved) = multiplicative penalty.

        Score range: 0-100. Designed so that:
        - Perfect setup (strong OI + OB wall + fresh EMA cross + no displacement) = 90-100
        - Good setup (OI or OB + some confirmation) = 60-75
        - Marginal (only lagging indicators agree) = 40-55
        - Stale/exhausted (high displacement, RSI extreme) = 20-40

        Returns (score: int, side: Side, reasons: List[str])
        """
        # ═══════════════════════════════════════════════════════════════
        # OPPORTUNITY SCORING v2 — Leading indicators first, lagging penalized
        # ═══════════════════════════════════════════════════════════════
        reasons = []
        bull_setup = 0   # leading indicators pointing LONG (max ~55)
        bear_setup = 0   # leading indicators pointing SHORT (max ~55)
        confirm_pts = 0  # lagging confirmation (can be negative, range -15 to +25)

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
        _ob_signed = 0

        # ── SETUP LAYER 1: Orderbook Imbalance (LEADING — wall = pressure building) ──
        ob = self.cache.orderbook.get(asset) if hasattr(self.cache, 'orderbook') else None
        imb = 0.0
        if ob:
            try:
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

                # Spread Filter — reject illiquid assets
                if bids and asks and bids[0][0] > 0:
                    spread_pct = (asks[0][0] - bids[0][0]) / asks[0][0]
                    if spread_pct > 0.0015:
                        if out_components is not None:
                            out_components["ob_signed"] = 0
                            out_components["oi_signed"] = 0
                            out_components["liq_signed"] = 0
                        return 0, Side.LONG, ["REJECT: Spread too wide"]
            except Exception:
                imb = 0.0

            # OB imbalance = pressure building (LEADING — wall hasn't broken yet)
            # [AUDIT #14 FIX] OB reduced when HTF regime is CHOPPY.
            # Was: abs(trend_pct) < 0.035 — unreliable, OB=18 still appeared 59/161 signals.
            # Now: uses htf_regime directly (same detector that drives threshold adj).
            # Data: OB r=-0.148 (INVERSE in choppy). Wall = liquidity trap, not support.
            _ob_regime_mult = 1.0
            if htf_regime == "CHOPPY":
                _ob_regime_mult = 0.6  # 40% reduction in choppy — wall less reliable
            if abs(imb) > 0.45:
                pts = int(18 * _ob_regime_mult)
                if imb > 0:
                    bull_setup += pts; _ob_signed = pts
                    reasons.append(f"📗 Strong bid wall ({imb:.2f}) — pressure building")
                else:
                    bear_setup += pts; _ob_signed = -pts
                    reasons.append(f"📕 Strong ask wall ({imb:.2f}) — pressure building")
                if _ob_regime_mult < 1.0:
                    reasons.append(f"⚠️ OB reduced ×{_ob_regime_mult} (ranging — wall less reliable)")
                _c_ob = pts
            elif abs(imb) > 0.20:
                pts = int(10 * _ob_regime_mult)
                if imb > 0:
                    bull_setup += pts; _ob_signed = pts
                    reasons.append(f"🟢 Bid pressure ({imb:.2f})")
                else:
                    bear_setup += pts; _ob_signed = -pts
                    reasons.append(f"🔴 Ask pressure ({imb:.2f})")
                _c_ob = pts

        # ── SETUP LAYER 2: OI + Funding (LEADING — money flowing in before move) ──
        # OI rising = new money entering = move ABOUT to happen (strongest leading signal)
        if oi_bull > 0 or oi_bear > 0:
            fund_delta = oi_bull - oi_bear
            # Scale: max ±28 pts (largest single contributor — most predictive per audit)
            fund_pts = max(-28, min(28, int(fund_delta)))
            _c_fund = abs(fund_pts)
            if fund_pts > 0:
                bull_setup += fund_pts
                reasons.append(f"📊 OI/Funding bullish setup (+{fund_pts})")
            elif fund_pts < 0:
                bear_setup += abs(fund_pts)
                reasons.append(f"📊 OI/Funding bearish setup ({fund_pts})")
            if fund_reasons:
                reasons.extend(fund_reasons)

        # ── SETUP LAYER 3: Liquidation — [AUDIT #17] DISABLED ──
        # Data: liquidation_score = 0 di 174/174 signal (0% firing), konsisten dengan
        # Audit #16 (0/349). r(PnL)=0.000 — komponen mati total.
        # Root cause (Audit #16): OKX liq events sparse per-asset (~1/min); threshold
        # _calc_liq_cluster (≥$2K notional + ≥2 events/10min) tidak pernah tercapai untuk
        # altcoin. Teorinya sound (cascade = katalis), implementasinya tidak punya data.
        # Persona rule: "Component 0% firing 2+ audits → Disable."
        # Telemetry (liq_signed) tetap dicatat untuk monitoring jika threshold OKX
        # nanti diturunkan (Audit #17 task: lower OKX threshold). Re-enable HANYA setelah
        # liq fire rate > 0% terbukti di data baru.
        _LIQ_SCORING_ENABLED = False
        if _LIQ_SCORING_ENABLED and (liq_bull > 0 or liq_bear > 0):
            liq_delta = liq_bull - liq_bear
            liq_pts = max(-12, min(12, int(liq_delta)))
            _c_liq = abs(liq_pts)
            if liq_pts > 0:
                bull_setup += liq_pts
                reasons.append(f"💥 Liq cascade potential bullish (+{liq_pts})")
            elif liq_pts < 0:
                bear_setup += abs(liq_pts)
                reasons.append(f"💥 Liq cascade potential bearish ({liq_pts})")
            if liq_reasons:
                reasons.extend(liq_reasons)

        if len(candles) < 10:
            side = Side.LONG if bull_setup >= bear_setup else Side.SHORT
            raw = max(bull_setup, bear_setup)
            score = min(raw, 100)
            if out_components is not None:
                out_components["ob_signed"] = int(_ob_signed)
                out_components["oi_signed"] = int(oi_bull - oi_bear)
                out_components["liq_signed"] = int(liq_bull - liq_bear)
                out_components["bull_setup"] = int(bull_setup)
                out_components["bear_setup"] = int(bear_setup)
            return score, side, reasons

        # Extract OHLCV
        closes, opens, highs_c, lows_c, volumes = [], [], [], [], []
        for c in candles:
            if isinstance(c, dict):
                try:
                    closes.append(float(c.get("c", 0)))
                    opens.append(float(c.get("o", 0)))
                    highs_c.append(float(c.get("h", 0)))
                    lows_c.append(float(c.get("l", 0)))
                    volumes.append(float(c.get("v", 0)))
                except (ValueError, TypeError):
                    pass

        if len(closes) < 10:
            side = Side.LONG if bull_setup >= bear_setup else Side.SHORT
            raw = max(bull_setup, bear_setup)
            score = min(raw, 100)
            if out_components is not None:
                out_components["ob_signed"] = int(_ob_signed)
                out_components["oi_signed"] = int(oi_bull - oi_bear)
                out_components["liq_signed"] = int(liq_bull - liq_bear)
                out_components["bull_setup"] = int(bull_setup)
                out_components["bear_setup"] = int(bear_setup)
            return score, side, reasons
        # ── CONFIRMATION LAYER: EMA Cross (lagging — freshness matters) ──
        def ema(data: list, period: int) -> float:
            k = 2 / (period + 1)
            e = data[0]
            for v in data[1:]:
                e = v * k + e * (1 - k)
            return e

        # [AUDIT #11 FIX] EMA 8/21 on 1m = too short, 93% fire (noise).
        # EMA 13/34 = more stable, needs real trend to cross. Window 34 bars minimum.
        ema13 = ema(closes[-34:], 13) if len(closes) >= 13 else closes[-1]
        ema34 = ema(closes[-34:], 34) if len(closes) >= 34 else closes[-1]

        # Gap 0.04% — period 13/34 already filters noise, gap just prevents micro-touches
        ema_bullish = ema13 > ema34 * 1.0004
        ema_bearish = ema13 < ema34 * 0.9996
        candles_since_cross = 0
        if ema_bullish or ema_bearish:
            # [AUDIT #12 FIX] Was using EMA 8/21 for freshness detection while
            # cross detection uses 13/34. This MISMATCH caused candles_since_cross
            # to always be >=8 (because 8/21 crosses much earlier than 13/34),
            # resulting in permanent -5 stale penalty on ALL signals.
            # Fix: use same periods (13/34) for freshness check.
            for i in range(len(closes) - 2, max(0, len(closes) - 12), -1):
                if i < 34:
                    break
                e13_hist = ema(closes[:i + 1], 13)
                e34_hist = ema(closes[:i + 1], 34)
                same_dir = (e13_hist > e34_hist) if ema_bullish else (e13_hist < e34_hist)
                if same_dir:
                    candles_since_cross += 1
                else:
                    break

            # [AUDIT #13 FIX] Tightened: ≤2 = fresh (was ≤3), bonus +8 (was +10).
            # Data: EMA +10 fired 57% signals, r=-0.185 (INVERSE). Too many
            # "fresh" crosses that were actually 3min old = price already moved.
            # Crypto moves fast — 3 candles (3min) is NOT fresh for 1m scalping.
            if candles_since_cross <= 2:
                # Truly fresh cross = just happened = GOOD confirmation
                pts = 8  # [AUDIT #13] was 10 — reduced to prevent score inflation
                confirm_pts += pts; _c_ema = pts
                if ema_bullish:
                    bull_setup += 4  # [AUDIT #13] was 5
                    reasons.append(f"📈 Fresh EMA cross ({candles_since_cross}m ago) +{pts}")
                else:
                    bear_setup += 4
                    reasons.append(f"📉 Fresh EMA cross ({candles_since_cross}m ago) +{pts}")
            elif candles_since_cross >= 8:
                # Stale cross = move already well underway = PENALTY
                penalty = min(candles_since_cross - 5, 10)
                confirm_pts -= penalty; _c_ema = -penalty
                reasons.append(f"⚠️ Stale EMA ({candles_since_cross}m) — move old (-{penalty})")
            else:
                # Medium freshness (3-7 candles) — small confirmation
                confirm_pts += 4; _c_ema = 4
                if ema_bullish:
                    bull_setup += 2
                else:
                    bear_setup += 2
                reasons.append(f"📊 EMA aligned ({candles_since_cross}m)")
        else:
            reasons.append("📊 EMA neutral (no cross)")
        # ── CONFIRMATION LAYER: RSI (penalizes exhaustion, rewards neutral) ──
        rsi = 50.0  # default neutral
        if len(closes) >= 15:
            gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
            losses_r = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
            avg_gain = sum(gains[-14:]) / 14
            avg_loss_r = sum(losses_r[-14:]) / 14
            if avg_loss_r > 0:
                rs = avg_gain / avg_loss_r
                rsi = 100 - (100 / (1 + rs))
            else:
                rsi = 100.0

            # Opportunity scoring RSI logic:
            # RSI extreme = price already moved far = PENALTY (exhaustion)
            # RSI neutral (40-60) = price hasn't moved much = OPPORTUNITY
            if rsi > 75:
                confirm_pts -= 8; _c_rsi = -8
                reasons.append(f"⚠️ RSI {rsi:.0f} extreme OB — exhaustion risk (-8)")
            elif rsi > 65:
                confirm_pts -= 4; _c_rsi = -4
                reasons.append(f"📊 RSI {rsi:.0f} high — mild exhaustion (-4)")
            elif rsi < 25:
                confirm_pts -= 8; _c_rsi = -8
                reasons.append(f"⚠️ RSI {rsi:.0f} extreme OS — exhaustion risk (-8)")
            elif rsi < 35:
                confirm_pts -= 4; _c_rsi = -4
                reasons.append(f"📊 RSI {rsi:.0f} low — mild exhaustion (-4)")
            elif 42 <= rsi <= 58:
                # Neutral RSI = price hasn't moved much = good for fresh entry
                confirm_pts += 5; _c_rsi = 5
                reasons.append(f"✅ RSI {rsi:.0f} neutral — fresh opportunity (+5)")

        # ── SETUP LAYER 5: RSI Divergence (1m vs 5m aggregated — zero API calls) ──
        # Bullish div: price lower low + RSI higher low = reversal UP
        # Bearish div: price higher high + RSI lower high = reversal DOWN
        _c_div = 0
        if len(closes) >= 15:
            # Aggregate 1m → 5m candles (groups of 5)
            n5 = (len(closes) // 5) * 5
            closes_5m = [closes[i+4] for i in range(0, n5, 5)]

            if len(closes_5m) >= 4:
                # 5m RSI (simple)
                gains_5m = [max(closes_5m[i] - closes_5m[i-1], 0) for i in range(1, len(closes_5m))]
                losses_5m = [max(closes_5m[i-1] - closes_5m[i], 0) for i in range(1, len(closes_5m))]
                ag5 = sum(gains_5m) / len(gains_5m) if gains_5m else 0
                al5 = sum(losses_5m) / len(losses_5m) if losses_5m else 0
                rsi_5m = 100 - (100 / (1 + ag5/al5)) if al5 > 0 else (100 if ag5 > 0 else 50)

                # [FIX 2026-05-21] RSI confirms trend — not divergence.
                # RSI rising + price rising = momentum building = trend confirmation.
                # RSI divergence (price down but RSI up) was a reversal signal — removed.
                price_rising = closes[-1] > closes_5m[-2] if len(closes_5m) >= 2 else False
                price_falling = closes[-1] < closes_5m[-2] if len(closes_5m) >= 2 else False

                if price_rising and rsi > rsi_5m + 5:
                    # [AUDIT #15 FIX] RSI momentum moved from setup → confirm_pts.
                    # Was: bull_setup += 8 → inflated aligned_setup → score 65+ = LOSS.
                    # Root cause: RSI momentum = LAGGING (price already moved, RSI confirms after).
                    # Data: score 65+ trades (where RSI momentum fires) = 0% WR, -$5.11.
                    # Fix: +8 to confirm_pts (not setup). Still contributes to raw score,
                    # but doesn't inflate aligned_setup which drives direction + setup strength.
                    confirm_pts += 8; _c_div = 8
                    reasons.append(f"📈 RSI momentum: RSI 1m({rsi:.0f})>5m({rsi_5m:.0f}) + price rising +8")
                elif price_falling and rsi < rsi_5m - 5:
                    confirm_pts += 8; _c_div = 8
                    reasons.append(f"📉 RSI momentum: RSI 1m({rsi:.0f})<5m({rsi_5m:.0f}) + price falling +8")

        # ── CONFIRMATION LAYER: CVD — [AUDIT #17] RE-ENABLED (5m window, ZONE logic) ──
        # Disabled 24 Mei dengan klaim r=-0.21 (pakai window 80-trade lagging).
        # Re-test Audit #17 (candle-based proxy, directional, 56 trades):
        #   CVD 3m window → r=+0.037 (netral)
        #   CVD 5m window → r=+0.172 (PREDIKTIF, > +0.15 threshold)
        # Pola NON-LINEAR (sama seperti LOC/OI — ekstrem = exhaustion):
        #   net delta -0.3..+0.3 (flat/bingung) → WR 0%  → NO bonus
        #   net delta +0.3..+0.7 (tekanan sedang) → WR 50% → BONUS (sweet spot)
        #   net delta +0.7..+1.0 (ekstrem, semua sudah masuk) → WR 37% → NO bonus
        # Implementasi: CVD = candle-based net delta 5m (sign(close-open)×volume),
        # directional ke trade side, HANYA beri +confirm bila di zona sedang (0.3-0.7).
        # CAVEAT: ini PROXY candle, bukan tick CVD asli. n=56, 1 rezim. Re-validate
        # Audit #18 dengan tick CVD asli dari WS cache. Bobot kecil (confirm, bukan setup).
        _c_cvd = 0
        _CVD_ENABLED = True
        if _CVD_ENABLED and len(closes) >= 5 and len(opens) >= 5 and len(volumes) >= 5:
            _cvd_delta = 0.0; _cvd_totvol = 0.0
            for _i in range(-5, 0):
                _o = opens[_i]; _cl = closes[_i]; _v = volumes[_i]
                if _o > 0 and _v > 0:
                    _cvd_delta += (1 if _cl >= _o else -1) * _v
                    _cvd_totvol += _v
            _cvd_norm = (_cvd_delta / _cvd_totvol) if _cvd_totvol > 0 else 0.0
            # Direction-align: bull pressure helps LONG, sell pressure helps SHORT
            _cvd_dir = _cvd_norm if bull_setup >= bear_setup else -_cvd_norm
            # ZONE: only the moderate band (0.3-0.7) is predictive (sweet spot WR 50%)
            if 0.30 <= _cvd_dir < 0.70:
                _c_cvd = 6
                confirm_pts += 6
                reasons.append(f"📊 CVD 5m moderate pressure ({_cvd_dir:+.2f}) — sweet spot +6")
            elif _cvd_dir >= 0.70:
                reasons.append(f"⚠️ CVD 5m extreme ({_cvd_dir:+.2f}) — exhaustion zone, no bonus")
            # flat/negative-aligned → no contribution

        # ── SETUP LAYER 4: Bybit Long/Short Ratio (CONTRARIAN — fade the crowd) ──
        # Ratio > 1.5 = crowd heavily long → contrarian SHORT setup
        # Ratio < 0.67 = crowd heavily short → contrarian LONG setup
        # This is institutional-grade data not available on Hyperliquid.
        if ls_ratio is not None and ls_ratio > 0:
            if ls_ratio > 2.0:
                bear_setup += 12
                reasons.append(f"🐻 L/S ratio {ls_ratio:.2f} — crowd VERY long, fade SHORT +12")
            elif ls_ratio > 1.5:
                bear_setup += 7
                reasons.append(f"🐻 L/S ratio {ls_ratio:.2f} — crowd long, SHORT tilt +7")
            elif ls_ratio < 0.5:
                bull_setup += 12
                reasons.append(f"🐂 L/S ratio {ls_ratio:.2f} — crowd VERY short, fade LONG +12")
            elif ls_ratio < 0.67:
                bull_setup += 7
                reasons.append(f"🐂 L/S ratio {ls_ratio:.2f} — crowd short, LONG tilt +7")

        # ── CONFIRMATION LAYER: MTF 15m Trend — DISABLED ──
        # [AUDIT FIX 2026-05-21] r=-0.68 vs PnL (p=0.0008). Strongest inverse predictor.
        # 15m timeframe too slow for 8-12min scalper hold. Confirmed over 21 trades.
        import config
        scfg = config.SCALPER
        _c_mtf = 0  # disabled — do not contribute to score

        # ── DISPLACEMENT PENALTY (multiplicative — the key anti-chase mechanism) ──
        # If price already moved significantly in our direction, the opportunity is STALE.
        # This is the #1 fix for "score inverse predictive" problem.
        # [AUDIT FIX 2026-05-20] SHORT fix: mild drop (0.3-0.8%) = trend confirmation, not stale.
        # Only penalize SHORT if drop > 1.5% (exhaustion/bounce risk).

        # ── EDGE: Cross-Asset Momentum (leader-follower lag) ──
        # [AUDIT FIX 2026-05-21] Re-enabled with relaxed lag threshold.
        # Old: alt must move < 0.05% (too strict, never fires).
        # New: alt must move < 50% of leader move (relative lag detection).
        _xam_pts, _xam_reason = self._calc_cross_asset_momentum(asset)
        if _xam_pts != 0:
            if _xam_pts > 0:
                bull_setup += _xam_pts
            else:
                bear_setup += abs(_xam_pts)
            reasons.append(_xam_reason)

        # ── SETUP LAYER 5: Large Order Clustering — [AUDIT #17] DISABLED ──
        # Data (56 trades, 41 LOC-fired, directional):
        #   LOC r(PnL) = -0.284 (INVERSE — worse than aggregate score -0.159)
        #   LOC fired → WR 32% PnL -$17.17 | LOC absent → WR 40% PnL +$4.14
        # Root cause (investigated, NOT a threshold issue — tested 3 angles):
        #   1. By dominance: 60-75%→0% WR, 90-99%→29%, 99-100%→42% — ALL buckets lose.
        #   2. By notional: bigger money = WORSE (>$80K → WR 25%). Inverted "follow money".
        #   3. By pre-move: even "early" (price flat) LOC trades → WR 31%, still lose.
        # CONCEPTUAL FLAW (not fixable via threshold): LOC reads cache.trades =
        # EXECUTED taker market orders = LAGGING. By the time 19 large buys @100% dom
        # are visible, the move ALREADY happened → bot enters at the top → reversal.
        # Contrast: OB (orderbook walls = resting limit orders = LEADING) r=+0.337.
        # Also: LOC was a same-direction amplifier (40 aligned / 1 against) = no
        # discrimination. Persona: inverse + no-discrimination + no fixable path → disable.
        # A real "follow the money" edge needs on-chain wallet flow (Arkham/Nansen) +
        # longer hold — a separate feature, not this trade-tape detector.
        # Telemetry (loc_pts) still recorded for monitoring. Re-enable only with
        # leading data source + proof of r > +0.10.
        _LOC_SCORING_ENABLED = False
        _loc_pts = 0  # large order clustering
        _loc_trades = self.cache.trades.get(asset, []) if hasattr(self.cache, 'trades') else []
        if _LOC_SCORING_ENABLED and len(_loc_trades) >= 30:
            import time as _t_loc
            _now_ms = _t_loc.time() * 1000
            _window_ms = 120_000  # 2 minutes

            # Calculate median notional from recent trades
            _all_notionals = []
            for _tr in _loc_trades[-200:]:
                try:
                    _n = float(_tr.get('sz', 0)) * float(_tr.get('px', 0))
                    if _n > 0:
                        _all_notionals.append(_n)
                except (ValueError, TypeError):
                    pass

            if len(_all_notionals) >= 20:
                _all_notionals.sort()
                _median_not = _all_notionals[len(_all_notionals) // 2]
                _large_threshold = max(_median_not * 3, 1000.0)  # 3× median OR $1K minimum (whichever higher)

                # Filter large orders in last 2 minutes
                _large_buys = 0.0
                _large_sells = 0.0
                _large_buy_count = 0
                _large_sell_count = 0

                for _tr in _loc_trades[-200:]:
                    try:
                        _tr_time = float(_tr.get('time', _tr.get('T', 0)))
                        if _tr_time < _now_ms - _window_ms:
                            continue
                        _n = float(_tr.get('sz', 0)) * float(_tr.get('px', 0))
                        if _n < _large_threshold:
                            continue
                        _tr_side = _tr.get('side', '')
                        if _tr_side in ('B', 'buy'):
                            _large_buys += _n
                            _large_buy_count += 1
                        elif _tr_side in ('A', 'S', 'sell'):
                            _large_sells += _n
                            _large_sell_count += 1
                    except (ValueError, TypeError):
                        pass

                _large_total = _large_buys + _large_sells
                if _large_total > 0 and (_large_buy_count >= 4 or _large_sell_count >= 4):
                    _buy_dominance = _large_buys / _large_total

                    if _buy_dominance > 0.70 and _large_buy_count >= 4:
                        # Strong buy clustering — institutional accumulation
                        _loc_pts = min(10, 4 + _large_buy_count)
                        bull_setup += _loc_pts
                        reasons.append(
                            f"🏦 Large order cluster: {_large_buy_count} buys "
                            f"(${_large_buys:.0f}, {_buy_dominance*100:.0f}% dom) +{_loc_pts}"
                        )
                    elif _buy_dominance < 0.30 and _large_sell_count >= 4:
                        # Strong sell clustering — institutional distribution
                        _loc_pts = min(10, 4 + _large_sell_count)
                        bear_setup += _loc_pts
                        reasons.append(
                            f"🏦 Large order cluster: {_large_sell_count} sells "
                            f"(${_large_sells:.0f}, {(1-_buy_dominance)*100:.0f}% dom) +{_loc_pts}"
                        )

        # ── CONFIRMATION: MFI (Money Flow Index) — replaces DVI (Audit #11) ──
        # MFI = volume-weighted RSI. Measures conviction (money) behind price move.
        # Unlike DVI (snapshot aggression), MFI uses 14-bar lookback = smoother, less noise.
        # [AUDIT #12 FIX] MFI bearish DISABLED for SHORT signals.
        # Data: 15 SHORT trades with MFI bearish → WR 40%, PnL -$5.67.
        # Root cause: MFI on 1m altcoin candles is STRUCTURALLY always low (1-36)
        # because volume is small/lumpy and typical price drifts down.
        # MFI "bearish" is not a real signal — it's a timeframe artifact.
        # MFI bullish (>60) for LONG remains useful (WR 50%, PnL +$2.10).
        _mfi_pts = 0
        if len(closes) >= 15 and len(volumes) >= 15 and len(highs_c) >= 15 and len(lows_c) >= 15:
            # Typical price = (H + L + C) / 3
            tp = [(highs_c[i] + lows_c[i] + closes[i]) / 3 for i in range(-15, 0)]
            raw_mf = [tp[i] * volumes[len(volumes)-15+i] for i in range(15)]
            pos_mf, neg_mf = 0.0, 0.0
            for i in range(1, 15):
                if tp[i] > tp[i-1]:
                    pos_mf += raw_mf[i]
                else:
                    neg_mf += raw_mf[i]
            mfi = 100 - (100 / (1 + pos_mf / neg_mf)) if neg_mf > 0 else 100
            
            if mfi > 60:
                # Bullish MFI — useful for LONG confirmation
                _mfi_pts = min(8, int((mfi - 50) / 5))
                reasons.append(f"💰 MFI {mfi:.0f} — money flowing in (+{_mfi_pts})")
            # Bearish MFI disabled — structural bias on 1m altcoin, not a real signal
        

        # OB Absorption removed — reversal signal, not compatible with trend following strategy.
        _abs_pts = 0

        disp_mult = 1.0
        if len(closes) >= 6:
            price_5ago = closes[-6]
            if price_5ago > 0:
                displacement = (closes[-1] - price_5ago) / price_5ago
                dir_disp = displacement if bull_setup >= bear_setup else -displacement

                # [FIX 2026-05-21] Regime-aware displacement thresholds.
                # Trending market: price already moved = trend confirmation, not stale.
                # Choppy market: price already moved = chasing noise, penalize hard.
                _is_trending = trend_pct is not None and abs(trend_pct) > 0.015
                _disp_mild   = 0.010 if _is_trending else 0.003   # 1.0% trending, 0.3% choppy
                _disp_stale  = 0.020 if _is_trending else 0.005   # 2.0% trending, 0.5% choppy
                _disp_very   = 0.030 if _is_trending else 0.008   # 3.0% trending, 0.8% choppy

                if bull_setup >= bear_setup:
                    if dir_disp > _disp_very:
                        disp_mult = 0.40
                        reasons.append(f"🚫 Displacement {dir_disp*100:.2f}% — very stale (×0.40)")
                    elif dir_disp > _disp_stale:
                        disp_mult = 0.60
                        reasons.append(f"⚠️ Displacement {dir_disp*100:.2f}% — stale (×0.60)")
                    elif dir_disp > _disp_mild:
                        disp_mult = 0.80
                        reasons.append(f"📊 Displacement {dir_disp*100:.2f}% — mild (×0.80)")
                else:
                    # SHORT: only penalize extreme exhaustion
                    if dir_disp > 0.015:
                        disp_mult = 0.50
                        reasons.append(f"🚫 SHORT exhaustion {dir_disp*100:.2f}% — bounce risk (×0.50)")
                    elif dir_disp > 0.010:
                        disp_mult = 0.70
                        reasons.append(f"⚠️ SHORT extended {dir_disp*100:.2f}% — caution (×0.70)")
                    elif 0.003 < dir_disp <= 0.010:
                        disp_mult = 1.05
                        reasons.append(f"📉 SHORT trend confirmed {dir_disp*100:.2f}% (×1.05)")
                    elif dir_disp < -0.002:
                        disp_mult = 0.70
                        reasons.append(f"⚠️ SHORT counter-trend {dir_disp*100:.2f}% — price rising (×0.70)")

        # ── FINAL SCORE ASSEMBLY ──────────────────────────────────────
        # [AUDIT #6 FIX 2026-05-22] DIRECTION DECISION RESTRUCTURED
        # Data (115 trades): OB dominates direction → WR 38.8%, PnL -$7.25
        #                    OI dominates direction → WR 54.2%, PnL +$7.83
        # OB imbalance r=-0.098 vs PnL (counter-predictive for direction).
        # OI/Funding r=+0.091 vs PnL (predictive).
        #
        # FIX: Direction determined by STABLE signals only (OI/Funding + EMA + momentum).
        # OB contributes to SCORE (setup strength) but NOT to direction decision.
        # This prevents volatile OB snapshots from overriding 8h fundamental data.

        _oi_signed = int(oi_bull - oi_bear)

        # Direction votes (exclude OB — it's noise for direction, data proves it)
        _dir_bull = 0
        _dir_bear = 0

        # Vote 1: OI/Funding (strongest predictor, weight 3x)
        if _oi_signed > 3:
            _dir_bull += 3
        elif _oi_signed < -3:
            _dir_bear += 3

        # Vote 2: EMA direction (trend structure)
        if ema_bullish:
            _dir_bull += 2
        elif ema_bearish:
            _dir_bear += 2

        # Vote 3: Price momentum (5min net move — already calculated)
        _mom_5m = 0.0
        if len(closes) >= 6:
            _mom_5m = (closes[-1] - closes[-6]) / closes[-6] if closes[-6] > 0 else 0
            if _mom_5m > 0.001:
                _dir_bull += 1
            elif _mom_5m < -0.001:
                _dir_bear += 1

        # Vote 4: RSI momentum (confirms trend acceleration)
        if _c_div > 0:  # RSI momentum bullish (+8 was added to bull_setup)
            if any("price rising" in r for r in reasons):
                _dir_bull += 1
            elif any("price falling" in r for r in reasons):
                _dir_bear += 1

        # Vote 5: 1H HTF regime (higher-timeframe trend — now actually discriminates)
        if htf_regime == "TRENDING_UP":
            _dir_bull += 2
        elif htf_regime == "TRENDING_DOWN":
            _dir_bear += 2

        # Vote 6: Momentum strength confidence
        # Data: mom 0.50%+ = WR 57.6%, mom 0.30-0.50% = WR 36.4% (false breakout zone)
        # Strong momentum in one direction = higher confidence that direction is correct
        if len(closes) >= 6 and abs(_mom_5m) >= 0.005:  # 0.5%+ = strong trend
            if _mom_5m > 0:
                _dir_bull += 1
            else:
                _dir_bear += 1

        # Vote 7: Large Trade Imbalance (institutional/whale flow detection)
        # HL WS trades have: side ("B"=taker buy, "A"=taker sell), sz, px
        # Large trades (>3× median size) = institutional conviction.
        # Small trades = noise. Filter for whales only.
        _lti_trades = self.cache.trades.get(asset, []) if hasattr(self.cache, 'trades') else []
        if len(_lti_trades) >= 50:
            _recent = _lti_trades[-200:]  # last 200 trades
            _sizes = [float(t.get('sz', 0)) * float(t.get('px', 0)) for t in _recent]
            _median_sz = sorted(_sizes)[len(_sizes) // 2] if _sizes else 0
            _whale_threshold = _median_sz * 3  # 3× median = whale

            if _whale_threshold > 0:
                _whale_buy_vol = sum(
                    float(t.get('sz', 0)) * float(t.get('px', 0))
                    for t in _recent
                    if t.get('side', '') in ('B', 'buy')
                    and float(t.get('sz', 0)) * float(t.get('px', 0)) >= _whale_threshold
                )
                _whale_sell_vol = sum(
                    float(t.get('sz', 0)) * float(t.get('px', 0))
                    for t in _recent
                    if t.get('side', '') in ('A', 'S', 'sell', 'Bid')
                    and float(t.get('sz', 0)) * float(t.get('px', 0)) >= _whale_threshold
                )
                # [AUDIT #8 FIX] Minimum sample: need >=5 whale trades for statistical significance.
                # Before: 1 whale trade = 100% imbalance = always votes. 78% fire rate.
                _whale_count = sum(
                    1 for t in _recent
                    if float(t.get('sz', 0)) * float(t.get('px', 0)) >= _whale_threshold
                )
                _whale_total = _whale_buy_vol + _whale_sell_vol
                if _whale_count >= 5 and _whale_total > 0:
                    _whale_ratio = (_whale_buy_vol - _whale_sell_vol) / _whale_total
                    # [AUDIT #8 FIX] Raise threshold from 30% to 50% — need clear dominance
                    if _whale_ratio > 0.50:
                        _dir_bull += 2
                        reasons.append(f"🐋 Whale buy flow {_whale_ratio*100:.0f}% imbalance")
                    elif _whale_ratio < -0.50:
                        _dir_bear += 2
                        reasons.append(f"🐋 Whale sell flow {_whale_ratio*100:.0f}% imbalance")

        # Direction decision: if votes tied, fall back to bull_setup vs bear_setup
        if _dir_bull > _dir_bear:
            side = Side.LONG
        elif _dir_bear > _dir_bull:
            side = Side.SHORT
        else:
            # Tied — use full setup as tiebreaker (includes OB)
            side = Side.LONG if bull_setup >= bear_setup else Side.SHORT

        reasons.append(
            f"🧭 Direction: {side.value.upper()} (votes: bull={_dir_bull} bear={_dir_bear} | "
            f"OI={_oi_signed:+d} EMA={'bull' if ema_bullish else 'bear' if ema_bearish else 'flat'})"
        )

        # MFI alignment: only add confirm points if MFI direction matches chosen side
        if _mfi_pts != 0:
            if (side == Side.LONG and _mfi_pts > 0) or (side == Side.SHORT and _mfi_pts < 0):
                confirm_pts += abs(_mfi_pts)
            else:
                # MFI opposing = slight penalty (money flowing against trade)
                confirm_pts -= min(3, abs(_mfi_pts))

        # [AUDIT #8 FIX] Score must reflect conviction in the CHOSEN direction.
        # Before: max(bull, bear) → high score from OPPOSING setup = inverse predictive.
        # After: use setup aligned with direction. High score = strong evidence FOR the trade.
        aligned_setup = bull_setup if side == Side.LONG else bear_setup
        dominant_setup = aligned_setup  # keep var name for downstream log compatibility

        # ── [AUDIT #17] OB-DOMINANT EDGE — orderbook is the ONLY robust predictor ──
        # Data (56 trades POST-P0, single-user, directional/sign-corrected):
        #   orderbook  r(PnL) = +0.337 (spearman +0.286)  ← ONLY robust component
        #   net conv.  r(PnL) = +0.076 (noise)
        #   oi_funding r(PnL) = -0.072 (inverse)   RSI +0.035   session +0.091 (noise)
        # Aggregate score was INVERSE (r=-0.159) because it summed 1 predictive signal
        # (OB) with ~8 noise/inverse components — the noise drowned the edge.
        #
        # OB is a STEP FUNCTION, not linear (stored ob_dir ∈ {-6,0,6,10,18}):
        #   strong aligned wall (raw ±18) → WR 70%, PF 3.46  (n=10)
        #   mild/no wall (≤10)            → WR 24-26%, net loss (n=46)
        #   OB contradicts trade dir       → WR 0% (n=1; Audit #12 confirmed same)
        # No continuous re-weighting passed the replay gate (binary edge can't be
        # smoothed). Fix = make a STRONG ALIGNED WALL dominate the score, and let an
        # OB-contradiction sink the score below threshold (soft veto via existing gate).
        #
        # Replay (production scale): score↔PnL r = -0.159 → +0.24, top-quartile WR 64%.
        # CAVEAT: strong-wall n=10, single regime (rally), 46h window. Components are
        # NOT disabled (still contribute as confirmation) — only re-weighted toward the
        # one measurable edge. Re-validate at Audit #18 with 50+ fresh trades.
        _ob_dir = _ob_signed if side == Side.LONG else -_ob_signed  # OB aligned to trade side
        _OB_STRONG_THRESHOLD = 12   # captures strong wall (18×regime_mult), excludes mild (10)
        _OB_STRONG_BONUS     = 15
        _OB_CONTRA_PENALTY   = -20
        _ob_edge_adj = 0
        if _ob_dir >= _OB_STRONG_THRESHOLD:
            _ob_edge_adj = _OB_STRONG_BONUS
            reasons.append(
                f"🧱 Strong aligned orderbook wall (ob_dir={_ob_dir:+d}) — dominant edge (+{_OB_STRONG_BONUS})"
            )
        elif _ob_dir < 0:
            _ob_edge_adj = _OB_CONTRA_PENALTY
            reasons.append(
                f"⛔ Orderbook contradicts {side.value.upper()} (ob_dir={_ob_dir:+d}) — edge inverted ({_OB_CONTRA_PENALTY})"
            )

        # Raw score = setup (0-63) + confirmation (-15 to +31) + OB edge (±) → pre-scaling
        raw = dominant_setup + confirm_pts + _ob_edge_adj
        raw = max(0, raw)

        # Scale to 0-100: multiply by 1.6 so typical good setups (35-50 raw) reach 55-80
        # Max realistic raw ≈ 63 (all setup) + 25 (all confirm) = 88 × 1.6 = 100+
        # Typical good raw ≈ 35-45 × 1.6 = 56-72 (good trading range)
        scaled = int(raw * 1.6)

        # Apply displacement multiplier (the anti-chase mechanism)
        score = int(scaled * disp_mult)
        score = max(0, min(100, score))

        # ── Per-coin SCORE-DEBUG log (pre-regime: before vol/regime multiplier) ──
        log.info(
            f"[SCORE-DEBUG] {asset} | {side.value.upper()} pre_regime={score} | "
            f"setup={dominant_setup} confirm={confirm_pts} ob_edge={_ob_edge_adj:+d} disp={disp_mult:.2f} | "
            f"OB={_c_ob} ob_dir={_ob_dir:+d} EMA={_c_ema} RSI={_c_rsi} CVD={_c_cvd} "
            f"FUND={_c_fund} LIQ={_c_liq} MTF={_c_mtf} | "
            f"bull_s={bull_setup} bear_s={bear_setup}"
        )

        # Populate breakdown telemetry for downstream persistence
        if out_components is not None:
            out_components["ob_signed"] = int(_ob_signed)
            out_components["oi_signed"] = int(oi_bull - oi_bear)
            out_components["liq_signed"] = int(liq_bull - liq_bear)
            out_components["bull_setup"] = int(bull_setup)
            out_components["bear_setup"] = int(bear_setup)
            out_components["vote_margin"] = abs(_dir_bull - _dir_bear)
            out_components["cvd_pts"] = int(_c_cvd)
            out_components["xam_pts"] = int(_xam_pts)
            out_components["loc_pts"] = int(_loc_pts)
            out_components["abs_pts"] = int(_abs_pts)
            out_components["ema_pts"] = int(_c_ema)
            out_components["rsi_pts"] = int(_c_rsi)
            out_components["mtf_pts"] = int(_c_mtf)
            out_components["mfi_pts"] = int(_mfi_pts)

        return score, side, reasons

    # ══════════════════════════════════════════════════════════════════
    # EDGE COMPONENTS (2026-05-21)
    # ══════════════════════════════════════════════════════════════════

    def _calc_cross_asset_momentum(self, asset: str) -> Tuple[int, str]:
        """
        Cross-Asset Momentum: BTC/ETH move first, alts follow 30-120s later.
        Returns (signed_pts, reason) — positive=LONG bias, negative=SHORT bias.
        """
        if asset in ("BTC", "ETH"):
            return 0, ""
        leaders = ["BTC", "ETH"]
        leader_moves = []
        for ldr in leaders:
            history = self._price_history.get(ldr, [])
            if len(history) < 2:
                continue
            now_mono = time.monotonic()
            # [AUDIT #13 FIX] Window 5min → 7min. Catches slightly slower BTC
            # rotations without being too lagging. 5min missed moves that take
            # 5-7min to develop (common in low-vol sessions).
            pts_window = [(t, p) for t, p in history if t > now_mono - 420]  # 7min
            if len(pts_window) >= 2:
                move = (pts_window[-1][1] - pts_window[0][1]) / pts_window[0][1]
                leader_moves.append(move)
        if not leader_moves:
            return 0, ""
        avg_leader = sum(leader_moves) / len(leader_moves)
        # Check if THIS asset has already followed
        my_history = self._price_history.get(asset, [])
        my_move = 0.0
        if len(my_history) >= 2:
            now_mono = time.monotonic()
            pts_window = [(t, p) for t, p in my_history if t > now_mono - 420]
            if len(pts_window) >= 2:
                my_move = (pts_window[-1][1] - pts_window[0][1]) / pts_window[0][1]
        # Edge: leader moved significantly but alt hasn't followed proportionally
        # [AUDIT #13 FIX] Threshold 0.10% → 0.08%. Data: XAM=12 (BTC +0.50%) = WIN.
        # Lower threshold fires more often with smaller but still meaningful moves.
        # Lag threshold 50% → 60%: alt can have moved slightly, just not caught up.
        # Pts multiplier 4000 → 5000: more weight when XAM fires (proven edge).
        leader_threshold = 0.0008  # 0.08% move in 7min
        if avg_leader > leader_threshold and my_move < avg_leader * 0.6:
            pts = min(12, int(avg_leader * 5000))  # scale: 0.08%=4, 0.20%=10, 0.24%=12
            return pts, f"🔗 BTC/ETH leading +{avg_leader*100:.2f}%, {asset} lagging → LONG +{pts}"
        elif avg_leader < -leader_threshold and my_move > avg_leader * 0.6:
            pts = min(12, int(abs(avg_leader) * 5000))
            return -pts, f"🔗 BTC/ETH dumping {avg_leader*100:.2f}%, {asset} lagging → SHORT +{pts}"
        return 0, ""

    def _calc_delta_volume_imbalance(self, asset: str) -> Tuple[int, str]:
        """
        Delta Volume Imbalance: measure aggressor urgency via dollar-weighted buy/sell.
        More predictive than simple CVD because it captures SIZE of aggression.
        Returns (signed_pts, reason).
        """
        trades = self.cache.trades.get(asset, []) if hasattr(self.cache, 'trades') else []
        if len(trades) < 30:
            return 0, ""
        # Last 2 minutes of trades
        now_ms = time.time() * 1000
        recent = [t for t in trades if float(t.get("time", 0)) > now_ms - 120_000]
        if len(recent) < 15:
            return 0, ""
        buy_dollar = 0.0
        sell_dollar = 0.0
        for t in recent:
            px = float(t.get("px", 0))
            sz = float(t.get("sz", 0))
            dollar = px * sz
            side = t.get("side", "")
            if side in ("B", "buy"):
                buy_dollar += dollar
            elif side in ("A", "S", "sell"):
                sell_dollar += dollar
        total = buy_dollar + sell_dollar
        if total < 100:  # minimum $100 volume
            return 0, ""
        imbalance = (buy_dollar - sell_dollar) / total  # -1 to +1
        # [AUDIT #11 FIX] Threshold 45%→55%. 45% = 60% fire rate + r=-0.126 inverse.
        # Target: 30-40% fire. Need stronger imbalance to be meaningful.
        if imbalance > 0.55:
            pts = min(10, int(imbalance * 12))
            return pts, f"🔥 DVI aggressive buying {imbalance*100:.0f}% (${buy_dollar:.0f} vs ${sell_dollar:.0f}) +{pts}"
        elif imbalance < -0.55:
            pts = min(10, int(abs(imbalance) * 12))
            return -pts, f"🔥 DVI aggressive selling {imbalance*100:.0f}% (${sell_dollar:.0f} vs ${buy_dollar:.0f}) +{pts}"
        return 0, ""

    def _calc_liq_cluster(self, asset: str) -> Tuple[int, int, str]:
        """
        Liq Cluster Score: detect cascade events from Binance/HL stream.
        Cluster = 3+ liq events same direction within 5min, notional > $5k.
        Returns (bull_pts, bear_pts, reason).
        
        SELL liq (longs rekt) = bearish cascade pressure
        BUY liq (shorts squeezed) = bullish cascade pressure
        """
        import time as _time
        liqs = self.cache.liquidations if hasattr(self.cache, 'liquidations') else []
        now_ms = _time.time() * 1000
        # Filter: this asset, last 10 minutes
        recent = [e for e in liqs 
                  if e.get("coin", e.get("asset", "")) == asset
                  and float(e.get("time", 0)) > now_ms - 600_000]
        if len(recent) < 2:
            return 0, 0, ""
        # Split by liquidated side
        long_liqs = [e for e in recent if e.get("side") in ("long", "SELL")]
        short_liqs = [e for e in recent if e.get("side") in ("short", "BUY")]
        
        def cluster_notional(events):
            """Find densest 10-min window, return (count, total_notional)."""
            if len(events) < 2:
                return 0, 0
            events = sorted(events, key=lambda e: float(e.get("time", 0)))
            # For altcoins, use full 10min window (events are sparse)
            notional = sum(float(e.get("px", e.get("price", 0))) * float(e.get("sz", e.get("size", 0))) for e in events)
            return len(events), notional
        
        long_n, long_not = cluster_notional(long_liqs)   # longs rekt → bearish
        short_n, short_not = cluster_notional(short_liqs) # shorts squeezed → bullish
        
        def pts_from_notional(n):
            if n >= 20_000: return 12
            if n >= 8_000: return 8
            if n >= 2_000: return 4
            return 0
        
        bear_pts = pts_from_notional(long_not)   # longs getting rekt = bearish
        bull_pts = pts_from_notional(short_not)  # shorts getting squeezed = bullish
        
        if bull_pts == 0 and bear_pts == 0:
            return 0, 0, ""
        parts = []
        if bull_pts > 0:
            parts.append(f"{short_n} shorts squeezed (${short_not:.0f}) +{bull_pts}")
        if bear_pts > 0:
            parts.append(f"{long_n} longs rekt (${long_not:.0f}) +{bear_pts}")
        reason = f"💥 Liq cluster: {' | '.join(parts)}"
        return bull_pts, bear_pts, reason

    def _calc_ob_absorption(self, asset: str) -> Tuple[int, str]:
        """
        Orderbook Absorption: detect walls that HOLD under pressure.
        A bid wall that survives selling = institutional support.
        Returns (signed_pts, reason).
        """
        ob = self.cache.orderbook.get(asset) if hasattr(self.cache, 'orderbook') else None
        if not ob:
            return 0, ""
        trades = self.cache.trades.get(asset, []) if hasattr(self.cache, 'trades') else []
        try:
            levels = ob.get("levels", [[], []]) if isinstance(ob, dict) else [[], []]
            bids_raw = levels[0][:10] if len(levels) > 0 else []
            asks_raw = levels[1][:10] if len(levels) > 1 else []
            def parse_lvl(x):
                if isinstance(x, dict):
                    return float(x.get("px", 0)), float(x.get("sz", 0))
                try: return float(x[0]), float(x[1])
                except: return 0.0, 0.0
            bids = [parse_lvl(b) for b in bids_raw]
            asks = [parse_lvl(a) for a in asks_raw]
            if not bids or not asks:
                return 0, ""
            # Find largest wall
            bid_wall = max(bids, key=lambda x: x[0]*x[1]) if bids else (0, 0)
            ask_wall = max(asks, key=lambda x: x[0]*x[1]) if asks else (0, 0)
            bid_wall_dollar = bid_wall[0] * bid_wall[1]
            ask_wall_dollar = ask_wall[0] * ask_wall[1]
            # Check if wall is being tested (recent trades near wall price)
            now_ms = time.time() * 1000
            recent_sells = [t for t in trades
                           if float(t.get("time", 0)) > now_ms - 180_000
                           and t.get("side", "") in ("S", "sell", "Bid")]
            recent_buys = [t for t in trades
                          if float(t.get("time", 0)) > now_ms - 180_000
                          and t.get("side", "") in ("B", "buy", "Ask")]
            # Bid wall absorption: selling happened near bid wall but wall still stands
            if bid_wall_dollar > 500:  # minimum $500 wall
                sell_near_wall = sum(
                    float(t.get("sz", 0)) * float(t.get("px", 0))
                    for t in recent_sells
                    if abs(float(t.get("px", 0)) - bid_wall[0]) / bid_wall[0] < 0.002
                )
                if sell_near_wall > bid_wall_dollar * 0.3:
                    # Wall absorbed 30%+ of its size in selling → strong support
                    pts = min(10, int(sell_near_wall / bid_wall_dollar * 10))
                    return pts, f"🧱 Bid wall ${bid_wall_dollar:.0f} absorbing sells (${sell_near_wall:.0f} absorbed) +{pts}"
            # Ask wall absorption
            if ask_wall_dollar > 500:
                buy_near_wall = sum(
                    float(t.get("sz", 0)) * float(t.get("px", 0))
                    for t in recent_buys
                    if abs(float(t.get("px", 0)) - ask_wall[0]) / ask_wall[0] < 0.002
                )
                if buy_near_wall > ask_wall_dollar * 0.3:
                    pts = min(10, int(buy_near_wall / ask_wall_dollar * 10))
                    return -pts, f"🧱 Ask wall ${ask_wall_dollar:.0f} absorbing buys (${buy_near_wall:.0f} absorbed) +{pts}"
        except Exception:
            pass
        return 0, ""

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
        bull_setup: int = 0,
        bear_setup: int = 0,
        # [2026-05-21] Sub-component detail + momentum gate + HTF regime
        sub_components: dict = None,
        momentum_gate_passed: bool = None,
        momentum_move_pct: float = 0.0,
        momentum_candles: str = "",
        htf_regime: str = "",
        htf_threshold_adj: int = 0,
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

        # ── [AUDIT #16] HOLD-AWARE SL (vol-scaled to expected swing in hold window) ──
        # Bug fix: ATR-SL above is per-minute scaled; for 10-25 min hold window,
        # SL distance must cover expected swing AT HOLD HORIZON, not per-minute.
        #
        # Data evidence:
        #   RV bucket 0-3%   → avg SL/RV ratio 0.318 (over-protective)
        #   RV bucket 9-15%  → avg SL/RV ratio 0.078 (severely under-protective)
        #   At RV 8%, hold 12min → expected std swing ≈ 0.7%. Old SL 0.6-0.8% =
        #   exactly at 1× std dev → 50% probability of getting whipsawed by
        #   normal noise WITHOUT trend break. 5 real SL hits in POST = $-10.15.
        #
        # Formula: SL = SL_NOISE_MULT × realized_vol × sqrt(hold_min / minutes_per_day)
        # Default SL_NOISE_MULT=2.5 → SL covers ~2 std dev = ~95% noise tolerance.
        # Hold time picked here matches score-bucket time_exit_min later in this fn.
        if realized_vol > 0:
            # Pre-compute hold_min based on score (matches matrix below at line ~2360)
            if score >= 66 or (fade_mode and score >= 60):
                _hold_min_est = 25
            elif score >= 61:
                _hold_min_est = 20
            elif score >= 56:
                _hold_min_est = 15
            else:
                _hold_min_est = 10

            SL_NOISE_MULT = getattr(scfg, 'sl_noise_mult', 2.5)
            _expected_swing = realized_vol * (_hold_min_est / (60.0 * 24.0)) ** 0.5
            _hold_sl_pct   = _expected_swing * SL_NOISE_MULT

            _hard_min = getattr(scfg, 'sl_pct_min', 0.005)
            _hard_max = max(getattr(scfg, 'sl_pct_max', 0.020), 0.025)  # raise ceiling for high-vol
            _hold_sl_pct = max(_hard_min, min(_hold_sl_pct, _hard_max))

            # Take MAX of (per-minute ATR-driven, hold-aware): protect against under-sized SL
            # at high vol while keeping ATR-driven calculation for low-vol consistency.
            _final_sl_pct = max(sl_pct, _hold_sl_pct)
            if _final_sl_pct != sl_pct:
                log.info(
                    f"[HOLD-SL] {asset} | rv={realized_vol*100:.2f}% hold={_hold_min_est}m | "
                    f"expected_swing={_expected_swing*100:.3f}% × {SL_NOISE_MULT} = {_hold_sl_pct*100:.3f}% | "
                    f"old_sl={sl_pct*100:.3f}% → new_sl={_final_sl_pct*100:.3f}%"
                )
            sl_pct = _final_sl_pct

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
            # HIGH CONVICTION → TP1 dekat (achievable 20m), TP2 lebih jauh untuk runner
            sl_pct = max(sl_pct, 0.010)
            sl_pct = min(sl_pct, _sl_max)
            tp1_pct = max(tp1_pct, sl_pct * 0.6)   # 0.6% — achievable dalam 20m
            tp2_pct = max(tp2_pct, sl_pct * 1.5)   # 1.5% — runner target
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

        # [AUDIT #12 FIX] SHORT-specific exit adjustments.
        # Data: SHORT avg favorable move = 0.39% (vs LONG 0.74%).
        # SHORT winners hold avg 13.2min (vs LONG 13.8min — similar).
        # Problem: TP1 same as LONG but SHORT moves are smaller/slower.
        # Fix: Lower TP1 for SHORT (easier to reach) + slightly more hold time.
        if side == Side.SHORT:
            # TP1 reduction: SHORT needs lower target to activate trailing
            # Data: 0/22 failed SHORT trades even got close to 0.3% move
            tp1_pct = tp1_pct * 0.70  # e.g. 0.6% → 0.42%, 0.8% → 0.56%
            # Hold time extension: SHORT winners need 13.2min avg
            time_exit_min = time_exit_min + 3  # give 3 extra minutes
            reasons.append(f"📉 SHORT exit adj: TP1×0.70={tp1_pct*100:.2f}%, hold+3={time_exit_min}m")

        # [AUDIT #13 FIX] LONG TP1 reduction in ranging/choppy regime.
        # Data: LONG trailing dropped 53.8% → 30.0%. TP1=0.85% unreachable in choppy.
        # SHORT fix (×0.70) worked brilliantly — apply similar logic to LONG but gentler.
        # ×0.80 (not ×0.70) because LONG moves are naturally larger than SHORT in crypto.
        if side == Side.LONG and htf_regime == "CHOPPY":
            tp1_pct = tp1_pct * 0.80  # e.g. 0.6% → 0.48%, 0.8% → 0.64%
            reasons.append(f"📈 LONG choppy adj: TP1×0.80={tp1_pct*100:.2f}% (easier trailing activation)")

        # Fade mode override: contrarian entry = tighter SL, wider TP (asymmetric RR)
        if fade_mode:
            sl_pct = sl_pct * 0.8
            tp2_pct = tp2_pct * 1.5
            time_exit_min = min(time_exit_min, 12)
            reasons.append("FADE: tight SL 0.8×, wide TP 1.5×, max 12min")

        # Leverage hanya dari config — HL exchange cap dihapus.
        # User cap diterapkan di executor (paper/bitget), bukan di signal.
        leverage = min(scfg.default_leverage, scfg.max_leverage)

        # [AUDIT FIX 2026-05-21] TP display must match actual exit trigger in risk_manager.
        # Actual trigger = sl_distance × partial_tp1/tp2_at_sl_multiple.
        # Old tp1_pct/tp2_pct from score matrix were NEVER used for exit decisions.
        _tp1_mult = getattr(scfg, 'partial_tp1_at_sl_multiple', 0.7)
        _tp2_mult = getattr(scfg, 'partial_tp2_at_sl_multiple', 1.0)

        if side == Side.LONG:
            stop_loss = round(mark_price * (1 - sl_pct), 8)
            tp1       = round(mark_price * (1 + sl_pct * _tp1_mult), 8)
            tp2       = round(mark_price * (1 + sl_pct * _tp2_mult), 8)
        else:
            stop_loss = round(mark_price * (1 + sl_pct), 8)
            tp1       = round(mark_price * (1 - sl_pct * _tp1_mult), 8)
            tp2       = round(mark_price * (1 - sl_pct * _tp2_mult), 8)

        strength = SignalStrength.STRONG if score >= 70 else SignalStrength.MODERATE
        breakdown = ScoreBreakdown(
            oi_funding_score=oi_signed,
            liquidation_score=liq_signed,
            orderbook_score=ob_signed,
            raw_score=score,
            final_score=score,
            session_bonus=session_bonus,
            total_bull=bull_setup,
            total_bear=bear_setup,
            reasons=reasons,
            components=sub_components or {},
            momentum_gate_passed=momentum_gate_passed,
            momentum_move_pct=momentum_move_pct,
            momentum_candles=momentum_candles,
            htf_regime=htf_regime,
            htf_threshold_adj=htf_threshold_adj,
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
            sells = sum(float(t.get('sz', 0)) for t in recent_trades if t.get('side') in ('A', 'S'))
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
            
            # [AUDIT #11 FIX] Old thresholds (6%/12%) = ALL altcoins permanently HIGH_VOL.
            # Crypto altcoin normal daily vol = 5-12%. Only penalize true spikes.
            # Data: GRASS 14.4%, GMT 12.8%, ZEC 10%, NEAR 9.4% = all "normal" for alts.
            if realized_vol < 0.02:
                regime = MarketRegime.LOW_VOL
            elif realized_vol < 0.10:
                regime = MarketRegime.NORMAL
            elif realized_vol < 0.18:
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
    # 1H REGIME DETECTION (was 4H — Audit #14 redesign)
    # ──────────────────────────────────────────

    async def _fetch_1h_regime(self, asset: str) -> str:
        """
        Classify 1H market regime for scalper (12-min hold window).
        Returns: "TRENDING_UP" | "TRENDING_DOWN" | "CHOPPY"

        [AUDIT #14 REDESIGN] Was 4H — always returned CHOPPY (98% of time).
        Root cause: 4H EMA + strength 0.30 threshold too strict for crypto.
        
        New design (1H):
          - Fetch last 24 × 1h candles (24 hours)
          - EMA8 vs EMA21 on 1h closes → direction (faster response)
          - Strength threshold 0.15 (was 0.30 — crypto is inherently volatile)
          - EMA gap 0.1% (was 0.2% — more sensitive to trend changes)
          - Cache 15 minutes (was 4h — scalper needs fresher regime data)
          
        Expected: ~40-60% CHOPPY (was 98%), rest split TRENDING_UP/DOWN.
        This gives the threshold/leverage adjustments actual discriminating power.
        """
        cache_key = f"1h_{asset}"
        cached = self._vol_cache.get(cache_key)
        if cached and (time.monotonic() - cached[0]) < 900:  # 15min cache (was 4h)
            return cached[1]

        try:
            async with self.candle_sem:
                await asyncio.sleep(0.2)
                now_ms   = int(time.time() * 1000)
                start_ms = now_ms - (24 * 3600 * 1000)  # 24 × 1h candles
                resp, succ = await self.client._call_info_endpoint(
                    "candleSnapshot",
                    {"req": {"coin": asset, "interval": "1h",
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

            # [FIX A+B] EMA8/21 (faster than 10/20) + lower gap threshold
            ema8  = _ema(closes, 8) if len(closes) >= 8 else closes[-1]
            ema21 = _ema(closes, 21) if len(closes) >= 21 else closes[-1]

            # [FIX A] Strength threshold 0.15 (was 0.30)
            # Crypto moves 0.5-2% per hour routinely. 30% strength = only parabolic moves.
            # 15% = moderate directional bias, appropriate for 1H timeframe.
            n = min(len(closes), 8)  # last 8 hours (was 10 candles of 4H = 40 hours)
            total_range = sum(highs[-n:][i] - lows[-n:][i] for i in range(n))
            net_move    = abs(closes[-1] - closes[-n])
            strength    = net_move / total_range if total_range > 0 else 0

            STRENGTH_THRESHOLD = 0.15  # [FIX A] was 0.30

            # [FIX B] EMA gap 0.1% (was 0.2%)
            # 0.2% gap on 4H = needs multi-day trend. 0.1% on 1H = few hours of direction.
            EMA_GAP = 1.001  # was 1.002

            if ema8 > ema21 * EMA_GAP and strength >= STRENGTH_THRESHOLD:
                regime = "TRENDING_UP"
            elif ema8 < ema21 * (2 - EMA_GAP) and strength >= STRENGTH_THRESHOLD:
                regime = "TRENDING_DOWN"
            else:
                regime = "CHOPPY"

            self._vol_cache[cache_key] = (time.monotonic(), regime)
            log.info(
                f"[1H-REGIME] {asset}: {regime} | EMA8={ema8:.4f} EMA21={ema21:.4f} "
                f"strength={strength:.3f} (thr={STRENGTH_THRESHOLD})"
            )
            return regime

        except Exception as e:
            log.debug(f"[1H-REGIME] {asset}: fetch failed ({e}), defaulting CHOPPY")
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

