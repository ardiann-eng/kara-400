"""
KARA Bot - Base Executor (Abstract Interface)

Mendefinisikan kontrak yang harus dipenuhi semua executor:
PaperExecutor, LiveExecutor (Hyperliquid), BitgetExecutor.

RiskManager dan main.py loop hanya bicara ke interface ini, sehingga
penambahan exchange baru di masa depan tidak butuh modifikasi cross-module.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from models.schemas import AccountState, Position, TradeSignal


class BaseExecutor(ABC):
    """Abstract interface untuk semua executor (paper/live HL/live Bitget)."""

    # Setiap executor wajib expose chat_id (untuk DB/logging) dan mode.
    chat_id: str
    mode: object  # BotMode enum — concrete class set this

    # ── ACCOUNT STATE ────────────────────────────────────────────────
    @abstractmethod
    async def get_account_state(self) -> AccountState:
        """Return current account state (equity, margin, positions, etc)."""

    @property
    @abstractmethod
    def open_positions(self) -> List[Position]:
        """List of currently open positions (status=OPEN)."""

    # ── TRADE LIFECYCLE ──────────────────────────────────────────────
    @abstractmethod
    async def open_position(self, signal: TradeSignal) -> Optional[Position]:
        """Open a new position from a TradeSignal. Returns Position or None if blocked."""

    @abstractmethod
    async def update_positions(self, prices: Dict[str, float]) -> List[Dict]:
        """
        Per-tick monitor: update unrealized PnL and check TP/SL/trailing triggers.

        `prices` is asset → current_mark_price mapping. For Bitget executor the
        caller is responsible for supplying Bitget prices (not HL prices) so
        that SL/TP triggers are evaluated against the venue we actually trade on.

        Returns a list of action dicts (e.g. tp1, tp2, stop_loss, trailing_stop)
        produced by RiskManager.check_tp_trail.
        """

    @abstractmethod
    async def close_position(
        self,
        position_id: str,
        current_price: float,
        reason: str = "manual",
    ) -> Optional[Dict]:
        """Fully close a position. Returns {position_id, pnl, reason} or None."""

    async def close_all_positions(self, prices: Dict[str, float]) -> List[Dict]:
        """Close all open positions. Default impl walks open_positions."""
        results = []
        for pos in list(self.open_positions):
            price = prices.get(pos.asset, pos.entry_price)
            res = await self.close_position(pos.position_id, price, reason="close_all")
            if res:
                results.append(res)
        return results

    # ── OPTIONAL HOOKS ──────────────────────────────────────────────
    async def sync_positions_from_chain(self) -> None:
        """Optional: sync open positions from exchange on startup.

        Default: no-op (paper mode has nothing to sync).
        LiveExecutor and BitgetExecutor override this.
        """
        return None
