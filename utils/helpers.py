"""
KARA Bot - Helper Utilities
"""

import uuid
from datetime import datetime, timezone


def gen_id(prefix: str = "ID") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_usd(value: float) -> str:
    return f"${value:,.2f}" if value >= 0 else f"-${abs(value):,.2f}"

def format_idr(value: float) -> str:
    from config import USD_TO_IDR
    val_idr = value * USD_TO_IDR
    return f"Rp{val_idr:,.0f}".replace(",", ".") if val_idr >= 0 else f"-Rp{abs(val_idr):,.0f}".replace(",", ".")


def format_price(value: float) -> str:
    """Format price with more decimals for low-priced assets."""
    if value >= 1000:
        return f"{value:,.2f}"
    elif value >= 1:
        return f"{value:,.4f}"
    else:
        return f"{value:,.6f}"


def format_pct(value: float, show_sign: bool = True) -> str:
    sign = "+" if value >= 0 and show_sign else ""
    return f"{sign}{value*100:.2f}%"


def clamp(value: float, min_val: float, max_val: float) -> float:
    return max(min_val, min(max_val, value))
