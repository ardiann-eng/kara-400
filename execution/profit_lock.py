"""Profit-lock execution helpers shared by paper tests and executor."""

from models.schemas import Side


def paper_profit_lock_fill(
    trigger_price: float,
    position_side: Side,
    fill_simulator,
) -> float:
    """Model stop execution at trigger, not a later polling observation."""
    close_side = Side.SHORT if position_side == Side.LONG else Side.LONG
    return float(fill_simulator(trigger_price, close_side))
