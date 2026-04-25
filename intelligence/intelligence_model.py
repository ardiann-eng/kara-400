import os
import time
import logging
import asyncio
import numpy as np
import joblib
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import train_test_split
import config
from intelligence.experience_buffer import experience_buffer

log = logging.getLogger("IntelligenceModel")

MODEL_PATH = os.path.join(config.STORAGE_DIR, "kara_intelligence.pkl")

class IntelligenceModel:
    def __init__(self):
        self.model = None
        self.is_training = False
        self.last_train_samples = 0
        # is_ready = True hanya setelah retrain() berhasil di session ini.
        # Model yang di-load dari disk dianggap stale sampai terbukti valid.
        self.is_ready = False
        self.load_model()

    def load_model(self):
        if not os.path.exists(MODEL_PATH):
            return

        # Validasi: model harus dilatih dari jumlah sample yang sama dengan DB sekarang.
        # Kalau DB punya lebih sedikit data dari saat model disimpan -> data di-reset -> stale.
        labeled_count = 0
        try:
            labeled_count = len(experience_buffer.get_training_data())
        except Exception:
            pass

        if labeled_count < config.INTELLIGENCE_RETRAIN_MIN_SAMPLES:
            log.warning(
                f"[Intelligence] Model pkl ada tapi DB hanya {labeled_count} samples "
                f"(butuh {config.INTELLIGENCE_RETRAIN_MIN_SAMPLES}) — model dihapus, mulai fresh."
            )
            try:
                os.remove(MODEL_PATH)
            except Exception:
                pass
            return

        try:
            self.model = joblib.load(MODEL_PATH)
            self.last_train_samples = labeled_count
            self.is_ready = True
            log.info(f"[Intelligence] Model loaded dan valid ({labeled_count} training samples).")
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
                random_state=42,
                class_weight="balanced"  # tangani imbalance win rate rendah (12-28%)
            )

            # Train/test split jika data cukup — hindari in-sample accuracy palsu
            if len(X) >= 100:
                X_train, X_test, y_train, y_test = train_test_split(
                    X, y, test_size=0.2, random_state=42, stratify=y
                )
                new_model.fit(X_train, y_train)
                test_acc = new_model.score(X_test, y_test)
                log.info(
                    f"🧠 Intelligence updated: {len(data)} samples | "
                    f"Out-of-sample accuracy: {test_acc*100:.1f}% (n_test={len(X_test)})"
                )
            else:
                # Data terlalu sedikit untuk split — fit semua tapi tandai sebagai tidak valid
                new_model.fit(X, y)
                train_acc = new_model.score(X, y)
                log.warning(
                    f"🧠 Intelligence updated (IN-SAMPLE ONLY — {len(data)} data < 100). "
                    f"Accuracy: {train_acc*100:.1f}% — angka ini TIDAK VALID, butuh 100+ trades."
                )

            self.model = new_model
            self.last_train_samples = len(data)
            self.is_ready = True
            joblib.dump(self.model, MODEL_PATH)
            
        except Exception as e:
            log.error(f"Failed to retrain ML model: {e}")
        finally:
            self.is_training = False

    async def retrain_async(self):
        # Run synchronous retrain in a thread
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.retrain)

    def predict_edge(self, features: list) -> float:
        """Predict the expected edge (0.0 - 1.0 probability of win).
        Return 0.5 (netral) kalau model belum siap — tidak memblokir trade."""
        if self.model is None or not self.is_ready:
            return 0.5

        try:
            X = np.array(features).reshape(1, -1)
            probs = self.model.predict_proba(X)
            prob_win = float(probs[0][1])
            return prob_win
        except Exception as e:
            log.debug(f"Predict error: {e}")
            return 0.5

intelligence_model = IntelligenceModel()
