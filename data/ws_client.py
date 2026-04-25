"""
KARA Bot - Hyperliquid WebSocket Client
Native WS for: orderbook, trades, funding, liquidations, user events.
Features: exponential backoff reconnect, health check, pub/sub callbacks.
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed

import config

log = logging.getLogger("kara.ws_client")

# ──────────────────────────────────────────────
# WS ENDPOINT
# ──────────────────────────────────────────────
WS_URL_MAINNET = "wss://api.hyperliquid.xyz/ws"
WS_URL_TESTNET = "wss://api.hyperliquid-testnet.xyz/ws"


class KaraWebSocketClient:
    """
    Long-lived WebSocket connection to Hyperliquid.
    Dispatches messages to registered callbacks by channel name.
    Reconnects automatically with exponential backoff.
    """

    def __init__(self):
        self.url = (
            "wss://api.hyperliquid.xyz/ws"
            if config.DATA_SOURCE == "mainnet"
            else "wss://api.hyperliquid-testnet.xyz/ws"
        )
        self._ws: Optional[Any] = None
        self._callbacks: Dict[str, List[Callable]] = defaultdict(list)
        self._subscriptions: List[Dict] = []   # remembered for re-subscribe
        self._running = False
        self._connected = False
        self._last_message_ts: float = 0
        self._reconnect_count = 0
        self._task: Optional[asyncio.Task] = None

    # ──────────────────────────────────────────
    # PUBLIC API
    # ──────────────────────────────────────────

    def on(self, channel: str, callback: Callable):
        """Register a callback for a specific channel/subscription type."""
        self._callbacks[channel].append(callback)

    def off(self, channel: str, callback: Callable):
        if callback in self._callbacks[channel]:
            self._callbacks[channel].remove(callback)

    async def subscribe_orderbook(self, coin: str):
        await self._subscribe({"type": "l2Book", "coin": coin})

    async def subscribe_trades(self, coin: str):
        await self._subscribe({"type": "trades", "coin": coin})

    async def subscribe_funding(self, coin: str):
        await self._subscribe({"type": "activeAssetCtx", "coin": coin})

    async def subscribe_liquidations(self):
        """Subscribe to all liquidation events on the network."""
        await self._subscribe({"type": "liquidations"})

    async def subscribe_user_events(self, wallet: str):
        """Subscribe to fills, orders, funding for a wallet."""
        await self._subscribe({"type": "userEvents", "user": wallet})

    async def start(self):
        """Start the WS listener loop (runs until stop() is called)."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        log.info(f" WS client starting -> {self.url}")

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()
        log.info("WS client stopped")

    @property
    def is_healthy(self) -> bool:
        age = time.monotonic() - self._last_message_ts
        return self._connected and age < 60   # no message in 60s = unhealthy

    # ──────────────────────────────────────────
    # INTERNAL
    # ──────────────────────────────────────────

    async def _subscribe(self, sub: Dict):
        """Send subscribe message; remember for reconnect."""
        if sub not in self._subscriptions:
            self._subscriptions.append(sub)
        if self._ws and self._connected:
            await self._send({"method": "subscribe", "subscription": sub})

    async def _send(self, payload: Dict):
        try:
            await self._ws.send(json.dumps(payload))
        except Exception as e:
            log.warning(f"WS send failed: {e}")

    async def _run_loop(self):
        """Main reconnect loop. Never permanently gives up."""
        cfg = config.EXEC
        consecutive_failures = 0
        while self._running:
            try:
                await self._connect_and_listen()
                consecutive_failures = 0  # reset on any successful connection
            except Exception as e:
                self._connected = False
                if not self._running:
                    break
                consecutive_failures += 1

                delay = min(
                    cfg.ws_reconnect_base_delay_s * (2 ** min(consecutive_failures, 6)),
                    120.0   # cap at 2 minutes
                )
                log.warning(
                    f"🔄 WS disconnected (failure #{consecutive_failures}). "
                    f"Retrying in {delay:.1f}s... ({e})"
                )
                await asyncio.sleep(delay)
                # NEVER give up — keep retrying as long as bot is running

    async def _connect_and_listen(self):
        """Single WS session: connect -> subscribe -> listen."""
        async with websockets.connect(
            self.url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=10,
        ) as ws:
            self._ws = ws
            self._connected = True
            self._last_message_ts = time.monotonic()
            log.info(f" WS connected to {self.url}")

            # Re-subscribe to all remembered subscriptions
            for sub in self._subscriptions:
                await self._send({"method": "subscribe", "subscription": sub})

            # Start health-check task
            health_task = asyncio.create_task(self._health_check())

            try:
                async for raw in ws:
                    self._last_message_ts = time.monotonic()
                    try:
                        msg = json.loads(raw)
                        await self._dispatch(msg)
                    except json.JSONDecodeError:
                        pass
                    except Exception as e:
                        log.debug(f"WS dispatch error: {e}")
            finally:
                health_task.cancel()
                self._connected = False

    async def _health_check(self):
        """Periodically check if WS is alive; send ping if stale."""
        interval = config.EXEC.ws_health_check_interval_s
        while True:
            await asyncio.sleep(interval)
            age = time.monotonic() - self._last_message_ts
            if age > interval * 1.5:
                log.warning(f"  WS idle for {age:.0f}s - sending ping")
                if self._ws:
                    try:
                        await self._ws.ping()
                    except Exception:
                        break   # will trigger reconnect

    async def _dispatch(self, msg: Dict):
        """Route message to registered callbacks."""
        # Hyperliquid WS message structure:
        # {"channel": "...", "data": {...}}  OR  {"channel": "pong"}
        channel = msg.get("channel", "")
        if channel == "pong":
            return

        data = msg.get("data", msg)

        # Normalize channel name for callback lookup
        # e.g. "l2Book" -> "orderbook", "activeAssetCtx" -> "funding"
        channel_map = {
            "l2Book":         "orderbook",
            "trades":         "trades",
            "activeAssetCtx": "funding",
            "liquidations":   "liquidations",
            "userEvents":     "user_events",
        }
        mapped = channel_map.get(channel, channel)

        callbacks = self._callbacks.get(mapped, []) + self._callbacks.get("*", [])
        for cb in callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(data)
                else:
                    cb(data)
            except Exception as e:
                log.debug(f"WS callback error ({mapped}): {e}")


