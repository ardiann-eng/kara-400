"""KARA hybrid execution engine.

Turns a v10 gate-pass signal into an execution intent. The goal is not to add
another signal filter, but to decide whether the current price is a tradeable
location for the setup.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from models.schemas import Side, TradeSignal

log = logging.getLogger("kara.execution_engine")


@dataclass
class ExecutionIntent:
    playbook: str
    order_type: str
    status: str
    trigger: str = ""
    cancel_reason: Optional[str] = None
    reference_level: Optional[float] = None
    invalidation_level: Optional[float] = None
    intended_entry: Optional[float] = None
    actual_entry: Optional[float] = None
    ttl_sec: int = 0
    wait_sec: float = 0.0
    spread_bps: float = 0.0
    cost_bps: float = 0.0
    chase_pct: float = 0.0
    notes: List[str] = field(default_factory=list)

    @property
    def can_enter(self) -> bool:
        return self.status in ("ready", "shadow_ready")


class ExecutionEngine:
    def __init__(self, data_client: Any, cfg: Any):
        self.data_client = data_client
        self.cfg = cfg

    async def resolve(self, signal: TradeSignal) -> ExecutionIntent:
        if not getattr(self.cfg, "enabled", True):
            intent = ExecutionIntent(
                playbook="disabled",
                order_type="market",
                status="ready",
                intended_entry=signal.entry_price,
                actual_entry=signal.entry_price,
                notes=["execution_engine_disabled"],
            )
            self.apply_to_signal(signal, intent)
            return intent

        ctx = await self._load_context(signal)
        intent = self._initial_intent(signal, ctx)

        if self._cost_is_bad(signal, ctx, intent):
            intent.status = "cancelled"
            intent.order_type = "cancel"
            intent.cancel_reason = "cost_bad_tp1_lt_3x_cost"
            intent.notes.append("tp1_distance_not_enough_for_fee_slippage")
            self.apply_to_signal(signal, intent)
            return intent

        if intent.status == "pending":
            intent = await self._wait_for_trigger(signal, intent, ctx)

        if getattr(self.cfg, "shadow_mode", False) and intent.status == "cancelled":
            intent.status = "shadow_ready"
            intent.notes.append(f"shadow_would_cancel:{intent.cancel_reason}")
            intent.cancel_reason = None

        if intent.can_enter and intent.actual_entry and intent.actual_entry > 0:
            self._shift_levels(signal, intent.actual_entry)

        self.apply_to_signal(signal, intent)
        return intent

    def apply_to_signal(self, signal: TradeSignal, intent: ExecutionIntent) -> None:
        signal.execution_playbook = intent.playbook
        signal.execution_order_type = intent.order_type
        signal.execution_status = intent.status
        signal.execution_trigger = intent.trigger
        signal.execution_cancel_reason = intent.cancel_reason
        signal.execution_reference_level = intent.reference_level
        signal.execution_invalidation_level = intent.invalidation_level
        signal.execution_intended_entry = intent.intended_entry
        signal.execution_actual_entry = intent.actual_entry
        signal.execution_ttl_sec = intent.ttl_sec
        signal.execution_wait_sec = intent.wait_sec
        signal.execution_spread_bps = intent.spread_bps
        signal.execution_cost_bps = intent.cost_bps
        signal.execution_chase_pct = intent.chase_pct
        signal.execution_notes = intent.notes

    async def _load_context(self, signal: TradeSignal) -> Dict[str, Any]:
        mark = signal.entry_price
        candles: List[Any] = []
        ob = None

        try:
            px = await self.data_client.get_mark_price(signal.asset)
            if px and px > 0:
                mark = float(px)
        except Exception as e:
            log.debug(f"[EXEC-ENGINE] mark price fallback {signal.asset}: {e}")

        try:
            candles = await self.data_client.get_candles(
                signal.asset,
                getattr(self.cfg, "candle_interval", "1m"),
                limit=getattr(self.cfg, "candle_limit", 40),
            )
        except Exception as e:
            log.debug(f"[EXEC-ENGINE] candles unavailable {signal.asset}: {e}")

        try:
            ob = await self.data_client.get_orderbook(signal.asset, depth=20)
        except Exception as e:
            log.debug(f"[EXEC-ENGINE] orderbook unavailable {signal.asset}: {e}")

        highs, lows, closes = self._extract_ohlc(candles)
        if not closes:
            closes = [mark]
            highs = [mark]
            lows = [mark]

        spread_bps = 0.0
        if ob is not None and getattr(ob, "spread_pct", 0.0):
            spread_bps = float(ob.spread_pct) * 10000.0

        return {
            "mark": mark,
            "highs": highs,
            "lows": lows,
            "closes": closes,
            "ema13": self._ema(closes, getattr(self.cfg, "ema_fast", 13)),
            "ema21": self._ema(closes, getattr(self.cfg, "ema_slow", 21)),
            "spread_bps": spread_bps,
        }

    def _initial_intent(self, signal: TradeSignal, ctx: Dict[str, Any]) -> ExecutionIntent:
        side = signal.side
        setup = (getattr(signal, "v10_setup", "none") or "none").lower()
        tier = (getattr(signal, "v10_tier", "B") or "B").upper()
        mark = float(ctx["mark"])
        rv = float(getattr(signal, "realized_vol", 0.0) or 0.0)
        size_mult = float(getattr(signal, "size_mult", 1.0) or 1.0)
        high_rv = rv >= getattr(self.cfg, "high_rv_threshold", 0.06) or size_mult <= 0.31

        if high_rv:
            return self._retest_intent(signal, ctx, "high_rv_retest", ttl=getattr(self.cfg, "high_rv_ttl_sec", 120))

        if side == Side.SHORT and setup == "momentum":
            return self._short_momentum_intent(signal, ctx)

        if setup == "pullback":
            return self._retest_intent(signal, ctx, "pullback_limit", ttl=getattr(self.cfg, "pullback_ttl_sec", 90))

        if setup in ("sweep", "reclaim"):
            return self._retest_intent(signal, ctx, "sweep_reclaim", ttl=120)

        if side == Side.LONG:
            return self._retest_intent(signal, ctx, "long_reclaim", ttl=getattr(self.cfg, "long_reclaim_ttl_sec", 120))

        # Non-momentum shorts can still be tradeable, but avoid blind market entry.
        if side == Side.SHORT:
            return self._retest_intent(signal, ctx, f"short_{setup}_retest", ttl=getattr(self.cfg, "retest_ttl_sec", 60))

        return ExecutionIntent(
            playbook=f"{side.value}_{tier}_{setup}",
            order_type="market",
            status="ready",
            trigger="gate_pass_direct",
            intended_entry=mark,
            actual_entry=mark,
            spread_bps=float(ctx.get("spread_bps", 0.0)),
            cost_bps=self._estimated_cost_bps(ctx),
            notes=["direct_fallback"],
        )

    def _short_momentum_intent(self, signal: TradeSignal, ctx: Dict[str, Any]) -> ExecutionIntent:
        mark = float(ctx["mark"])
        lows = ctx["lows"]
        lookback = max(2, int(getattr(self.cfg, "recent_break_lookback", 3)))
        prior_lows = lows[-(lookback + 1):-1] if len(lows) > lookback else lows[:-1]
        ref = min(prior_lows) if prior_lows else mark
        chase = max(0.0, (ref - mark) / max(ref, 1e-9))
        spread_bps = float(ctx.get("spread_bps", 0.0))
        fresh_break = mark <= ref and chase <= getattr(self.cfg, "market_max_chase_pct", 0.003)

        if fresh_break and spread_bps <= getattr(self.cfg, "market_max_spread_bps", 4.0):
            return ExecutionIntent(
                playbook="short_momentum",
                order_type="aggressive_limit",
                status="ready",
                trigger="break_recent_low",
                reference_level=ref,
                invalidation_level=max(ctx["highs"][-lookback:]) if ctx["highs"] else None,
                intended_entry=mark * (1 - getattr(self.cfg, "aggressive_limit_offset_bps", 2.0) / 10000.0),
                actual_entry=mark,
                ttl_sec=getattr(self.cfg, "short_momentum_ttl_sec", 30),
                spread_bps=spread_bps,
                cost_bps=self._estimated_cost_bps(ctx),
                chase_pct=chase,
                notes=["fresh_short_break"],
            )

        intent = self._retest_intent(signal, ctx, "short_momentum_retest", ttl=getattr(self.cfg, "retest_ttl_sec", 60))
        intent.reference_level = ref
        intent.chase_pct = chase
        intent.notes.append("short_momentum_extended_wait_retest")
        return intent

    def _retest_intent(self, signal: TradeSignal, ctx: Dict[str, Any], playbook: str, ttl: int) -> ExecutionIntent:
        mark = float(ctx["mark"])
        side = signal.side
        ema13 = float(ctx.get("ema13") or mark)
        ema21 = float(ctx.get("ema21") or ema13)
        highs = ctx["highs"]
        lows = ctx["lows"]

        if side == Side.LONG:
            ref = min(max(ema13, ema21), mark) if mark >= min(ema13, ema21) else max(ema13, ema21)
            if lows:
                ref = max(ref, min(lows[-min(len(lows), 5):]))
            invalid = min(lows[-min(len(lows), 5):]) if lows else signal.stop_loss
            trigger = "pullback_reclaim_level"
            order_type = "passive_limit"
        else:
            ref = max(min(ema13, ema21), mark) if mark <= max(ema13, ema21) else min(ema13, ema21)
            if highs:
                ref = min(ref, max(highs[-min(len(highs), 5):]))
            invalid = max(highs[-min(len(highs), 5):]) if highs else signal.stop_loss
            trigger = "retest_resistance_level"
            order_type = "passive_limit"

        return ExecutionIntent(
            playbook=playbook,
            order_type=order_type,
            status="pending",
            trigger=trigger,
            reference_level=ref,
            invalidation_level=invalid,
            intended_entry=ref,
            ttl_sec=ttl,
            spread_bps=float(ctx.get("spread_bps", 0.0)),
            cost_bps=self._estimated_cost_bps(ctx),
            notes=["waiting_for_tradeable_location"],
        )

    async def _wait_for_trigger(
        self, signal: TradeSignal, intent: ExecutionIntent, ctx: Dict[str, Any]
    ) -> ExecutionIntent:
        start = time.monotonic()
        ttl = max(1, intent.ttl_sec)
        poll = max(1.0, float(getattr(self.cfg, "poll_interval_sec", 5.0)))
        ref = float(intent.reference_level or signal.entry_price)
        tol = float(getattr(self.cfg, "retest_tolerance_pct", 0.0015))
        reclaim = float(getattr(self.cfg, "reclaim_buffer_pct", 0.0005))

        last_price = float(ctx["mark"])
        touched = False
        while time.monotonic() - start <= ttl:
            try:
                px = await self.data_client.get_mark_price(signal.asset)
                if px and px > 0:
                    last_price = float(px)
            except Exception:
                pass

            if signal.side == Side.LONG:
                if last_price <= ref * (1 + tol):
                    touched = True
                if touched and last_price >= ref * (1 + reclaim):
                    intent.status = "ready"
                    intent.order_type = "aggressive_limit"
                    intent.actual_entry = last_price
                    intent.intended_entry = last_price
                    intent.wait_sec = round(time.monotonic() - start, 2)
                    intent.notes.append("retest_reclaim_confirmed")
                    return intent
                if not touched and last_price > ref * (1 + getattr(self.cfg, "pullback_max_away_pct", 0.0025)):
                    # Still allow some time for long trend to pull back; don't cancel immediately.
                    pass
            else:
                if last_price >= ref * (1 - tol):
                    touched = True
                if touched and last_price <= ref * (1 - reclaim):
                    intent.status = "ready"
                    intent.order_type = "aggressive_limit"
                    intent.actual_entry = last_price
                    intent.intended_entry = last_price
                    intent.wait_sec = round(time.monotonic() - start, 2)
                    intent.notes.append("retest_reject_confirmed")
                    return intent

            await asyncio.sleep(poll)

        intent.status = "cancelled"
        intent.order_type = "cancel"
        intent.cancel_reason = "no_retest_or_reclaim"
        intent.wait_sec = round(time.monotonic() - start, 2)
        intent.actual_entry = last_price
        return intent

    def _cost_is_bad(self, signal: TradeSignal, ctx: Dict[str, Any], intent: ExecutionIntent) -> bool:
        entry = float(ctx["mark"] or signal.entry_price)
        tp1_dist_bps = abs(float(signal.tp1) - entry) / max(entry, 1e-9) * 10000.0
        cost_bps = self._estimated_cost_bps(ctx)
        intent.cost_bps = cost_bps
        if tp1_dist_bps < cost_bps * float(getattr(self.cfg, "min_tp1_to_cost_multiple", 3.0)):
            return True
        return False

    def _estimated_cost_bps(self, ctx: Dict[str, Any]) -> float:
        return float(getattr(self.cfg, "default_roundtrip_cost_bps", 7.0)) + float(ctx.get("spread_bps", 0.0))

    def _shift_levels(self, signal: TradeSignal, new_entry: float) -> None:
        old = float(signal.entry_price)
        if old <= 0 or abs(new_entry - old) / old < 1e-8:
            signal.entry_price = new_entry
            return

        sl_dist = abs(old - float(signal.stop_loss)) / old
        tp1_dist = abs(float(signal.tp1) - old) / old
        tp2_dist = abs(float(signal.tp2) - old) / old
        tp3_dist = abs(float(getattr(signal, "tp3", 0.0) or 0.0) - old) / old if getattr(signal, "tp3", 0.0) else 0.0

        signal.entry_price = round(new_entry, 8)
        if signal.side == Side.LONG:
            signal.stop_loss = round(new_entry * (1 - sl_dist), 8)
            signal.tp1 = round(new_entry * (1 + tp1_dist), 8)
            signal.tp2 = round(new_entry * (1 + tp2_dist), 8)
            if tp3_dist:
                signal.tp3 = round(new_entry * (1 + tp3_dist), 8)
        else:
            signal.stop_loss = round(new_entry * (1 + sl_dist), 8)
            signal.tp1 = round(new_entry * (1 - tp1_dist), 8)
            signal.tp2 = round(new_entry * (1 - tp2_dist), 8)
            if tp3_dist:
                signal.tp3 = round(new_entry * (1 - tp3_dist), 8)

    @staticmethod
    def _extract_ohlc(candles: List[Any]) -> Tuple[List[float], List[float], List[float]]:
        highs: List[float] = []
        lows: List[float] = []
        closes: List[float] = []
        for c in candles or []:
            try:
                if isinstance(c, dict):
                    h = float(c.get("h") or c.get("high") or 0)
                    l = float(c.get("l") or c.get("low") or 0)
                    cl = float(c.get("c") or c.get("close") or 0)
                elif isinstance(c, (list, tuple)) and len(c) >= 5:
                    h = float(c[2])
                    l = float(c[3])
                    cl = float(c[4])
                else:
                    continue
                if cl > 0:
                    highs.append(h)
                    lows.append(l)
                    closes.append(cl)
            except (TypeError, ValueError):
                continue
        return highs, lows, closes

    @staticmethod
    def _ema(values: List[float], period: int) -> float:
        vals = [float(v) for v in values if v and v > 0]
        if not vals:
            return 0.0
        if len(vals) < period:
            return vals[-1]
        alpha = 2.0 / (period + 1.0)
        ema = vals[0]
        for v in vals[1:]:
            ema = v * alpha + ema * (1.0 - alpha)
        return ema
