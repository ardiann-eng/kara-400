import json

from core.db import UserDB
from models.schemas import MarketRegime, ScoreBreakdown, Side, SignalStrength, TradeSignal


def test_rejected_demo_candidate_persists_reason_and_cohort(tmp_path):
    db = UserDB(file_path=str(tmp_path / "users.json"), db_path=str(tmp_path / "kara.db"))
    signal = TradeSignal(
        signal_id="candidate-1", asset="UNKNOWN", side=Side.LONG, score=70,
        strength=SignalStrength.MODERATE, regime=MarketRegime.NORMAL,
        breakdown=ScoreBreakdown(), entry_price=100, stop_loss=99, tp1=101,
        tp2=102, suggested_leverage=1,
    )
    db.save_execution_candidate(
        "1", signal, status="rejected",
        reason="inactive_or_unsupported_bybit_metadata", execution_environment="demo",
        extra={"capital_allocation_usd": 62.5},
    )
    row = db._get_conn().execute(
        "SELECT chat_id, status, reason, execution_environment, data FROM execution_candidates"
    ).fetchone()
    assert row[:4] == ("1", "rejected", "inactive_or_unsupported_bybit_metadata", "demo")
    data = json.loads(row[4])
    assert data["capital_allocation_usd"] == 62.5
    assert data["asset"] == "UNKNOWN"
