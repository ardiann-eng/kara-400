import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.database_entry_audit import (
    MODEL_FEATURES,
    analyze,
    build_purged_walk_forward_folds,
    connect_readonly,
    exact_trade_ml_join,
    infer_unique_signals,
    side_signed_move,
    validate_feature_whitelist,
)


class DatabaseEntryAuditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.data_path = root / "data.db"
        self.ml_path = root / "ml.db"
        self._create_data_fixture()
        self._create_ml_fixture()

    def tearDown(self):
        self.temp.cleanup()

    def _create_data_fixture(self):
        conn = sqlite3.connect(self.data_path)
        conn.executescript(
            """
            CREATE TABLE trade_history (
                trade_id TEXT PRIMARY KEY, chat_id TEXT, asset TEXT, side TEXT,
                pnl_usd REAL, pnl_pct REAL, data TEXT, created_at REAL
            );
            CREATE TABLE signals_history (
                sig_id TEXT PRIMARY KEY, asset TEXT, side TEXT, score INTEGER,
                price REAL, data TEXT, created_at REAL
            );
            CREATE TABLE weak_confirmation_events (
                event_id TEXT PRIMARY KEY, asset TEXT, side TEXT, status TEXT,
                signal_price REAL, observed_price REAL, score INTEGER,
                armed_at REAL, decided_at REAL
            );
            CREATE TABLE weak_confirmation_outcomes (
                event_id TEXT PRIMARY KEY, asset TEXT, side TEXT, signal_price REAL,
                observed_price REAL, mfe_pct REAL, mae_pct REAL,
                final_return_pct REAL, tp1_hit INTEGER, tp2_hit INTEGER,
                sl_hit INTEGER, completed_at REAL
            );
            """
        )
        trade_payload = json.dumps({"entry_price": 100, "exit_price": 101, "reason": "time_exit"})
        conn.execute(
            "INSERT INTO trade_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("T1", "PRIVATE", "BTC", "LONG", 2.0, 0.02, trade_payload, 200.0),
        )
        signal_payload = json.dumps({"regime": "low_vol", "entry_location_quality": "weak"})
        conn.execute(
            "INSERT INTO signals_history VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("S1", "BTC", "LONG", 64, 100.0, signal_payload, 95.0),
        )
        conn.execute(
            "INSERT INTO weak_confirmation_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("E1", "BTC", "LONG", "confirmed", 100, 100.2, 64, 300, 360),
        )
        conn.execute(
            "INSERT INTO weak_confirmation_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("E2", "ETH", "SHORT", "rejected_structure", 100, 100.1, 63, 400, 460),
        )
        conn.execute(
            "INSERT INTO weak_confirmation_outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("E1", "BTC", "LONG", 100, 100.2, 0.004, -0.002, 0.001, 1, 0, 0, 1400),
        )
        conn.commit()
        conn.close()

    def _create_ml_fixture(self):
        conn = sqlite3.connect(self.ml_path)
        conn.execute(
            """
            CREATE TABLE ml_experience (
                pos_id TEXT PRIMARY KEY, chat_id TEXT, timestamp REAL, asset TEXT,
                side TEXT, score INTEGER, meta_delta INTEGER, oi_score INTEGER,
                liq_score INTEGER, ob_score INTEGER, session_bonus INTEGER,
                funding_rate REAL, realized_vol REAL, trend_pct REAL,
                expected_edge REAL, actual_pnl_pct REAL, duration_sec REAL,
                is_win INTEGER, trade_mode TEXT, entry_location_quality TEXT,
                micro_risk_pct REAL, exit_reason TEXT, mfe_pct REAL,
                time_exit_trigger TEXT, impulse_win INTEGER
            )
            """
        )
        conn.execute(
            "INSERT INTO ml_experience VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "T1", "PRIVATE", 100.0, "BTC", "LONG", 64, -2, 1, 2, 3, 0,
                0.0, 0.2, 0.01, 0.5, 0.02, 100, 1, "scalper", "weak",
                0.008, "time_exit", 0.004, "no_follow_through", 1,
            ),
        )
        conn.commit()
        conn.close()

    def test_readonly_missing_path_does_not_create_file(self):
        missing = Path(self.temp.name) / "missing.db"
        with self.assertRaises(sqlite3.OperationalError):
            connect_readonly(str(missing))
        self.assertFalse(missing.exists())

    def test_exact_join_checks_durable_key_and_identity(self):
        trades = [{"_trade_key": "T", "_owner_key": "P", "asset": "BTC", "side": "long"}]
        ml = [{"pos_id": "T", "chat_id": "P", "asset": "BTC", "side": "LONG", "timestamp": 1}]
        joined, counts = exact_trade_ml_join(trades, ml)
        self.assertEqual(counts["joined"], 1)
        self.assertNotIn("ml_chat_id", joined[0])
        mismatch, counts = exact_trade_ml_join(trades, [{**ml[0], "asset": "ETH"}])
        self.assertEqual(mismatch, [])
        self.assertEqual(counts["identity_mismatch"], 1)

    def test_side_signed_move_long_and_short(self):
        self.assertAlmostEqual(side_signed_move("LONG", 100, 101), 0.01)
        self.assertAlmostEqual(side_signed_move("SHORT", 100, 99), 0.01)
        self.assertAlmostEqual(side_signed_move("SHORT", 100, 101), -0.01)

    def test_primary_label_uses_observed_mfe_not_final_pnl(self):
        report = analyze(str(self.data_path), str(self.ml_path))
        primary = report["enriched_mfe_cohort"]["overall"]["primary_observed_mfe_ge_0_35pct"]
        self.assertEqual(primary["n"], 1)
        self.assertEqual(primary["rate"], 1.0)
        self.assertEqual(report["enriched_mfe_cohort"]["overall"]["secondary_pnl_usd"]["mean"], 2.0)

    def test_weak_telemetry_is_candidate_level_and_keeps_missing_outcome(self):
        report = analyze(str(self.data_path), str(self.ml_path))["weak_confirmation"]
        self.assertEqual(report["candidate_n"], 2)
        self.assertEqual(report["outcome_join_n"], 1)
        self.assertEqual(report["by_status"]["rejected_structure"]["candidate_n"], 1)
        self.assertEqual(report["by_status"]["rejected_structure"]["outcome_n"], 0)

    def test_signal_attribution_requires_unique_match_both_directions(self):
        trade = {"asset": "BTC", "side": "long", "ml_score": 64, "entry_at": 100}
        signal = {
            "asset": "BTC", "side": "long", "score": 64, "created_at": 90,
            "regime": "low_vol", "reasons": ["Momentum bull", "Volume surge"],
        }
        inferred, counts = infer_unique_signals([trade], [signal])
        self.assertEqual(len(inferred), 1)
        self.assertEqual(inferred[0]["inferred_reason_flags"], ["momentum", "volume"])
        inferred, counts = infer_unique_signals([trade], [signal, dict(signal)])
        self.assertEqual(inferred, [])
        self.assertEqual(counts["ambiguous_or_unmatched"], 1)
        inferred, _ = infer_unique_signals([trade, dict(trade)], [signal])
        self.assertEqual(inferred, [])

    def test_purge_has_no_interval_overlap_and_feature_whitelist_blocks_leakage(self):
        rows = [
            {"entry_at": float(i * 100), "created_at": float(i * 100 + 80)}
            for i in range(10)
        ]
        folds = build_purged_walk_forward_folds(rows, folds=2, purge_seconds=30)
        self.assertTrue(folds)
        for train, test in folds:
            test_start = min(rows[index]["entry_at"] for index in test)
            self.assertTrue(all(rows[index]["created_at"] < test_start - 30 for index in train))
        validate_feature_whitelist(MODEL_FEATURES)
        with self.assertRaises(ValueError):
            validate_feature_whitelist((*MODEL_FEATURES, "mfe_pct"))

    def test_output_omits_raw_private_identifiers(self):
        encoded = json.dumps(analyze(str(self.data_path), str(self.ml_path)), sort_keys=True)
        self.assertNotIn("PRIVATE", encoded)
        self.assertNotIn('"T1"', encoded)
        self.assertNotIn('"E1"', encoded)


if __name__ == "__main__":
    unittest.main()
