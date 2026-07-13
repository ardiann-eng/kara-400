from datetime import datetime, timedelta, timezone

from models.schemas import Position, PositionStatus, Side
from risk.risk_manager import RiskManager


def make_scalper_position(age_minutes: float, trailing_high: float = 100.0) -> Position:
    return Position(
        position_id="TEST",
        asset="BTC",
        side=Side.LONG,
        entry_price=100.0,
        size_initial=1.0,
        size_current=1.0,
        leverage=25,
        margin_usd=4.0,
        stop_loss=99.2,
        tp1=100.45,
        tp2=100.75,
        trailing_high=trailing_high,
        trade_mode="scalper",
        opened_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        status=PositionStatus.OPEN,
    )


def test_scalper_retest_grace_requires_market_state():
    risk = RiskManager()
    pos = make_scalper_position(13, trailing_high=100.36)

    action = risk.check_tp_trail(
        pos,
        99.9,
        {
            "structure_valid": True,
            "trend_aligned": True,
            "momentum_opposes": False,
        },
    )

    assert action is None


def test_scalper_exits_no_follow_through_without_valid_state():
    risk = RiskManager()
    pos = make_scalper_position(13, trailing_high=100.36)

    action = risk.check_tp_trail(
        pos,
        99.9,
        {
            "structure_valid": False,
            "trend_aligned": True,
            "momentum_opposes": True,
        },
    )

    assert action["action"] == "time_exit"
    assert "no follow-through" in action["message"]


def test_scalper_cuts_invalid_structure_before_wider_stop():
    risk = RiskManager()
    pos = make_scalper_position(10.5)

    action = risk.check_tp_trail(
        pos,
        99.6,
        {
            "structure_valid": False,
            "trend_aligned": True,
            "momentum_opposes": True,
        },
    )

    assert action["action"] == "time_exit"
    assert "microstructure invalid" in action["message"]


def test_scalper_pre_tp1_impulse_does_not_move_stop():
    risk = RiskManager()
    pos = make_scalper_position(5)
    original_stop = pos.stop_loss

    action = risk.check_tp_trail(pos, 100.40)

    assert action is None
    assert pos.early_profit_lock is False
    assert pos.stop_loss == original_stop
    assert pos.tp1_hit is False


def test_scalper_tp1_takes_partial_without_pre_tp1_lock():
    risk = RiskManager()
    pos = make_scalper_position(5)
    risk.check_tp_trail(pos, 100.40)

    action = risk.check_tp_trail(pos, 100.46)

    assert action["action"] == "tp1"


def test_post_tp1_breakeven_stop_is_profit_lock_exit():
    risk = RiskManager()
    pos = make_scalper_position(6)
    pos.tp1_hit = True
    pos.stop_loss = pos.entry_price

    action = risk.check_tp_trail(pos, 99.99)

    assert action["action"] == "profit_lock_stop"
    assert action["trigger_price"] == pos.entry_price
