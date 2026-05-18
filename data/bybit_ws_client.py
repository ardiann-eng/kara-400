"""
KARA Bot — Bybit V5 WebSocket Client

Low-latency price stream via Bybit public WebSocket.
Drop-in replacement for BitgetWSClient.

Docs: https://bybit-exchange.github.io/docs/v5/ws/connect
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import aiohttp

log = logging.getLogger("kara.bybit_ws")

WS_PUBLIC_URL = "wss://stream.bybit.com/v5/public/linear"
WS_TESTNET_URL = "wss://stream-testnet.bybit.com/v5/public/linear"
PING_INTERVAL = 20  # seconds


@dataclass
class TickerCache:
    """Thread-safe price cache from WS tickers."""
    prices: Dict[str, float] = field(default_factory=dict)
    timestamps: Dict[str, float] = field(default_factory=dict)

    def get_price(self, symbol: str, max_age_s: float = 5.0) -> float:
        ts = self.timestamps.get(symbol, 0)
        if time.time() - ts > max_age_s:
            return 0.0
        return self.prices.get(symbol, 0.0)

    def is_fresh(self, symbol: str, max_age_s: float = 5.0) -> bool:
        return (time.time() - self.timestamps.get(symbol, 0)) <= max_age_s


class BybitWSClient:
    """Bybit V5 public WebSocket — subscribes to ticker for low-latency mark prices."""

    def __init__(self, testnet: bool = False):
        self.url = WS_TESTNET_URL if testnet else WS_PUBLIC_URL
        self.cache = TickerCache()
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._subscribed: set = set()
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._run_loop())
        log.info(f"[BYBIT-WS] Started (url={self.url})")

    async def stop(self) -> None:
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._task:
            self._task.cancel()
        if self._session and not self._session.closed:
            await self._session.close()
        log.info("[BYBIT-WS] Stopped")

    async def subscribe_ticker(self, symbol: str) -> None:
        """Subscribe to ticker.{symbol} for mark price updates."""
        topic = f"tickers.{symbol}"
        if topic in self._subscribed:
            return
        self._subscribed.add(topic)
        if self._ws and not self._ws.closed:
            await self._ws.send_json({"op": "subscribe", "args": [topic]})
            log.debug(f"[BYBIT-WS] Subscribed: {topic}")

    async def unsubscribe_ticker(self, symbol: str) -> None:
        topic = f"tickers.{symbol}"
        self._subscribed.discard(topic)
        if self._ws and not self._ws.closed:
            await self._ws.send_json({"op": "unsubscribe", "args": [topic]})

    async def _run_loop(self) -> None:
        while self._running:
            try:
                self._ws = await self._session.ws_connect(self.url, heartbeat=PING_INTERVAL)
                log.info("[BYBIT-WS] Connected")

                # Re-subscribe all topics
                if self._subscribed:
                    await self._ws.send_json({"op": "subscribe", "args": list(self._subscribed)})

                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._handle_message(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning(f"[BYBIT-WS] Connection error: {e}, reconnecting in 3s...")
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[BYBIT-WS] Unexpected error: {e}", exc_info=True)

            if self._running:
                await asyncio.sleep(3)

    def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        topic = data.get("topic", "")
        if not topic.startswith("tickers."):
            return

        d = data.get("data", {})
        symbol = d.get("symbol", "")
        mark_price = d.get("markPrice")

        if symbol and mark_price:
            try:
                self.cache.prices[symbol] = float(mark_price)
                self.cache.timestamps[symbol] = time.time()
            except (ValueError, TypeError):
                pass
