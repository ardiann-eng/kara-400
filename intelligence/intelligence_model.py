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
FEATURE_COUNT = 12

class IntelligenceModel:
    def __init__(self):
        self.model = None
        self.is_training = False
        self._training_lock = asyncio.Lock()
        self.last_train_samples = 0
        self.last_retrain_time = 0.0   # unix timestamp of last completed retrain
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
            labeled_count = len(experience_buffer.get_training_data(enriched_only=True))
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
            if getattr(self.model, "n_features_in_", FEATURE_COUNT) != FEATURE_COUNT:
                log.warning("[Intelligence] Stored model uses an old feature contract — retraining required.")
                self.model = None
                return
            self.last_train_samples = labeled_count
            self.is_ready = True
            log.info(f"[Intelligence] Model loaded dan valid ({labeled_count} training samples).")
        except Exception as e:
            log.error(f"Failed to load model: {e}")
            self.model = None

    def get_features(self, row):
        """Convert a row from experience buffer into a feature array"""
        # We must align this perfectly with the predict method.
        # Exit outcome fields are labels only and never feed prediction.
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
                float(row.get('trend_pct', 0)),
                float(row.get('micro_risk_pct', 0)),
                {"invalid": 0.0, "weak": 1.0, "valid": 2.0, "excellent": 3.0}.get(
                    str(row.get('entry_location_quality', 'unknown')), -1.0
                ),
                1.0 if row.get('trade_mode') == 'scalper' else 0.0,
            ]
        except Exception:
            return [0.0] * 12

    def retrain(self):
        if self.is_training:
            return

        # Cek interval minimum antar retrain
        interval_hours = getattr(config, 'INTELLIGENCE_RETRAIN_INTERVAL_HOURS', 12)
        if self.last_retrain_time and (time.time() - self.last_retrain_time) < interval_hours * 3600:
            remaining = interval_hours * 3600 - (time.time() - self.last_retrain_time)
            log.debug(f"🧠 Retrain skipped — interval belum lewat ({remaining/3600:.1f}h tersisa).")
            return

        data = experience_buffer.get_training_data(enriched_only=True)
        min_samples = getattr(config, 'INTELLIGENCE_RETRAIN_MIN_SAMPLES', 300)
        min_samples = max(min_samples, getattr(config, 'INTELLIGENCE_RETRAIN_MIN_ENRICHED_SAMPLES', 300))
        if len(data) < min_samples:
            log.debug(f"🧠 Not enough data to train. Have {len(data)}, need {min_samples}.")
            return

        if len(data) <= self.last_train_samples + 20 and self.model is not None:
            return

        self.is_training = True   # set sebelum log agar thread ke-2 langsung block
        log.info(f"🧠 Retraining Intelligence model with {len(data)} samples...")

        try:
            X = []
            y = []
            for row in data:
                features = self.get_features(row)
                X.append(features)
                # Predict fast follow-through, not merely a green final close.
                # This aligns model target with 1m scalper entry and 12m hold logic.
                y.append(int(row['impulse_win']))
                
            X = np.array(X)
            y = np.array(y)
            
            # Guard: kedua kelas harus ada
            if sum(y) == 0 or sum(y) == len(y):
                log.warning("🧠 Cannot train model: Only one class present (all wins or all losses).")
                self.is_training = False
                return

            # Guard: data harus cukup berimbang — min 10% dari kelas minoritas
            win_rate = sum(y) / len(y)
            if win_rate > 0.90 or win_rate < 0.10:
                log.warning(
                    f"🧠 Skipping retrain: data terlalu imbalanced "
                    f"(win_rate={win_rate*100:.1f}%, butuh 10%-90%). "
                    f"Model lama tetap dipakai."
                )
                self.is_training = False
                return

            new_model = HistGradientBoostingClassifier(
                max_iter=100,
                learning_rate=0.05,
                early_stopping=True,
                validation_fraction=0.1,
                random_state=42,
                class_weight="balanced"
            )

            # Wajib train/test split — tolak model kalau akurasi mencurigakan
            # Pastikan kelas minoritas punya cukup sampel di test set (min 5)
            minority_count = min(sum(y), len(y) - sum(y))
            test_size = max(0.2, 10 / len(X)) if minority_count >= 10 else 0.1
            X_train, X_test, y_train, y_test = train_test_split(
                X, y, test_size=test_size, random_state=42, stratify=y
            )
            new_model.fit(X_train, y_train)
            test_acc = new_model.score(X_test, y_test)

            # Guard: akurasi > 90% berarti overfit atau data bocor — jangan pakai
            if test_acc > 0.90:
                log.warning(
                    f"🧠 Skipping retrain: akurasi terlalu tinggi ({test_acc*100:.1f}%) "
                    f"— kemungkinan overfit atau data tidak valid. Model lama tetap dipakai."
                )
                self.is_training = False
                return

            log.info(
                f"🧠 Intelligence updated: {len(data)} samples | win_rate={win_rate*100:.1f}% | "
                f"out-of-sample accuracy: {test_acc*100:.1f}% (n_test={len(X_test)})"
            )

            self.model = new_model
            self.last_train_samples = len(data)
            self.last_retrain_time = time.time()
            self.is_ready = True
            joblib.dump(self.model, MODEL_PATH)

        except Exception as e:
            log.error(f"Failed to retrain ML model: {e}")
        finally:
            self.is_training = False

    async def retrain_async(self):
        # Lock mencegah dua coroutine masuk bersamaan (race condition pada is_training)
        if self._training_lock.locked():
            return
        async with self._training_lock:
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
