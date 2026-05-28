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
        self._on_dead_callbacks: List[Callable] = []  # called when WS permanently dies

    def add_dead_callback(self, fn: Callable):
        """Register an async or sync callback invoked when WS exceeds max retries."""
        self._on_dead_callbacks.append(fn)

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
        """Main reconnect loop. Never gives up — notifies callbacks if max retries hit."""
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

                if consecutive_failures > cfg.ws_reconnect_max_retries:
                    log.error(
                        f"🚨 WS max reconnect retries ({cfg.ws_reconnect_max_retries}) exceeded. "
                        "Notifying admin and entering slow-retry mode."
                    )
                    for cb in self._on_dead_callbacks:
                        try:
                            if asyncio.iscoroutinefunction(cb):
                                asyncio.create_task(cb())
                            else:
                                cb()
                        except Exception as cb_err:
                            log.debug(f"WS dead callback error: {cb_err}")
                    consecutive_failures = 0
                    await asyncio.sleep(60.0)
                    continue

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
            # HL activeAssetCtx format: {"coin": "X", "ctx": {"funding": "0.0001", ...}}
            ctx = data.get("ctx", data)
            rate = float(ctx.get("funding", 0) if isinstance(ctx, dict) else data.get("funding", 0))
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
        before = len(self.liquidations)
        if isinstance(data, list):
            self.liquidations.extend(data)
        elif isinstance(data, dict):
            self.liquidations.append(data)
        
        # Increase cache from 100 to 1000 to cover more assets over time
        self.liquidations = self.liquidations[-1000:]
        
        if len(self.liquidations) > before:
            # Change to INFO so user can see that data is actually arriving
            log.info(f"[WS] Liquidation event: +{len(self.liquidations)-before} (Cache: {len(self.liquidations)})")


# Global cache singleton
market_cache = MarketDataCache()


# ──────────────────────────────────────────────
# BINANCE LIQUIDATION STREAM (free, no API key)
# ──────────────────────────────────────────────
# Binance futures forceOrder WS provides ALL liquidation events across all pairs.
# Volume 10-50x higher than Hyperliquid → much better liquidation detection.
# REST API is 403 blocked from Railway, but WebSocket WORKS.

BINANCE_LIQ_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"

# Map Binance symbols to KARA asset names (strip "USDT"/"USDC" suffix)
def _binance_symbol_to_coin(symbol: str) -> str:
    for suffix in ("USDT", "USDC", "BUSD"):
        if symbol.endswith(suffix):
            return symbol[: -len(suffix)]
    return symbol


