"""
KARA v10 — Gate System (REKONSTRUKSI Fase 1)

Mengganti scoring aditif 8-komponen dengan sistem 3-lapis institusional:
  LAPIS 1 — HARD GATES (murah, regime-agnostic): regime align, exhaustion, junk filter
  LAPIS 2 — ENTRY TIERS (modulate size, TIDAK reject): liquidity context, OB edge, vol
  LAPIS 3 — SETUP CLASSIFIER (label untuk audit): sweep / pullback / breakout / momentum

Entry Quality Tiers:
  S = OB trend-aligned (strong wall + searah HTF) — premium entry, +$5.41 WR 44.4%
  A = Good liquidity context (near level / OB wall / CVD moderate)
  B = No strong evidence, but passes all gates — acceptable, size dikurangi

Prinsip anti-over-filter (funnel study 90 sinyal):
  Hanya filter MURAH yang hard-reject. Filter yang buang >30% sinyal = sizing modifier.
  RV filter = SIZING (full/0.75x/0.3x), block hanya >8% (AUDIT #21: RV r=-0.50 → tighten).

Edge yang dipertahankan: trailing stop, momentum death (di risk_manager, bukan di sini).
Edge baru: level likuiditas (session H/L, range extreme) + order flow (OB wall, CVD).

Semua perhitungan dari data HL yang SUDAH ada (candles, OB, trades). Zero API call.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone


@dataclass
class GateDecision:
    """Hasil evaluasi gate system."""
    passed: bool
    reject_reason: str = ""          # diisi kalau passed=False
    size_mult: float = 1.0           # sizing modifier (Lapis 2): 1.0 / 0.75 / 0.6 / 0.5 / 0.3
    setup: str = "none"              # Lapis 3 label: sweep / pullback / breakout / momentum
    tier: str = "B"                  # S (OB trend-aligned) / A (liq context) / B (basic pass)
    reasons: List[str] = field(default_factory=list)
    # Telemetri untuk audit
    has_liq_context: bool = False
    near_level: str = ""             # level mana yang dekat (session_high, range_low, dst)
    rv_tier: str = "full"


class GateSystem:
    """
    Sistem gate v10. Stateless kecuali level cache (recompute per evaluasi).
    Dipanggil dari scoring_engine setelah direction + htf_regime ditentukan.
    """

    # ── Thresholds (dari funnel study + audit data — [AUDIT #21] tuned) ──
    RV_HARD_MAX        = 0.08    # >8% = hard reject (diturunkan dari 15%, RV r=-0.50 p=0.0005)
    RV_FULL_MAX        = 0.04    # <=4% = size penuh
    RV_REDUCED_MAX     = 0.06    # 4-6% = 0.75x
    # 6-8% = 0.3x (damage control: vol tinggi tapi belum ekstrem, size dikecilkan drastis)
    CVD_EXTREME        = 0.70    # CVD 5m >0.7 searah = exhaustion, reject
    LEVEL_PROXIMITY    = 0.004   # dalam 0.4% dari level = "di level"
    SWEEP_RECLAIM_BARS = 2       # tembus level lalu balik dalam <=2 candle
    OB_WALL_STRONG     = 12      # ob_dir >= 12 = institutional wall
    MIN_DISPLACEMENT   = 0.0008  # 0.08% net move = displacement proof (LONG) — [AUDIT #21] loosened from 0.0015
    MIN_DISP_SHORT     = 0.0012  # 0.12% SHORT — [AUDIT #21] loosened from 0.0020

    def __init__(self):
        pass

    # ──────────────────────────────────────────
    # FEATURE BUILDERS (dari candles)
    # ──────────────────────────────────────────

    @staticmethod
    def _extract_ohlc(candles: list) -> Tuple[List[float], List[float], List[float]]:
        """Ekstrak highs, lows, closes dari candle (dict atau list format)."""
        highs, lows, closes = [], [], []
        for c in candles:
            try:
                if isinstance(c, dict):
                    h = float(c.get("h", 0)); l = float(c.get("l", 0)); cl = float(c.get("c", 0))
                elif isinstance(c, (list, tuple)) and len(c) >= 5:
                    h = float(c[2]); l = float(c[3]); cl = float(c[4])
                else:
                    continue
                if cl > 0:
                    highs.append(h); lows.append(l); closes.append(cl)
            except (TypeError, ValueError):
                continue
        return highs, lows, closes

    def compute_levels(self, highs: List[float], lows: List[float], closes: List[float]) -> Dict[str, float]:
        """
        Hitung level likuiditas objektif dari candle history.
        Session H/L diproksi dari window (candle 1m: 60=1h, 240=4h).
        Return dict level → harga. Level diabaikan jika range terlalu sempit
        (high≈low = pasar mati, bukan level likuiditas nyata).
        """
        levels = {}
        n = len(closes)
        if n < 10:
            return levels
        _MIN_RANGE = 0.003  # range minimal 0.3% agar dianggap level nyata

        def _add(name_hi, name_lo, hi, lo):
            mid = (hi + lo) / 2
            if mid > 0 and (hi - lo) / mid >= _MIN_RANGE:
                levels[name_hi] = hi
                levels[name_lo] = lo

        if n >= 60:
            _add("range_1h_high", "range_1h_low", max(highs[-60:]), min(lows[-60:]))
        if n >= 240:
            _add("range_4h_high", "range_4h_low", max(highs[-240:]), min(lows[-240:]))
        if not levels:
            # fallback: seluruh window (tetap cek min range)
            _add("range_full_high", "range_full_low", max(highs), min(lows))
        return levels

    def near_level(self, price: float, levels: Dict[str, float]) -> Tuple[bool, str]:
        """Apakah harga dekat (dalam LEVEL_PROXIMITY) salah satu level?"""
        if price <= 0:
            return False, ""
        for name, lvl in levels.items():
            if lvl <= 0:
                continue
            if abs(price - lvl) / price <= self.LEVEL_PROXIMITY:
                return True, name
        return False, ""

    def detect_sweep(self, highs: List[float], lows: List[float], closes: List[float],
                     side: str, levels: Dict[str, float]) -> bool:
        """
        Liquidity sweep + reclaim: harga tembus level lalu balik dalam <=2 candle.
        LONG: sweep low (tembus range_low ke bawah lalu reclaim ke atas).
        SHORT: sweep high (tembus range_high ke atas lalu reject ke bawah).
        """
        if len(closes) < self.SWEEP_RECLAIM_BARS + 1:
            return False
        recent_low = min(lows[-(self.SWEEP_RECLAIM_BARS+1):])
        recent_high = max(highs[-(self.SWEEP_RECLAIM_BARS+1):])
        cur = closes[-1]
        if side == "long":
            for name in ("range_1h_low", "range_4h_low", "range_full_low"):
                lvl = levels.get(name, 0)
                if lvl > 0 and recent_low < lvl and cur > lvl:
                    return True  # tembus low lalu reclaim
        else:
            for name in ("range_1h_high", "range_4h_high", "range_full_high"):
                lvl = levels.get(name, 0)
                if lvl > 0 and recent_high > lvl and cur < lvl:
                    return True  # tembus high lalu reject
        return False

    def detect_breakout(self, highs: List[float], lows: List[float], closes: List[float],
                        side: str, levels: Dict[str, float]) -> bool:
        """Breakout: harga tembus level dan close di luar (continuation)."""
        if len(closes) < 2:
            return False
        cur = closes[-1]
        if side == "long":
            for name in ("range_1h_high", "range_4h_high", "range_full_high"):
                lvl = levels.get(name, 0)
                if lvl > 0 and cur > lvl:
                    return True
        else:
            for name in ("range_1h_low", "range_4h_low", "range_full_low"):
                lvl = levels.get(name, 0)
                if lvl > 0 and cur < lvl:
                    return True
        return False

    # ──────────────────────────────────────────
    # MAIN EVALUATION
    # ──────────────────────────────────────────

    def evaluate(
        self,
        asset: str,
        side: str,                      # "long" / "short"
        htf_regime: str,                # TRENDING_UP / TRENDING_DOWN / CHOPPY
        candles: list,
        price: float,
        realized_vol: float,
        cvd_dir: float,                 # CVD 5m directional (aligned to side), -1..1
        ob_dir: int,                    # OB signed aligned to side
        net_move_5m: float,             # net price move 5m (signed to side)
        spread_pct: float = 0.0,
        oi_usd: float = 0.0,
    ) -> GateDecision:
        d = GateDecision(passed=False)
        side = (side or "").lower()

        highs, lows, closes = self._extract_ohlc(candles)

        # ═══════════════════════════════════════════
        # LAPIS 1 — HARD GATES (murah, reject)
        # ═══════════════════════════════════════════

        # G1: Regime align (CHOPPY = dua arah; hanya counter-trend murni di-block)
        if side == "long" and htf_regime == "TRENDING_DOWN":
            d.reject_reason = "long_against_downtrend"; return d
        if side == "short" and htf_regime == "TRENDING_UP":
            d.reject_reason = "short_against_uptrend"; return d

        # G2: Exhaustion veto
        if cvd_dir >= self.CVD_EXTREME:
            d.reject_reason = "cvd_exhaustion"; return d

        # G3: Junk filter (extreme only)
        if realized_vol > self.RV_HARD_MAX:
            d.reject_reason = f"rv_extreme_{realized_vol*100:.0f}pct"; return d
        if spread_pct > 0.0015:
            d.reject_reason = "spread_too_wide"; return d
        if oi_usd > 0 and oi_usd < 50_000_000:
            d.reject_reason = "oi_too_thin"; return d

        # G4: Displacement proof (bunuh time_exit — jangan masuk market flat)
        _min_disp = self.MIN_DISPLACEMENT if side == "long" else self.MIN_DISP_SHORT
        if abs(net_move_5m) < _min_disp:
            d.reject_reason = "no_displacement"; return d
        # arah displacement harus searah trade
        if (side == "long" and net_move_5m < 0) or (side == "short" and net_move_5m > 0):
            d.reject_reason = "displacement_wrong_dir"; return d

        # ═══════════════════════════════════════════
        # LAPIS 2 — SIZING TIERS (tidak reject)
        # ═══════════════════════════════════════════
        d.passed = True

        # Vol tier — [AUDIT #21] RV r=-0.50, p=0.0005 → 6-8% dikecilkan ke 0.3×
        if realized_vol <= self.RV_FULL_MAX:
            vol_mult = 1.0; d.rv_tier = "full"
        elif realized_vol <= self.RV_REDUCED_MAX:
            vol_mult = 0.75; d.rv_tier = "0.75x"
        else:
            vol_mult = 0.3; d.rv_tier = "0.3x"     # 6-8%: damage control, size kecil

        # Liquidity context → tier A/B
        # [v10 OB EDGE] OB strong wall = predictor terkuat (r=+0.208 p=0.049, 90 trade).
        # OB strong+trend aligned: WR 44.4% +$5.41. OB ZERO: WR 0% -$16.29 (bencana).
        levels = self.compute_levels(highs, lows, closes)
        is_near, lvl_name = self.near_level(price, levels)
        has_strong_wall = abs(ob_dir) >= self.OB_WALL_STRONG
        has_cvd_mod = 0.30 <= cvd_dir < self.CVD_EXTREME
        d.has_liq_context = is_near or has_strong_wall or has_cvd_mod

        # [v10 OB GATE] OB zero + tanpa konteks likuiditas lain = WR 0% historis.
        # Reject: tidak ada bukti likuiditas institusional sama sekali.
        _ob_zero = (ob_dir == 0)
        if _ob_zero and not is_near and not has_cvd_mod:
            d.passed = False
            d.reject_reason = "no_liquidity_evidence"  # OB zero + no level + no CVD
            return d

        d.near_level = lvl_name

        # [v10 OB EDGE] OB strong wall + searah HTF trend = kombinasi terbaik
        # (WR 44.4%, +$5.41). Boost size 1.5×, Tier S (entry quality premium).
        _ob_trend_aligned = (
            has_strong_wall and (
                (side == "long" and htf_regime == "TRENDING_UP") or
                (side == "short" and htf_regime == "TRENDING_DOWN")
            )
        )

        # Entry Quality Tiers: S > A > B
        if _ob_trend_aligned:
            d.tier = "S"; liq_mult = 1.0
        elif d.has_liq_context:
            d.tier = "A"; liq_mult = 1.0
        else:
            d.tier = "B"; liq_mult = 0.6   # tanpa konteks = size kecil, TAPI tetap trade

        ob_boost = 1.5 if _ob_trend_aligned else 1.0

        d.size_mult = round(max(1.0, vol_mult * liq_mult * ob_boost), 3)

        # ═══════════════════════════════════════════
        # LAPIS 3 — SETUP CLASSIFIER (label)
        # ═══════════════════════════════════════════
        if self.detect_sweep(highs, lows, closes, side, levels):
            d.setup = "sweep"
        elif self.detect_breakout(highs, lows, closes, side, levels):
            d.setup = "breakout"
        elif htf_regime in ("TRENDING_UP", "TRENDING_DOWN") and is_near:
            d.setup = "pullback"
        else:
            d.setup = "momentum"

        d.reasons.append(
            f"🚪 v10 GATE PASS | tier={d.tier} setup={d.setup} size×{d.size_mult} "
            f"liq={int(d.has_liq_context)}({lvl_name or 'none'}) rv={d.rv_tier} "
            f"disp={net_move_5m*100:+.2f}% ob_dir={ob_dir} cvd={cvd_dir:.2f}"
        )
        return d


# Singleton
gate_system = GateSystem()
