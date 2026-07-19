from pathlib import Path

from core.db import UserDB


def test_option_b_reset_deletes_all_user_and_persistent_data(tmp_path, monkeypatch):
    import core.db as db_module

    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setattr(db_module.config, "STORAGE_DIR", str(storage))
    db = UserDB(
        file_path=str(storage / "users.json"),
        db_path=str(storage / "kara_data.db"),
    )
    db.create_user("1", "operator", 100)
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO trade_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("trade-1", "1", "BTC", "long", 1, 0.01, "{}", 1),
    )
    conn.execute(
        "INSERT INTO execution_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("candidate-1", "1", "BTC", "long", "rejected", "test", "demo", "{}", 1),
    )
    conn.commit()
    (storage / "kara_ml.db").write_bytes(b"ml")
    (storage / "kara_intelligence.pkl").write_bytes(b"model")
    (storage / "trade_history.xlsx").write_bytes(b"xlsx")

    summary = db.hard_reset_all_data()

    assert summary["status"] == "ok"
    assert summary["users_deleted"] == 1
    assert summary["trade_history"] == 1
    assert summary["execution_candidates"] == 1
    assert db.get_all_users() == []
    assert conn.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM execution_candidates").fetchone()[0] == 0
    assert not (storage / "kara_ml.db").exists()
    assert not (storage / "kara_intelligence.pkl").exists()
    assert not (storage / "trade_history.xlsx").exists()
    assert (storage / "users.json").read_text(encoding="utf-8") == "{}"


def test_total_reset_startup_requires_exact_confirmation_and_marker():
    source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")

    assert "TOTAL_RESET_CONFIRMATION != config.TOTAL_RESET_ACK_VALUE" in source
    assert "TOTAL_RESET_MARKER_PATH" in source
    assert "run_total_reset_if_confirmed()" in source
