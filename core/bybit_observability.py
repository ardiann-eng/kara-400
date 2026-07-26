"""Secret-free per-user Bybit telemetry and rate-limited alerts."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import logging
import time
from typing import Awaitable, Callable, Dict, Optional


log = logging.getLogger("kara.bybit_observability")


@dataclass
class BybitTelemetry:
    environment: str = "BYBIT TESTNET"
    rest_healthy: bool = False
    rest_last_success_at: float = 0.0
    rest_last_error_at: float = 0.0
    rest_error_count: int = 0
    rest_latency_ms: float = 0.0
    ws_connected: bool = False
    ws_stale: bool = True
    ws_last_message_at: float = 0.0
    ws_disconnected_at: float = 0.0
    ws_reconnect_count: int = 0
    last_reconciliation_at: float = 0.0
    reconciliation_mismatch_count: int = 0
    hard_sl_healthy_count: int = 0
    hard_sl_missing_count: int = 0
    hard_sl_by_symbol: Dict[str, bool] = field(default_factory=dict)
    entry_latency_ms: float = 0.0
    fill_latency_ms: float = 0.0
    close_latency_ms: float = 0.0
    price_bridge_gap_pct: float = 0.0
    estimated_slippage_pct: float = 0.0
    actual_slippage_pct: float = 0.0
    last_fill_fee: float = 0.0
    circuit_open: bool = False
    circuit_remaining_s: float = 0.0
    unknown_recovered_positions: int = 0
    emergency_close_attempts: int = 0
    emergency_close_successes: int = 0
    emergency_close_failures: int = 0
    risk_rejection_count: int = 0
    last_risk_rejection_reason: str = ""
    venue_equity: float = 0.0
    sizing_equity: float = 0.0

    def snapshot(self) -> Dict[str, object]:
        data = asdict(self)
        data["ws_stale_duration_s"] = (
            max(0.0, time.time() - self.ws_disconnected_at)
            if self.ws_stale and self.ws_disconnected_at
            else 0.0
        )
        return data

    def record_rest_success(self, started_at: float) -> None:
        self.rest_healthy = True
        self.rest_last_success_at = time.time()
        self.rest_latency_ms = max(0.0, (time.monotonic() - started_at) * 1000)

    def record_rest_error(self, started_at: float) -> None:
        self.rest_healthy = False
        self.rest_last_error_at = time.time()
        self.rest_error_count += 1
        self.rest_latency_ms = max(0.0, (time.monotonic() - started_at) * 1000)


class BybitAlertManager:
    def __init__(
        self,
        sink: Optional[Callable[[str], Awaitable[None]]] = None,
        *,
        cooldown_s: float = 300.0,
    ):
        self.sink = sink
        self.cooldown_s = cooldown_s
        self._last_sent: Dict[str, float] = {}
        self._suppressed: Dict[str, int] = {}

    @staticmethod
    def _level(message: str) -> int:
        if message.startswith("CRITICAL"):
            return logging.CRITICAL
        if message.startswith("WARNING"):
            return logging.WARNING
        return logging.INFO

    async def emit(self, key: str, message: str) -> bool:
        # Telegram is throttled, may be unconfigured, and is not retained for audit.
        # These alerts are the only record that venue protection failed, so log every
        # occurrence before delivery is even attempted. Without this the deployment
        # log cannot answer when a position lost its hard SL.
        log.log(self._level(message), "BYBIT_ALERT key=%s %s", key, message)
        if not self.sink:
            return False
        now = time.monotonic()
        if now - self._last_sent.get(key, -self.cooldown_s) < self.cooldown_s:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            log.info(
                "BYBIT_ALERT_SUPPRESSED key=%s repeat=%s cooldown_s=%s",
                key,
                self._suppressed[key],
                self.cooldown_s,
            )
            return False
        try:
            await self.sink(message)
        except Exception:
            log.exception("Bybit alert delivery failed for key=%s", key)
            return False
        self._last_sent[key] = now
        swallowed = self._suppressed.pop(key, 0)
        if swallowed:
            log.info(
                "BYBIT_ALERT_DELIVERED key=%s after_suppressed=%s", key, swallowed
            )
        return True

    def schedule(self, key: str, message: str) -> None:
        if not self.sink:
            return
        try:
            asyncio.get_running_loop().create_task(self.emit(key, message))
        except RuntimeError:
            log.warning("Bybit alert dropped without running event loop: %s", key)
