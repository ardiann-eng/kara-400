"""
KARA Bot - Liquidation Heatmap & Cascade Analyzer
Edge component #2: Detect liquidation clusters, cascade probability.
Returns (bull, bear, reasons, warnings, liq_map).

v3 2026-04-05: OI proxy now produces differentiated scores instead
of equal bull/bear split. Uses OI magnitude tiers + funding direction.
"""

from __future__ import annotations
import logging
import math
from typing import Dict, List, Tuple

from config import SIGNAL
from models.schemas import LiquidationLevel, LiquidationMap, Side

log = logging.getLogger("kara.liq_analyzer")


class LiquidationAnalyzer:
    """
    Builds a liquidation heatmap from:
    - Live WS liquidation events (recent)
    - Estimated liq levels based on OI & mark price

    When no liquidation WS data is available (common), uses OI magnitude
    as a proxy for liquidation risk with differentiated scoring.
    """

    def analyze(
        self,
        asset: str,
        current_price: float,
        recent_liq_events: List[Dict],
        oi_usd: float,
        funding_rate: float = 0.0,
    ) -> Tuple[int, int, List[str], List[str], LiquidationMap]:
        """
        Returns:
            bull, bear, reasons, warnings, liq_map
        """
        bull     = 0
        bear     = 0
        reasons  = []
        warnings = []

        liq_map = self._build_map(asset, current_price, recent_liq_events, oi_usd)

        b, s, r, w = self._score_map(liq_map, oi_usd, funding_rate)
        bull    += b
        bear    += s
        reasons += r
        warnings += w

        return bull, bear, reasons, warnings, liq_map

    def _build_map(
        self,
        asset: str,
        current_price: float,
        liq_events: List[Dict],
        oi_usd: float,
    ) -> LiquidationMap:
        """
        Construct liquidation levels from recent WS events.
        Groups by price bucket (0.5% bands) and sums notional.
        """
        asset_events = [
            e for e in liq_events
            if e.get("coin", e.get("asset", "")) == asset
        ]

        buckets: Dict[float, Tuple[float, str]] = {}
        for ev in asset_events:
            px  = float(ev.get("px", ev.get("price", 0)))
            sz  = float(ev.get("sz", ev.get("size", 0))) * px
            if px == 0:
                continue
            bucket = round(px / (current_price * 0.005)) * (current_price * 0.005)
            existing = buckets.get(bucket, (0.0, "long"))
            buckets[bucket] = (existing[0] + sz, ev.get("side", "long"))

        levels = []
        for px, (notional, side_str) in buckets.items():
            if notional < 10_000:
                continue
            dist = abs(px - current_price) / current_price
            levels.append(LiquidationLevel(
                price=round(px, 2),
                notional_usd=notional,
                side=Side.LONG if side_str in ("long", "buy") else Side.SHORT,
                distance_pct=round(dist, 4)
            ))

        levels.sort(key=lambda l: l.distance_pct)

        nearby_notional = sum(
            l.notional_usd for l in levels
            if l.distance_pct < SIGNAL.liq_cascade_threshold
        )
        cascade_risk = min(nearby_notional / max(oi_usd * 0.02, 1), 1.0)
        nearest_pct = levels[0].distance_pct if levels else 1.0

        return LiquidationMap(
            asset=asset,
            current_price=current_price,
            levels=levels,
            nearest_liq_pct=nearest_pct,
            cascade_risk=cascade_risk,
        )

    def _score_map(
        self,
        liq_map: LiquidationMap,
        oi_usd: float = 0.0,
        funding_rate: float = 0.0,
    ) -> Tuple[int, int, List[str], List[str]]:
        bull     = 0
        bear     = 0
        reasons  = []
        warnings = []

        levels = liq_map.levels
        if not levels:
            # ── No liquidation data — use OI-based proxy ─────────────
            # DIFFERENTIATED scoring based on OI magnitude + funding direction
            
            # Base conviction from OI size
            if oi_usd > 1_000_000_000:     # > $1B
                base_points = 10
                risk_label = f"Very high OI (${oi_usd/1e9:.1f}B) -> significant liq risk"
            elif oi_usd > 200_000_000:      # > $200M
                base_points = 8
                risk_label = f"High OI (${oi_usd/1e6:.0f}M) -> moderate liq risk"
            elif oi_usd > 50_000_000:       # > $50M
                base_points = 6
                risk_label = f"Moderate OI (${oi_usd/1e6:.0f}M) -> some liq risk"
            elif oi_usd > 10_000_000:       # > $10M
                base_points = 4
                risk_label = f"Low OI (${oi_usd/1e6:.0f}M) -> limited liq risk"
            else:
                base_points = 2
                risk_label = f"Very low OI (${oi_usd/1e6:.1f}M) -> minimal liq risk"

            # Use funding direction to tilt the split
            # Positive funding = longs crowded = more long liq risk = SHORT
            # Negative funding = shorts crowded = more short liq risk = LONG
            # Real funding rates are typically +-0.00001 to +-0.00003
            if funding_rate > 0.000005:
                # longs crowded — more liq risk on long side = SHORT
                tilt = min(int(abs(funding_rate) * 200000), 4)  # up to 4pt tilt
                bear += base_points // 2 + tilt
                bull += max(base_points // 2 - tilt, 0)
                reasons.append(f"{risk_label} + positive funding -> SHORT liq tilt")
            elif funding_rate < -0.000005:
                # shorts crowded — more liq risk on short side = LONG
                tilt = min(int(abs(funding_rate) * 200000), 4)
                bull += base_points // 2 + tilt
                bear += max(base_points // 2 - tilt, 0)
                reasons.append(f"{risk_label} + negative funding -> LONG liq tilt")
            else:
                # No funding tilt — even split but still differentiated by OI size
                bull += base_points // 2
                bear += base_points // 2
                reasons.append(f"{risk_label} (no funding tilt)")

            return bull, bear, reasons, warnings

        # ── Has real liquidation levels ────────────────────────────────
        long_liq_above = sum(
            l.notional_usd for l in levels
            if l.side == Side.LONG and l.distance_pct < 0.03
        )
        short_liq_below = sum(
            l.notional_usd for l in levels
            if l.side == Side.SHORT and l.distance_pct < 0.03
        )

        if long_liq_above > short_liq_below * 1.5:
            bear += 14
            reasons.append(
                f"Large long liq cluster above ${long_liq_above:,.0f} -> SHORT cascade"
            )
        elif short_liq_below > long_liq_above * 1.5:
            bull += 14
            reasons.append(
                f"Large short liq cluster below ${short_liq_below:,.0f} -> LONG cascade"
            )
        else:
            bull += 4
            bear += 4
            reasons.append("Balanced liquidations -> no directional edge")

        # Cascade risk bonus
        if liq_map.cascade_risk > 0.5:
            if long_liq_above > short_liq_below:
                bear += 8
            else:
                bull += 8
            reasons.append(
                f"High cascade risk {liq_map.cascade_risk*100:.0f}%"
            )
        elif liq_map.cascade_risk > 0.2:
            if long_liq_above > short_liq_below:
                bear += 4
            else:
                bull += 4
            reasons.append(
                f"Moderate cascade risk {liq_map.cascade_risk*100:.0f}%"
            )

        if liq_map.nearest_liq_pct < 0.01:
            warnings.append(
                f"Liq cluster {liq_map.nearest_liq_pct*100:.1f}% from price!"
            )

        return bull, bear, reasons, warnings