# ──────────────────────────────────────────────
# DATA CACHE (maintained from WS events)
# ──────────────────────────────────────────────

class MarketDataCache:
    """
    In-memory cache of latest WS data per asset.
    Injected into analyzers so they always have fresh data.
    """

    def __init__(self):
        self.funding: Dict[str, Dict]     = {}    # asset -> latest funding ctx
        self.orderbook: Dict[str, Dict]   = {}    # asset -> latest L2 snap
        self.trades: Dict[str, List]      = {}    # asset -> last N trades
        self.liquidations: List[Dict]     = []    # global liq events (rolling 100)
        self.oi_history: Dict[str, List]  = {}    # asset -> [(ts, oi_usd), ...]
        self.funding_history: Dict[str, List] = {}# asset -> [rate, ...]

    def on_funding(self, data: Dict):
        coin = data.get("coin", data.get("name", ""))
        if coin:
            self.funding[coin] = data
            # Keep rolling funding history for trend detection
            rate = float(data.get("funding", 0))
            if coin not in self.funding_history:
                self.funding_history[coin] = []
            self.funding_history[coin].append(rate)
            if len(self.funding_history[coin]) > 96:   # keep last 96 periods
                self.funding_history[coin].pop(0)

    def on_orderbook(self, data: Dict):
        coin = data.get("coin", "")
        if coin:
            self.orderbook[coin] = data

    def on_trades(self, data):
        if isinstance(data, list) and data:
            coin = data[0].get("coin", "")
            if coin:
                if coin not in self.trades:
                    self.trades[coin] = []
                self.trades[coin].extend(data)
                self.trades[coin] = self.trades[coin][-500:]  # last 500 trades

    def on_liquidations(self, data):
        if isinstance(data, list):
            self.liquidations.extend(data)
        elif isinstance(data, dict):
            self.liquidations.append(data)
        self.liquidations = self.liquidations[-100:]


# Global cache singleton
market_cache = MarketDataCache()
