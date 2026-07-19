"""Authenticated Bybit V5 private WebSocket with REST-safe event caching."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Awaitable, Callable, Dict, Optional, Set

import aiohttp

from data.bybit_client import BybitClient
from execution.exchange_client import VenueOrder


log = logging.getLogger("kara.bybit_private_ws")


class BybitPrivateWebSocket:
    MAINNET_URL = "wss://stream.bybit.com/v5/private"
    TESTNET_URL = "wss://stream-testnet.bybit.com/v5/private"
    DEMO_URL = "wss://stream-demo.bybit.com/v5/private"
    TOPICS = ["order", "execution", "position", "wallet"]

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        demo: bool = False,
        stale_after_s: float = 45.0,
        on_reconnect: Optional[Callable[[], Awaitable[None]]] = None,
        on_state_event: Optional[Callable[[str, dict], Awaitable[None]]] = None,
        telemetry=None,
    ):
        if demo and testnet:
            raise ValueError("Bybit demo and testnet cannot both be enabled")
        self.api_key = api_key
        self._api_secret = api_secret
        self.url = self.DEMO_URL if demo else self.TESTNET_URL if testnet else self.MAINNET_URL
        # Retained for caller compatibility. Private topic silence cannot prove
        # a stale connection; transport state is determined by aiohttp heartbeat.
        self.stale_after_s = stale_after_s
        self.on_reconnect = on_reconnect
        self.on_state_event = on_state_event
        self.telemetry = telemetry
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._connected = False
        self._ever_connected = False
        self._last_message_at = 0.0
        self._orders: Dict[str, VenueOrder] = {}
        self._order_waiters: Dict[str, asyncio.Event] = {}
        self._execution_ids: Set[str] = set()
        self.latest_wallet: Optional[dict] = None
        self.latest_positions: Dict[str, dict] = {}

    @staticmethod
    def auth_signature(api_secret: str, expires: int) -> str:
        payload = f"GET/realtime{expires}"
        return hmac.new(
            api_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def stale(self) -> bool:
        # Private account topics are event-driven. A quiet account receives no
        # order, execution, position, or wallet payload, so message silence is
        # not a disconnected transport. aiohttp heartbeat detects dead peers
        # and _run marks the socket disconnected before REST fallback is used.
        value = not self._connected
        if self.telemetry:
            self.telemetry.ws_connected = self._connected
            self.telemetry.ws_stale = value
        return value

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        self._connected = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()

    async def _authenticate(self) -> None:
        expires = int((time.time() + 10) * 1000)
        await self._ws.send_json({
            "op": "auth",
            "args": [
                self.api_key,
                expires,
                self.auth_signature(self._api_secret, expires),
            ],
        })
        response = await asyncio.wait_for(self._ws.receive(), timeout=10)
        data = json.loads(response.data)
        if not data.get("success"):
            raise RuntimeError(f"Bybit WS auth failed: {data.get('ret_msg', 'unknown')}")

    async def _run(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                self._ws = await self._session.ws_connect(self.url, heartbeat=20)
                await self._authenticate()
                await self._ws.send_json({"op": "subscribe", "args": self.TOPICS})
                was_reconnect = self._ever_connected
                self._connected = True
                self._ever_connected = True
                self._last_message_at = time.monotonic()
                if self.telemetry:
                    self.telemetry.ws_connected = True
                    self.telemetry.ws_stale = False
                    self.telemetry.ws_disconnected_at = 0.0
                    if was_reconnect:
                        self.telemetry.ws_reconnect_count += 1
                backoff = 1.0
                if was_reconnect and self.on_reconnect:
                    await self.on_reconnect()
                async for message in self._ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        self.handle_message(message.data)
                    elif message.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("Bybit private WS reconnecting: %s", exc)
            finally:
                self._connected = False
                if self.telemetry:
                    self.telemetry.ws_connected = False
                    self.telemetry.ws_stale = True
                    self.telemetry.ws_disconnected_at = time.time()
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def handle_message(self, raw: str) -> None:
        data = json.loads(raw)
        self._last_message_at = time.monotonic()
        if self.telemetry:
            self.telemetry.ws_last_message_at = time.time()
            self.telemetry.ws_connected = self._connected
            self.telemetry.ws_stale = False
        topic = str(data.get("topic", ""))
        rows = data.get("data") or []
        if topic == "execution":
            unique = []
            for row in rows:
                exec_id = str(row.get("execId", ""))
                if exec_id and exec_id in self._execution_ids:
                    continue
                if exec_id:
                    self._execution_ids.add(exec_id)
                unique.append(row)
            if len(self._execution_ids) > 5000:
                self._execution_ids = set(list(self._execution_ids)[-2500:])
            for row in unique:
                self._dispatch_state_event(topic, row)
            return
        if topic == "position":
            for row in rows:
                key = f"{row.get('symbol', '')}:{row.get('positionIdx', 0)}"
                self.latest_positions[key] = row
                self._dispatch_state_event(topic, row)
            return
        if topic == "wallet":
            for row in rows:
                self.latest_wallet = row
                self._dispatch_state_event(topic, row)
            return
        if topic != "order":
            return
        for row in rows:
            client_order_id = str(row.get("orderLinkId", ""))
            if not client_order_id:
                continue
            order = BybitClient._parse_order(row)
            self._orders[client_order_id] = order
            self._order_waiters.setdefault(client_order_id, asyncio.Event()).set()

    def _dispatch_state_event(self, topic: str, row: dict) -> None:
        if self.on_state_event:
            asyncio.create_task(self.on_state_event(topic, row))

    async def wait_for_order(
        self, client_order_id: str, timeout_s: float
    ) -> Optional[VenueOrder]:
        cached = self._orders.get(client_order_id)
        if cached:
            return cached
        if self.stale:
            return None
        event = self._order_waiters.setdefault(client_order_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return None
        finally:
            event.clear()
        return self._orders.get(client_order_id)
