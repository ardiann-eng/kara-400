import os
import time
import logging
import asyncio
import numpy as np
import joblib
from sklearn.ensemble import HistGradientBoostingClassifier
import config
from intelligence.experience_buffer import experience_buffer

log = logging.getLogger("IntelligenceModel")

MODEL_PATH = os.path.join(config.STORAGE_DIR, "kara_intelligence.pkl")

class IntelligenceModel:
    def __init__(self):
        self.model = None
        self.is_training = False
        self.last_train_samples = 0
        self.load_model()
        
    def load_model(self):
        if os.path.exists(MODEL_PATH):
            try:
                self.model = joblib.load(MODEL_PATH)
                log.info("🧠 Loaded existing Intelligence model.")
            except Exception as e:
                log.error(f"Failed to load model: {e}")
                self.model = None

    def get_features(self, row):
        """Convert a row from experience buffer into a feature array"""
        # We must align this perfectly with the predict method.
        # Format: [score, meta_delta, oi_score, liq_score, ob_score, session_bonus, funding_rate, realized_vol, trend_pct]
        try:
            return [
                float(row.get('score', 0)),
                float(row.get('meta_delta', 0)),
                float(row.get('oi_score', 0)),
                float(row.get('liq_score', 0)),
                float(row.get('ob_score', 0)),
                float(row.get('session_bonus', 0)),
                float(row.get('funding_rate', 0)),
                float(row.get('realized_vol', 0)),
                float(row.get('trend_pct', 0))
            ]
        except Exception:
            return [0.0] * 9

    def retrain(self):
        if self.is_training:
            return
            
        data = experience_buffer.get_training_data()
        if len(data) < 50:
            log.debug(f"🧠 Not enough data to train. Have {len(data)}, need 50.")
            return
            
        if len(data) <= self.last_train_samples + 20 and self.model is not None:
            # Need at least 20 new samples to bother retraining
            return
            
        log.info(f"🧠 Retraining Intelligence model with {len(data)} samples...")
        self.is_training = True
        
        try:
            X = []
            y = []
            for row in data:
                features = self.get_features(row)
                X.append(features)
                y.append(int(row['is_win']))
                
            X = np.array(X)
            y = np.array(y)
            
            # Count wins and losses to ensure both classes exist
            if sum(y) == 0 or sum(y) == len(y):
                log.warning("🧠 Cannot train model: Only one class present (all wins or all losses).")
                self.is_training = False
                return

            new_model = HistGradientBoostingClassifier(
                max_iter=100,
                learning_rate=0.05,
                early_stopping=True,
                validation_fraction=0.1,
                random_state=42
            )
            
            new_model.fit(X, y)
            self.model = new_model
            self.last_train_samples = len(data)
            
            joblib.dump(self.model, MODEL_PATH)
            
            # Evaluate briefly
            train_acc = new_model.score(X, y)
            log.info(f"🧠 Intelligence updated: Expected Edge model refreshed. Accuracy: {train_acc*100:.1f}%.")
            
        except Exception as e:
            log.error(f"Failed to retrain ML model: {e}")
        finally:
            self.is_training = False

    async def retrain_async(self):
        # Run synchronous retrain in a thread
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.retrain)

    def predict_edge(self, features: list) -> float:
        """Predict the expected edge (0.0 - 1.0 probability of win)"""
        if self.model is None:
            return 0.5  # Neutral if no model
            
        try:
            X = np.array(features).reshape(1, -1)
            probs = self.model.predict_proba(X)
            # Output is [prob_loss, prob_win]
            prob_win = float(probs[0][1])
            return prob_win
        except Exception as e:
            log.debug(f"Predict error: {e}")
            return 0.5

intelligence_model = IntelligenceModel()
