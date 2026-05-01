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
        log.debug(f"[FUNDING] {asset}: rate={fr:.6f} (is this 0.0? then API fetch failed)")

        # ── 1. Funding Rate Analysis ──────────────────────────────────
        # Real HL funding rates are typically +-0.00002 range
        # Extreme is around 0.0001+ (0.01%/8h)
        # Very extreme is 0.0003+ (0.03%/8h)
        
        # POSITIVE funding = extreme momentum = LONG signal
        if fr > SIGNAL.funding_extreme_threshold * 2:     # > 0.0006
            bull += 18  # reduced from 20 to temper inflation
            reasons.append(
                f"EXTREME positive funding {fr*100:.4f}%/8h - massive upside momentum -> LONG"
            )
        elif fr > SIGNAL.funding_extreme_threshold:        # > 0.0003
            bull += 12  # reduced from 14
            reasons.append(
                f"HIGH positive funding {fr*100:.4f}%/8h -> STRONG LONG momentum"
            )
        elif fr > 0.00005:                                 # > 0.005%/8h - meaningful positive
            bull += 6   # reduced from 8
            reasons.append(
                f"Moderate positive funding {fr*100:.4f}%/8h -> LONG tilt"
            )
        elif fr < -SIGNAL.funding_extreme_threshold * 2:   # < -0.0006
            bear += 18  # reduced from 20 to temper inflation
            reasons.append(
                f"EXTREME negative funding {fr*100:.4f}%/8h - massive downside momentum -> SHORT"
            )
        elif fr < -SIGNAL.funding_extreme_threshold:       # < -0.0003
            bear += 12  # reduced from 14
            reasons.append(
                f"HIGH negative funding {fr*100:.4f}%/8h -> STRONG SHORT momentum"
            )
        elif fr < -0.00005:                                # < -0.005%/8h - meaningful negative
            bear += 6   # reduced from 8
            reasons.append(
                f"Moderate negative funding {fr*100:.4f}%/8h -> SHORT tilt"
            )
        elif fr > 0.00001:                                 # slightly positive
            bull += 3
            reasons.append(
                f"Slight positive funding {fr*100:.4f}%/8h -> minor LONG lean"
            )
        elif fr < -0.00001:                                # slightly negative
            bear += 3
            reasons.append(
                f"Slight negative funding {fr*100:.4f}%/8h -> minor SHORT lean"
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
                
                # Positive slope = strong upside momentum = LONG pressure
                if slope > 0.000005:
                    pts = min(int(slope * 400000), 8)
                    pts = max(pts, 1)
                    bull += pts
                    reasons.append(f"• Funding trend: 📈 RISING (slope {slope:.6f}) -> LONG pressure")
                # Negative slope = strong downside momentum = SHORT pressure
                elif slope < -0.000005:
                    pts = min(int(abs(slope) * 400000), 8)
                    pts = max(pts, 1)
                    bear += pts
                    reasons.append(f"• Funding trend: 📉 FALLING (slope {slope:.6f}) -> SHORT pressure")
                else:
                    reasons.append(f"• Funding trend: ⚖️ STABLE (slope {slope:.6f}) -> Neutral")

        # ── 3. Predicted vs actual ────────────────────────────────────
        if funding.predicted_rate is not None:
            pred_diff = funding.predicted_rate - fr
            if abs(pred_diff) > 0.00005:  # lowered from 0.0003
                if pred_diff > 0:
                    # Predicted funding more positive = momentum confirming LONG
                    bull += 3
                    reasons.append(
                        f"Predicted funding shifting positive -> LONG pressure building"
                    )
                else:
                    bear += 3
                    reasons.append(
                        f"Predicted funding shifting negative -> SHORT pressure building"
                    )

        # ── 4. OI Change Analysis ─────────────────────────────────────
        oi_chg = oi.oi_change_pct
        if price_change_1h > 0.002 and oi_chg > SIGNAL.oi_change_threshold_pct:
            bull += 22  # increased from 18
            reasons.append(
                f"OI +{oi_chg*100:.1f}% with price up -> STRONG LONG confirmed"
            )
        elif price_change_1h < -0.002 and oi_chg > SIGNAL.oi_change_threshold_pct:
            bear += 22  # increased from 18
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

        # ── 5. OI Magnitude (tiebreaker kecil, bukan amplifier besar) ───
        # Masalah sebelumnya: BTC selalu dapat +8 bonus hanya karena OI besar.
        # Ini bukan sinyal — itu hanya fakta struktural yang tidak berubah.
        # Bonus dikurangi ke maksimum +4 dan hanya dipakai saat bull == bear (tiebreaker).
        oi_usd = oi.open_interest
        if oi_usd > 1_000_000_000:      # > $1B  (BTC, ETH)
            magnitude_bonus = 4
        elif oi_usd > 200_000_000:       # > $200M (SOL, HYPE)
            magnitude_bonus = 3
        elif oi_usd > 50_000_000:        # > $50M
            magnitude_bonus = 2
        elif oi_usd > 10_000_000:        # > $10M
            magnitude_bonus = 1
        else:
            magnitude_bonus = 0

        # Hanya gunakan magnitude untuk memutus seri — jangan amplifikasi yang sudah unggul
        if bull == bear and magnitude_bonus > 0:
            if fr > 0:
                bear += magnitude_bonus
            elif fr < 0:
                bull += magnitude_bonus
            else:
                bull += magnitude_bonus // 2
                bear += magnitude_bonus // 2
            reasons.append(f"OI size tiebreaker (OI ${oi_usd/1e6:.0f}M)")
        # Jika sudah ada pemenang jelas, magnitude tidak menambah apapun

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
        # Basis dan funding menunjukkan sinyal yang sama (demand perp > spot).
        # Jika funding sudah memberikan poin besar (>=12), basis tidak menambah apapun
        # karena itu akan menghitung ulang sinyal yang sudah dihitung.
        # Basis hanya berkontribusi saat funding lemah/netral (< 6 poin dari fr).
        if spot_price > 0 and mark_price > 0:
            basis = (mark_price - spot_price) / spot_price
            signal_str = "neutral"

            # Ukur seberapa besar funding sudah berkontribusi ke sisi yang sama
            # agar kita tahu apakah basis masih menambah informasi baru
            funding_bull_pts = bull  # poin bull yang sudah terkumpul sebelum basis
            funding_bear_pts = bear

            if basis > 0.0015:
                # Kurangi poin basis jika funding bull sudah besar (>= 10 dari fr alone)
                basis_pts = 6 if funding_bull_pts >= 10 else 10
                bull += basis_pts
                signal_str = f"bull (+{basis_pts})"
                reasons.append(f"Spot-Perp basis +{basis*100:.3f}% -> bullish premium (LONG)")
            elif basis > 0.0008:
                basis_pts = 3 if funding_bull_pts >= 10 else 5
                bull += basis_pts
                signal_str = f"bull (+{basis_pts})"
                reasons.append(f"Spot-Perp basis +{basis*100:.3f}% -> bullish momentum")
            elif basis < -0.0015:
                basis_pts = 6 if funding_bear_pts >= 10 else 10
                bear += basis_pts
                signal_str = f"bear (+{basis_pts})"
                reasons.append(f"Spot-Perp basis {basis*100:.3f}% -> fear/discount (SHORT)")
            elif basis < -0.0008:
                basis_pts = 3 if funding_bear_pts >= 10 else 5
                bear += basis_pts
                signal_str = f"bear (+{basis_pts})"
                reasons.append(f"Spot-Perp basis {basis*100:.3f}% -> bearish momentum")

            log.debug(f"[BASIS] {asset}: spot={spot_price:.2f} perp={mark_price:.2f} basis={basis*100:.3f}% signal={signal_str}")
        else:
            log.warning(f"[{asset}] Spot/Oracle price unavailable, skipping Basis score")

        # Cap dikembalikan ke 35 — sumber inflasi utama sudah dihapus
        bull = min(bull, 35)
        bear = min(bear, 35)

        return bull, bear, reasons, warnings
