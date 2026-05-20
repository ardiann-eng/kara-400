"""
KARA Bot - Learning Engine
Self-learning system that adjusts scores and flips sides based on trade history.

Layer 1: Pattern Memory — fast, activates after 5 trades per pattern
Layer 2: ML Model — learns complex interactions, activates after 200 trades
"""
from __future__ import annotations
import logging
import time
import json
from typing import Dict, Optional, Tuple
from dataclasses import dataclass

log = logging.getLogger("kara.learning")


@dataclass
class PatternStats:
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    ema_wr: float = 0.5  # exponential moving average win rate

    @property
    def n(self) -> int:
        return self.wins + self.losses

    @property
    def wr(self) -> float:
        return self.wins / self.n if self.n > 0 else 0.5


@dataclass
class LearningDecision:
    score_adj: int = 0          # points to add/subtract from score
    flip_side: bool = False     # True = flip LONG↔SHORT
    size_mult: float = 1.0      # position size multiplier
    reason: str = ""


class LearningEngine:
    """
    Two-layer self-learning engine.
    
    Layer 1 (Pattern Memory): tracks {asset}_{side}_{regime} win rates.
      - WR < 25% (n>=5): penalty -20 OR flip side if opposite is better
      - WR < 40% (n>=5): penalty -10
      - WR > 65% (n>=5): bonus +8
      - WR > 80% (n>=8): bonus +12
    
    Layer 2 (ML Model): HistGradientBoosting predicts P(win) from features.
      - Activates after 200 trades
      - P(win) < 0.35: size × 0.5
      - P(win) > 0.65: size × 1.3
    """

    # EMA decay factor (recent trades weighted more)
    EMA_ALPHA = 0.15

    def __init__(self):
        self._patterns: Dict[str, PatternStats] = {}
        self._model = None
        self._model_ready = False
        self._training_count = 0
        self._last_retrain_count = 0
        self._loaded = False

    def load(self):
        """Load pattern memory from DB."""
        if self._loaded:
            return
        try:
            from core.db import user_db
            rows = user_db.load_pattern_memory()
            for key, wins, losses, total_pnl, ema_wr in rows:
                self._patterns[key] = PatternStats(
                    wins=wins, losses=losses,
                    total_pnl=total_pnl, ema_wr=ema_wr
                )
            self._training_count = user_db.get_training_data_count()
            log.info(f"[LEARN] Loaded {len(self._patterns)} patterns, {self._training_count} training samples")
            self._loaded = True
            # Try loading model if enough data
            if self._training_count >= 200:
                self._try_load_or_train_model()
        except Exception as e:
            log.warning(f"[LEARN] Failed to load: {e}")
            self._loaded = True

    def evaluate(self, asset: str, side: str, regime: str, score: int,
                 features: Optional[Dict] = None) -> LearningDecision:
        """
        Evaluate a potential trade against learned patterns.
        Called BEFORE threshold check in scoring_engine.
        
        Returns LearningDecision with score adjustment, side flip, and size multiplier.
        """
        self.load()
        decision = LearningDecision()

        # ── Layer 1: Pattern Memory ──
        pattern_key = f"{asset}_{side}_{regime}"
        pattern = self._patterns.get(pattern_key)

        if pattern and pattern.n >= 5:
            wr = pattern.ema_wr

            if wr < 0.25:
                # Very bad pattern — check if opposite side is better
                opposite_side = "short" if side == "long" else "long"
                opp_key = f"{asset}_{opposite_side}_{regime}"
                opp_pattern = self._patterns.get(opp_key)

                if opp_pattern and opp_pattern.n >= 3 and opp_pattern.ema_wr > 0.50:
                    # Opposite side is profitable → FLIP
                    decision.flip_side = True
                    decision.score_adj = 5  # small bonus for flipped trade
                    decision.reason = (
                        f"[LEARN-FLIP] {pattern_key} WR={wr:.0%} n={pattern.n} → "
                        f"flip to {opposite_side} (WR={opp_pattern.ema_wr:.0%})"
                    )
                else:
                    # No good opposite data → heavy penalty
                    decision.score_adj = -20
                    decision.reason = (
                        f"[LEARN-PENALTY] {pattern_key} WR={wr:.0%} n={pattern.n} → -20 pts"
                    )

            elif wr < 0.40:
                # Below average — mild penalty
                decision.score_adj = -10
                decision.reason = f"[LEARN] {pattern_key} WR={wr:.0%} n={pattern.n} → -10 pts"

            elif wr > 0.80 and pattern.n >= 8:
                # Excellent pattern — strong bonus
                decision.score_adj = 12
                decision.size_mult = 1.2
                decision.reason = f"[LEARN-BOOST] {pattern_key} WR={wr:.0%} n={pattern.n} → +12 pts, size×1.2"

            elif wr > 0.65:
                # Good pattern — bonus
                decision.score_adj = 8
                decision.reason = f"[LEARN-GOOD] {pattern_key} WR={wr:.0%} n={pattern.n} → +8 pts"

        # ── Layer 2: ML Model ──
        if self._model_ready and features:
            try:
                prob_win = self._predict(features)
                if prob_win < 0.35:
                    decision.size_mult = min(decision.size_mult, 0.5)
                    decision.reason += f" | ML P(win)={prob_win:.2f} → size×0.5"
                elif prob_win > 0.65:
                    decision.size_mult = max(decision.size_mult, 1.3)
                    decision.reason += f" | ML P(win)={prob_win:.2f} → size×1.3"
            except Exception as e:
                log.debug(f"[LEARN] ML predict failed: {e}")

        return decision

    def record_outcome(self, asset: str, side: str, regime: str, score: int,
                       pnl_usd: float, features: Optional[Dict] = None):
        """
        Record trade outcome. Updates pattern memory and training data.
        Called from executors when a trade closes.
        """
        self.load()
        win = pnl_usd > 0
        pattern_key = f"{asset}_{side}_{regime}"

        # Update pattern memory
        if pattern_key not in self._patterns:
            self._patterns[pattern_key] = PatternStats()
        p = self._patterns[pattern_key]

        if win:
            p.wins += 1
        else:
            p.losses += 1
        p.total_pnl += pnl_usd
        # EMA update: recent results weighted more
        p.ema_wr = p.ema_wr * (1 - self.EMA_ALPHA) + (1.0 if win else 0.0) * self.EMA_ALPHA

        # Persist to DB
        try:
            from core.db import user_db
            user_db.save_pattern_memory(pattern_key, p.wins, p.losses, p.total_pnl, p.ema_wr)
        except Exception as e:
            log.warning(f"[LEARN] Failed to persist pattern: {e}")

        # Save training data for ML model
        if features:
            try:
                from core.db import user_db
                user_db.save_training_data(features, int(win), pnl_usd)
                self._training_count += 1
            except Exception as e:
                log.debug(f"[LEARN] Failed to save training data: {e}")

        # Retrain model every 50 new trades (after initial 200)
        if (self._training_count >= 200 and
                self._training_count - self._last_retrain_count >= 50):
            self._try_load_or_train_model()

        log.info(
            f"[LEARN] {pattern_key} outcome={'WIN' if win else 'LOSS'} "
            f"pnl=${pnl_usd:+.3f} | WR={p.ema_wr:.0%} n={p.n}"
        )

    def _predict(self, features: Dict) -> float:
        """Predict P(win) from features using trained model."""
        if not self._model_ready or self._model is None:
            return 0.5
        import numpy as np
        feature_order = [
            'oi_funding_score', 'orderbook_score', 'liquidation_score',
            'displacement_5m', 'rsi', 'ema_freshness', 'atr_pct',
            'regime_code', 'hour_utc', 'score'
        ]
        x = np.array([[features.get(f, 0.0) for f in feature_order]])
        prob = self._model.predict_proba(x)[0][1]  # P(win)
        return float(prob)

    def _try_load_or_train_model(self):
        """Train or retrain the ML model from stored training data."""
        try:
            from core.db import user_db
            import numpy as np
            from sklearn.ensemble import HistGradientBoostingClassifier

            rows = user_db.load_training_data()
            if len(rows) < 200:
                return

            feature_order = [
                'oi_funding_score', 'orderbook_score', 'liquidation_score',
                'displacement_5m', 'rsi', 'ema_freshness', 'atr_pct',
                'regime_code', 'hour_utc', 'score'
            ]

            X, y = [], []
            for features_json, outcome, _ in rows:
                f = json.loads(features_json) if isinstance(features_json, str) else features_json
                row = [f.get(k, 0.0) for k in feature_order]
                X.append(row)
                y.append(outcome)

            X = np.array(X, dtype=np.float32)
            y = np.array(y, dtype=np.int32)

            model = HistGradientBoostingClassifier(
                max_iter=100, max_depth=4, learning_rate=0.1,
                min_samples_leaf=10, random_state=42
            )
            model.fit(X, y)
            self._model = model
            self._model_ready = True
            self._last_retrain_count = self._training_count

            # Log feature importances
            importances = model.feature_importances_
            top_feats = sorted(zip(feature_order, importances), key=lambda x: -x[1])[:5]
            feat_str = ", ".join(f"{f}={i:.3f}" for f, i in top_feats)
            log.info(f"[LEARN-ML] Model trained on {len(X)} samples. Top features: {feat_str}")

        except ImportError:
            log.info("[LEARN-ML] sklearn not available — Layer 2 disabled")
        except Exception as e:
            log.warning(f"[LEARN-ML] Training failed: {e}")


# Singleton instance
learning_engine = LearningEngine()
