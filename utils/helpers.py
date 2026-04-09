"""
KARA Bot - Helper Utilities
"""

import uuid
from datetime import datetime, timezone
from typing import Any


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
    """Smart price formatting for human readability.
    >= 1000: 0 decimals
    100-999: 2 decimals
    10-99: 3 decimals
    1-10: 4 decimals
    < 1: 5-6 decimals (stripped)
    """
    if value >= 1000:
        return f"{value:,.0f}"
    elif value >= 100:
        return f"{value:,.2f}"
    elif value >= 10:
        return f"{value:,.3f}"
    elif value >= 1:
        return f"{value:,.4f}"
    else:
        # Show 6 decimals but strip trailing zeros
        return f"{value:,.6f}".rstrip('0').rstrip('.')


def format_pct(value: float, show_sign: bool = True) -> str:
    sign = "+" if value >= 0 and show_sign else ""
    return f"{sign}{value*100:.2f}%"


def clamp(value: float, min_val: float, max_val: float) -> float:
    return max(min_val, min(max_val, value))


def safe_float(value: Any, default: float = 0.0) -> float:
    """Safely convert any value to float, handling strings, None, and garbage."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default
