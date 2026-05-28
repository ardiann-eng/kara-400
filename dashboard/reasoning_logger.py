"""
KARA Bot - Reasoning Logger
Emits structured JSON at each decision step for admin dashboard transparency.
Zero performance impact on bot (async, ring buffer, no blocking I/O).
"""
from __future__ import annotations
import time
import json
import logging
from collections import deque
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field, asdict

log = logging.getLogger("kara.reasoning")

# Ring buffer — keeps last 500 decisions in memory (no disk I/O during trading)
MAX_DECISIONS = 500
MAX_STEPS = 2000


@dataclass
class ReasoningStep:
    timestamp: float
    asset: str
    step: str          # "signal", "learning", "threshold", "filters", "decision"
    data: Dict[str, Any]

    def to_dict(self):
        return asdict(self)


@dataclass
class DecisionTrace:
    """Full trace of one scoring cycle for one asset."""
    trace_id: str
    asset: str
    timestamp: float
    steps: List[Dict] = field(default_factory=list)
    final_decision: str = "pending"  # "execute", "skip", "blocked"
    final_score: int = 0
    side: str = ""
    reason: str = ""

    def add_step(self, step: str, data: Dict):
        self.steps.append({
            "ts": time.time(),
            "step": step,
            "data": data,
        })

    def to_dict(self):
        return {
            "trace_id": self.trace_id,
            "asset": self.asset,
            "timestamp": self.timestamp,
            "steps": self.steps,
            "final_decision": self.final_decision,
            "final_score": self.final_score,
            "side": self.side,
            "reason": self.reason,
        }


