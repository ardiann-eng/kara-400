"""
KARA v10 — Gate System (REKONSTRUKSI Fase 2.1 — Scalping Regime + Diversity Scope)

Mengganti scoring aditif 8-komponen dengan sistem 3-lapis institusional:
  LAPIS 1 — HARD GATES (murah, regime-agnostic): scalp regime, exhaustion, junk, displacement
  LAPIS 2 — ENTRY TIERS (modulate size, TIDAK reject): G1 modifier, liquidity, vol, OI, diversity
  LAPIS 3 — SETUP CLASSIFIER (label + diversity update): sweep / pullback / breakout / momentum

Perubahan Fase 2.1 (2026-06-07):
  [P1 — SCALP REGIME] G1 ganti 1h EMA8/21 → scalp regime EMA5/13 dari 1m closes
    → Scalping 15 menit pakai trend 5-13 menit, BUKAN 1h.
    → Counter-trend scalp TIDAK di-block. Hanya size reduction:
        counter scalp (0.75x) | double CT scalp+HTF (0.55x) | CHOPPY scalp (0.90x)
  [P3 — DIVERSITY SCOPE] Diversity key dari {asset} → {asset}_{side}_{setup}
    → Hanya penalize entry SAMA (arah + setup identik).
    → LONG sweep lalu LONG breakout 60 detik = tidak kena (setup beda).
    → Penalty diturunkan: 0.6→0.75 (repeat), 0.8→0.85 (medium).

Entry Quality Tiers:
  S = OB trend-aligned (strong wall + searah HTF) — premium entry, +$5.41 WR 44.4%
  A = Good liquidity context (near level / OB wall / CVD moderate)
  B = No strong evidence, but passes all gates — acceptable, size dikurangi

Prinsip anti-over-filter (funnel study 90 sinyal):
  Hanya filter MURAH yang hard-reject. Filter yang buang >30% sinyal = sizing modifier.
  RV filter = SIZING (full/0.75x/0.3x), block hanya >8% (AUDIT #21: RV r=-0.50 -> tighten).
  OI filter = SIZING ($10-50M = 0.6x), block hanya <$10M.
  G1 Regime = SIZING (bukan reject), sesuai prinsip scalping 15 menit.

Edge yang dipertahankan: trailing stop, momentum death (di risk_manager, bukan di sini).
Edge baru: level likuiditas (session H/L) + order flow (OB wall, CVD) + scalp regime.

Semua perhitungan dari data yang SUDAH ada (candles, OB, trades). Zero API call.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from collections import deque


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
    Sistem gate v10 Fase 2. Stateful: menyimpan CVD history + diversity tracker.
    Dipanggil dari scoring_engine setelah direction + htf_regime ditentukan.
    """

    # ── Thresholds (dari funnel study + audit data — [AUDIT #21] tuned) ──
    RV_HARD_MAX        = 0.08    # >8% = hard reject (diturunkan dari 15%, RV r=-0.50 p=0.0005)
    RV_FULL_MAX        = 0.04    # <=4% = size penuh
    RV_REDUCED_MAX     = 0.06    # 4-6% = 0.75x
    # 6-8% = 0.3x (damage control: vol tinggi tapi belum ekstrem, size dikecilkan drastis)

    # ── CVD — PREDICTIVE dengan EWMA + SLOPE (Fase 2.1) ──
    CVD_EXTREME        = 0.85    # Hard reject: safety cap
    CVD_MOMENTUM_WARN  = 0.65    # Trigger predictive check: CVD >= 0.65 -> cek momentum
    CVD_MOMENTUM_FADE  = -0.06   # [F2.1] Momentum < -0.06 + CVD >= 0.65 = exhaustion (reject, diturunkan dari -0.08 karena EWMA+slope lebih presisi)
    CVD_MOMENTUM_HIST  = 15      # [F2.1] 15 history points (~3.75 menit) — cukup untuk scalping 15m
    CVD_EWMA_ALPHA     = 0.40    # [F2.1] EWMA smoothing factor: 0.40 = 60% weight ke recent
    CVD_SLOPE_PERIODS  = 5       # [F2.1] Linear regression window — 5 readings ~1.25 menit

    # ── OI — ADAPTIVE TIERING (Fase 2: bukan hard reject $50M) ──
    OI_HARD_MIN        = 10_000_000   # [FASE 2] $10M hard floor (was $50M hard reject)
    OI_SMALL_CAP       = 50_000_000   # [FASE 2] $10-50M = small cap, reduced size 0.6x

    # ── Level & Setup ──
    LEVEL_PROXIMITY    = 0.004   # dalam 0.4% dari level = "di level"
    SWEEP_RECLAIM_BARS = 2       # tembus level lalu balik dalam <=2 candle
    OB_WALL_STRONG     = 12      # ob_dir >= 12 = institutional wall (static fallback)
    # ── OB Adaptive (Opsi B: threshold per aset dari median 24h) ──
    OB_HISTORY_SIZE    = 100     # track 100 scan ~25 menit
    OB_MEDIAN_MULT     = 2.0     # threshold = median(|ob_dir|) * 2
    OB_THRESHOLD_MIN   = 4       # minimum threshold (aset sangat tipis)
    OB_THRESHOLD_MAX   = 24      # maximum threshold (BTC/SOL dalam)
    MIN_DISPLACEMENT   = 0.0008  # 0.08% net move = displacement proof (LONG) — [AUDIT #21] loosened from 0.0015
    MIN_DISP_SHORT     = 0.0012  # 0.12% SHORT — [AUDIT #21] loosened from 0.0020

    # ── Scalp Regime (P1: scalping 5-13m, bukan 1h) ──
    SCALP_EMA_FAST     = 5       # EMA period cepat untuk scalp regime
    SCALP_EMA_SLOW     = 13      # EMA period lambat untuk scalp regime
    SCALP_EMA_GAP      = 1.0005  # 0.05% gap — sensitif untuk 1m data

    # ── G1: Counter-trend size modifier (P1: bukan hard block) ──
    G1_COUNTER_TREND_MULT = 0.75    # counter-trend scalp → size 0.75x
    G1_DOUBLE_PENALTY_MULT = 0.55   # counter-trend scalp + counter-trend 1h → size 0.55x
    G1_CHOPPY_MULT        = 0.90    # CHOPPY scalp → size 0.90x

    # ── Diversity (P3: scoped ke direction + setup) ──
    DIVERSITY_WINDOW   = 120     # Dalam 120 detik = repeat trade → penalized
    DIVERSITY_PENALTY  = 0.75    # [P3] Size mult untuk repeat (diturunkan dari 0.6)
    DIVERSITY_MEDIUM   = 0.85    # [P3] Penalty medium (diturunkan dari 0.8)
    DIVERSITY_MAX_CAP  = 2       # Maks 2 entry (asset+side+setup) per diversity_window

    def __init__(self):
        # CVD history per asset untuk momentum calculation
        self._cvd_history: Dict[str, deque] = {}
        # Lacak asset yang baru lolos gate (untuk diversity)
        # asset -> [timestamps dari gate passes]
        self._recent_passes: Dict[str, deque] = {}
        # [Opsi B] OB strength history per asset — untuk threshold adaptif
        # asset -> deque(|ob_dir| values)
        self._ob_histories: Dict[str, deque] = {}

    # ──────────────────────────────────────────
    # OB ADAPTIVE THRESHOLD (Opsi B)
    # ──────────────────────────────────────────

    def _track_ob(self, asset: str, ob_dir: int):
        """Record |ob_dir| untuk tracking median per aset."""
        if asset not in self._ob_histories:
            self._ob_histories[asset] = deque(maxlen=self.OB_HISTORY_SIZE)
        self._ob_histories[asset].append(abs(ob_dir))

    def _get_ob_threshold(self, asset: str, current_ob: int = 0) -> int:
        """
        Dynamic OB wall threshold per aset.
        Formula: max(MIN, min(MAX, median(|ob|) * MEDIAN_MULT))
        - Asset tipis (median ~1-2): threshold ~2-4
        - Asset sedang (median ~5-7): threshold ~10-14  
        - Asset dalam (median ~8-12): threshold ~16-24
        """
        hist = self._ob_histories.get(asset)
        if hist is None or len(hist) < 20:
            # Not enough data — use static fallback
            return self.OB_WALL_STRONG
        vals = sorted(hist)
        n = len(vals)
        median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
        dynamic = max(self.OB_THRESHOLD_MIN, min(self.OB_THRESHOLD_MAX, int(round(median * self.OB_MEDIAN_MULT))))
        return dynamic

    # ──────────────────────────────────────────
    # CVD MOMENTUM (predictive exhaustion)
    # ──────────────────────────────────────────

    def _store_cvd(self, asset: str, side: str, cvd_val: float):
        """
        Simpan CVD value untuk momentum tracking.
        Key = (asset, side) karena LONG/SHORT punya tanda CVD berlawanan.
        """
        key = f"{asset}_{side}"
        if key not in self._cvd_history:
            self._cvd_history[key] = deque(maxlen=self.CVD_MOMENTUM_HIST + 1)
        self._cvd_history[key].append(cvd_val)

    def _calc_cvd_momentum(self, asset: str, side: str) -> float:
        """
        CVD momentum — EWMA baseline + linear regression slope.
        
        Menggantikan simple average dengan:
          1. EWMA baseline: exponentially weighted, recent lebih penting
          2. Linear regression slope: trend direction over last N readings
             - Slope positif = masih accelerating (CVD menguat)
             - Slope negatif = decelerating (CVD melemah = exhaustion)
          3. Deviation: current vs EWMA baseline
        
        Masalah lama: simple average memberi bobot sama ke semua history.
        Akibatnya CVD yang naik 0.30→0.85 lalu turun 0.85→0.78 tetap dianggap
        momentum positif (karena average 0.70, current 0.78 masih di atas).
        Dengan EWMA + slope, penurunan recent terdeteksi sebagai exhaustion.
        
        Returns: float, range ~[-0.3, +0.3] — kompatibel dengan threshold -0.08.
        Positif = still accelerating (strong flow, NOT exhaustion).
        Negatif = decelerating (flow fading -> exhaustion risk).
        """
        key = f"{asset}_{side}"
        hist = self._cvd_history.get(key)
        if hist is None or len(hist) < 3:
            return 0.0

        vals = list(hist)
        n = len(vals)
        current = vals[-1]

        # 1. EWMA baseline — exponentially weighted moving average
        alpha = self.CVD_EWMA_ALPHA
        ewma = vals[0]
        for v in vals[1:]:
            ewma = v * alpha + ewma * (1.0 - alpha)
        deviation = current - ewma

        # 2. Linear regression slope over last SLOPE_PERIODS
        k = min(n, self.CVD_SLOPE_PERIODS)
        if k >= 3:
            y = vals[-k:]
            x_mean = (k - 1.0) / 2.0
            y_mean = sum(y) / k
            num = 0.0
            den = 0.0
            for i in range(k):
                dx = float(i) - x_mean
                dy = y[i] - y_mean
                num += dx * dy
                den += dx * dx
            slope = num / den if den != 0.0 else 0.0
        elif k == 2:
            slope = vals[-1] - vals[-2]  # simple difference
        else:
            slope = 0.0

        # 3. Composite momentum
        # slope*k*0.7 = recent trend (dominant, karena slope langsung mencerminkan
        #    arah CVD dalam SLOPE_PERIODS terakhir — bebas dari spike lama)
        # deviation*0.3 = current vs EWMA baseline (secondary, karena EWMA masih
        #    terpengaruh oleh history jauh yang mungkin sudah tidak relevan)
        # Positive combination = still accelerating (CVD semakin kuat)
        # Negative = decelerating or reversing (CVD melemah = exhaustion)
        momentum = slope * k * 0.7 + deviation * 0.3

        # Clamp ke range kompatibel dengan threshold lama (-0.08)
        return max(-0.5, min(0.5, momentum))

    # ──────────────────────────────────────────
    # DIVERSITY TRACKER — [P3] Scoped ke direction + setup
    # ──────────────────────────────────────────

    def _mark_passed(self, asset: str, side: str = "", setup: str = ""):
        """
        [P3] Catat (asset, side, setup) yang baru lolos gate.
        Hanya penalize kalau entry SAMA (arah + setup sama).
        Scalping momentum beruntun dalam 60 detik = valid (momentum run).
        """
        key = f"{asset}_{side}_{setup}"
        if key not in self._recent_passes:
            self._recent_passes[key] = deque(maxlen=10)
        self._recent_passes[key].append(time.time())

    def _get_diversity_mult(self, asset: str, side: str = "", setup: str = "") -> float:
        """
        [P3] Hitung diversity multiplier berdasarkan (asset, side, setup).
        - Hanya penalize kalau entry SAMA (arah + setup identik).
        - Entry beda arah atau beda setup = tidak kena penalty (dianggap setup baru).
        - Scalping momentum: entry LONG 2x dalam 60 detik dengan setup 'momentum'
          tetap kena penalty (cegah overtrading). Tapi LONG setup 'sweep' lalu 'breakout'
          dalam 60 detik = tidak kena (setup beda = thesis beda).
        """
        key = f"{asset}_{side}_{setup}"
        passes = self._recent_passes.get(key)
        if not passes:
            return 1.0
        now = time.time()
        recent = [t for t in passes if now - t < self.DIVERSITY_WINDOW]
        if not recent:
            return 1.0
        count = len(recent)
        if count >= self.DIVERSITY_MAX_CAP:
            return self.DIVERSITY_PENALTY  # udah 2+ entry sama arah+setup
        elapsed = now - recent[-1]
        if elapsed < 60:
            return self.DIVERSITY_PENALTY  # < 1 menit: high penalty
        elif elapsed < self.DIVERSITY_WINDOW:
            return self.DIVERSITY_MEDIUM   # 1-2 menit: medium penalty
        return 1.0

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
        Hitung level likuiditas objektif dari candle history + pivot points.
        Session H/L diproksi dari window (candle 1m: 60=1h, 240=4h).
        Pivot points dari 1h window — cocok untuk scalping hold max 20 menit.
        Return dict level -> harga. Level diabaikan jika range terlalu sempit
        (high~~low = pasar mati, bukan level likuiditas nyata).
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

        # Range-based levels
        if n >= 60:
            _add("range_1h_high", "range_1h_low", max(highs[-60:]), min(lows[-60:]))
        if n >= 240:
            _add("range_4h_high", "range_4h_low", max(highs[-240:]), min(lows[-240:]))
        if not levels:
            _add("range_full_high", "range_full_low", max(highs), min(lows))

        # Pivot points dari 1h window — untuk scalping 20 menit
        if n >= 60:
            h60 = max(highs[-60:])
            l60 = min(lows[-60:])
            c60 = closes[-1]
            mid60 = (h60 + l60) / 2
            if mid60 > 0 and (h60 - l60) / mid60 >= _MIN_RANGE:
                pivot = round((h60 + l60 + c60) / 3, 8)
                rng = h60 - l60
                levels["pivot"] = pivot
                levels["r1"] = round(2 * pivot - l60, 8)
                levels["s1"] = round(2 * pivot - h60, 8)
                levels["r2"] = round(pivot + rng, 8)
                levels["s2"] = round(pivot - rng, 8)

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

    @staticmethod
    def _detect_scalp_regime(closes: List[float]) -> str:
        """
        [P1] Scalping regime dari 1m closes — EMA5 vs EMA13.
        Scalping 15 menit pakai trend 5-13 menit, BUKAN 1h.
        Gap 0.05% cukup sensitif untuk micro-trend detection.
        Returns: TRENDING_UP / TRENDING_DOWN / CHOPPY
        """
        if len(closes) < GateSystem.SCALP_EMA_SLOW:
            return "CHOPPY"
        k_fast = 2 / (GateSystem.SCALP_EMA_FAST + 1)
        k_slow = 2 / (GateSystem.SCALP_EMA_SLOW + 1)
        ema_fast = closes[0]
        ema_slow = closes[0]
        for v in closes[1:]:
            ema_fast = v * k_fast + ema_fast * (1 - k_fast)
            ema_slow = v * k_slow + ema_slow * (1 - k_slow)
        gap = GateSystem.SCALP_EMA_GAP
        if ema_fast > ema_slow * gap:
            return "TRENDING_UP"
        elif ema_fast < ema_slow * (2 - gap):
            return "TRENDING_DOWN"
        return "CHOPPY"

    def evaluate(
        self,
        asset: str,
        side: str,                      # "long" / "short"
        htf_regime: str,                # TRENDING_UP / TRENDING_DOWN / CHOPPY (1h, untuk OB alignment)
        candles: list,
        price: float,
        realized_vol: float,
        cvd_dir: float,                 # CVD 5m directional (aligned to side), -1..1
        ob_dir: int,                    # OB signed aligned to side
        net_move_5m: float,             # net price move 5m (signed to side)
        spread_pct: float = 0.0,
        oi_usd: float = 0.0,
        ob_levels: Optional[Dict[str, float]] = None,  # OB cluster levels dari orderbook
        scalp_regime: str = "",         # [P1] Scalping regime (5-13m). Otodetect jika kosong.
    ) -> GateDecision:
        d = GateDecision(passed=False)
        side = (side or "").lower()

        # Store CVD value for momentum tracking (even before pass/fail)
        # Keyed by (asset, side) karena tanda CVD terbalik untuk LONG vs SHORT
        self._store_cvd(asset, side, cvd_dir)

        highs, lows, closes = self._extract_ohlc(candles)

        # [P1] Scalping regime — otodetect dari 1m closes jika tidak dipass dari luar
        if not scalp_regime:
            scalp_regime = self._detect_scalp_regime(closes)

        # [Opsi B] Track OB strength untuk threshold adaptif
        self._track_ob(asset, ob_dir)

        # ═══════════════════════════════════════════
        # LAPIS 1 — HARD GATES (murah, reject)
        # ═══════════════════════════════════════════

        # [P1] G1: Scalping regime — BUKAN hard block, tapi size modifier
        # Counter-trend scalp valid untuk 15m hold. Jangan block.
        _g1_ct = (
            (side == "long" and scalp_regime == "TRENDING_DOWN") or
            (side == "short" and scalp_regime == "TRENDING_UP")
        )
        _g1_aligned = (
            (side == "long" and scalp_regime == "TRENDING_UP") or
            (side == "short" and scalp_regime == "TRENDING_DOWN")
        )
        # Double penalty: counter-trend scalp + counter-trend 1h = terlalu berat
        _g1_double_ct = (
            (side == "long" and htf_regime == "TRENDING_DOWN") or
            (side == "short" and htf_regime == "TRENDING_UP")
        )
        # Simpan untuk dipakai di Lapis 2
        _g1_mult = 1.0
        if _g1_ct:
            if _g1_double_ct:
                _g1_mult = self.G1_DOUBLE_PENALTY_MULT  # 0.55x: counter both timeframes
            else:
                _g1_mult = self.G1_COUNTER_TREND_MULT   # 0.75x: counter scalp aja
        elif not _g1_aligned:
            _g1_mult = self.G1_CHOPPY_MULT               # 0.90x: CHOPPY scalp

        # G2: Exhaustion veto — PREDICTIVE (Fase 2)
        # [FASE 2] Daripada reject semua CVD >= 0.70, kita bedakan:
        #   - CVD >= 0.85                  → hard reject (safety cap, naik dari 0.70)
        #   - CVD >= 0.65 DAN momentum negatif → exhaustion, reject
        #   - CVD >= 0.65 DAN momentum positif → strong flow, ALLOW (bukan exhaustion)
        if cvd_dir >= self.CVD_EXTREME:
            d.reject_reason = "cvd_exhaustion_extreme"; return d
        if cvd_dir >= self.CVD_MOMENTUM_WARN:
            _mom = self._calc_cvd_momentum(asset, side)
            if _mom < self.CVD_MOMENTUM_FADE:
                d.reject_reason = f"cvd_exhaustion_mom_{_mom:+.2f}"; return d
            # Else: high CVD but still accelerating → strong ongoing flow, allow

        # G3: Junk filter — OI ADAPTIVE (Fase 2)
        # [FASE 2] OI $10M floor (turun dari $50M). $10-50M = reduced size (bukan reject).
        if realized_vol > self.RV_HARD_MAX:
            d.reject_reason = f"rv_extreme_{realized_vol*100:.0f}pct"; return d
        if spread_pct > 0.0015:
            d.reject_reason = "spread_too_wide"; return d
        # [FASE 2] OI hard floor: hanya reject di <$10M (bukan $50M)
        if oi_usd > 0 and oi_usd < self.OI_HARD_MIN:
            d.reject_reason = "oi_too_thin"; return d
        # OI $10-50M: flagged untuk sizing modifier di Lapis 2
        _oi_small_cap = oi_usd > 0 and oi_usd < self.OI_SMALL_CAP

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

        multipliers_note = []  # untuk log

        # [P1] G1 multiplier (dari scalp regime — applied early di Lapis 2)
        if _g1_mult < 1.0:
            if _g1_double_ct:
                multipliers_note.append(f"g1_dbl={_g1_mult}")
            elif _g1_ct:
                multipliers_note.append(f"g1_ct={_g1_mult}")
            else:
                multipliers_note.append(f"g1_choppy={_g1_mult}")

        # Vol tier — [AUDIT #21] RV r=-0.50, p=0.0005 → 6-8% dikecilkan ke 0.3x
        if realized_vol <= self.RV_FULL_MAX:
            vol_mult = 1.0; d.rv_tier = "full"
        elif realized_vol <= self.RV_REDUCED_MAX:
            vol_mult = 0.75; d.rv_tier = "0.75x"
        else:
            vol_mult = 0.3; d.rv_tier = "0.3x"     # 6-8%: damage control, size kecil
        if vol_mult < 1.0:
            multipliers_note.append(f"rv={vol_mult}")

        # OI tier — [FASE 2] Adaptive sizing untuk small-cap
        oi_mult = 0.6 if _oi_small_cap else 1.0
        if oi_mult < 1.0:
            multipliers_note.append(f"oi={oi_mult}")

        # Liquidity context → tier A/B
        # [v10 OB EDGE] OB strong wall = predictor terkuat (r=+0.208 p=0.049, 90 trade).
        # OB strong+trend aligned: WR 44.4% +$5.41. OB ZERO: WR 0% -$16.29 (bencana).
        levels = self.compute_levels(highs, lows, closes)
        # Merge OB cluster levels dari orderbook (bid wall = support, ask wall = resistance)
        if ob_levels:
            levels.update(ob_levels)
        is_near, lvl_name = self.near_level(price, levels)
        # [Opsi B] Dynamic OB threshold per aset — bukan fix 12
        _ob_threshold = self._get_ob_threshold(asset, abs(ob_dir))
        has_strong_wall = abs(ob_dir) >= _ob_threshold
        has_cvd_mod = 0.30 <= cvd_dir < self.CVD_EXTREME  # moderate CVD
        d.has_liq_context = is_near or has_strong_wall or has_cvd_mod

        # [v10 OB GATE] OB zero + tanpa konteks likuiditas lain = WR 0% historis.
        # Reject: tidak ada bukti likuiditas institusional sama sekali.
        _ob_zero = (ob_dir == 0)
        if _ob_zero and not is_near and not has_cvd_mod:
            d.passed = False
            d.reject_reason = "no_liquidity_evidence"  # OB zero + no level + no CVD
            return d

        d.near_level = lvl_name

        # [v10 OB EDGE + Opsi A] OB strong wall + searah SCALP regime = Grade S
        # Scalp regime (EMA5/13 dari 1m) sinkron dengan timeframe trading 15m.
        # HTF 1h masih di-log tapi tidak dipakai untuk grade — terlalu lambat.
        _ob_trend_aligned = (
            has_strong_wall and (
                (side == "long" and scalp_regime == "TRENDING_UP") or
                (side == "short" and scalp_regime == "TRENDING_DOWN")
            )
        )

        # Entry Quality Tiers: S > A > B
        if _ob_trend_aligned:
            d.tier = "S"; liq_mult = 1.0
        elif d.has_liq_context:
            d.tier = "A"; liq_mult = 1.0
        else:
            d.tier = "B"; liq_mult = 0.6   # tanpa konteks = size kecil, TAPI tetap trade
        if liq_mult < 1.0:
            multipliers_note.append(f"liq={liq_mult}")

        ob_boost = 1.5 if _ob_trend_aligned else 1.0
        if ob_boost > 1.0:
            multipliers_note.append(f"ob={ob_boost}")

        # [P3] Diversity akan dihitung SETELAH Lapis 3 (setup final diketahui)
        # Sementara simpan multiplier parsial dulu
        _partial_mults = {
            "g1": _g1_mult, "vol": vol_mult, "oi": oi_mult,
            "liq": liq_mult, "ob": ob_boost,
        }
        # Set size_mult sementara (akan di-update setelah Lapis 3 + diversity)
        d.size_mult = round(max(0.3, _g1_mult * vol_mult * oi_mult * liq_mult * ob_boost), 3)

        # ═══════════════════════════════════════════
        # LAPIS 3 — SETUP CLASSIFIER (label)
        # ═══════════════════════════════════════════
        if self.detect_sweep(highs, lows, closes, side, levels):
            d.setup = "sweep"
        elif self.detect_breakout(highs, lows, closes, side, levels):
            d.setup = "breakout"
        elif scalp_regime in ("TRENDING_UP", "TRENDING_DOWN") and is_near:
            d.setup = "pullback"
        else:
            d.setup = "momentum"

        # ═══════════════════════════════════════════
        # [P3] DIVERSITY — dihitung SETELAH setup final diketahui
        # ═══════════════════════════════════════════
        diversity_mult = self._get_diversity_mult(asset, side, d.setup)
        if diversity_mult < 1.0:
            multipliers_note.append(f"div={diversity_mult}")

        # [P3] Compound size_mult FINAL — semua multiplier termasuk diversity
        d.size_mult = round(max(0.3,
            _partial_mults["g1"] * _partial_mults["vol"] *
            _partial_mults["oi"] * _partial_mults["liq"] *
            _partial_mults["ob"] * diversity_mult
        ), 3)

        # [P3] Mark passed dengan key lengkap (asset+side+setup)
        self._mark_passed(asset, side, d.setup)

        # [P1] Log scalp regime juga
        _mult_str = "×".join(multipliers_note) if multipliers_note else "1.0"
        d.reasons.append(
            f"🚪 v10 GATE PASS | tier={d.tier} setup={d.setup} size×{d.size_mult} "
            f"[{_mult_str}] "
            f"liq={int(d.has_liq_context)}({lvl_name or 'none'}) rv={d.rv_tier} "
            f"disp={net_move_5m*100:+.2f}% ob_dir={ob_dir} cvd={cvd_dir:.2f} "
            f"oi={oi_usd/1e6:.0f}M "
            f"scalp={scalp_regime} htf={htf_regime}"
        )
        return d


# Singleton
gate_system = GateSystem()
