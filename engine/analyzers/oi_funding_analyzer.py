"""
KARA Bot - OI + Funding Rate Analyzer
Edge component #1: Detect crowding, squeeze probability, funding divergence.
Returns bull/bear scores so direction is determined AFTER all analysis.

v3 2026-04-05: Recalibrated for real Hyperliquid data ranges:
- Funding rates are typically +-0.00002, not +-0.0003
- Added OI magnitude scoring for conviction differentiation
- Neutral funding now uses smaller price_change threshold
"""

from __future__ import annotations
import logging
import math
from typing import Dict, List, Tuple

from config import SIGNAL
from models.schemas import FundingData, OIData, Side

log = logging.getLogger("kara.oi_funding")


class OIFundingAnalyzer:
    """
    Analyzes Open Interest + Funding Rate to detect:
    - CROWDED longs/shorts via extreme funding LEVEL -> CONTRARIAN signal
      (extreme positive funding adds BEAR points; extreme negative adds BULL points)
    - Funding SLOPE (recent trend) -> directional momentum signal (LONG-friendly when rising)
    - OI expansion with price move (trend confirmation)
    - Predicted-vs-actual funding shift -> contrarian (where positioning is heading)
    - Spot-Perp basis -> directional premium signal
    - OI magnitude -> threshold modifier (handled by scoring engine)

    Returns (bull, bear, reasons, warnings) — NO pre-determined direction.
    """

    def analyze(
        self,
        asset: str,
        funding: FundingData,
        oi: OIData,
        funding_history: List[float],
        price_change_1h: float,
        mark_price: float,
        spot_price: float,
        price_change_5m: float = None,
    ) -> Tuple[int, int, List[str], List[str]]:
        bull     = 0
        bear     = 0
        reasons  = []
        warnings = []

        fr = funding.funding_rate
        log.debug(f"[FUNDING] {asset}: rate={fr:.6f} (is this 0.0? then API fetch failed)")

        # ── 1. Funding Rate — PURE CONTRARIAN (no mild-direction bias) ──
        # [AUDIT #19 ROOT CAUSE FIX 2026-06-04]
        #
        # DATA: 169 trade, OI/Funding r=-0.179 (p=0.020, SIGNIFIKAN).
        # - LONG+OI bullish(5-8): 89 trade WR 33.7%, -$29.31 (BLEED)
        # - LONG+OI bearish(<=-4): 4 trade WR 100%, +$4.84
        # - Per regime: TRENDING_UP r=-0.414, CHOPPY r=-0.258
        #
        # ROOT CAUSE: "mild positive funding → bull +8" fire 38% waktu,
        # memberi constant LONG bias. Di HL, funding > 0.005%/8h = NORMAL
        # (bukan signal). Bot masuk LONG di posisi yang sudah crowded.
        #
        # FIX: Funding level HANYA contrarian. Mild = ZERO poin.
        # Rationale trader futures: funding LEVEL = cost of carry = positioning.
        # - Tinggi positif = longs bayar shorts = longs crowded = FADE short.
        # - Tinggi negatif = shorts bayar longs = shorts crowded = SQUEEZE long.
        # - Mild = noise, bukan edge. Tidak kasih arah.
        # Funding SLOPE (Section 2) tetap directional — itu momentum asli.
        #
        if fr > SIGNAL.funding_extreme_threshold * 2:     # > 0.0006
            bear += 8    # contrarian: EXTREME crowded longs
            reasons.append(
                f"⚠️ EXTREME positive funding {fr*100:.4f}%/8h - longs over-crowded -> contrarian BEARISH +8"
            )
            log.info(
                f"[FUNDING-CONTRA] {asset} | fr={fr:.6f} | extreme>2x | bias=bear | pts=8"
            )
        elif fr > SIGNAL.funding_extreme_threshold:        # > 0.0003
            bear += 5    # contrarian: heavy long positioning
            reasons.append(
                f"⚠️ HIGH positive funding {fr*100:.4f}%/8h -> longs crowded, contrarian SHORT +5"
            )
            log.info(
                f"[FUNDING-CONTRA] {asset} | fr={fr:.6f} | high | bias=bear | pts=5"
            )
        elif fr < -SIGNAL.funding_extreme_threshold * 2:   # < -0.0006
            bull += 8    # contrarian: EXTREME crowded shorts → squeeze
            reasons.append(
                f"⚠️ EXTREME negative funding {fr*100:.4f}%/8h - shorts over-crowded -> SQUEEZE +8"
            )
            log.info(
                f"[FUNDING-CONTRA] {asset} | fr={fr:.6f} | extreme<-2x | bias=bull | pts=8"
            )
        elif fr < -SIGNAL.funding_extreme_threshold:       # < -0.0003
            bull += 5    # contrarian: heavy short positioning
            reasons.append(
                f"⚠️ HIGH negative funding {fr*100:.4f}%/8h -> shorts crowded, contrarian LONG +5"
            )
            log.info(
                f"[FUNDING-CONTRA] {asset} | fr={fr:.6f} | high neg | bias=bull | pts=5"
            )
        else:
            # [AUDIT #19] Mild funding = NO POINTS. Not a signal for scalper.
            # Data: mild_pos fired 38% = constant bias, not edge.
            reasons.append(f"Flat/noise funding {fr*100:.4f}%/8h -> no signal")

        # ── 2. Funding trend (slope of last 8) ────────────────────────
        # [AUDIT #19] Slope = REAL momentum signal (unlike level).
        # Keep directional, but cap at ±4 pts (secondary, not primary).
        if len(funding_history) >= 8:
            y = funding_history[-8:]
            x = list(range(8))
            n = 8
            sum_x = sum(x)
            sum_y = sum(y)
            sum_xy = sum(x[i]*y[i] for i in range(n))
            sum_xx = sum(x[i]*x[i] for i in range(n))
            
            denom = (n * sum_xx - sum_x**2)
            if denom != 0:
                slope = (n * sum_xy - sum_x * sum_y) / denom
                
                trend_str = "rising" if slope > 0 else "falling"
                log.debug(f"[FTRD] {asset}: funding_slope={slope:.6f}, trend={trend_str}")
                
                # Positive slope = funding rising = longs getting more aggressive
                if slope > 0.000005:
                    pts = min(int(slope * 400000), 4)  # [AUDIT #19] cap 8→4
                    pts = max(pts, 1)
                    bull += pts
                    reasons.append(f"• Funding trend: 📈 RISING (slope {slope:.6f}) -> LONG pressure +{pts}")
                elif slope < -0.000005:
                    pts = min(int(abs(slope) * 400000), 4)  # [AUDIT #19] cap 8→4
                    pts = max(pts, 1)
                    bear += pts
                    reasons.append(f"• Funding trend: 📉 FALLING (slope {slope:.6f}) -> SHORT pressure +{pts}")
                else:
                    reasons.append(f"• Funding trend: ⚖️ STABLE (slope {slope:.6f}) -> Neutral")

        # ── 3. Predicted vs actual — CONTRARIAN (minor signal) ───────
        # [AUDIT #19] Reduce weight: predicted shift is noisy, +1 max.
        if funding.predicted_rate is not None:
            pred_diff = funding.predicted_rate - fr
            if abs(pred_diff) > 0.0001:  # only meaningful shift
                if pred_diff > 0:
                    bear += 1
                    reasons.append(
                        f"Predicted funding shifting positive -> longs crowding -> contrarian bear +1"
                    )
                else:
                    bull += 1
                    reasons.append(
                        f"Predicted funding shifting negative -> shorts crowding -> contrarian bull +1"
                    )

        # ── 4. OI Change Analysis (graduated scoring) ──────────────────
        # [AUDIT #19 FIX 2026-06-04] Tighten price confirmation threshold.
        #
        # ROOT CAUSE: _px_min 0.0005 (0.05%) terlalu rendah. Di crypto 5-min,
        # random noise bisa +0.1% kapan saja. Efeknya: OI "confirms" arah yang
        # sebenarnya noise → bull += 8 hampir setiap kali → constant bias.
        #
        # DATA: "oi_bull" fired 282/484 signals (58%) → BUKAN signal, itu noise floor.
        #
        # FIX: Naikkan price confirmation ke 0.15% (same as momentum gate).
        # Rationale: OI expansion + price move 0.15% = REAL directional aggression.
        # OI expansion + price move 0.05% = noise / spread jitter.
        #
        # Also: Reduce max points to +5 (from +22). OI bukan primary edge.
        # OB (+15) tetap dominan. OI = secondary confirmation saja.
        oi_chg = oi.oi_change_pct
        oi_threshold = SIGNAL.oi_change_threshold_pct  # 0.3%
        _px_chg = price_change_5m if price_change_5m is not None else price_change_1h
        _px_min = 0.0015  # [AUDIT #19] 0.0005→0.0015 (0.15%): real move, not noise

        if _px_chg > _px_min and oi_chg > oi_threshold * 3:  # > 0.9%
            bull += 5
            reasons.append(f"📊 OI surge + price up → bullish confirmation (+5)")
        elif _px_chg > _px_min and oi_chg > oi_threshold:  # > 0.3%
            bull += 3
            reasons.append(f"📊 OI rising + price up → mild bull (+3)")
        elif _px_chg < -_px_min and oi_chg > oi_threshold * 3:
            bear += 5
            reasons.append(f"📊 OI surge + price down → bearish confirmation (+5)")
        elif _px_chg < -_px_min and oi_chg > oi_threshold:
            bear += 3
            reasons.append(f"📊 OI rising + price down → mild bear (+3)")
        elif _px_chg > 0.005 and oi_chg < -0.005:
            # Price up but OI falling = short covering rally, not real demand
            bear += 2
            warnings.append(f"Price up but OI falling -> short covering, weak move")
        elif oi_chg < -oi_threshold:
            # OI dropping = position unwinding, no direction
            reasons.append(f"OI dropping {oi_chg*100:.1f}% -> deleveraging (no score)")

        # ── 5. OI Magnitude (context label only — no longer a score adder) ────
        # FIX #9: Removed magnitude_bonus from bull/bear score.
        # Previously added up to +8 to BTC/ETH unconditionally — pure inflation.
        # OI size is now used by the scoring engine as a THRESHOLD modifier:
        # large-cap markets are more efficient (harder to edge) → stricter threshold.
        # small-cap markets are more explosive → more permissive threshold.
        # We still log the OI tier for transparency.
        oi_usd = oi.open_interest
        if oi_usd > 1_000_000_000:
            reasons.append(f"📊 Large-cap market (OI ${oi_usd/1e9:.1f}B) — threshold +3 in engine")
        elif oi_usd > 200_000_000:
            reasons.append(f"📊 Mid-cap market (OI ${oi_usd/1e6:.0f}M) — threshold +1 in engine")
        elif oi_usd > 50_000_000:
            reasons.append(f"📊 Active market (OI ${oi_usd/1e6:.0f}M) — threshold neutral")
        elif oi_usd > 10_000_000:
            reasons.append(f"📊 Small-cap market (OI ${oi_usd/1e6:.0f}M) — threshold -2 in engine")
        else:
            reasons.append(f"📊 Micro-cap market (OI ${oi_usd/1e6:.1f}M) — threshold -3 in engine")

        # ── 6. 24h OI perspective ────────────────────────────────────
        oi_24h = oi.oi_change_24h
        if oi_24h > 0.10:
            if price_change_1h > 0:
                bull += 3
            else:
                bear += 3
            reasons.append(f"24h OI surge +{oi_24h*100:.0f}%")
        elif oi_24h < -0.10:
            reasons.append(f"24h OI decline {oi_24h*100:.0f}% -> deleveraging")

        # ── 7. Spot-Perp Basis ─────────────────────────────────────────
        # [AUDIT #19] Basis = premium/discount. Keep directional but reduce to max ±4.
        # Basis > 0.15% = perp premium over spot = bullish positioning.
        # Jangan double-count dengan funding contrarian di atas.
        if spot_price > 0 and mark_price > 0:
            basis = (mark_price - spot_price) / spot_price
            signal_str = "neutral"

            if basis > 0.0015:
                bull += 3
                signal_str = "bull (+3)"
                reasons.append(f"Spot-Perp basis +{basis*100:.3f}% -> bullish premium")
            elif basis > 0.0008:
                bull += 1
                signal_str = "bull (+1)"
                reasons.append(f"Spot-Perp basis +{basis*100:.3f}% -> mild bullish")
            elif basis < -0.0015:
                bear += 3
                signal_str = "bear (+3)"
                reasons.append(f"Spot-Perp basis {basis*100:.3f}% -> fear/discount")
            elif basis < -0.0008:
                bear += 1
                signal_str = "bear (+1)"
                reasons.append(f"Spot-Perp basis {basis*100:.3f}% -> mild bearish")

            log.debug(f"[BASIS] {asset}: spot={spot_price:.2f} perp={mark_price:.2f} basis={basis*100:.3f}% signal={signal_str}")
        else:
            log.warning(f"[{asset}] Spot/Oracle price unavailable, skipping Basis score")

        # [AUDIT #9 FIX 2026-05-23] Cap reduced 35 → 8.
        # Data (134 trades): OI/Funding does NOT predict trailing_stop fire rate
        # (32-34% across all score buckets). High OI score = high realized_vol =
        # bigger time_exit losses (-7.9% vs -1.2%). OI inflates score without
        # adding edge for 12-min scalper hold. OB (±18) is the real predictor.
        bull = min(bull, 8)
        bear = min(bear, 8)

        return bull, bear, reasons, warnings