class ReasoningLogger:
    """
    In-memory ring buffer for bot reasoning traces.
    Dashboard reads from here via API — zero disk I/O during trading.
    """

    def __init__(self):
        self._decisions: deque = deque(maxlen=MAX_DECISIONS)
        self._live_steps: deque = deque(maxlen=MAX_STEPS)
        self._active_traces: Dict[str, DecisionTrace] = {}  # asset -> current trace
        self._ws_callbacks: List = []  # WebSocket broadcast callbacks
        # ML metrics
        self._ml_stats = {
            "total_predictions": 0,
            "correct_predictions": 0,
            "blocked_by_ml": 0,
            "boosted_by_ml": 0,
        }
        # Pattern memory stats
        self._pattern_stats = {
            "total_patterns": 0,
            "flips_triggered": 0,
            "penalties_applied": 0,
            "boosts_applied": 0,
        }

    # ── Trace lifecycle ──

    def start_trace(self, asset: str, trace_id: str = None) -> DecisionTrace:
        """Start a new decision trace for an asset."""
        import uuid
        tid = trace_id or f"{asset}_{int(time.time()*1000)}"
        trace = DecisionTrace(trace_id=tid, asset=asset, timestamp=time.time())
        self._active_traces[asset] = trace
        return trace

    def end_trace(self, asset: str, decision: str, score: int, side: str, reason: str = ""):
        """Finalize a trace and move to history."""
        trace = self._active_traces.pop(asset, None)
        if trace:
            trace.final_decision = decision
            trace.final_score = score
            trace.side = side
            trace.reason = reason
            self._decisions.appendleft(trace.to_dict())
            self._broadcast(trace.to_dict())

    # ── Step logging ──

    def log_signal(self, asset: str, score: int, side: str, components: Dict):
        """Log signal generation step."""
        trace = self._active_traces.get(asset)
        step_data = {
            "score": score,
            "side": side,
            "components": components,
        }
        if trace:
            trace.add_step("signal", step_data)
        self._live_steps.appendleft({"ts": time.time(), "asset": asset, "step": "signal", "data": step_data})

    def log_learning(self, asset: str, decision_data: Dict):
        """Log learning engine evaluation."""
        trace = self._active_traces.get(asset)
        if trace:
            trace.add_step("learning", decision_data)
        self._live_steps.appendleft({"ts": time.time(), "asset": asset, "step": "learning", "data": decision_data})
        # Update stats
        if decision_data.get("flip_side"):
            self._pattern_stats["flips_triggered"] += 1
        if decision_data.get("score_adj", 0) < 0:
            self._pattern_stats["penalties_applied"] += 1
        elif decision_data.get("score_adj", 0) > 0:
            self._pattern_stats["boosts_applied"] += 1

    def log_filters(self, asset: str, filter_data: Dict):
        """Log filter checks (threshold, funding, squeeze, etc)."""
        trace = self._active_traces.get(asset)
        if trace:
            trace.add_step("filters", filter_data)
        self._live_steps.appendleft({"ts": time.time(), "asset": asset, "step": "filters", "data": filter_data})

    def log_momentum_gate(self, asset: str, passed: bool, move_pct: float,
                          bull_candles: int, total_candles: int, is_leading: bool,
                          required_move: float = 0.04, required_candles: int = 3):
        """Log momentum confirmation gate result (2026-05-21 change)."""
        data = {
            "passed": passed,
            "move_pct": round(move_pct * 100, 4),
            "bull_candles": bull_candles,
            "total_candles": total_candles,
            "is_leading_signal": is_leading,
            "required_move_pct": required_move,
            "required_candles": required_candles,
            "gate": "momentum_confirmation",
            "change": "2026-05-21: require 5m move ≥0.04% + 3/5 candles (leading: 2/5 only)",
        }
        trace = self._active_traces.get(asset)
        if trace:
            trace.add_step("momentum_gate", data)
        self._live_steps.appendleft({"ts": time.time(), "asset": asset, "step": "momentum_gate", "data": data})

    def log_regime_adjustment(self, asset: str, regime: str, multiplier: float,
                              score_before: int, score_after: int,
                              htf_regime: str = "", htf_threshold_adj: int = 0):
        """Log regime multiplier + 1H HTF adjustment."""
        data = {
            "vol_regime": regime,
            "regime_multiplier": multiplier,
            "score_before": score_before,
            "score_after": score_after,
            "htf_regime": htf_regime,
            "htf_threshold_adj": htf_threshold_adj,
            "gate": "regime_adjustment",
            "change": "2026-05-21: CHOPPY threshold +8 (was +2), late_trend ×0.70",
        }
        trace = self._active_traces.get(asset)
        if trace:
            trace.add_step("regime_adjustment", data)
        self._live_steps.appendleft({"ts": time.time(), "asset": asset, "step": "regime_adjustment", "data": data})

    def log_ml_prediction(self, asset: str, prob_win: float, size_mult: float):
        """Log ML model prediction."""
        trace = self._active_traces.get(asset)
        data = {"prob_win": prob_win, "size_mult": size_mult}
        if trace:
            trace.add_step("ml_prediction", data)
        self._ml_stats["total_predictions"] += 1
        if prob_win < 0.35:
            self._ml_stats["blocked_by_ml"] += 1
        elif prob_win > 0.65:
            self._ml_stats["boosted_by_ml"] += 1

    def log_execution(self, asset: str, exec_data: Dict):
        """Log final execution decision."""
        trace = self._active_traces.get(asset)
        if trace:
            trace.add_step("execution", exec_data)
        self._live_steps.appendleft({"ts": time.time(), "asset": asset, "step": "execution", "data": exec_data})

    def record_ml_outcome(self, predicted_win: bool, actual_win: bool):
        """Record ML prediction accuracy."""
        if predicted_win == actual_win:
            self._ml_stats["correct_predictions"] += 1

    # ── Query API ──

    def get_recent_decisions(self, limit: int = 50) -> List[Dict]:
        """Get recent decision traces for dashboard."""
        return list(self._decisions)[:limit]

    def get_live_steps(self, limit: int = 100) -> List[Dict]:
        """Get recent reasoning steps (real-time feed)."""
        return list(self._live_steps)[:limit]

    def get_ml_stats(self) -> Dict:
        """Get ML model performance stats."""
        total = self._ml_stats["total_predictions"]
        correct = self._ml_stats["correct_predictions"]
        return {
            **self._ml_stats,
            "accuracy": correct / total if total > 0 else 0.0,
        }

    def get_pattern_stats(self) -> Dict:
        """Get pattern memory stats with action indicators."""
        from engine.learning_engine import learning_engine
        learning_engine.load()
        patterns = learning_engine._patterns
        self._pattern_stats["total_patterns"] = len(patterns)

        def _action_label(wr, n):
            """Show what the learning engine DOES with this pattern."""
            if n < 5:
                return "📊 collecting"
            if wr >= 0.80 and n >= 8:
                return "🚀 +12 pts"
            if wr >= 0.65:
                return "✅ +8 pts"
            if wr < 0.25:
                return "🔄 FLIP or -20 pts"
            if wr < 0.40:
                return "⚠️ -10 pts"
            return "— neutral"

        sorted_patterns = sorted(patterns.items(), key=lambda x: x[1].total_pnl)
        top_losers = [{"key": k, "wr": f"{v.ema_wr:.0%}", "n": v.n, "pnl": round(v.total_pnl, 2),
                       "action": _action_label(v.ema_wr, v.n)}
                      for k, v in sorted_patterns[:10] if v.n >= 3 and v.total_pnl < 0]
        top_winners = [{"key": k, "wr": f"{v.ema_wr:.0%}", "n": v.n, "pnl": round(v.total_pnl, 2),
                        "action": _action_label(v.ema_wr, v.n)}
                       for k, v in reversed(sorted_patterns) if v.n >= 3 and v.total_pnl > 0][:10]

        return {
            **self._pattern_stats,
            "top_losers": top_losers,
            "top_winners": list(reversed(top_winners)),
        }

    def get_active_traces(self) -> List[Dict]:
        """Get currently active (in-progress) traces."""
        return [t.to_dict() for t in self._active_traces.values()]

    # ── WebSocket broadcast ──

    def register_ws(self, callback):
        self._ws_callbacks.append(callback)

    def unregister_ws(self, callback):
        self._ws_callbacks = [c for c in self._ws_callbacks if c != callback]

    def _broadcast(self, data: Dict):
        """Non-blocking broadcast to all connected WebSocket clients."""
        for cb in self._ws_callbacks:
            try:
                cb(data)
            except Exception:
                pass


# Singleton
reasoning_logger = ReasoningLogger()