class BinanceLiquidationStream:
    """
    Connects to Binance Futures forceOrder stream and feeds liquidation events
    into the shared MarketDataCache. Runs as background task alongside HL WS.
    """

    def __init__(self, cache: MarketDataCache):
        self._cache = cache
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._connected = False

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        log.info("[BinanceLiq] Starting Binance liquidation stream...")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def _run_loop(self):
        while self._running:
            try:
                await self._connect_and_listen()
            except Exception as e:
                if not self._running:
                    break
                log.warning(f"[BinanceLiq] Disconnected: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _connect_and_listen(self):
        async with websockets.connect(
            BINANCE_LIQ_URL, ping_interval=20, ping_timeout=10, close_timeout=5
        ) as ws:
            self._connected = True
            log.info("[BinanceLiq] Connected to Binance forceOrder stream")
            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                    self._process(msg)
                except Exception:
                    pass
            self._connected = False

    def _process(self, msg: dict):
        """
        Binance forceOrder format:
        {"e":"forceOrder","E":ts,"o":{"s":"BTCUSDT","S":"SELL","p":"9910","q":"0.014",...}}

        S="SELL" means a LONG position was liquidated (forced sell) → bearish pressure
        S="BUY"  means a SHORT position was liquidated (forced buy) → bullish pressure
        """
        order = msg.get("o", {})
        if not order:
            return
        symbol = order.get("s", "")
        coin = _binance_symbol_to_coin(symbol)
        price = float(order.get("p", 0))
        qty = float(order.get("q", 0))
        liq_side = order.get("S", "")  # BUY or SELL

        if not coin or price == 0:
            return

        # Normalize to KARA format: side = the position that got liquidated
        # Binance S="SELL" → long got liquidated, S="BUY" → short got liquidated
        normalized = {
            "coin": coin,
            "px": price,
            "sz": qty,
            "side": "long" if liq_side == "SELL" else "short",
            "source": "binance",
            "time": msg.get("E", int(time.time() * 1000)),
        }
        self._cache.on_liquidations([normalized])


# ──────────────────────────────────────────────
# OKX LIQUIDATION STREAM (free, no API key, no geo-block)
# ──────────────────────────────────────────────
# OKX public WS provides liquidation-orders channel for all SWAP instruments.
# Unlike Binance (blocked from Railway), OKX is accessible globally.
# Supplements HL sparse liquidation data with high-volume OKX data.

OKX_WS_PUBLIC_URL = "wss://ws.okx.com:8443/ws/v5/public"


def _okx_instid_to_coin(inst_id: str) -> str:
    """Convert OKX instId like 'BTC-USDT-SWAP' to KARA coin name 'BTC'."""
    parts = inst_id.split("-")
    return parts[0] if parts else inst_id


class OKXLiquidationStream:
    """
    Connects to OKX public WebSocket and subscribes to liquidation-orders channel.
    Feeds normalized liquidation events into MarketDataCache.
    No auth required. Not geo-blocked from Railway.
    """

    def __init__(self, cache: "MarketDataCache"):
        self._cache = cache
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._connected = False
        self._event_count = 0

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        log.info("[OKXLiq] Starting OKX liquidation stream...")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    @property
    def connected(self) -> bool:
        return self._connected

    async def _run_loop(self):
        backoff = 5
        while self._running:
            try:
                await self._connect_and_listen()
                backoff = 5  # reset on clean disconnect
            except Exception as e:
                if not self._running:
                    break
                log.warning(f"[OKXLiq] Disconnected: {e}. Reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _connect_and_listen(self):
        async with websockets.connect(
            OKX_WS_PUBLIC_URL, ping_interval=20, ping_timeout=10, close_timeout=5
        ) as ws:
            self._connected = True
            log.info("[OKXLiq] Connected to OKX public WS")

            # Subscribe to liquidation-orders for SWAP (perpetual futures)
            sub_msg = {
                "op": "subscribe",
                "args": [{"channel": "liquidation-orders", "instType": "SWAP"}]
            }
            await ws.send(json.dumps(sub_msg))
            log.info("[OKXLiq] Subscribed to liquidation-orders (SWAP)")

            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                    if msg.get("event") == "subscribe":
                        log.info(f"[OKXLiq] Subscription confirmed: {msg}")
                        continue
                    if msg.get("event") == "error":
                        log.error(f"[OKXLiq] Error: {msg}")
                        continue
                    if "data" in msg:
                        self._process(msg)
                except Exception as e:
                    log.debug(f"[OKXLiq] Parse error: {e}")

            self._connected = False

    def _process(self, msg: dict):
        """
        OKX liquidation-orders format:
        {
          "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
          "data": [{
            "details": [{
              "side": "buy",       // buy = short liquidated (bullish)
              "sz": "0.1",         // size in contracts
              "px": "67000",       // price
              "ts": "1716000000000"
            }],
            "instId": "BTC-USDT-SWAP"
          }]
        }

        side="buy" → short position liquidated (forced buy to close) → BULLISH pressure
        side="sell" → long position liquidated (forced sell to close) → BEARISH pressure
        """
        data_list = msg.get("data", [])
        for item in data_list:
            inst_id = item.get("instId", "")
            coin = _okx_instid_to_coin(inst_id)
            if not coin:
                continue

            details = item.get("details", [])
            for detail in details:
                price = float(detail.get("px", 0))
                qty = float(detail.get("sz", 0))
                liq_side = detail.get("side", "")
                ts = int(detail.get("ts", int(time.time() * 1000)))

                if price == 0 or qty == 0:
                    continue

                # Normalize: side = the position that got liquidated
                # OKX side="sell" → long got liquidated, side="buy" → short got liquidated
                normalized = {
                    "coin": coin,
                    "px": price,
                    "sz": qty,
                    "side": "long" if liq_side == "sell" else "short",
                    "source": "okx",
                    "time": ts,
                }
                self._cache.on_liquidations([normalized])
                self._event_count += 1
