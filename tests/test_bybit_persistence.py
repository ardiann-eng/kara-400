from core.db import UserDB
from execution.exchange_client import LivePositionStatus
from models.schemas import Position, Side


def test_bybit_position_round_trip_uses_separate_table(tmp_path):
    db = UserDB(
        file_path=str(tmp_path / "users.json"),
        db_path=str(tmp_path / "kara.db"),
    )
    position = Position(
        position_id="BYBIT-POS-1",
        asset="BTC",
        side=Side.LONG,
        entry_price=100,
        size_initial=0.1,
        size_current=0.1,
        leverage=5,
        margin_usd=2,
        stop_loss=99,
        tp1=101,
        tp2=102,
        is_paper=False,
    )

    db.save_bybit_position(
        "chat",
        position,
        "BTCUSDT",
        LivePositionStatus.OPEN_PROTECTED.value,
        "entry-link-id",
    )
    rows = db.load_bybit_positions("chat")

    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["entry_order_link_id"] == "entry-link-id"
    assert rows[0]["position"].position_id == position.position_id

    db.remove_bybit_position(position.position_id)
    assert db.load_bybit_positions("chat") == []
