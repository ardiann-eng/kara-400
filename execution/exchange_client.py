"""Venue-neutral contract for real-money execution clients."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from models.schemas import Side


class ExecutionOrderStatus(str, Enum):
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class LivePositionStatus(str, Enum):
    PENDING_ENTRY = "pending_entry"
    PARTIALLY_FILLED = "partially_filled"
    OPEN_UNPROTECTED = "open_unprotected"
    OPEN_PROTECTED = "open_protected"
    PENDING_CLOSE = "pending_close"
    CLOSED = "closed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True)
class InstrumentSpec:
    asset: str
    symbol: str
    tick_size: float
    qty_step: float
    min_qty: float
    min_notional: float
    max_leverage: int


@dataclass(frozen=True)
class VenueAccount:
    total_equity: float
    wallet_balance: float
    available_balance: float
    used_margin: float
    unrealized_pnl: float


@dataclass(frozen=True)
class VenueOrder:
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    requested_qty: float
    filled_qty: float
    average_fill_price: float
    fee_paid: float
    status: ExecutionOrderStatus
    reduce_only: bool = False


@dataclass(frozen=True)
class VenuePosition:
    symbol: str
    side: Side
    size: float
    entry_price: float
    leverage: int
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    unrealized_pnl: float = 0.0


class ExecutionClient(ABC):
    """Minimum API required from Bybit or another execution venue."""

    @abstractmethod
    async def connect(self) -> None:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass

    @abstractmethod
    async def get_account(self) -> VenueAccount:
        pass

    @abstractmethod
    async def get_instrument(self, asset: str) -> InstrumentSpec:
        pass

    @abstractmethod
    async def get_mark_price(self, symbol: str) -> float:
        pass

    @abstractmethod
    async def get_positions(self, symbol: Optional[str] = None) -> List[VenuePosition]:
        pass

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        pass

    @abstractmethod
    async def place_order(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: float,
        client_order_id: str,
        reduce_only: bool = False,
    ) -> VenueOrder:
        pass

    @abstractmethod
    async def get_order(self, symbol: str, client_order_id: str) -> VenueOrder:
        pass

    @abstractmethod
    async def cancel_order(self, symbol: str, client_order_id: str) -> None:
        pass

    @abstractmethod
    async def set_protection(
        self,
        *,
        symbol: str,
        side: Side,
        stop_loss: float,
        take_profit: Optional[float] = None,
    ) -> None:
        pass
