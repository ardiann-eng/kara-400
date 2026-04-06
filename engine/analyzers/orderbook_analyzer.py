"""
KARA Bot - Orderbook Imbalance + VWAP Analyzer
Edge component #3: Detect order flow imbalance, VWAP deviation, spread.
Returns (bull, bear, reasons, warnings).

v3 2026-04-05: Recalibrated for real Hyperliquid L2 data:
- Imbalance threshold lowered from 0.45 to graduated tiers (0.10+)
- VWAP threshold uses graduated tiers
- Added dollar depth asymmetry scoring
- Wall detection keeps working unchanged
"""

from __future__ import annotations
import logging
from typing import List, Tuple

from config import SIGNAL
from models.schemas import OrderbookSnapshot, Side

log = logging.getLogger("kara.ob_analyzer")


class OrderbookAnalyzer:
    """
    Analyzes L2 orderbook to detect:
    1. Bid/ask imbalance -> direction bias (graduated, not binary)
    2. VWAP deviation -> mean reversion or momentum signal
    3. Wall detection -> large orders acting as support/resistance
    4. Trade flow -> recent buy/sell volume ratio
    5. Dollar depth asymmetry -> total $ on each side

    Returns (bull, bear, reasons, warnings) — NO pre-determined direction.
    """

    def __init__(self):
        self._cvd_history = {}

    def analyze(
        self,
        ob: OrderbookSnapshot,
        recent_trades: List[dict],
    ) -> Tuple[int, int, List[str], List[str]]:
        bull     = 0
        bear     = 0
        reasons  = []
        warnings = []

        # ── 1. Imbalance score (graduated tiers) ─────────────────────
        imb = ob.bid_ask_imbalance   # positive = bid heavy = bullish

        if imb > 0.50:
            # Strong bid imbalance
            pts = 10 + int((imb - 0.50) * 20)  # 10-14pts
            pts = min(pts, 14)
            bull += pts
            reasons.append(
                f"Strong bid imbalance {imb*100:.0f}% -> LONG pressure"
            )
        elif imb > 0.25:
            pts = 6 + int((imb - 0.25) * 16)   # 6-9pts
            bull += pts
            reasons.append(
                f"Moderate bid imbalance {imb*100:.0f}% -> LONG tilt"
            )
        elif imb > 0.10:
            pts = 3 + int((imb - 0.10) * 20)   # 3-5pts
            bull += pts
            reasons.append(
                f"Slight bid imbalance {imb*100:.0f}% -> minor LONG lean"
            )
        elif imb < -0.50:
            pts = 10 + int((abs(imb) - 0.50) * 20)
            pts = min(pts, 14)
            bear += pts
            reasons.append(
                f"Strong ask imbalance {abs(imb)*100:.0f}% -> SHORT pressure"
            )
        elif imb < -0.25:
            pts = 6 + int((abs(imb) - 0.25) * 16)
            bear += pts
            reasons.append(
                f"Moderate ask imbalance {abs(imb)*100:.0f}% -> SHORT tilt"
            )
        elif imb < -0.10:
            pts = 3 + int((abs(imb) - 0.10) * 20)
            bear += pts
            reasons.append(
                f"Slight ask imbalance {abs(imb)*100:.0f}% -> minor SHORT lean"
            )
        else:
            # True equilibrium (-0.10 to +0.10)
            bull += 2
            bear += 2
            reasons.append(f"Balanced orderbook ({imb*100:+.0f}%)")

        # ── 2. VWAP deviation (graduated tiers) ──────────────────────
        dev = ob.vwap_deviation_pct   # (mid - vwap) / vwap

        if dev > 0.005:
            # Price well above VWAP -> overbought -> SHORT setup
            bear += 10
            reasons.append(
                f"Price {dev*100:.3f}% above VWAP -> overbought, SHORT setup"
            )
        elif dev > 0.002:
            # Price slightly above VWAP -> bullish momentum
            bull += 7
            reasons.append(
                f"Price {dev*100:.3f}% above VWAP -> bullish momentum"
            )
        elif dev > 0.0005:
            # Small positive deviation
            bull += 4
            reasons.append(
                f"Price slightly above VWAP ({dev*100:.3f}%) -> mild bull"
            )
        elif dev < -0.005:
            # Price well below VWAP -> oversold -> LONG setup
            bull += 10
            reasons.append(
                f"Price {dev*100:.3f}% below VWAP -> oversold, LONG setup"
            )
        elif dev < -0.002:
            bear += 7
            reasons.append(
                f"Price {dev*100:.3f}% below VWAP -> bearish momentum"
            )
        elif dev < -0.0005:
            bear += 4
            reasons.append(
                f"Price slightly below VWAP ({dev*100:.3f}%) -> mild bear"
            )
        else:
            # At VWAP (< +-0.05%)
            bull += 1
            bear += 1
            reasons.append(
                f"Price at VWAP ({dev*100:.4f}% dev) -> fair value"
            )

        # ── 3. Dollar depth asymmetry ────────────────────────────────
        # Compare total $ value on bid vs ask side
        if ob.bids and ob.asks and len(ob.bids) >= 5 and len(ob.asks) >= 5:
            bid_depth_usd = sum(b[0] * b[1] for b in ob.bids)
            ask_depth_usd = sum(a[0] * a[1] for a in ob.asks)
            total_depth = bid_depth_usd + ask_depth_usd

            if total_depth > 0:
                depth_ratio = bid_depth_usd / total_depth  # > 0.5 = more bids

                if depth_ratio > 0.65:
                    bull += 5
                    reasons.append(
                        f"Bid depth dominance {depth_ratio*100:.0f}% -> LONG support"
                    )
                elif depth_ratio > 0.55:
                    bull += 3
                    reasons.append(
                        f"Bid-heavy depth {depth_ratio*100:.0f}% -> mild LONG"
                    )
                elif depth_ratio < 0.35:
                    bear += 5
                    reasons.append(
                        f"Ask depth dominance {(1-depth_ratio)*100:.0f}% -> SHORT pressure"
                    )
                elif depth_ratio < 0.45:
                    bear += 3
                    reasons.append(
                        f"Ask-heavy depth {(1-depth_ratio)*100:.0f}% -> mild SHORT"
                    )

        # ── 4. Wall detection ─────────────────────────────────────────
        if ob.bids and ob.asks:
            bid_sizes = [b[1] for b in ob.bids]
            ask_sizes = [a[1] for a in ob.asks]

            avg_bid = sum(bid_sizes) / len(bid_sizes) if bid_sizes else 0
            avg_ask = sum(ask_sizes) / len(ask_sizes) if ask_sizes else 0

            bid_walls = [b for b in ob.bids if b[1] > avg_bid * 5]
            ask_walls = [a for a in ob.asks if a[1] > avg_ask * 5]

            if bid_walls:
                wall_px = bid_walls[0][0]
                bull += 3
                reasons.append(
                    f"Bid wall at ${wall_px:,.1f} -> support"
                )
            if ask_walls:
                wall_px = ask_walls[0][0]
                bear += 3
                reasons.append(
                    f"Ask wall at ${wall_px:,.1f} -> resistance"
                )

        # ── 5. Cumulative Volume Delta (CVD) ──────────────────────────
        if recent_trades and len(recent_trades) >= 20:
            subset = recent_trades[-100:]
            cvd = 0.0
            start_px = float(subset[0].get("px", ob.mid_price))
            end_px = float(subset[-1].get("px", ob.mid_price))
            
            for t in subset:
                sz = float(t.get("sz", 0)) * float(t.get("px", 0))
                if t.get("side") in ("buy", "B", True):
                    cvd += sz
                else:
                    cvd -= sz
            
            self._cvd_history[ob.asset] = cvd
            log.debug(f"[CVD] {ob.asset}: cvd={cvd:.2f}, signal={'bull' if cvd>0 else 'bear'}")
            
            p_change = (end_px - start_px) / start_px if start_px > 0 else 0
            is_flat = abs(p_change) < 0.002  # < 0.2% price change
            
            # Dynamic threshold for CVD (BTC/ETH need higher)
            cvd_thresh = 50000 if ob.asset in ("BTC", "ETH") else 20000
            
            if cvd > cvd_thresh and is_flat:
                # Rising CVD + flat price = accumulation
                pts = min(int(cvd / (cvd_thresh/2)), 8)
                pts = max(pts, 2)
                bull += pts
                reasons.append(f"• CVD: 🟢 Accumulation (+${cvd/1000:,.1f}k) during flat price")
            elif cvd < -cvd_thresh and is_flat:
                # Falling CVD + flat price = distribution
                pts = min(int(abs(cvd) / (cvd_thresh/2)), 8)
                pts = max(pts, 2)
                bear += pts
                reasons.append(f"• CVD: 🔴 Distribution (${cvd/1000:,.1f}k) during flat price")
            elif abs(cvd) > cvd_thresh * 2:
                side_str = "Bid Flow" if cvd > 0 else "Ask Flow"
                side_emoji = "🟢" if cvd > 0 else "🔴"
                pts = min(int(abs(cvd) / (cvd_thresh*2)), 5)
                pts = max(pts, 1)
                if cvd > 0: bull += pts
                else: bear += pts
                reasons.append(f"• CVD: {side_emoji} {side_str} (+${abs(cvd)/1000:,.1f}k)")
            else:
                reasons.append(f"• CVD: ⚪ Neutral Flow (${cvd/1000:,.1f}k)")

        # CAP max score to prevent inflation
        bull = min(bull, 25)
        bear = min(bear, 25)

        return bull, bear, reasons, warnings
