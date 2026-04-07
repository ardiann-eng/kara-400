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
    - Crowded longs/shorts (extreme funding -> mean reversion signal)
    - OI expansion with price move (trend confirmation)
    - OI magnitude -> market conviction amplifier
    - Funding rate divergence vs market direction

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
    ) -> Tuple[int, int, List[str], List[str]]:
        bull     = 0
        bear     = 0
        reasons  = []
        warnings = []

        fr = funding.funding_rate

        # ── 1. Funding Rate Analysis ──────────────────────────────────
        # Real HL funding rates are typically +-0.00002 range
        # Extreme is around 0.0001+ (0.01%/8h)
        # Very extreme is 0.0003+ (0.03%/8h)
        
        # POSITIVE funding = longs paying shorts = longs crowded -> SHORT
        if fr > SIGNAL.funding_extreme_threshold * 2:     # > 0.0006
            bear += 20  # increased from 15
            reasons.append(
                f"EXTREME positive funding {fr*100:.4f}%/8h - longs very crowded -> SHORT"
            )
        elif fr > SIGNAL.funding_extreme_threshold:        # > 0.0003
            bear += 14  # increased from 10
            reasons.append(
                f"HIGH positive funding {fr*100:.4f}%/8h -> SHORT pressure"
            )
        elif fr > 0.00005:                                 # > 0.005%/8h - meaningful positive
            bear += 8   # increased from 6
            reasons.append(
                f"Moderate positive funding {fr*100:.4f}%/8h -> mild SHORT tilt"
            )
        elif fr < -SIGNAL.funding_extreme_threshold * 2:   # < -0.0006
            bull += 20  # increased from 15
            reasons.append(
                f"EXTREME negative funding {fr*100:.4f}%/8h - shorts crowded -> LONG"
            )
        elif fr < -SIGNAL.funding_extreme_threshold:       # < -0.0003
            bull += 14  # increased from 10
            reasons.append(
                f"HIGH negative funding {fr*100:.4f}%/8h -> LONG pressure"
            )
        elif fr < -0.00005:                                # < -0.005%/8h - meaningful negative
            bull += 8   # increased from 6
            reasons.append(
                f"Moderate negative funding {fr*100:.4f}%/8h -> mild LONG tilt"
            )
        elif fr > 0.00001:                                 # slightly positive
            bear += 3
            reasons.append(
                f"Slight positive funding {fr*100:.4f}%/8h -> minor SHORT lean"
            )
        elif fr < -0.00001:                                # slightly negative
            bull += 3
            reasons.append(
                f"Slight negative funding {fr*100:.4f}%/8h -> minor LONG lean"
            )
        else:
            # Truly flat funding — follow price momentum
            if price_change_1h > 0.001:
                bull += 2
                reasons.append(f"Flat funding + price up -> LONG lean")
            elif price_change_1h < -0.001:
                bear += 2
                reasons.append(f"Flat funding + price down -> SHORT lean")
            else:
                bull += 1
                bear += 1
                reasons.append(f"Flat funding, flat price -> no direction")

        # ── 2. Funding trend (slope of last 8) ────────────────────────
        if len(funding_history) >= 8:
            y = funding_history[-8:]
            x = list(range(8))
            n = 8
            sum_x = sum(x)
            sum_y = sum(y)
            sum_xy = sum(x[i]*y[i] for i in range(n))
            sum_xx = sum(x[i]*x[i] for i in range(n))
            
            # Prevent division by zero
            denom = (n * sum_xx - sum_x**2)
            if denom != 0:
                slope = (n * sum_xy - sum_x * sum_y) / denom
                
                trend_str = "rising" if slope > 0 else "falling"
                log.debug(f"[FTRD] {asset}: funding_slope={slope:.6f}, trend={trend_str}")
                
                # Positive slope = longs getting crowded = SHORT pressure
                if slope > 0.000005:
                    pts = min(int(slope * 400000), 8)
                    pts = max(pts, 1)
                    bear += pts
                    reasons.append(f"• Funding trend: 📈 RISING (slope {slope:.6f}) -> SHORT pressure")
                # Negative slope = shorts getting crowded = LONG pressure
                elif slope < -0.000005:
                    pts = min(int(abs(slope) * 400000), 8)
                    pts = max(pts, 1)
                    bull += pts
                    reasons.append(f"• Funding trend: 📉 FALLING (slope {slope:.6f}) -> LONG pressure")
                else:
                    reasons.append(f"• Funding trend: ⚖️ STABLE (slope {slope:.6f}) -> Neutral")

        # ── 3. Predicted vs actual ────────────────────────────────────
        if funding.predicted_rate is not None:
            pred_diff = funding.predicted_rate - fr
            if abs(pred_diff) > 0.00005:  # lowered from 0.0003
                if pred_diff > 0:
                    # Predicted funding more positive = longs will pay more = SHORT
                    bear += 3
                    reasons.append(
                        f"Predicted funding shifting positive -> SHORT pressure building"
                    )
                else:
                    bull += 3
                    reasons.append(
                        f"Predicted funding shifting negative -> LONG pressure building"
                    )

        # ── 4. OI Change Analysis ─────────────────────────────────────
        oi_chg = oi.oi_change_pct
        if price_change_1h > 0.002 and oi_chg > SIGNAL.oi_change_threshold_pct:
            bull += 18  # significantly increased from 10
            reasons.append(
                f"OI +{oi_chg*100:.1f}% with price up -> STRONG LONG confirmed"
            )
        elif price_change_1h < -0.002 and oi_chg > SIGNAL.oi_change_threshold_pct:
            bear += 18  # significantly increased from 10
            reasons.append(
                f"OI +{oi_chg*100:.1f}% with price down -> STRONG SHORT confirmed"
            )
        elif price_change_1h > 0.005 and oi_chg < -0.005:
            bear += 3
            warnings.append(
                f"Price up but OI falling -> weak move, short covering"
            )
        elif oi_chg < -SIGNAL.oi_change_threshold_pct:
            if price_change_1h > 0:
                bull += 5
            else:
                bear += 5
            reasons.append(
                f"OI dropping {oi_chg*100:.1f}% -> position unwinding"
            )

        # ── 5. OI Magnitude (conviction amplifier) ────────────────────
        # Different OI levels = different market conviction
        # This produces DIFFERENT scores per asset even when other data is similar
        oi_usd = oi.open_interest
        if oi_usd > 1_000_000_000:      # > $1B  (BTC, ETH level)
            # High liquidity market — small funding differences matter more
            magnitude_bonus = 8  # was 5
            reasons.append(f"High conviction market (OI ${oi_usd/1e9:.1f}B)")
        elif oi_usd > 200_000_000:       # > $200M (SOL, HYPE level)
            magnitude_bonus = 6  # was 4
            reasons.append(f"Mid-cap market (OI ${oi_usd/1e6:.0f}M)")
        elif oi_usd > 50_000_000:        # > $50M
            magnitude_bonus = 4  # was 3
            reasons.append(f"Active market (OI ${oi_usd/1e6:.0f}M)")
        elif oi_usd > 10_000_000:        # > $10M
            magnitude_bonus = 2  
            reasons.append(f"Small-cap market (OI ${oi_usd/1e6:.0f}M)")
        else:
            magnitude_bonus = 0  # was 1
            reasons.append(f"Low OI market (${oi_usd/1e6:.1f}M) -> neutral")

        # Magnitude amplifies the winning side
        if bull > bear:
            bull += magnitude_bonus
        elif bear > bull:
            bear += magnitude_bonus
        else:
            # Tied — use funding direction to break tie
            if fr > 0:
                bear += magnitude_bonus
            elif fr < 0:
                bull += magnitude_bonus
            else:
                bull += magnitude_bonus // 2
                bear += magnitude_bonus // 2

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
        if spot_price > 0 and mark_price > 0:
            basis = (mark_price - spot_price) / spot_price
            signal_str = "neutral"
            
            if basis > 0.0015:
                bear += 8
                signal_str = "bear (+8)"
                reasons.append(f"Spot-Perp basis +{basis*100:.3f}% -> longs paying heavy premium (SHORT)")
            elif basis > 0.0008:
                bear += 4
                signal_str = "bear (+4)"
                reasons.append(f"Spot-Perp basis +{basis*100:.3f}% -> mild long crowding")
            elif basis < -0.0015:
                bull += 8
                signal_str = "bull (+8)"
                reasons.append(f"Spot-Perp basis {basis*100:.3f}% -> extreme fear/panic (LONG)")
            elif basis < -0.0008:
                bull += 4
                signal_str = "bull (+4)"
                reasons.append(f"Spot-Perp basis {basis*100:.3f}% -> mild fear (LONG)")
                
            log.debug(f"[BASIS] {asset}: spot={spot_price:.2f} perp={mark_price:.2f} basis={basis*100:.3f}% signal={signal_str}")
        else:
            log.warning(f"[{asset}] Spot/Oracle price unavailable, skipping Basis score")

        # CAP max score to prevent inflation (increased for Conviction)
        bull = min(bull, 40)
        bear = min(bear, 40)

        return bull, bear, reasons, warnings
