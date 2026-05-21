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
    ) -> Tuple[int, int, List[str], List[str]]:
        bull     = 0
        bear     = 0
        reasons  = []
        warnings = []

        fr = funding.funding_rate
        log.debug(f"[FUNDING] {asset}: rate={fr:.6f} (is this 0.0? then API fetch failed)")

        # ── 1. Funding Rate — CONTRARIAN interpretation ──────────────
        # [AUDIT FIX 2026] Extreme funding LEVEL = crowded positioning, not momentum.
        # Empirical evidence (Phase 1 109-trade audit): score bucket 65-69 was
        # counter-predictive (44% WR) precisely because absolute funding bias
        # pushed the bot into trades AT the top of crowding. Reverse the bias:
        # extreme positive funding -> LONGS already crowded -> mean-reversion BEARISH.
        # Mild funding still gives a small same-side tilt (positioning still building).
        # Funding SLOPE (Section 2 below) keeps its directional meaning — slope IS
        # a real momentum signal, LEVEL is a positioning indicator only.
        if fr > SIGNAL.funding_extreme_threshold * 2:     # > 0.0006
            bear += 18   # [QUANT AGGRESSION] contrarian: crowded longs → strong fade signal
            reasons.append(
                f"⚠️ EXTREME positive funding {fr*100:.4f}%/8h - longs over-crowded -> contrarian BEARISH +18"
            )
            log.info(
                f"[FUNDING-CONTRA] {asset} | fr={fr:.6f} | threshold={SIGNAL.funding_extreme_threshold:.6f} | "
                f"bias=bear | pts=18 | direction=contrarian"
            )
        elif fr > SIGNAL.funding_extreme_threshold:        # > 0.0003
            bear += 12   # [QUANT AGGRESSION] contrarian: heavy long positioning
            reasons.append(
                f"⚠️ HIGH positive funding {fr*100:.4f}%/8h -> longs crowded, contrarian SHORT bias +12"
            )
            log.info(
                f"[FUNDING-CONTRA] {asset} | fr={fr:.6f} | threshold={SIGNAL.funding_extreme_threshold:.6f} | "
                f"bias=bear | pts=12 | direction=contrarian"
            )
        elif fr > 0.00005:                                 # > 0.005%/8h - mild positive
            bull += 8    # [AUDIT FIX 2026-05-21] Was +5. Funding r=+0.62 vs PnL — strongest proven edge. Boost weight.
            reasons.append(
                f"Mild positive funding {fr*100:.4f}%/8h -> LONG tilt (+8)"
            )
        elif fr < -SIGNAL.funding_extreme_threshold * 2:   # < -0.0006
            bull += 18   # [QUANT AGGRESSION] contrarian: crowded shorts → squeeze potential
            reasons.append(
                f"⚠️ EXTREME negative funding {fr*100:.4f}%/8h - shorts over-crowded -> SQUEEZE / contrarian LONG +18"
            )
            log.info(
                f"[FUNDING-CONTRA] {asset} | fr={fr:.6f} | threshold={SIGNAL.funding_extreme_threshold:.6f} | "
                f"bias=bull | pts=18 | direction=contrarian"
            )
        elif fr < -SIGNAL.funding_extreme_threshold:       # < -0.0003
            bull += 12   # [QUANT AGGRESSION] contrarian: heavy short positioning
            reasons.append(
                f"⚠️ HIGH negative funding {fr*100:.4f}%/8h -> shorts crowded, contrarian LONG bias +12"
            )
            log.info(
                f"[FUNDING-CONTRA] {asset} | fr={fr:.6f} | threshold={SIGNAL.funding_extreme_threshold:.6f} | "
                f"bias=bull | pts=12 | direction=contrarian"
            )
        elif fr < -0.00005:                                # < -0.005%/8h - mild negative
            bear += 8    # [AUDIT FIX 2026-05-21] Was +5. Funding r=+0.62 — boost contrarian weight.
            reasons.append(
                f"Mild negative funding {fr*100:.4f}%/8h -> SHORT tilt (+8)"
            )
        else:
            reasons.append(f"Flat/noise funding {fr*100:.4f}%/8h -> no signal")

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

        # ── 3. Predicted vs actual — CONTRARIAN ──────────────────────
        # [AUDIT FIX 2026] Predicted funding = where positioning is HEADING.
        # Predicted shifting MORE positive = longs getting MORE crowded = contrarian bear.
        # Aligned with Section 1 reversal above.
        if funding.predicted_rate is not None:
            pred_diff = funding.predicted_rate - fr
            if abs(pred_diff) > 0.00005:
                if pred_diff > 0:
                    bear += 3
                    reasons.append(
                        f"Predicted funding shifting positive -> longs crowding further -> contrarian bear"
                    )
                else:
                    bull += 3
                    reasons.append(
                        f"Predicted funding shifting negative -> shorts crowding further -> contrarian bull"
                    )

        # ── 4. OI Change Analysis (graduated scoring) ──────────────────
        # [FIX 2026-05-18] Sebelumnya all-or-nothing: 0 atau 22 poin.
        # Sekarang graduated: OI kecil = sedikit poin, OI besar = banyak poin.
        oi_chg = oi.oi_change_pct
        oi_threshold = SIGNAL.oi_change_threshold_pct  # 0.3%

        if price_change_1h > 0.001 and oi_chg > oi_threshold * 3:  # > 0.9%
            bull += 22
            reasons.append(f"📊 OI/Funding bullish (+22)")
        elif price_change_1h > 0.001 and oi_chg > oi_threshold * 1.5:  # > 0.45%
            bull += 14
            reasons.append(f"📊 OI/Funding bullish (+14)")
        elif price_change_1h > 0.001 and oi_chg > oi_threshold:  # > 0.3%
            bull += 8
            reasons.append(f"📊 OI/Funding bullish (+8)")
        elif price_change_1h < -0.001 and oi_chg > oi_threshold * 3:
            bear += 22
            reasons.append(f"📊 OI/Funding bearish (+22)")
        elif price_change_1h < -0.001 and oi_chg > oi_threshold * 1.5:
            bear += 14
            reasons.append(f"📊 OI/Funding bearish (+14)")
        elif price_change_1h < -0.001 and oi_chg > oi_threshold:
            bear += 8
            reasons.append(f"📊 OI/Funding bearish (+8)")
        elif price_change_1h > 0.005 and oi_chg < -0.005:
            bear += 3
            warnings.append(f"Price up but OI falling -> weak move, short covering")
        elif oi_chg < -oi_threshold:
            if price_change_1h > 0:
                bull += 5
            else:
                bear += 5
            reasons.append(f"OI dropping {oi_chg*100:.1f}% -> position unwinding")

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
