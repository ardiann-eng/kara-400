"""
KARA Bot - Risk Manager
THE most critical module. Protects student capital.
Never bypassed. Never disabled. Always running.

Features:
- Position sizing formula (account-aware, mode-aware)
- Daily loss limit enforcement (per-mode thresholds)
- Max drawdown kill-switch
- Post-loss cooldown
- Concurrent position limits (3 scalper / 10 standard)
- Margin check before execution
- Time-based Exit, Dynamic TP, Aggressive Trailing Stop
- Scalper Mode: 12-min force-exit, 0.20% trailing
"""

from __future__ import annotations
import asyncio
import logging
import time
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.db import user_db
from config import RISK, SCALPER, MODE
from models.schemas import (
    AccountState, Position, TradeSignal, Side, PositionStatus,
    BotMode, ExecutionMode
)
from utils.helpers import format_usd

log = logging.getLogger("kara.risk")


class RiskViolation(Exception):
    """Raised when a trade violates risk rules. Non-fatal."""
    pass


class RiskManager:
    """
    Enforces all risk rules before any trade is executed.
    Also manages trailing stops and TP logic.
    """

    def __init__(self, mode_manager=None, chat_id: str = ""):
        self._chat_id         = chat_id
        self._daily_pnl:      float = 0.0
        self._peak_balance:   float = 0.0
        self._session_start_balance: float = 0.0
        self._last_reset_day: Optional[str] = None   # YYYY-MM-DD
        self._cooldown_until: Optional[datetime] = None  # UTC datetime — persists across restarts
        self._kill_switch:    bool = False
        self._paused:         bool = False
        self._latest_score:   Dict[str, int] = {}     # asset -> latest score from scanner

        # --- Hydrate from persisted state if exists
        self._load_risk_state()

    def _persist_risk_state(self):
        if not self._chat_id: return
        user_db.save_risk_state(self._chat_id, {
            "daily_pnl":      self._daily_pnl,
            "peak_balance":   self._peak_balance,
            "session_start_balance": self._session_start_balance,
            "kill_switch":    self._kill_switch,
            "last_reset_day": self._last_reset_day,
            # Store as ISO string so it survives restart (monotonic() would not)
            "cooldown_until": self._cooldown_until.isoformat() if self._cooldown_until else None,
        })

    def _load_risk_state(self):
        if not self._chat_id: return
        state = user_db.load_risk_state(self._chat_id)
        if state:
            self._daily_pnl      = state.get("daily_pnl", 0.0)
            self._peak_balance   = state.get("peak_balance", 0.0)
            self._session_start_balance = state.get("session_start_balance", 0.0)
            self._kill_switch    = state.get("kill_switch", False)
            self._last_reset_day = state.get("last_reset_day")

            # Restore cooldown as UTC datetime
            raw_cd = state.get("cooldown_until")
            if raw_cd:
                try:
                    cd = datetime.fromisoformat(raw_cd)
                    # Only restore if it's still in the future
                    if cd > datetime.now(timezone.utc):
                        self._cooldown_until = cd
                        log.warning(
                            f"[RISK] Cooldown restored from DB — expires at {cd.isoformat()}"
                        )
                except Exception:
                    pass

            # Validation: if session_start_balance is 0 but we have a peak, use that as fallback
            # to prevent 'amnesia' during mid-day restarts
            if self._session_start_balance <= 0 and self._peak_balance > 0:
                self._session_start_balance = self._peak_balance

    def _cfg(self):
        """Return active mode config (SCALPER or RISK) based on current mode."""
        if self._is_scalper():
            return SCALPER
        return RISK

    def _is_scalper(self) -> bool:
        """True if scalper mode is currently active for this user."""
        if not self._chat_id: return False
        user = user_db.get_user(self._chat_id)
        if user and user.config.trading_mode == "scalper":
            return True
        return False

    def _is_live(self) -> bool:
        """True if user is in live (real money) trading mode."""
        if not self._chat_id: return False
        from models.schemas import BotMode
        user = user_db.get_user(self._chat_id)
        return user is not None and user.config.bot_mode == BotMode.LIVE

    def _get_user_value(self, key: str, global_fallback=None):
        """Helper to get mode-specific value from user config."""
        user = user_db.get_user(self._chat_id)
        if not user: return global_fallback
        
        is_scalper = user.config.trading_mode == "scalper"
        prefix = "scl_" if is_scalper else "std_"
        return getattr(user.config, f"{prefix}{key}", global_fallback)

    # ──────────────────────────────────────────
    # DAILY RESET
    # ──────────────────────────────────────────

    def reset_daily(self, current_balance: float) -> bool:
        """Call at midnight UTC or on first run. Returns True if reset happened."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._last_reset_day:
            self._daily_pnl     = 0.0
            self._session_start_balance = current_balance
            self._last_reset_day = today
            self._persist_risk_state()
            log.info(f"📅 Daily reset - session balance: {format_usd(current_balance)}")
            return True
        return False

    def reset_kill_switch(self, requester_id: str):
        admin_id = os.getenv("ADMIN_CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", ""))
        if requester_id != admin_id:
            raise PermissionError("Hanya admin yang bisa reset kill-switch.")
        self._kill_switch = False
        self._persist_risk_state()
        log.warning(f"Kill-switch explicitly reset by Admin {admin_id}")

    def update_score(self, asset: str, score: int):
        """Called by scanner to update the latest score for an asset."""
        self._latest_score[asset] = score

    # ──────────────────────────────────────────
    # PRE-TRADE CHECK
    # ──────────────────────────────────────────

    def pre_trade_check(
        self,
        signal: TradeSignal,
        account: AccountState,
        open_positions: List[Position],
    ) -> Tuple[bool, str]:
        """
        Full risk check before executing a trade.
        Returns (approved: bool, reason: str)
        """
        import config as _cfg_mod
        cfg = self._cfg()
        is_live = self._is_live()

        # ── Risk limits: live mode uses tighter env-var overrides ─────
        if is_live:
            max_dd     = _cfg_mod.LIVE_MAX_DRAWDOWN_PCT
            daily_hard = _cfg_mod.LIVE_DAILY_LOSS_HARD_PCT
        else:
            max_dd     = cfg.max_drawdown_pct if hasattr(cfg, 'max_drawdown_pct') else RISK.max_drawdown_pct
            daily_hard = cfg.daily_loss_hard_pct if hasattr(cfg, 'daily_loss_hard_pct') else RISK.daily_loss_hard_pct

        # Kill switch TIDAK pernah auto-reset — hanya admin yang bisa reset via reset_kill_switch().
        # Auto-reset dihapus karena berbahaya: drawdown -95% → harga naik 1% → bot trading lagi dari -93%.
        if self._kill_switch or account.kill_switch_active:
            return False, "🚨 KILL SWITCH ACTIVE - trading stopped (max drawdown hit)"

        # ── Intelligence Filter (ML Expected Edge) ────────────────────
        import config as _cfg
        from intelligence.intelligence_model import intelligence_model as _im
        edge = getattr(signal, 'expected_edge', None)
        # Hanya block jika: intelligence aktif DAN model sudah is_ready (dilatih session ini)
        # is_ready=False berarti model dari disk stale atau belum ada data cukup
        if edge is not None and edge < 0.45 and _cfg.ENABLE_INTELLIGENCE and _im.is_ready:
            return False, f"🤖 [AI ABORT] Expected Edge too low ({edge*100:.1f}% win prob < 45%)"
        elif edge is not None and edge < 0.45:
            log.debug(
                f"[AI] {getattr(signal, 'asset', '?')}: low edge ({edge*100:.1f}%) "
                f"— passing through (is_ready={getattr(_im, 'is_ready', False)})"
            )

        # ── Paused ────────────────────────────────────────────────────
        if self._paused or account.is_paused:
            return False, "⏸️  Bot is paused by user"

        # ── Post-loss cooldown ─────────────────────────────────────────
        if self._cooldown_until and datetime.now(timezone.utc) < self._cooldown_until:
            remaining = int((self._cooldown_until - datetime.now(timezone.utc)).total_seconds())
            hrs = remaining // 3600
            mins = (remaining % 3600) // 60
            return False, f"❄️  Post-loss cooldown active - {hrs}h {mins}m remaining"

        # ── Concurrent positions cap (mode-aware & user-specific) ──────
        open_count = len([p for p in open_positions if p.status == PositionStatus.OPEN])
        max_pos = self._get_user_value("max_concurrent_positions", cfg.max_concurrent_positions)

        if open_count >= max_pos:
            mode_tag = "[SCALPER]" if self._is_scalper() else "[STANDARD]"
            return False, f"⛔ {mode_tag} Max concurrent positions ({max_pos}) reached"

        # ── Same asset already open (Pyramid Logic) ───────────────────
        asset_positions = [
            p for p in open_positions
            if p.asset == signal.asset and p.status == PositionStatus.OPEN
        ]
        if asset_positions:
            if self._is_scalper() and cfg.enable_pyramid:
                p = asset_positions[0]
                profit = p.floating_pct(signal.entry_price)
                if profit >= cfg.pyramid_at_profit_pct:
                    log.info(f"📐 [PYRAMID] Found profitable position on {signal.asset} ({profit*100:.2f}%). Allowing scale-in.")
                    signal.is_pyramid = True
                else:
                    return False, f"📌 Already holding {signal.asset} but profit {profit*100:.2f}% < {cfg.pyramid_at_profit_pct*100:.1f}% for pyramid"
            else:
                return False, f"📌 Already have an open position on {signal.asset}"

        # ── Daily loss limit ───────────────────────────────────────────
        daily_pnl_pct = self._daily_pnl / max(account.total_equity, 1)

        if abs(daily_pnl_pct) >= daily_hard and self._daily_pnl < 0:
            self._paused = True
            mode_tag = "[LIVE]" if is_live else "[PAPER]"
            return False, (
                f"🚫 {mode_tag} Daily loss limit reached: {daily_pnl_pct*100:.1f}% "
                f"(limit: {daily_hard*100:.0f}%) - trading paused for today"
            )

        if hasattr(RISK, 'daily_loss_limit_pct') and abs(daily_pnl_pct) >= RISK.daily_loss_limit_pct and self._daily_pnl < 0:
            log.warning(f"⚠️  Daily loss at {daily_pnl_pct*100:.1f}% — approaching limit")

        # ── Max drawdown kill-switch ───────────────────────────────────
        if account.current_drawdown_pct >= max_dd:
            self._kill_switch = True
            mode_tag = "[LIVE]" if is_live else "[PAPER]"
            return False, (
                f"🚨 {mode_tag} MAX DRAWDOWN KILL-SWITCH: {account.current_drawdown_pct*100:.1f}% "
                f"(limit: {max_dd*100:.0f}%) - ALL trading stopped."
            )

        # ── Available margin check ─────────────────────────────────────
        required_margin = self.calculate_margin_required(signal, account)
        if required_margin > account.available:
            return False, (
                f"💸 Insufficient margin - need {format_usd(required_margin)}, "
                f"have {format_usd(account.available)}"
            )

        return True, "✅ Risk check passed"

    # ──────────────────────────────────────────
    # POSITION SIZING
    # ──────────────────────────────────────────

    def calculate_position_size(
        self,
        signal: TradeSignal,
        account_balance: float,
    ) -> Tuple[float, float, int]:
        """
        Returns (size_usd, size_contracts)
        Formula: (balance * risk_pct) / (entry * sl_pct * leverage)
        OR Fixed Margin: size_usd = fixed_margin
        """
        entry = signal.entry_price
        if entry <= 0:
            raise ValueError("Invalid entry price")

        sl_pct = abs(signal.entry_price - signal.stop_loss) / signal.entry_price
        if sl_pct <= 0:
            sl_pct = RISK.default_sl_pct

        # ── Leverage: Triple-Cap (Signal vs User vs Exchange) ──────────
        # Dynamic Risk Sizing using Intelligence Model
        import config as _cfg
        if _cfg.ENABLE_INTELLIGENCE:
            from intelligence.dynamic_risk import calculate_risk_multiplier
            edge = getattr(signal, 'expected_edge', None)
            multiplier = calculate_risk_multiplier(edge)
        else:
            multiplier = 1.0

        # Scale leverage and risk parameter
        cfg = self._cfg()
        default_lev = signal.suggested_leverage
        actual_lev = min(int(default_lev * multiplier), cfg.max_leverage)
        user_max_lev = self._get_user_value("max_leverage", cfg.max_leverage)
        
        # Get exchange-allowed max for this specific asset (Market-Aware)
        from data.hyperliquid_client import get_client
        client = get_client()
        exchange_max = 50 # Default
        if client._market_cache:
            universe, _ = client._market_cache
            for u in universe:
                if isinstance(u, dict) and u.get("name") == signal.asset:
                    exchange_max = int(u.get("maxLeverage", 50))
                    break
        
        # Apply the triple cap
        lev = min(actual_lev, user_max_lev, exchange_max)

        if lev != signal.suggested_leverage:
            log.debug(
                f"🛡️ [RISK] {signal.asset} Leverage capped: "
                f"signal={signal.suggested_leverage}x, user={user_max_lev}x, exchange={exchange_max}x -> using {lev}x"
            )

        # ── 1. Determine size_usd (margin) — mode-aware ───────────────
        cfg = self._cfg()
        
        # --- CONVICTION-WEIGHTED POSITION SIZING (AGGRESSIVE) ---
        score = getattr(signal, 'score', 0)
        risk_pct = self.get_risk_pct(score, account_balance)
        
        # Apply AI Multiplier to Risk!
        risk_pct = min(risk_pct * multiplier, cfg.max_risk_per_trade_pct)

        # Compound sizing
        size_usd = (account_balance * risk_pct) / max(sl_pct * lev, 0.0001)

        # Drawdown guard: if we are >15% below peak, cut risk in half!
        # Find drawdown:
        drawdown = (self._peak_balance - account_balance) / max(self._peak_balance, 1)
        if drawdown >= 0.15:
            size_usd *= 0.5
            log.warning(f"[RISK] Drawdown guard active (DD: {drawdown*100:.1f}% >= 15%). Risk halved to {risk_pct/2*100:.1f}%.")

        # ── 3. Hard Margin Cap (Safety First - 35% Max Equity) ────────
        max_allowed_margin = account_balance * 0.35
        if size_usd > max_allowed_margin:
            log.warning(f"[RISK] Margin cap hit: {format_usd(size_usd)} -> {format_usd(max_allowed_margin)} (35% limit)")
            size_usd = max_allowed_margin

        # ── 4. Calculate Contracts ────────────────────────────────────
        # isolated margin = notional / leverage -> notional = margin * leverage
        notional = size_usd * lev
        contracts = notional / entry

        log.debug(
            f"[RISK] {signal.asset}: balance={format_usd(account_balance)} "
            f"margin={format_usd(size_usd)} lev={lev}x -> {contracts:.4f} contracts"
        )
        return round(size_usd, 2), round(contracts, 4), int(lev)

    def calculate_margin_required(
        self, signal: TradeSignal, account: AccountState
    ) -> float:
        """Margin = notional / leverage"""
        _, contracts, lev = self.calculate_position_size(signal, account.total_equity)
        notional = contracts * signal.entry_price
        return notional / lev

    def _calculate_trade_risk(
        self, signal: TradeSignal, balance: float
    ) -> float:
        """Max loss in USD if stop-loss is hit."""
        _, contracts = self.calculate_position_size(signal, balance)
        sl_pct = abs(signal.entry_price - signal.stop_loss) / signal.entry_price
        return contracts * signal.entry_price * sl_pct

    def get_risk_pct(self, score: int, equity: float) -> float:
        # Score-based tiers from 124-trade analysis (user-calibrated)
        if score >= 75:
            risk_pct = 0.035   # 3.5% high conviction
        elif score >= 68:
            risk_pct = 0.030   # 3.0%
        elif score >= 60:
            risk_pct = 0.025   # 2.5%
        else:
            risk_pct = 0.020   # 2.0% minimum
        
        # Equity protection multiplier
        ratio = equity / self._session_start_balance if self._session_start_balance > 0 else 1.0
        if ratio >= 1.5:   equity_mult = 0.8   # protect gains
        elif ratio <= 0.8: equity_mult = 0.5   # damaged mode
        else:              equity_mult = 1.0
        
        return risk_pct * equity_mult

    # ──────────────────────────────────────────
    # ATR HELPER (dipakai main.py untuk localize_for_user)
    # ──────────────────────────────────────────

    def calculate_tp_levels(self, asset: str, entry_price: float, side: Side, realized_vol: float) -> Tuple[float, float, float]:
        """Vol-based SL/TP pcts used by scoring engine R:R gate."""
        daily_vol = realized_vol
        if daily_vol > 0.05:
            sl_pct, tp1_pct, tp2_pct = 0.025, 0.040, 0.065
        elif daily_vol > 0.025:
            sl_pct, tp1_pct, tp2_pct = 0.020, 0.030, 0.050
        else:
            sl_pct, tp1_pct, tp2_pct = 0.015, 0.022, 0.038
        return sl_pct, tp1_pct, tp2_pct

    def calculate_atr(self, candles: List[Dict[str, Any]]) -> float:
        """
        ATR sebagai persentase dari close price.
        Mendukung dua format candle:
          - Dict: {'h': high, 'l': low, 'c': close}
          - List: [timestamp, open, high, low, close, volume]
        Returns atr_pct (misal 0.015 = 1.5%).
        """
        if not candles or len(candles) < 2:
            return 0.0

        def _parse(c):
            if isinstance(c, dict):
                return float(c.get("h", 0)), float(c.get("l", 0)), float(c.get("c", 0))
            elif isinstance(c, (list, tuple)) and len(c) >= 5:
                return float(c[2]), float(c[3]), float(c[4])
            return 0.0, 0.0, 0.0

        trs = []
        for i in range(1, len(candles)):
            h, l, c = _parse(candles[i])
            _, _, prev_c = _parse(candles[i - 1])
            if prev_c <= 0 or h <= 0:
                continue
            tr_pct = max(h - l, abs(h - prev_c), abs(l - prev_c)) / prev_c
            trs.append(tr_pct)

        if not trs:
            return 0.0
        return sum(trs) / len(trs)

    # ──────────────────────────────────────────
    # VOL-AWARE LEVEL CALCULATOR  (Satu-satunya fungsi SL/TP)
    # ──────────────────────────────────────────

    def calculate_levels(
        self,
        asset: str,
        side: str,
        entry_price: float,
        score: int,
        vol_cache: dict,
    ) -> dict:
        """
        Satu-satunya fungsi SL/TP yang dipakai pipeline standard mode.
        Dipanggil dari main.py setelah signal dibuat, meng-override SL/TP awal.

        Prinsip utama: SL harus di luar zona noise harian aset tersebut.
        - realized_vol dari vol_cache adalah volatilitas 1h terannualisasi per hari
        - Untuk aset small-cap (OI < $50M) vol minimum dipaksakan ke 5% karena
          aset ini bergerak lebih liar dari yang dilaporkan candle 1h
        - Fallback default dinaikkan dari 2.5% ke 4.0% agar tidak ada SL < 3%
          saat API gagal mengambil data volatilitas
        - Session adjustment hanya memperlebar (NY +20%), tidak mempersempit di Asia
          karena mempersempit SL di Asia terbukti meningkatkan SL kena noise

        Data 800 trade: SL kena noise menyebabkan -$126.25 = 82% gross profit hilang.
        Target: SL hit rate < 15% dari trade yang akhirnya profit.
        """
        from datetime import datetime, timezone as _tz

        # ── Step 1: Ambil realized vol dari cache ────────────────────────
        cached = vol_cache.get(asset)
        if cached and len(cached) >= 3:
            _, regime_obj, realized_vol = cached[0], cached[1], cached[2]
            regime = regime_obj.value if hasattr(regime_obj, "value") else str(regime_obj)
        else:
            # Fallback saat vol cache kosong: pakai 4% (bukan 2.5% lama).
            # Aset yang tidak ada di cache biasanya small-cap volatile — lebih aman
            # memulai dari asumsi vol tinggi daripada vol rendah.
            realized_vol = 0.040
            regime = "normal"

        regime_lower = regime.lower()

        # ── Step 2: Minimum vol per aset berdasarkan OI tier ────────────
        # Aset kecil (CHIP, MEGA, FARTCOIN, kLUNC) sering punya vol nyata
        # jauh lebih tinggi dari yang terukur di candle 1h karena likuiditas rendah.
        # Paksa minimum realized_vol agar SL tidak kena di gerakan biasa.
        from data.hyperliquid_client import get_client as _get_client
        try:
            _client = _get_client()
            oi_usd_est = 0.0
            if _client._market_cache:
                universe, _ = _client._market_cache
                for u in universe:
                    if isinstance(u, dict) and u.get("name") == asset:
                        # OI dalam contracts, estimasi kasar pakai mid_price tidak tersedia di sini
                        # Gunakan maxLeverage sebagai proxy likuiditas:
                        # aset dengan maxLeverage rendah = lebih illiquid = vol lebih tinggi
                        max_lev = int(u.get("maxLeverage", 50))
                        if max_lev <= 10:
                            realized_vol = max(realized_vol, 0.060)   # min 6%/hari untuk illiquid
                        elif max_lev <= 20:
                            realized_vol = max(realized_vol, 0.045)   # min 4.5%/hari
                        break
        except Exception:
            pass

        # Aset dengan nama yang diketahui bervolatilitas sangat tinggi
        # (dari data 800 trade: CHIP, MEGA, FARTCOIN, kLUNC WR < 50%)
        HIGH_VOL_ASSETS = {"CHIP", "MEGA", "FARTCOIN", "kLUNC", "VINE", "MON", "VVV",
                           "kBONK", "PEPE", "WIF", "BONK", "REZ", "PYTH"}
        if asset in HIGH_VOL_ASSETS:
            realized_vol = max(realized_vol, 0.055)   # min 5.5%/hari tanpa pengecualian

        # ── Step 3: Regime-based noise multiplier & floor ────────────────
        if regime_lower == "low_vol":
            noise_mult = 0.85
            sl_floor   = 0.025   # minimal 2.5% bahkan di low vol
            tp_mult    = 2.2
        elif regime_lower in ("normal", "unknown"):
            noise_mult = 1.00
            sl_floor   = 0.030   # minimal 3.0% di normal
            tp_mult    = 2.3
        elif regime_lower == "high_vol":
            noise_mult = 1.20
            sl_floor   = 0.035   # minimal 3.5% di high vol
            tp_mult    = 2.6
        else:  # extreme / volatile
            noise_mult = 1.40
            sl_floor   = 0.045   # minimal 4.5% di extreme
            tp_mult    = 3.0

        sl_pct = max(realized_vol * noise_mult, sl_floor)
        sl_pct = min(sl_pct, 0.080)   # hard cap 8% — di atas ini posisi terlalu berisiko

        # ── Step 4: Score-adjusted TP multiplier ─────────────────────────
        if score >= 80:
            tp_mult *= 1.30
        elif score >= 70:
            tp_mult *= 1.15
        elif score < 62:
            tp_mult *= 0.90

        # ── Step 5: Session adjustment — hanya perlebar, tidak persempit ─
        # Mempersempit SL di Asia terbukti tidak membantu karena aset bergerak
        # bebas 24 jam. Hanya tambah buffer saat NY session karena volume lebih tinggi.
        hour = datetime.now(_tz.utc).hour
        if 13 <= hour < 21:   # NY session — market lebih likuid, gerakan lebih besar
            sl_pct  = min(sl_pct * 1.20, 0.060)
            tp_mult *= 1.15

        # ── Step 6: TP levels ─────────────────────────────────────────────
        tp1_pct = sl_pct * tp_mult * 0.55   # TP1 = 55% dari target penuh
        tp2_pct = sl_pct * tp_mult          # TP2 = target penuh

        # RR minimum 1.5:1 — tidak mau trade dengan TP < 1.5× SL
        tp2_pct = max(tp2_pct, sl_pct * 1.50)
        tp1_pct = max(tp1_pct, sl_pct * 0.65)

        # ── Step 7: Absolute price levels ────────────────────────────────
        if side == "long":
            sl_price  = round(entry_price * (1 - sl_pct),  8)
            tp1_price = round(entry_price * (1 + tp1_pct), 8)
            tp2_price = round(entry_price * (1 + tp2_pct), 8)
        else:
            sl_price  = round(entry_price * (1 + sl_pct),  8)
            tp1_price = round(entry_price * (1 - tp1_pct), 8)
            tp2_price = round(entry_price * (1 - tp2_pct), 8)

        rr = tp2_pct / sl_pct

        log.info(
            f"[LEVELS] {asset} {side.upper()} "
            f"vol={realized_vol*100:.2f}% regime={regime} "
            f"sl={sl_pct*100:.2f}% tp1={tp1_pct*100:.2f}% tp2={tp2_pct*100:.2f}% "
            f"RR={rr:.2f}x score={score}"
        )

        return {
            "sl_pct":       sl_pct,
            "tp1_pct":      tp1_pct,
            "tp2_pct":      tp2_pct,
            "sl_price":     sl_price,
            "tp1_price":    tp1_price,
            "tp2_price":    tp2_price,
            "rr_ratio":     rr,
            "regime":       regime,
            "realized_vol": realized_vol,
        }

    # ──────────────────────────────────────────
    # EXPECTED VALUE FILTER  (Fix 2)
    # ──────────────────────────────────────────

    def score_to_win_prob(self, score: int) -> float:
        """
        Convert signal score to conservative win probability estimate.
        Based on empirical 92-trade paper data.
        Score 70-74 anomaly (14% WR) handled upstream by IntelligenceModel.
        """
        if score >= 80: return 0.65
        if score >= 75: return 0.60
        if score >= 70: return 0.57
        if score >= 65: return 0.58
        if score >= 60: return 0.55
        return 0.52

    def check_expected_value(
        self,
        score: int,
        sl_pct: float,
        tp2_pct: float,
        min_ev: float = 0.001,
    ) -> Tuple[bool, float]:
        """
        Gate trade on positive expected value. Pure math, <0.01ms.
        Uses score-based win probability, not IntelligenceModel.

        92-trade proof: EV was -0.226%/trade despite 57.6% WR because
        avg loss (1.09%) was 2.66x avg win (0.41%). This filter enforces
        that the math works before capital is risked.
        """
        win_prob      = self.score_to_win_prob(score)
        loss_prob     = 1.0 - win_prob
        realistic_win = tp2_pct * 0.70   # realistic: not all trades reach TP2
        ev = (win_prob * realistic_win) - (loss_prob * sl_pct)

        passes = ev >= min_ev
        if passes:
            log.debug(
                f"[EV] score={score} win_prob={win_prob:.2f} "
                f"sl={sl_pct*100:.2f}% tp={tp2_pct*100:.2f}% "
                f"ev={ev*100:.3f}% APPROVED"
            )
        else:
            log.info(
                f"[EV] Trade rejected: ev={ev*100:.3f}% < min={min_ev*100:.3f}% "
                f"(score={score} win_prob={win_prob:.2f} "
                f"sl={sl_pct*100:.2f}% tp={tp2_pct*100:.2f}%)"
            )
        return passes, ev

    # ──────────────────────────────────────────
    # PARTIAL TP & TRAILING STOP
    # ──────────────────────────────────────────

    def check_tp_trail(
        self,
        position: Position,
        current_price: float,
    ) -> Optional[Dict]:
        """
        Exit hierarchy — Fix 5 (partial ratios) + Fix 6 (momentum time-exit).

        Distribution after fix:
          TP1 close 25% (was 40%) — let 75% keep running
          TP2 close 50% of remaining (37.5% original) — trail last 37.5%
          Trailing on last piece with vol-aware distance

        92-trade data: time_exit 72.2% WR but only +$0.23 avg because hard
        12-min cut trades mid-move. New logic: exit on momentum reversal or
        flatline, NEVER time-exit a trade that's above TP1.
        """
        from datetime import timezone as _tz, datetime as _dt

        floating = position.floating_pct(current_price)

        if position.side == Side.LONG:
            new_high = max(position.trailing_high, current_price)
            max_floating = (new_high - position.entry_price) / position.entry_price
        else:
            new_low = min(position.trailing_high, current_price)
            max_floating = (position.entry_price - new_low) / position.entry_price

        # ── Rule A: Hard SL ───────────────────────────────────────────────
        if (position.side == Side.LONG and current_price <= position.stop_loss) or \
           (position.side == Side.SHORT and current_price >= position.stop_loss):
            return {
                "action":      "stop_loss",
                "close_ratio": 1.0,
                "price":       current_price,
                "message":     (
                    f"🛑 Stop-loss hit at {position.stop_loss:.4f}. "
                    f"Loss: {floating*100:.2f}%."
                )
            }

        cfg = self._cfg()
        # Fix 5: ratios now come from config (0.25 / 0.50)
        tp1_ratio = getattr(cfg, 'tp1_close_ratio', 0.25)
        tp2_ratio = getattr(cfg, 'tp2_close_ratio', 0.50)

        # ── Rule B: TP1 hit — close 25%, move SL to breakeven+0.1% ──────
        tp1_hit_now = (
            (position.side == Side.LONG  and current_price >= position.tp1) or
            (position.side == Side.SHORT and current_price <= position.tp1)
        )
        if not position.tp1_hit and tp1_hit_now:
            return {
                "action":      "tp1",
                "close_ratio": tp1_ratio,
                "price":       current_price,
                "message":     (
                    f"🎯 TP1 hit! +{floating*100:.2f}%. "
                    f"Closing {int(tp1_ratio*100)}%, SL → breakeven+0.1%."
                )
            }

        # ── Rule C: TP2 hit — close 50% of remaining, trail last piece ──
        tp2_hit_now = (
            (position.side == Side.LONG  and current_price >= position.tp2) or
            (position.side == Side.SHORT and current_price <= position.tp2)
        )
        if position.tp1_hit and not position.tp2_hit and tp2_hit_now:
            return {
                "action":      "tp2",
                "close_ratio": tp2_ratio,
                "price":       current_price,
                "message":     (
                    f"🎯 TP2 hit! +{floating*100:.2f}%. "
                    f"Closing {int(tp2_ratio*100)}% of remaining. Trailing last piece."
                )
            }

        # ── Rule D: Trailing stop on last position piece (post-TP1) ─────
        if position.tp1_hit:
            tp1_diff_pct = abs(position.entry_price - position.tp1) / position.entry_price
            activation_threshold = tp1_diff_pct + 0.003

            if max_floating >= activation_threshold:
                vol_est = getattr(position, 'realized_vol', 0.02)
                if position.tp2_hit:
                    trail_pct = max(vol_est * 0.30, 0.003)
                else:
                    trail_pct = max(vol_est * 0.50, 0.005)

                if position.side == Side.LONG:
                    trail_sl = new_high * (1 - trail_pct)
                    if current_price <= trail_sl:
                        return {
                            "action":      "trailing_stop",
                            "close_ratio": 1.0,
                            "price":       current_price,
                            "trail_price": trail_sl,
                            "message":     (
                                f"🛡️ Trailing Stop ({trail_pct*100:.1f}%) hit at {trail_sl:.4f} "
                                f"(peak +{max_floating*100:.1f}%)."
                            )
                        }
                else:
                    trail_sl = new_low * (1 + trail_pct)
                    if current_price >= trail_sl:
                        return {
                            "action":      "trailing_stop",
                            "close_ratio": 1.0,
                            "price":       current_price,
                            "trail_price": trail_sl,
                            "message":     (
                                f"🛡️ Trailing Stop ({trail_pct*100:.1f}%) hit at {trail_sl:.4f} "
                                f"(peak +{max_floating*100:.1f}%)."
                            )
                        }

        # ── Rule E: Scalper max-hold + momentum reversal exit ────────────
        if getattr(position, 'trade_mode', 'scalper') == 'scalper':
            scfg = SCALPER
            max_hold   = getattr(scfg, 'max_hold_minutes', 20.0)
            grace      = getattr(scfg, 'max_hold_grace_minutes', 8.0)
            soft_floor = getattr(scfg, 'max_hold_soft_floor_pct', -0.0015)

            now    = _dt.now(_tz.utc)
            opened = position.opened_at
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=_tz.utc)
            hold_minutes = (now - opened).total_seconds() / 60.0

            # ── Rule E2: Momentum reversal exit (pre-SL protection) ──────
            # Keluar lebih awal kalau candle 1m berbalik arah sebelum SL hit.
            # Hanya aktif setelah 3 menit (hindari noise awal entry) dan
            # hanya kalau posisi sudah merugi melewati floor (bukan sekadar noise).
            if (
                not position.tp1_hit
                and getattr(scfg, 'momentum_exit_enabled', True)
                and hold_minutes >= getattr(scfg, 'momentum_exit_min_minutes', 3.0)
            ):
                loss_floor = getattr(scfg, 'momentum_exit_loss_floor', -0.003)
                n_candles  = getattr(scfg, 'momentum_exit_candles', 3)
                candle_history = getattr(position, 'candle_closes', [])

                if len(candle_history) >= n_candles and floating <= loss_floor:
                    recent = candle_history[-n_candles:]
                    # Momentum reversal: semua candle terakhir berlawanan arah posisi
                    if position.side == Side.LONG:
                        reversal = all(recent[i] < recent[i - 1] for i in range(1, len(recent)))
                    else:
                        reversal = all(recent[i] > recent[i - 1] for i in range(1, len(recent)))

                    if reversal:
                        return {
                            "action":      "momentum_exit",
                            "close_ratio": 1.0,
                            "price":       current_price,
                            "pnl":         position.pnl_unrealized,
                            "position_id": position.position_id,
                            "message":     (
                                f"↩️ Momentum reversal ({n_candles} candle bearish/bullish berturut). "
                                f"Exit pre-SL. PnL: {floating*100:.2f}%."
                            )
                        }

            # ── Rule E1: Max-hold force exit ─────────────────────────────
            if hold_minutes >= max_hold:
                if floating > soft_floor and hold_minutes < (max_hold + grace):
                    pass  # grace period — wait if loss is mild or position is profitable
                else:
                    return {
                        "action":      "time_exit",
                        "close_ratio": 1.0,
                        "price":       current_price,
                        "pnl":         position.pnl_unrealized,
                        "position_id": position.position_id,
                        "message":     (
                            f"⏱️ Scalper max-hold {hold_minutes:.0f}m — exit paksa. "
                            f"PnL: {floating*100:.2f}%."
                        )
                    }

        return None

    # ──────────────────────────────────────────
    # STATE UPDATES
    # ──────────────────────────────────────────

    def record_pnl(self, pnl_usd: float, account_balance: float):
        """Update daily PnL and check limits."""
        self._daily_pnl += pnl_usd

        # Update peak balance
        if account_balance > self._peak_balance:
            self._peak_balance = account_balance

        # Ensure we have a valid baseline for percentage calculation
        if self._session_start_balance <= 0:
            self._session_start_balance = account_balance
            log.debug(f"[RISK] Initialized mid-session start balance: ${self._session_start_balance:,.2f}")

        # Check if cooldown should be triggered (> 6% daily loss)
        daily_pnl_pct = self._daily_pnl / max(self._session_start_balance, 1)
        
        # PERSIST STATE IMMEDIATELY after update
        self._persist_risk_state()

        if daily_pnl_pct < -0.50 and not self._cooldown_until:
            cooldown_hrs = SCALPER.post_loss_cooldown_hrs if self._is_scalper() else RISK.post_loss_cooldown_hrs
            from datetime import timedelta
            self._cooldown_until = datetime.now(timezone.utc) + timedelta(hours=cooldown_hrs)
            self._persist_risk_state()  # Persist immediately so restart doesn't bypass cooldown
            log.warning(
                f"❄️  Daily loss {daily_pnl_pct*100:.1f}% > 50% - "
                f"cooldown activated until {self._cooldown_until.isoformat()}"
            )

    def pause(self):
        self._paused = True
        log.info("⏸️  Risk manager: trading paused")

    def resume(self):
        self._paused = False
        self._cooldown_until = None
        self._persist_risk_state()
        log.info("▶️  Risk manager: trading resumed")

    def reset_kill_switch(self):
        """Only call after manual review. NEVER auto-reset."""
        log.warning("🔓 Kill switch manually reset by user")
        self._kill_switch = False

    @property
    def status(self) -> Dict:
        return {
            "paused":        self._paused,
            "kill_switch":   self._kill_switch,
            "daily_pnl":     self._daily_pnl,
            "peak_balance":  self._peak_balance,
            "session_start_balance": self._session_start_balance,
            "cooldown_until": self._cooldown_until.isoformat() if self._cooldown_until else None,
            "in_cooldown":   bool(
                self._cooldown_until and
                datetime.now(timezone.utc) < self._cooldown_until
            ),
        }
