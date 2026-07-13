"""Common executor contract used by paper and live venues."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from models.schemas import AccountState, Position, TradeSignal


class BaseExecutor(ABC):
    """Stable interface consumed by the trading loop and Telegram handlers."""

    chat_id: str

    @abstractmethod
    async def get_account_state(self) -> AccountState:
        pass

    @property
    @abstractmethod
    def open_positions(self) -> List[Position]:
        pass

    @abstractmethod
    async def open_position(self, signal: TradeSignal) -> Optional[Position]:
        pass

    @abstractmethod
    async def update_positions(
        self,
        prices: Dict[str, float],
        market_states: Optional[Dict[str, Dict]] = None,
    ) -> List[Dict]:
        pass

    @abstractmethod
    async def close_position(
        self,
        position_id: str,
        current_price: float,
        reason: str = "manual",
        close_ratio: float = 1.0,
    ) -> Optional[Dict]:
        pass

    async def close_all_positions(self, prices: Dict[str, float]) -> List[Dict]:
        results = []
        for position in list(self.open_positions):
            price = prices.get(position.asset, position.entry_price)
            result = await self.close_position(
                position.position_id,
                price,
                reason="close_all",
            )
            if result:
                results.append(result)
        return results

    async def reconcile(self) -> None:
        """Synchronize local state with venue state. Paper executors need no work."""
