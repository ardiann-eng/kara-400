import pytest

from execution.profit_lock import paper_profit_lock_fill
from models.schemas import Side


def test_paper_profit_lock_uses_trigger_not_late_poll_price():
    observed_poll_price = 99.96
    fill = paper_profit_lock_fill(
        trigger_price=100.05,
        position_side=Side.LONG,
        fill_simulator=lambda price, side: price - 0.03,
    )

    assert fill == pytest.approx(100.02)
    assert fill > 100.0
    assert observed_poll_price < 100.0


def test_short_profit_lock_uses_buy_fill_at_trigger():
    fill = paper_profit_lock_fill(
        trigger_price=99.95,
        position_side=Side.SHORT,
        fill_simulator=lambda price, side: price + 0.03,
    )

    assert fill == pytest.approx(99.98)
    assert fill < 100.0
