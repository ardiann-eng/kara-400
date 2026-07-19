"""Auditable per-user capital allocation and effective sizing equity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


MIN_ALLOCATION_IDR = 100_000


class CapitalAllocationError(ValueError):
    pass


@dataclass(frozen=True)
class CapitalAllocation:
    idr: int
    usd: float
    fx_rate: float


def parse_allocation_idr(raw: object, *, minimum_idr: int = MIN_ALLOCATION_IDR) -> int:
    text = str(raw).strip().replace(".", "").replace(",", "")
    if not text.isdigit():
        raise CapitalAllocationError("Capital allocation must be a positive integer IDR amount")
    value = int(text)
    if value < minimum_idr:
        raise CapitalAllocationError(f"Capital allocation must be at least Rp{minimum_idr:,}")
    return value


def convert_allocation_idr(idr: int, fx_rate: float) -> CapitalAllocation:
    if not isinstance(idr, int) or idr <= 0:
        raise CapitalAllocationError("Capital allocation IDR must be positive")
    try:
        rate = Decimal(str(fx_rate))
    except (InvalidOperation, ValueError) as exc:
        raise CapitalAllocationError("USD/IDR rate must be positive") from exc
    if rate <= 0:
        raise CapitalAllocationError("USD/IDR rate must be positive")
    usd = (Decimal(idr) / rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return CapitalAllocation(idr=idr, usd=float(usd), fx_rate=float(rate))


def sizing_equity(venue_equity: float, allocation_usd: float | None) -> float:
    if venue_equity < 0:
        raise CapitalAllocationError("Venue equity cannot be negative")
    if allocation_usd is None or allocation_usd <= 0:
        raise CapitalAllocationError("Capital allocation is required")
    return min(float(venue_equity), float(allocation_usd))


def apply_allocation(user, allocation: CapitalAllocation, venue_equity: float, *, has_open_position: bool) -> float:
    if has_open_position:
        raise CapitalAllocationError("Capital allocation cannot change while venue position is open")
    if allocation.usd > venue_equity + 1e-9:
        raise CapitalAllocationError("Capital allocation exceeds venue equity")
    user.capital_allocation_idr = allocation.idr
    user.capital_allocation_usd = allocation.usd
    user.capital_fx_rate = allocation.fx_rate
    user.capital_updated_at = datetime.now(timezone.utc)
    return sizing_equity(venue_equity, allocation.usd)
