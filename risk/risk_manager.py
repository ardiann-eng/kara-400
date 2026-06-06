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
        # Per-asset trade tracker: asset -> [unix_ts, ...] of completed trades today
        self._asset_trade_times: Dict[str, List[float]] = {}

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
            "cooldown_until": self._cooldown_until.isoformat() if self._cooldown_until else None,
            "asset_trade_times": self._asset_trade_times,
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

            # Restore per-asset trade times, drop stale keys (yesterday's date)
            raw_att = state.get("asset_trade_times", {})
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self._asset_trade_times = {
                k: v for k, v in raw_att.items() if k.endswith(today)
            }
            if self._asset_trade_times:
                log.debug(f"[REPEAT GUARD] Restored trade times: {list(self._asset_trade_times.keys())}")

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
    # PER-ASSET REPEAT GUARD
    # ──────────────────────────────────────────

    MAX_TRADES_PER_ASSET_PER_DAY = 2
    ASSET_COOLDOWN_SECONDS       = 2 * 3600   # 2 jam setelah trade ke-2

    def _check_asset_repeat(self, asset: str) -> Tuple[bool, str]:
        """Block if asset already hit max trades today or is in per-asset cooldown."""
        now = time.time()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"{asset}_{today}"
        times = self._asset_trade_times.get(key, [])

        if len(times) >= self.MAX_TRADES_PER_ASSET_PER_DAY:
            last_ts = times[-1]
            elapsed = now - last_ts
            remaining = self.ASSET_COOLDOWN_SECONDS - elapsed
            if remaining > 0:
                hrs  = int(remaining) // 3600
                mins = (int(remaining) % 3600) // 60
                return False, (
                    f"🔁 {asset}: sudah {len(times)}x hari ini (WR turun drastis). "
                    f"Cooldown {hrs}h {mins}m lagi."
                )
            # Cooldown lewat tapi sudah max trades — blokir sampai besok
            return False, (
                f"🔁 {asset}: batas {self.MAX_TRADES_PER_ASSET_PER_DAY} trade/hari tercapai. "
                f"Lanjut besok."
            )
        return True, ""

    def record_asset_trade(self, asset: str):
        """Call setelah trade dieksekusi untuk update per-asset counter."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"{asset}_{today}"
        if key not in self._asset_trade_times:
            self._asset_trade_times[key] = []
        self._asset_trade_times[key].append(time.time())
        count = len(self._asset_trade_times[key])
        log.debug(f"[REPEAT GUARD] {asset}: trade ke-{count} hari ini dicatat.")
        self._persist_risk_state()   # survive restart

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
            self._asset_trade_times = {}   # reset per-asset counter setiap hari
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

        # ── Per-asset repeat trade guard ──────────────────────────────
        asset_ok, asset_reason = self._check_asset_repeat(signal.asset)
        if not asset_ok:
            return False, asset_reason

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

        # ── Leverage: pakai signal.suggested_leverage (sudah di-set executor dari user config)
        cfg = self._cfg()
        default_lev = signal.suggested_leverage
        user_max_lev = self._get_user_value("max_leverage", cfg.max_leverage)
        # Pakai signal leverage (sudah mencerminkan user setting dari executor).
        # Tidak ada HL exchange cap — user yang tentukan leverage via /settings.
        lev = max(1, int(default_lev))

        # ── 1. Determine size_usd (margin) — mode-aware ───────────────
        cfg = self._cfg()
        
        # --- CONVICTION-WEIGHTED POSITION SIZING (AGGRESSIVE) ---
        score = getattr(signal, 'score', 0)
        risk_pct = self.get_risk_pct(score, account_balance, leverage=lev)

        risk_pct = min(risk_pct, cfg.max_risk_per_trade_pct)

        # [AUDIT FIX 2026] Volatility-adjusted position sizing.
        # ONDO (8% vol) and BTC (2% vol) used to get identical risk allocation.
        # Now scale risk_pct inversely by realized vol — high-vol assets get smaller size.
        # Reference baseline = 2.0% daily realized vol; assets above scale down.
        try:
            _sig_vol = float(getattr(signal, 'realized_vol', 0.02) or 0.02)
            _baseline_vol = 0.020
            _vol_scale = min(_baseline_vol / max(_sig_vol, 0.005), 1.0)
            # Floor at 0.25 so we never shrink below 25% of baseline (sanity)
            _vol_scale = max(_vol_scale, 0.25)
            risk_pct = risk_pct * _vol_scale
            log.debug(
                f"[SIZE-VOL] {signal.asset}: realized_vol={_sig_vol*100:.2f}% "
                f"→ vol_scale={_vol_scale:.2f} → risk_pct={risk_pct*100:.3f}%"
            )
        except Exception as _vol_err:
            log.debug(f"[SIZE-VOL] vol scaling skipped: {_vol_err}")

        # [AUDIT FIX 2026 PHASE 3] Sizing uses REAL SL distance (was hardcoded 0.7%).
        # Old hardcode decoupled sizing from ATR-adaptive SL: when ATR yielded a 2% SL,
        # the position was sized as if SL = 0.7%, so a stop-out actually risked 2.86×
        # the configured risk_per_trade_pct. Now sizing reflects the actual stop_loss.
        # Floor at 0.4% to prevent runaway position size when SL is unusually tight
        # (e.g. ATR collapse during dead market) — caps risk_per_trade exposure.
        actual_sl_pct = abs(signal.entry_price - signal.stop_loss) / max(signal.entry_price, 1e-9)
        SL_SIZING_FLOOR = 0.004   # 0.4% — protects against tiny-SL → oversized position
        sl_pct_for_sizing = max(actual_sl_pct, SL_SIZING_FLOOR)

        size_usd = (account_balance * risk_pct) / max(sl_pct_for_sizing * lev, 0.0001)
        log.debug(
            f"[SIZE-SL] {signal.asset}: actual_sl={actual_sl_pct*100:.3f}% "
            f"floored→{sl_pct_for_sizing*100:.3f}% lev={lev}x "
            f"risk_pct={risk_pct*100:.3f}% → size_usd=${size_usd:.2f}"
        )

        # Drawdown guard: if we are >15% below peak, cut risk in half!
        # Find drawdown:
        drawdown = (self._peak_balance - account_balance) / max(self._peak_balance, 1)
        if drawdown >= 0.15:
            size_usd *= 0.5
            log.warning(f"[RISK] Drawdown guard active (DD: {drawdown*100:.1f}% >= 15%). Risk halved to {risk_pct/2*100:.1f}%.")

        # ── [v10] Gate sizing modifier (tier A/B × vol tier) ──────────
        # Tier B (no liquidity context) = 0.6×, high-vol = 0.5×, dst.
        # Risk dikelola lewat UKURAN, bukan menolak trade (jaga volume tinggi).
        _v10_mult = getattr(signal, 'size_mult', 1.0) or 1.0
        if _v10_mult != 1.0:
            size_usd *= _v10_mult
            log.debug(f"[RISK] {signal.asset}: v10 size_mult ×{_v10_mult} → {format_usd(size_usd)}")

        # ── 3. Hard Margin Cap — 15% balance untuk modal kecil ($62.50)
        max_allowed_margin = account_balance * 0.15
        if size_usd > max_allowed_margin:
            log.debug(f"[RISK] Margin cap: {format_usd(size_usd)} -> {format_usd(max_allowed_margin)} (15%)")
            size_usd = max_allowed_margin

        # ── 3b. Minimum margin floor — ensure meaningful trade size
        min_margin = getattr(cfg, 'fixed_margin_per_position', 0.0)
        if min_margin > 0 and size_usd < min_margin:
            size_usd = min_margin

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

    def get_risk_pct(self, score: int, equity: float, leverage: int = 15) -> float:
        # Target: margin ~10-15% balance regardless of leverage.
        # size_usd = (balance × risk_pct) / (sl_pct × lev)
        # Agar size_usd konstan, risk_pct harus naik proporsional dengan leverage.
        # Baseline: lev=15x → risk_pct=cfg.risk_per_trade_pct
        # lev=5x  → risk_pct = baseline × (5/15) agar margin dollar sama
        cfg = self._cfg()
        base = cfg.risk_per_trade_pct
        lev_ratio = leverage / 15.0
        base = base * lev_ratio   # scale down risk_pct saat leverage rendah

        # Score multiplier
        if score >= 65:
            risk_pct = base * 1.10
        else:
            risk_pct = base

        min_risk = getattr(cfg, 'min_risk_per_trade_pct', cfg.risk_per_trade_pct * 0.5)
        risk_pct = max(risk_pct, min_risk)

        # Equity protection multiplier
        ratio = equity / self._session_start_balance if self._session_start_balance > 0 else 1.0
        if ratio >= 1.5:   equity_mult = 0.8
        elif ratio <= 0.8: equity_mult = 0.5
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
    # LOCAL INDICATOR HELPERS  (zero API calls — semua dari candle list)
    # ──────────────────────────────────────────

    @staticmethod
    def _calc_atr_pct_from_closes(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
        """
        ATR14 sebagai % dari harga (misal 0.015 = 1.5%).
        Memakai true range: max(H-L, |H-prevC|, |L-prevC|) / prevC.
        Butuh min period+1 candle. Return 0.0 jika data kurang.
        """
        if len(closes) < period + 1 or len(highs) < period + 1 or len(lows) < period + 1:
            return 0.0
        trs = []
        for i in range(1, len(closes)):
            h, l, pc = highs[i], lows[i], closes[i - 1]
            if pc <= 0:
                continue
            trs.append(max(h - l, abs(h - pc), abs(l - pc)) / pc)
        if len(trs) < period:
            return 0.0
        return sum(trs[-period:]) / period

    @staticmethod
    def _calc_ema(values: List[float], period: int) -> float:
        """EMA dari list nilai. Return 0.0 jika data kurang dari period."""
        if len(values) < period:
            return 0.0
        k = 2.0 / (period + 1)
        ema = sum(values[:period]) / period
        for v in values[period:]:
            ema = v * k + ema * (1 - k)
        return ema

    @staticmethod
    def _calc_rsi(closes: List[float], period: int = 14) -> float:
        """RSI Wilder. Return 50.0 jika data kurang (netral, tidak trigger exit)."""
        if len(closes) < period + 1:
            return 50.0
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i - 1]
            gains.append(max(d, 0.0))
            losses.append(max(-d, 0.0))
        if not gains:
            return 50.0
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1 + rs))

    @staticmethod
    def _calc_macd_histogram(closes: List[float], fast: int = 12, slow: int = 26, signal_p: int = 9) -> float:
        """
        MACD histogram = MACD_line - Signal_line.
        Signal line = EMA9 dari MACD line series (bukan MACD line point tunggal).
        Butuh minimal slow+signal_p candle. Return 0.0 jika data kurang.
        """
        if len(closes) < slow + signal_p:
            return 0.0
        k_f = 2.0 / (fast + 1)
        k_s = 2.0 / (slow + 1)
        k_sig = 2.0 / (signal_p + 1)

        # Bangun MACD line series dari seluruh data
        ema_f = sum(closes[:fast]) / fast
        ema_s = sum(closes[:slow]) / slow
        for v in closes[fast:slow]:
            ema_f = v * k_f + ema_f * (1 - k_f)

        macd_series: List[float] = []
        for v in closes[slow:]:
            ema_f = v * k_f + ema_f * (1 - k_f)
            ema_s = v * k_s + ema_s * (1 - k_s)
            macd_series.append(ema_f - ema_s)

        if len(macd_series) < signal_p:
            return macd_series[-1] if macd_series else 0.0

        # Signal line = EMA dari MACD series
        sig = sum(macd_series[:signal_p]) / signal_p
        for m in macd_series[signal_p:]:
            sig = m * k_sig + sig * (1 - k_sig)

        return macd_series[-1] - sig  # histogram sesungguhnya

    @staticmethod
    def _calc_volume_sma(volumes: List[float], period: int = 20) -> float:
        """SMA dari volume. Return 0.0 jika data kurang."""
        if len(volumes) < period:
            return sum(volumes) / len(volumes) if volumes else 0.0
        return sum(volumes[-period:]) / period

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
        tp1_pct = sl_pct * tp_mult * 0.55   # TP1 = 55% dari target penuh (1:1 R:R-ish)
        tp2_pct = sl_pct * tp_mult          # TP2 = target penuh (2:1 R:R)
        tp3_pct = sl_pct * tp_mult * 1.50   # TP3 = 3:1 R:R (pro-level extension)

        # RR minimum 1.5:1 — tidak mau trade dengan TP < 1.5× SL
        tp2_pct = max(tp2_pct, sl_pct * 1.50)
        tp1_pct = max(tp1_pct, sl_pct * 0.65)
        tp3_pct = max(tp3_pct, sl_pct * 2.50)  # TP3 minimal 2.5:1 R:R

        # ── Step 7: Absolute price levels ────────────────────────────────
        if side == "long":
            sl_price  = round(entry_price * (1 - sl_pct),  8)
            tp1_price = round(entry_price * (1 + tp1_pct), 8)
            tp2_price = round(entry_price * (1 + tp2_pct), 8)
            tp3_price = round(entry_price * (1 + tp3_pct), 8)
        else:
            sl_price  = round(entry_price * (1 + sl_pct),  8)
            tp1_price = round(entry_price * (1 - tp1_pct), 8)
            tp2_price = round(entry_price * (1 - tp2_pct), 8)
            tp3_price = round(entry_price * (1 - tp3_pct), 8)

        rr = tp2_pct / sl_pct

        log.info(
            f"[LEVELS] {asset} {side.upper()} "
            f"vol={realized_vol*100:.2f}% regime={regime} "
            f"sl={sl_pct*100:.2f}% tp1={tp1_pct*100:.2f}% tp2={tp2_pct*100:.2f}% "
            f"tp3={tp3_pct*100:.2f}% RR={rr:.2f}x score={score}"
        )

        return {
            "sl_pct":       sl_pct,
            "tp1_pct":      tp1_pct,
            "tp2_pct":      tp2_pct,
            "tp3_pct":      tp3_pct,
            "sl_price":     sl_price,
            "tp1_price":    tp1_price,
            "tp2_price":    tp2_price,
            "tp3_price":    tp3_price,
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

    @staticmethod
    async def refresh_position_candles(position, hl_client) -> None:
        """
        Populate position.candle_closes/highs/lows/volumes (1m, last 30) and
        htf_candle_closes (15m, last 60) from the exchange.

        Called from executor.update_positions() before check_tp_trail() so that
        momentum_exit / emergency_exit / HTF override layers have actual data.
        Silent-failure-tolerant: if fetch fails, leaves existing arrays untouched.
        """
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        now = _dt.now(_tz.utc)
        last = getattr(position, 'candles_refreshed_at', None)
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=_tz.utc)
            # Refresh at most once every 30 seconds per position
            if (now - last) < _td(seconds=30):
                return
        position.candles_refreshed_at = now

        import logging
        _log = logging.getLogger("kara.risk_manager")
        import time
        _now_ms = int(time.time() * 1000)

        # ── 1m Candle Refresh (last 30) ──────────────────────────────
        try:
            # Bypass get_candles() SDK check to avoid "client not connected" errors
            start_1m = _now_ms - (60_000 * 30)
            res_1m, succ_1m = await hl_client._call_info_endpoint("candleSnapshot", {
                "req": {
                    "coin": position.asset,
                    "interval": "1m",
                    "startTime": start_1m,
                    "endTime": _now_ms
                }
            })
            
            candles_1m = res_1m if (succ_1m and isinstance(res_1m, list)) else []
            
            if candles_1m:
                closes, highs, lows, volumes = [], [], [], []
                for c in candles_1m:
                    if not isinstance(c, dict): continue
                    try:
                        closes.append(float(c.get("c", 0)))
                        highs.append(float(c.get("h", 0)))
                        lows.append(float(c.get("l", 0)))
                        volumes.append(float(c.get("v", 0)))
                    except (TypeError, ValueError): continue
                
                if closes:
                    position.candle_closes  = closes
                    position.candle_highs   = highs
                    position.candle_lows    = lows
                    position.candle_volumes = volumes
                    _log.info(
                        f"[CANDLE-REFRESH] {position.asset} | pos_id={position.position_id} | "
                        f"1m_candles={len(closes)} | last_close={closes[-1]:.6f}"
                    )
        except Exception as e:
            _log.info(f"[CANDLE-REFRESH] {position.asset} | 1m refresh FAILED | reason={e}")

        # ── 15m Candle Refresh (last 60) ─────────────────────────────
        try:
            start_15m = _now_ms - (900_000 * 60)
            res_15m, succ_15m = await hl_client._call_info_endpoint("candleSnapshot", {
                "req": {
                    "coin": position.asset,
                    "interval": "15m",
                    "startTime": start_15m,
                    "endTime": _now_ms
                }
            })
            
            candles_15m = res_15m if (succ_15m and isinstance(res_15m, list)) else []
            
            if candles_15m:
                htf_closes = []
                for c in candles_15m:
                    if not isinstance(c, dict): continue
                    try:
                        htf_closes.append(float(c.get("c", 0)))
                    except (TypeError, ValueError): continue
                
                if htf_closes:
                    position.htf_candle_closes = htf_closes
        except Exception as e:
            _log.debug(f"[REFRESH] {position.asset} 15m candle refresh failed: {e}")

    def check_tp_trail(
        self,
        position: Position,
        current_price: float,
    ) -> Optional[Dict]:
        """
        [QUANT AGGRESSION v8] Exit hierarchy:

        1. Hard SL (backstop)
        2. Breakeven trigger at 0.8× SL distance
        3. TP1: close 40% at 1.0× SL distance, move SL → breakeven+0.1%
        4. TP2: close 30% at 1.5× SL distance
        5. Trail: remaining 30% with ATR trailing at 2.0× SL distance activation
        6. Early trail (pre-TP1 profit lock)
        7. Score-driven time exit with grace period for runners

        ATR trailing: trail_sl = peak - (entry_atr * atr_mult * entry_price)
        Falls back to vol-based trail when entry_atr not available.
        """
        from datetime import timezone as _tz, datetime as _dt

        floating = position.floating_pct(current_price)

        if position.side == Side.LONG:
            new_high = max(position.trailing_high, current_price)
            max_floating = (new_high - position.entry_price) / position.entry_price
        else:
            new_low = min(position.trailing_high, current_price)
            max_floating = (position.entry_price - new_low) / position.entry_price

        cfg = self._cfg()

        # [QUANT AGGRESSION] SL-distance-based partial layers
        sl_distance = abs(position.entry_price - position.stop_loss) / max(position.entry_price, 1e-9)
        if sl_distance <= 0:
            sl_distance = 0.008  # fallback 0.8%

        tp1_at_mult = getattr(cfg, 'partial_tp1_at_sl_multiple', 1.0)
        tp2_at_mult = getattr(cfg, 'partial_tp2_at_sl_multiple', 1.5)
        tp3_at_mult = getattr(cfg, 'partial_tp3_trail_at', 2.0)
        be_at_mult  = getattr(cfg, 'breakeven_trigger_at_sl_multiple', 0.8)

        tp1_ratio = getattr(cfg, 'tp1_close_ratio', 0.40)   # [AUDIT FIX 2026-05-21] Was hardcoded 0.40. Now reads config (scalper=0.50, standard=0.25).
        tp2_ratio = getattr(cfg, 'tp2_close_ratio', 0.50)   # [AUDIT FIX 2026-05-21] Was hardcoded 0.50. Now reads config (scalper=0.667, standard=0.333).
        tp3_ratio = 1.0    # trail remaining — full close on trail trigger

        # ── Rule E2: Momentum Exit — Multi-Confirmation Engine (v2) ─────────
        #
        # Refactor dari 43/43 losses (-$29.53):
        # Root cause: threshold 0.43% = noise normal, zero confirmation layer.
        # Solusi: ATR-dynamic pullback threshold + 5 layer confirmation.
        # Signal Score TIDAK dipakai di exit — hanya untuk ENTRY filter.
        #
        # Semua kalkulasi dari local OHLCV — ZERO API CALLS.
        if getattr(position, 'trade_mode', 'scalper') == 'scalper' and not position.tp1_hit:
            scfg = SCALPER
            if getattr(scfg, 'momentum_exit_enabled', False):
                now    = _dt.now(_tz.utc)
                opened = position.opened_at
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=_tz.utc)
                hold_minutes = (now - opened).total_seconds() / 60.0

                min_minutes  = getattr(scfg, 'momentum_exit_min_minutes', 5.0)
                closes       = getattr(position, 'candle_closes', [])
                highs        = getattr(position, 'candle_highs', [])
                lows         = getattr(position, 'candle_lows', [])
                volumes      = getattr(position, 'candle_volumes', [])
                htf_closes   = getattr(position, 'htf_candle_closes', [])

                pullback_pct = -floating   # floating negatif = loss = pullback dari entry

                # ── Emergency exit: aktif SEBELUM min_minutes ─────────────
                # Hanya untuk dump ekstrem (1.5%+ drop + volume 2.5x SMA).
                # Mencegah kerugian besar di menit 0-5 tanpa mengorbankan noise filter.
                EMERGENCY_PULLBACK = 0.015
                EMERGENCY_VOL_MULT = 2.5
                if hold_minutes < min_minutes and len(closes) >= 3 and len(volumes) >= 3:
                    emg_vol_sma = self._calc_volume_sma(volumes, period=min(20, len(volumes)))
                    emg_cur_vol = volumes[-1]
                    emg_vol_ok  = emg_vol_sma > 0 and emg_cur_vol >= emg_vol_sma * EMERGENCY_VOL_MULT
                    if pullback_pct >= EMERGENCY_PULLBACK and emg_vol_ok:
                        return {
                            "action":      "momentum_exit",
                            "close_ratio": 1.0,
                            "price":       current_price,
                            "pnl":         position.pnl_unrealized,
                            "position_id": position.position_id,
                            "checks":      {"pullback": True, "volume": True,
                                            "trend": False, "momentum": False},
                            "message":     (
                                f"🚨 Emergency exit: dump {pullback_pct*100:.2f}% + vol "
                                f"{emg_cur_vol/emg_vol_sma:.1f}x dalam {hold_minutes:.1f}m. "
                                f"PnL: {floating*100:.2f}%."
                            )
                        }

                # ── Layer 0: Min hold time (normal flow) ──────────────────
                if hold_minutes < min_minutes:
                    pass  # emergency di atas tidak trigger — skip normal flow
                elif len(closes) < 3:
                    pass  # belum cukup candle data
                else:
                    # ── Layer 1: ATR-dynamic pullback threshold ────────────
                    floor_pct = getattr(scfg, 'momentum_exit_min_pullback_pct', 0.008)
                    atr_mult  = getattr(scfg, 'momentum_exit_atr_pullback_mult', 1.5)

                    if len(highs) >= 15 and len(lows) >= 15:
                        atr_pct = self._calc_atr_pct_from_closes(highs, lows, closes, period=14)
                    else:
                        sample = closes[-8:] if len(closes) >= 8 else closes
                        mean_c = sum(sample) / len(sample) if sample else 1.0
                        variance = sum((c - mean_c) ** 2 for c in sample) / len(sample)
                        atr_pct = (variance ** 0.5) / mean_c if mean_c > 0 else 0.0

                    if atr_pct > 0.030:
                        dyn_threshold = max(floor_pct, atr_pct * 1.2)
                    elif atr_pct > 0.010:
                        dyn_threshold = max(floor_pct, atr_pct * atr_mult)
                    else:
                        dyn_threshold = max(floor_pct, 0.015)

                    check_pullback = pullback_pct >= dyn_threshold

                    # ── Layer 2: Volume confirmation (opsional) ────────────
                    vol_mult     = getattr(scfg, 'momentum_exit_volume_mult', 1.3)
                    vol_sma      = self._calc_volume_sma(volumes, period=20)
                    cur_vol      = volumes[-1] if volumes else 0.0
                    check_volume = (vol_sma > 0 and cur_vol >= vol_sma * vol_mult)

                    # ── Layer 3: Trend structure break (wajib) ────────────
                    ema_fast_p = getattr(scfg, 'momentum_exit_ema_fast', 20)
                    ema_slow_p = getattr(scfg, 'momentum_exit_ema_slow', 50)
                    ema_fast   = self._calc_ema(closes, ema_fast_p)
                    ema_slow   = self._calc_ema(closes, ema_slow_p)

                    if ema_fast > 0 and ema_slow > 0:
                        # EMA20/50 tersedia — gunakan structure break
                        if position.side == Side.LONG:
                            check_trend = (current_price < ema_fast or ema_fast < ema_slow)
                        else:
                            check_trend = (current_price > ema_fast or ema_fast > ema_slow)
                    else:
                        # EMA20/50 belum cukup data — coba EMA9/20 lebih dulu
                        ema9  = self._calc_ema(closes, 9)
                        ema20 = self._calc_ema(closes, 20)
                        if ema9 > 0 and ema20 > 0:
                            if position.side == Side.LONG:
                                check_trend = (current_price < ema9 or ema9 < ema20)
                            else:
                                check_trend = (current_price > ema9 or ema9 > ema20)
                        elif len(closes) >= 3:
                            # Fallback terakhir: 3 candle berturut arah yang sama
                            if position.side == Side.LONG:
                                check_trend = closes[-1] < closes[-2] < closes[-3]
                            else:
                                check_trend = closes[-1] > closes[-2] > closes[-3]
                        else:
                            check_trend = False

                    # ── Layer 4: Momentum indicators (opsional) ───────────
                    rsi_thresh     = getattr(scfg, 'momentum_exit_rsi_threshold', 45.0)
                    rsi            = self._calc_rsi(closes, period=14)
                    macd_hist      = self._calc_macd_histogram(closes)
                    if position.side == Side.LONG:
                        check_momentum = (rsi < rsi_thresh or macd_hist < 0)
                    else:
                        check_momentum = (rsi > (100 - rsi_thresh) or macd_hist > 0)

                    # ── Layer 5: HTF trend filter ──────────────────────────
                    htf_ema_f_p      = getattr(scfg, 'momentum_exit_htf_ema_fast', 20)
                    htf_ema_s_p      = getattr(scfg, 'momentum_exit_htf_ema_slow', 50)
                    htf_override_pct = getattr(scfg, 'momentum_exit_htf_uptrend_pullback', 0.030)

                    if len(htf_closes) >= htf_ema_s_p:
                        htf_ema_fast = self._calc_ema(htf_closes, htf_ema_f_p)
                        htf_ema_slow = self._calc_ema(htf_closes, htf_ema_s_p)
                        if position.side == Side.LONG and htf_ema_fast > htf_ema_slow:
                            check_pullback = pullback_pct >= htf_override_pct
                        elif position.side == Side.SHORT and htf_ema_fast < htf_ema_slow:
                            check_pullback = pullback_pct >= htf_override_pct

                    # ── Fire: wajib (pullback + trend) + opsional (volume OR momentum)
                    # [FIX 2026-05-10] Lebih agresif: jika pullback + (trend OR momentum),
                    # exit langsung. Sebelumnya butuh pullback+trend+optional, terlalu lambat
                    # saat momentum redup tapi EMA belum cross.
                    if check_pullback and (check_trend or check_momentum):
                        reasons = []
                        reasons.append(f"drop {pullback_pct*100:.2f}%≥{dyn_threshold*100:.1f}%")
                        if check_volume:   reasons.append(f"vol {cur_vol/vol_sma:.1f}x" if vol_sma > 0 else "vol↑")
                        if check_trend:    reasons.append("EMA break")
                        if check_momentum: reasons.append(f"RSI {rsi:.0f}" if rsi < rsi_thresh else "MACD↓")
                        return {
                            "action":      "momentum_exit",
                            "close_ratio": 1.0,
                            "price":       current_price,
                            "pnl":         position.pnl_unrealized,
                            "position_id": position.position_id,
                            "checks":      {"pullback": check_pullback, "volume": check_volume,
                                            "trend": check_trend, "momentum": check_momentum},
                            "message":     (
                                f"↩️ Momentum exit ({', '.join(reasons)}). "
                                f"PnL: {floating*100:.2f}%."
                            )
                        }

        # ── Rule A: Hard SL (backstop — emergency only) ───────────────────
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

        # ── Rule A2: [QUANT AGGRESSION] Breakeven trigger ────────────────
        # Move SL to entry+0.1% when price reaches 0.8× SL distance.
        be_target = sl_distance * be_at_mult
        partial_done = getattr(position, 'partial_exits_done', [])
        if floating >= be_target and not position.tp1_hit and 'breakeven' not in partial_done:
            # Just move SL, don't close anything
            old_sl = position.stop_loss
            if position.side == Side.LONG:
                new_sl = position.entry_price * 1.001  # entry + 0.1%
            else:
                new_sl = position.entry_price * 0.999  # entry - 0.1%
            position.stop_loss = new_sl
            if not hasattr(position, 'partial_exits_done') or position.partial_exits_done is None:
                position.partial_exits_done = []
            position.partial_exits_done.append('breakeven')
            log.info(
                f"[BREAKEVEN] {position.asset} | pos_id={position.position_id} | "
                f"old_sl={old_sl:.6f} | new_sl={new_sl:.6f} | "
                f"trigger_dist={be_at_mult} | floating=+{floating*100:.2f}%"
            )
            # Don't return — continue checking for TP1

        # ── Rule B: [QUANT AGGRESSION] TP1 — close 40% at 1.0× SL distance ──
        tp1_target = sl_distance * tp1_at_mult
        tp1_hit_now = floating >= tp1_target
        if not position.tp1_hit and tp1_hit_now:
            log.info(
                f"[PARTIAL] {position.asset} | pos_id={position.position_id} | label=TP1 | "
                f"close_frac={tp1_ratio:.2f} | price={current_price:.6f} | floating=+{floating*100:.2f}%"
            )
            return {
                "action":      "tp1",
                "close_ratio": tp1_ratio,
                "price":       current_price,
                "message":     (
                    f"🎯 TP1 hit! +{floating*100:.2f}% (target {tp1_target*100:.2f}%). "
                    f"Closing {int(tp1_ratio*100)}%, SL → breakeven+0.1%."
                )
            }

        # ── Rule C: [QUANT AGGRESSION] TP2 — close 30% at 1.5× SL distance ──
        tp2_target = sl_distance * tp2_at_mult
        tp2_hit_now = floating >= tp2_target
        if position.tp1_hit and not position.tp2_hit and tp2_hit_now:
            log.info(
                f"[PARTIAL] {position.asset} | pos_id={position.position_id} | label=TP2 | "
                f"close_frac={tp2_ratio:.2f} | price={current_price:.6f} | floating=+{floating*100:.2f}%"
            )
            return {
                "action":      "tp2",
                "close_ratio": tp2_ratio,
                "price":       current_price,
                "message":     (
                    f"🎯 TP2 hit! +{floating*100:.2f}% (target {tp2_target*100:.2f}%). "
                    f"Closing {int(tp2_ratio*100)}% of remaining."
                )
            }

        # ── Rule C2: [QUANT AGGRESSION] Trail activation at 2.0× SL distance ─
        tp3_target = sl_distance * tp3_at_mult
        if position.tp2_hit and not position.tp3_hit and floating >= tp3_target:
            # Don't close — activate aggressive trailing on remaining position
            position.tp3_hit = True
            position.trailing_active = True
            log.info(
                f"🚀 [TRAIL] {position.asset}: Trail activated at +{floating*100:.2f}% "
                f"(2.0× SL). ATR trail on last {int(100-int(tp1_ratio*100)-int(tp2_ratio*50))}%."
            )
            # Fall through to Rule D to apply trail immediately

        # ── Rule D0: DISABLED (Audit #19 root cause) ────────────────────
        # [AUDIT #19 FIX 2026-06-04]
        # DATA: 87/92 trailing exits (95%) = pre-TP1 scratch, avg +0.025%, WR 51.7%.
        # REAL post-TP1 trailing: 5 trades, 100% WR, avg +0.317%.
        #
        # ROOT CAUSE: arm at +0.10% with width 0.12% puts stop at -0.02% (below BE).
        # Every minor retrace triggers exit = coin flip, not edge.
        #
        # FIX: Remove pre-TP1 trail exit. Let positions either:
        # - Reach TP1 → arm proper ATR trail (Rule D, the REAL edge)
        # - Get killed by momentum_death/time_exit (acceptable loss)
        #
        # The edge of this bot is trailing POST-TP1 (historically 100% WR).
        # Pre-TP1 scratch trades destroy that by exiting before trend develops.
        # --- Rule D0 code removed, fall through to Rule D ---

        # ── Rule D: ATR-based trailing stop on last position piece ───────
        # Standard: trail aktivasi setelah TP1, gunakan ATR × 2.0 dari peak.
        # Trailing level HANYA naik (ratchet) — tidak pernah dilebarkan.
        # Fallback ke vol-fraction bila entry_atr tidak tersedia.
        if position.tp1_hit:
            tp1_diff_pct = abs(position.entry_price - position.tp1) / position.entry_price
            _trail_extra = getattr(cfg, 'atr_trail_post_tp1_extra_pct', 0.001)
            activation_threshold = tp1_diff_pct + _trail_extra

            if max_floating >= activation_threshold:
                entry_atr = getattr(position, 'entry_atr', 0.0)
                atr_mult  = getattr(cfg, 'atr_trailing_multiplier', 2.0)

                if entry_atr > 0:
                    # ATR-based: trail distance = ATR_at_entry × multiplier
                    # This is the industry-standard approach — buffer scales with
                    # actual volatility measured at trade open, not current vol.
                    trail_pct = entry_atr * atr_mult
                    trail_pct = max(trail_pct, 0.003)   # floor 0.3%
                    trail_pct = min(trail_pct, 0.060)   # cap 6% (extreme assets)
                else:
                    # Fallback when ATR unavailable: tighter after TP2/TP3
                    vol_est = getattr(position, 'realized_vol', 0.02)
                    if position.tp2_hit:
                        trail_pct = max(vol_est * 0.30, 0.003)
                    else:
                        trail_pct = max(vol_est * 0.50, 0.005)

                # [REKONSTRUKSI v10 F2.2] Structural trail — trail di balik swing
                # low/high terbaru (bukan % tetap). Default OFF (flag). Kalau ON,
                # override trail_pct dengan jarak ke swing struktur + buffer.
                if getattr(cfg, 'structural_trail_enabled', False):
                    _lb = int(getattr(cfg, 'structural_trail_swing_lookback', 10))
                    _buf = getattr(cfg, 'structural_trail_buffer_pct', 0.001)
                    _highs_s = getattr(position, 'candle_highs', []) or []
                    _lows_s = getattr(position, 'candle_lows', []) or []
                    try:
                        if position.side == Side.LONG and len(_lows_s) >= _lb:
                            _swing_low = min(_lows_s[-_lb:])
                            _struct_pct = (new_high - _swing_low) / new_high + _buf
                            if 0.002 <= _struct_pct <= 0.060:
                                trail_pct = _struct_pct
                        elif position.side == Side.SHORT and len(_highs_s) >= _lb:
                            _swing_high = max(_highs_s[-_lb:])
                            _struct_pct = (_swing_high - new_low) / new_low + _buf
                            if 0.002 <= _struct_pct <= 0.060:
                                trail_pct = _struct_pct
                    except (ValueError, ZeroDivisionError):
                        pass

                if position.side == Side.LONG:
                    # Compute fresh trail level from current peak
                    trail_sl_new = new_high * (1 - trail_pct)
                    # Ratchet: only move trail up, never down
                    trail_sl = max(trail_sl_new, getattr(position, 'trailing_stop_price', 0.0))
                    if current_price <= trail_sl:
                        return {
                            "action":      "trailing_stop",
                            "close_ratio": 1.0,
                            "price":       current_price,
                            "trail_price": trail_sl,
                            "trail_pct":   trail_pct,
                            "message":     (
                                f"🛡️ ATR Trail ({trail_pct*100:.1f}%) hit at {trail_sl:.4f} "
                                f"(peak +{max_floating*100:.1f}%)."
                            )
                        }
                    # Update ratcheted trail level on position (executor persists it)
                    if trail_sl > getattr(position, 'trailing_stop_price', 0.0):
                        position.trailing_stop_price = trail_sl
                else:
                    trail_sl_new = new_low * (1 + trail_pct)
                    # Ratchet: for SHORT, trail_sl only moves down (tightens profit lock).
                    # Use trail_sl_new when no prior level saved (trailing_stop_price == 0).
                    existing = getattr(position, 'trailing_stop_price', 0.0)
                    if existing > 0:
                        trail_sl = min(trail_sl_new, existing)
                    else:
                        trail_sl = trail_sl_new
                    if current_price >= trail_sl:
                        return {
                            "action":      "trailing_stop",
                            "close_ratio": 1.0,
                            "price":       current_price,
                            "trail_price": trail_sl,
                            "trail_pct":   trail_pct,
                            "message":     (
                                f"🛡️ ATR Trail ({trail_pct*100:.1f}%) hit at {trail_sl:.4f} "
                                f"(peak +{max_floating*100:.1f}%)."
                            )
                        }
                    if existing == 0 or trail_sl < existing:
                        position.trailing_stop_price = trail_sl

        # ── Rule F0: Quick-profit exit — ambil profit langsung saat harga berbalik ──
        # Relevan terutama untuk leverage rendah (3-5x) di Hyperliquid di mana
        # ROE per % move kecil — jangan tunggu ATR trail cycle yang panjang.
        # Aktif bahkan setelah TP1 (untuk close sisa posisi dengan cepat).
        scfg_qp = SCALPER
        if getattr(scfg_qp, 'quick_profit_enabled', True) and floating > 0:
            pos_lev = getattr(position, 'leverage', 15)

            # Sesuaikan threshold berdasarkan leverage aktual posisi
            if pos_lev <= 5:
                qp_threshold = getattr(scfg_qp, 'quick_profit_low_lev_threshold', 0.005)
                qp_retrace   = getattr(scfg_qp, 'quick_profit_low_lev_retrace', 0.002)
            else:
                qp_threshold = getattr(scfg_qp, 'quick_profit_threshold_pct', 0.008)
                qp_retrace   = getattr(scfg_qp, 'quick_profit_retrace_pct', 0.003)

            if floating >= qp_threshold:
                # Hitung retrace dari peak
                if position.side == Side.LONG:
                    peak_qp  = max(position.trailing_high, current_price)
                    retrace_qp = (peak_qp - current_price) / max(peak_qp, 1e-9)
                else:
                    peak_qp  = min(position.trailing_high, current_price)
                    retrace_qp = (current_price - peak_qp) / max(peak_qp, 1e-9)

                if retrace_qp >= qp_retrace:
                    log.info(
                        f"[QUICK-PROFIT] {position.asset} | lev={pos_lev}x | "
                        f"floating=+{floating*100:.2f}% >= {qp_threshold*100:.2f}% | "
                        f"retrace={retrace_qp*100:.2f}% >= {qp_retrace*100:.2f}% | "
                        f"tp1_hit={position.tp1_hit} → EXIT FULL"
                    )
                    return {
                        "action":      "trailing_stop",
                        "close_ratio": 1.0,
                        "price":       current_price,
                        "trail_price": current_price,
                        "trail_pct":   qp_retrace,
                        "message":     (
                            f"💰 Quick-profit exit: +{floating*100:.2f}% "
                            f"(lev={pos_lev}x, retrace {retrace_qp*100:.2f}% dari peak). "
                            f"Ambil profit sebelum balik lebih dalam."
                        )
                    }

        # ── Rule F: Early Trailing — profit tapi belum TP1 ───────────────
        # Aktif saat floating >= threshold (misal +0.5%) tanpa nunggu TP1 flag.
        # Proteksi profit saat harga balik sebelum mencapai TP1.
        if not position.tp1_hit:
            scfg = SCALPER
            if getattr(scfg, 'early_trail_enabled', True):
                act_pct  = getattr(scfg, 'early_trail_activation_pct', 0.005)
                dist_pct = getattr(scfg, 'early_trail_distance_pct', 0.003)

                if floating >= act_pct:
                    if position.side == Side.LONG:
                        peak      = max(position.trailing_high, current_price)
                        retrace   = (peak - current_price) / max(peak, 1e-9)
                    else:
                        peak      = min(position.trailing_high, current_price)
                        retrace   = (current_price - peak) / max(peak, 1e-9)

                    if retrace >= dist_pct:
                        return {
                            "action":      "early_trail",
                            "close_ratio": 1.0,
                            "price":       current_price,
                            "pnl":         position.pnl_unrealized,
                            "position_id": position.position_id,
                            "message":     (
                                f"🛡️ Early trail: profit +{floating*100:.2f}% tapi "
                                f"retraced {retrace*100:.2f}% dari peak. Kunci profit."
                            ),
                        }

        # ── Rule G: Volume-spike exit — REMOVED (2026-05-10) ────────────────
        # Dihapus karena: momentum exit (Rule E2) dan emergency exit sudah
        # menangani kasus yang sama dengan lebih baik. Vol spike sering
        # false-positive karena perbandingan 2 candle terlalu noisy.

        # ── Rule E: [TIME EXIT REDESIGN 2026-05-18] ────────────────────────
        #
        # Masalah lama: time exit flat di menit X → winner dipotong paksa,
        # loser diberi grace period yang tidak perlu.
        #
        # Desain baru — 3 layer:
        #   L1. Early profit-lock: jika floating > 0.3% sebelum TP1 hit →
        #       aktifkan trailing stop 0.15% dari peak. Biarkan runner.
        #   L2. Early loss cut: jika floating < -0.5% dan hold > 8m →
        #       exit segera. Posisi yang langsung turun = sinyal salah.
        #   L3. Hard time limit: jika hold >= max_hold →
        #       - profit: exit (ambil sisa)
        #       - loss: grace HANYA jika posisi pernah profit (max_unrealized_loss > 0)
        if getattr(position, 'trade_mode', 'scalper') == 'scalper':
            scfg = SCALPER
            entry_score = getattr(position, 'entry_score', 50)

            # Score-driven max_hold (calibrated for opportunity scoring v2)
            # New scoring: 45-55 = marginal, 55-70 = good setup, 70+ = excellent
            if entry_score >= 70:
                max_hold = 25.0
            elif entry_score >= 60:
                max_hold = 20.0
            elif entry_score >= 50:
                max_hold = 15.0
            else:
                max_hold = 10.0

            grace      = getattr(scfg, 'max_hold_grace_minutes', 5.0)
            soft_floor = getattr(scfg, 'max_hold_soft_floor_pct', -0.010)

            # Threshold baru dari config
            early_trail_pct   = getattr(scfg, 'time_exit_early_trail_pct',   0.001)   # [AUDIT FIX 2026-05-21] 0.10% activation. trailing=100% WR, needs to fire more.
            early_trail_width = getattr(scfg, 'time_exit_early_trail_width',  0.0008)  # [AUDIT FIX 2026-05-21] 0.08% trail width. Lock profit tight.
            early_loss_pct    = getattr(scfg, 'time_exit_early_loss_pct',    -0.002)  # [AUDIT FIX 2026-05-21] Use config value -0.2%. Was defaulting to -0.3% which let losers bleed.
            early_loss_mins   = getattr(scfg, 'time_exit_early_loss_mins',    5.0)    # [AUDIT FIX 2026-05-21] 5min verdict. Gives trade time but doesn't hold dead weight.

            now    = _dt.now(_tz.utc)
            opened = position.opened_at
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=_tz.utc)
            hold_minutes = (now - opened).total_seconds() / 60.0

            # Runner grace: jika TP1 sudah hit, extend deadline 50%
            effective_max = max_hold * 1.5 if position.tp1_hit else max_hold

            # ── [REKONSTRUKSI v10 — F0.2] PROGRESS-BASED TIME STOP ───────────
            # Trader pro tidak hold trade yang tidak perform. Data KARA: time_exit
            # median hold 6min, WR 5.8%, -$55 (seluruh kerugian bot). Akar: trade
            # masuk lalu DIAM. Solusi: kalau dalam 8 menit belum capai +0.5R, thesis
            # kemungkinan salah → keluar SEBELUM jadi time_exit -1R penuh.
            # 0.5R = setengah jarak SL. Ubah time_exit dari "dump bucket" jadi
            # "early invalidation cut". Hanya pre-TP1 (post-TP1 sudah profit-locked).
            _prog_mins = getattr(scfg, 'progress_time_stop_minutes', 8.0)
            _prog_min_r = getattr(scfg, 'progress_time_stop_min_r', 0.5)
            if (not position.tp1_hit
                    and not getattr(position, 'trailing_active', False)
                    and hold_minutes >= _prog_mins):
                _sl_dist = sl_distance if sl_distance > 0 else 0.008
                _progress_r = floating / _sl_dist if _sl_dist > 0 else 0.0
                if _progress_r < _prog_min_r:
                    return {
                        "action":      "time_exit",
                        "close_ratio": 1.0,
                        "price":       current_price,
                        "pnl":         position.pnl_unrealized,
                        "position_id": position.position_id,
                        "message":     (
                            f"⏱️ Progress stop {hold_minutes:.0f}m: hanya {_progress_r:.2f}R "
                            f"(<{_prog_min_r}R) — thesis lemah, cut sebelum -1R penuh. "
                            f"PnL: {floating*100:.2f}%."
                        )
                    }

            # ── L1: Early profit-lock trailing — DISABLED ────────────────
            # [AUDIT #19 FIX 2026-06-04] This armed trailing at +0.10% pre-TP1.
            # With Rule D0 removed, arming here has no exit path anyway.
            # Let trades develop to TP1. Don't cut +0.12% ticks.
            # --- L1 early profit-lock disabled ---

            # ── L1.5: MOMENTUM DEATH — flat AND never developed favorable move ─
            # [AUDIT #18 P0] Skip if peak favorable >= 0.10% — those trades should
            # have armed early trail at +0.10%; don't cut a +0.12% tick then flat.
            _momentum_death_mins = getattr(scfg, 'momentum_death_min_minutes', 4.0)
            _momentum_death_threshold = getattr(scfg, 'momentum_death_flat_pct', 0.0005)
            _momentum_death_peak = getattr(scfg, 'momentum_death_peak_max_pct', 0.0010)
            if (hold_minutes >= _momentum_death_mins
                    and not position.tp1_hit
                    and not getattr(position, 'trailing_active', False)
                    and abs(floating) < _momentum_death_threshold
                    and max_floating < _momentum_death_peak):
                return {
                    "action":      "momentum_death",
                    "close_ratio": 1.0,
                    "price":       current_price,
                    "pnl":         position.pnl_unrealized,
                    "position_id": position.position_id,
                    "message":     (
                        f"💀 Momentum death {hold_minutes:.0f}m: "
                        f"flat {floating*100:.3f}% (peak +{max_floating*100:.2f}% < "
                        f"{_momentum_death_peak*100:.1f}%). No edge — exit."
                    )
                }

            # ── L2: Early loss cut ───────────────────────────────────────────
            # Posisi yang langsung turun -0.5% dalam 8m = sinyal salah, bukan noise.
            # Jangan tunggu max_hold — cut sekarang.
            if (hold_minutes >= early_loss_mins
                    and floating <= early_loss_pct
                    and not position.tp1_hit):
                # [AUDIT FIX 2026-05-21] Removed never_profited gate.
                # Data: 75% of time_exit trades were losers that briefly touched +0.01%
                # then bled out. If floating -0.2% after 5min, signal is dead regardless.
                return {
                    "action":      "time_exit",
                    "close_ratio": 1.0,
                    "price":       current_price,
                    "pnl":         position.pnl_unrealized,
                    "position_id": position.position_id,
                    "message":     (
                        f"⏱️ Early loss cut {hold_minutes:.0f}m: "
                        f"floating {floating*100:.2f}% < {early_loss_pct*100:.1f}%. "
                        f"Cut sekarang."
                    )
                }

            # ── L3: Hard time limit ──────────────────────────────────────────
            # [AUDIT #10 FIX] Grace period for high-conviction flat trades.
            # AR SHORT sc=80: direction benar, tapi dump delayed 5-10min setelah time_exit.
            # Grace = +10min HANYA jika: score≥70 + loss<0.3% + belum grace sebelumnya.
            _grace_extended = getattr(position, '_grace_extended', False)
            if (not _grace_extended
                    and entry_score >= 70
                    and hold_minutes >= effective_max
                    and floating > -0.003
                    and not position.tp1_hit):
                position._grace_extended = True
                effective_max += 10.0  # extend 10 menit

            if hold_minutes >= effective_max:
                if floating > 0:
                    # Profit saat time limit → exit, ambil sisa
                    return {
                        "action":      "time_exit",
                        "close_ratio": 1.0,
                        "price":       current_price,
                        "pnl":         position.pnl_unrealized,
                        "position_id": position.position_id,
                        "message":     (
                            f"⏱️ Time exit (profit) {hold_minutes:.0f}m/{effective_max:.0f}m "
                            f"(score={entry_score}). PnL: +{floating*100:.2f}%."
                        )
                    }

                # Loss saat time limit:
                # Grace HANYA untuk posisi yang pernah profit (pernah floating > 0)
                max_unreal = getattr(position, 'max_unrealized_loss', 0.0)
                ever_profited = max_unreal > 0.0
                is_within_grace  = hold_minutes < (effective_max + grace)
                is_recoverable   = floating > soft_floor

                if ever_profited and is_within_grace and is_recoverable:
                    pass  # pernah profit, masih dalam grace, masih recoverable → tunggu
                else:
                    if ever_profited and not is_within_grace:
                        note = f" (grace habis, total {hold_minutes:.0f}m)"
                    elif ever_profited and not is_recoverable:
                        note = f" (loss {floating*100:.2f}% > floor {soft_floor*100:.1f}%)"
                    elif not ever_profited:
                        note = f" (tidak pernah profit, cut)"
                    else:
                        note = ""
                    return {
                        "action":      "time_exit",
                        "close_ratio": 1.0,
                        "price":       current_price,
                        "pnl":         position.pnl_unrealized,
                        "position_id": position.position_id,
                        "message":     (
                            f"⏱️ Time exit {hold_minutes:.0f}m/{effective_max:.0f}m "
                            f"(score={entry_score}). PnL: {floating*100:.2f}%.{note}"
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
