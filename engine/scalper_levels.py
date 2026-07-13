"""Authoritative, side-aware scalper level calculation."""

from models.schemas import Side


def build_scalper_levels(
    entry_price: float,
    side: Side,
    sl_pct: float,
    tp1_pct: float,
    tp2_pct: float,
) -> tuple[float, float, float]:
    if side == Side.LONG:
        return (
            round(entry_price * (1 - sl_pct), 8),
            round(entry_price * (1 + tp1_pct), 8),
            round(entry_price * (1 + tp2_pct), 8),
        )
    return (
        round(entry_price * (1 + sl_pct), 8),
        round(entry_price * (1 - tp1_pct), 8),
        round(entry_price * (1 - tp2_pct), 8),
    )
