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

from config import SIGNAL, RISK
from models.schemas import (
    TradeSignal, ScoreBreakdown, SignalStrength, Side, MarketRegime,
    FundingData, OIData, OrderbookSnapshot, LiquidationMap
)
from engine.analyzers.oi_funding_analyzer  import OIFundingAnalyzer
from engine.analyzers.liquidation_analyzer import LiquidationAnalyzer
from engine.analyzers.orderbook_analyzer   import OrderbookAnalyzer
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
        self._oi_snapshots: Dict[str, list] = {}

        # Volatility cache: asset -> (ts, regime, realized_vol, trend)
        self._vol_cache: Dict[str, tuple] = {}
        
        # Spot-Perp basis cache (global)
        self._spot_prices: Dict[str, float] = {}
        self._spot_cache_time: float = 0.0

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

    async def run_asset(self, asset: str, active_modes: List[str] = None, meta_data=None) -> Dict[str, TradeSignal]:
        """
        Run full scoring pipeline for one asset.
        Computes standard and/or scalper score independently based on active_modes.
        Returns dictionary of signals by mode.
        """
        import config
        if active_modes is None:
            active_modes = ["standard"]
            
        signals = {}
        now_ts = time.monotonic()

        # 1. SCALPER MODE
        if "scalper" in active_modes:
            cooldown_secs = 5 * 60
            if hasattr(config, 'SCALPER') and hasattr(config.SCALPER, 'signal_cooldown_minutes'):
                cooldown_secs = config.SCALPER.signal_cooldown_minutes * 60
            
            last_ts = self._last_signal_ts.get(f"{asset}_scalper", 0)
            if now_ts - last_ts >= cooldown_secs:
                sig = await self._run_scalper(asset, meta_data)
                if sig:
                    signals["scalper"] = sig
                    self._last_signal_ts[f"{asset}_scalper"] = now_ts
            else:
                remaining = int(cooldown_secs - (now_ts - last_ts))
                log.debug(f"{asset} [SCALPER]: cooldown active ({remaining}s remaining)")

        # 2. STANDARD MODE
        if "standard" in active_modes:
            cooldown_secs = config.SIGNAL.signal_cooldown_minutes * 60
            last_ts = self._last_signal_ts.get(f"{asset}_standard", 0)
            if now_ts - last_ts >= cooldown_secs:
                sig = await self._run_standard(asset, meta_data)
                if sig:
                    signals["standard"] = sig
                    self._last_signal_ts[f"{asset}_standard"] = now_ts
            else:
                remaining = int(cooldown_secs - (now_ts - last_ts))
                log.debug(f"{asset} [STANDARD]: cooldown active ({remaining}s remaining)")

        return signals

    # ──────────────────────────────────────────
    # SCALPER SCORING
    # ──────────────────────────────────────────

    async def _run_scalper(self, asset: str, meta_data=None) -> Optional[TradeSignal]:
        """
        Ultra-fast scoring for Scalper Mode.
        Uses: Orderbook Imbalance, CVD, EMA8/21, RSI(14), Volume Surge.
        Targets score >= 45 (vs 56 for standard).
        """
        import config

        # 1. Get mark price
        mark_price = await self.client.get_mark_price(asset, meta=meta_data)
        if mark_price <= 0:
            return None

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

        # 3. Compute scalper indicators
        score, side, reasons = self._calculate_scalper_score(asset, mark_price, candles)

        if score < config.SCALPER.min_score_to_enter:
            log.debug(f"[SCALPER] {asset}: score {score} < {config.SCALPER.min_score_to_enter}")
            return None

        # 4. Build signal with scalper TP/SL
        signal = self._build_scalper_signal(asset, side, score, mark_price, reasons)
        log.info(f"⚡ SCALPER SIGNAL: {asset} {side.value.upper()} score={score}")
        return signal

    def _calculate_scalper_score(self, asset: str, mark_price: float, candles: list) -> Tuple[int, Side, List[str]]:
        """
        Fast scalper scoring using technical indicators on 1m candles.
        Returns (score: int, side: Side, reasons: List[str])
        """
        score = 0
        bull_pts = 0
        bear_pts = 0
        reasons = []

        # ── Orderbook Imbalance (from cache) ─────────────────────────
        ob = self.cache.orderbooks.get(asset) if hasattr(self.cache, 'orderbooks') else None
        if ob and hasattr(ob, 'bid_ask_imbalance'):
            imb = ob.bid_ask_imbalance
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
            # Cannot compute EMA/RSI without data → neutral score
            total = bull_pts + bear_pts
            side = Side.LONG if bull_pts >= bear_pts else Side.SHORT
            return min(total, 100), side, reasons

        # Extract close prices
        closes = []
        volumes = []
        for c in candles:
            if isinstance(c, dict):
                try:
                    closes.append(float(c.get("c", 0)))
                    volumes.append(float(c.get("v", 0)))
                except (ValueError, TypeError):
                    pass

        if len(closes) < 10:
            side = Side.LONG if bull_pts >= bear_pts else Side.SHORT
            return min(bull_pts + bear_pts, 100), side, reasons

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
            buy_vol = sum(t.get('sz', 0) for t in sample if t.get('side', '') in ('B', 'buy', 'Ask'))
            sell_vol = sum(t.get('sz', 0) for t in sample if t.get('side', '') in ('S', 'sell', 'Bid'))
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

        # ── Final tally ───────────────────────────────────────────────
        side = Side.LONG if bull_pts >= bear_pts else Side.SHORT
        raw = (bull_pts if side == Side.LONG else bear_pts)
        score = min(raw, 100)
        return score, side, reasons

    def _build_scalper_signal(self, asset: str, side: Side, score: int, mark_price: float, reasons: list) -> TradeSignal:
        """Build a TradeSignal with scalper-specific TP/SL levels."""
        import config
        from models.schemas import SignalStrength, MarketRegime, ScoreBreakdown
        scfg = config.SCALPER

        sl_pct  = scfg.sl_pct
        tp1_pct = scfg.tp1_pct
        tp2_pct = scfg.tp2_pct
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
        breakdown = ScoreBreakdown(raw_score=score, final_score=score, reasons=reasons)

        return TradeSignal(
            signal_id=str(uuid.uuid4())[:8].upper(),
            asset=asset,
            side=side,
            score=score,
            strength=strength,
            regime=MarketRegime.UNKNOWN,
            breakdown=breakdown,
            entry_price=mark_price,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            suggested_leverage=leverage,
        )

    # ──────────────────────────────────────────
    # STANDARD SCORING (existing pipeline)
    # ──────────────────────────────────────────

    async def _run_standard(self, asset: str, meta_data=None) -> Optional[TradeSignal]:
        """Standard scoring pipeline (OI, Funding, Liquidation, Orderbook)."""
        
        # 1. Fetch allMids ONCE per cycle for Spot-Perp basis (cached 10s by client)
        now_mono = time.monotonic()
        if not self._spot_prices or (now_mono - self._spot_cache_time) > 10:
            self._spot_prices = await self.client.get_all_mids()
            self._spot_cache_time = now_mono

        mark_price = await self.client.get_mark_price(asset, meta=meta_data)
        if mark_price <= 0:
            log.warning(f"{asset}: invalid mark price, skipping")
            return None

        # Funding data — REQUIRED
        try:
            funding = await self.client.get_funding_data(asset, meta=meta_data)
            log.debug(f"[{asset}] funding_rate={funding.funding_rate:.6f}")
        except Exception as e:
            log.error(f"[{asset}] Cannot fetch funding: {e}")
            return None  # skip this asset entirely

        # OI data — REQUIRED
        try:
            oi = await self.client.get_oi_data(asset, meta=meta_data)
            log.debug(
                f"[{asset}] oi_usd={oi.open_interest:,.0f} "
                f"change={oi.oi_change_pct:.4f}"
            )
        except Exception as e:
            log.error(f"[{asset}] Cannot fetch OI: {e}")
            return None

        # Orderbook — OPTIONAL (PROACTIVELY USE WS CACHE FIRST TO SAVE REST CALLS)
        ob_snap = None
        try:
            # Task: Use WS Cache for 100 markets efficiency
            ws_book = self.cache.orderbook.get(asset)
            if ws_book:
                # Convert raw WS book to OrderbookSnapshot
                from models.schemas import OrderbookSnapshot
                levels = ws_book.get("levels", [[], []])
                bids = [[float(b[0]), float(b[1])] for b in levels[0][:20]]
                asks = [[float(a[0]), float(a[1])] for a in levels[1][:20]]
                if bids and asks:
                    mid = (bids[0][0] + asks[0][0]) / 2
                    bid_liq = sum(b[0] * b[1] for b in bids)
                    ask_liq = sum(a[0] * a[1] for a in asks)
                    imbalance = (bid_liq - ask_liq) / (bid_liq + ask_liq) if (bid_liq + ask_liq) else 0
                    ob_snap = OrderbookSnapshot(
                        asset=asset, bids=bids, asks=asks, mid_price=mid,
                        spread_pct=(asks[0][0] - bids[0][0])/mid,
                        bid_ask_imbalance=imbalance, vwap=mid, vwap_deviation_pct=0
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

        # Store OI snapshot for change calculation
        now_ts = time.monotonic()
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
            return None

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
            f"🔍 [DIAG] {asset}: funding={funding.funding_rate:.7f}, "
            f"basis={basis:.4f}, cvd={cvd_val:.1f}, "
            f"ob_imbal={ob_snap.bid_ask_imbalance if ob_snap else 0.0:.3f}"
        )

        # ── Tally total bull vs bear evidence ──────────────────────────
        total_bull = oi_bull + liq_bull + ob_bull
        total_bear = oi_bear + liq_bear + ob_bear

        # ── Direction decided HERE after all evidence is in ────────────
        # raw_score = winning side score + confidence margin
        # The margin between bull and bear represents conviction strength
        margin = abs(total_bull - total_bear)
        confidence_bonus = min(margin * 2, 15)  # up to 15pts for strong conviction

        if total_bull > total_bear:
            side = Side.LONG
            raw_score = total_bull + confidence_bonus
        elif total_bear > total_bull:
            side = Side.SHORT
            raw_score = total_bear + confidence_bonus
        else:
            # True tie — skip, wait for clearer signal
            log.debug(
                f"{asset}: tied bull={total_bull} bear={total_bear}, skipping"
            )
            return None

        # Add session bonus
        raw_score += session_bonus
        raw_score = max(0, min(raw_score, 85))  # cap before multiplier

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
        all_reasons  = oi_reasons + liq_reasons + ob_reasons + session_reasons
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
            session_bonus=session_bonus,
            regime_multiplier=final_multiplier,
            total_bull=total_bull,
            total_bear=total_bear,
            raw_score=raw_score,
            final_score=final_score,
            reasons=all_reasons,
            warnings=all_warnings,
        )

        # ── Format Combat Report ────────────────────────────────────────
        # Format: CC | Score: XX/52 | (OI+XX Liq+XX OB+XX Ses+XX) | Regime: XXX
        breakdown_str = (
            f"(OI:{oi_bull+oi_bear:+} Liq:{liq_bull+liq_bear:+} "
            f"OB:{ob_bull+ob_bear:+} Ses:{session_bonus:+})"
        )
        
        log_msg = (
            f"{asset:5} | {side.value.upper():5} | "
            f"Score: {final_score:2d}/{SIGNAL.min_score_to_signal} | "
            f"{breakdown_str} | Regime: {log_regime.value}"
        )
        
        # ── Output Logic ───────────────────────────────────────────────
        threshold = 30 # Internal capture threshold (per-user filtering happens in main.py)
        
        if final_score >= threshold:
            log.info(f"🎯 [SIGNAL] {log_msg}")
        elif final_score >= 25:
            # Report scan results for active coins to keep the terminal "alive"
            log.info(f"🔍 [SCAN]   {log_msg}")
        else:
            log.debug(log_msg)

        # ── Check threshold ────────────────────────────────────────────
        if final_score < threshold:
            log.debug(
                f"{asset}: score {final_score} below internal capture threshold {threshold}"
            )
            return None

        # ── Build signal ───────────────────────────────────────────────
        signal = self._build_signal(
            asset, side, final_score, log_regime, breakdown, mark_price,
            oi_usd=oi.open_interest,
            vol_regime=vol_regime.value if hasattr(vol_regime, "value") else vol_regime
        )

        log.info(
            f"🎯 SIGNAL: {asset} {side.value.upper()} "
            f"score={final_score} strength={signal.strength.value}"
        )
        return signal

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
            # Use candle_semaphore (3) as requested
            async with self.candle_sem:
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
        oi_usd: float = 0.0,
        vol_regime: str = "normal",
    ) -> TradeSignal:
        # Determine strength
        if score >= 75:
            strength = SignalStrength.STRONG
        elif score >= 60:
            strength = SignalStrength.MODERATE
        else:
            strength = SignalStrength.WEAK

        # Calculate SL
        import config
        sl_pct = config.RISK.paper_sl_pct if config.TRADE_MODE == "paper" else config.RISK.default_sl_pct
        
        # ── DYNAMIC TP (Solution 2) ───────────────────────────────────
        # Get dynamic levels from RiskManager
        tp1_pct, tp2_pct = self.risk_mgr.get_dynamic_tp_levels(asset, oi_usd, vol_regime)

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
