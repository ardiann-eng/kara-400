"""
KARA Bot — Bybit V5 REST Client

Drop-in replacement for BitgetClient. Uses Bybit Unified Trading Account API V5.
Supports: linear USDT perpetual futures.

Docs: https://bybit-exchange.github.io/docs/v5/intro
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

log = logging.getLogger("kara.bybit_client")


class BybitAPIError(Exception):
    def __init__(self, code: str, msg: str, raw: Any = None):
        self.code = code
        self.msg = msg
        self.raw = raw
        super().__init__(f"Bybit API Error [{code}]: {msg}")


class BybitClient:
    """Async Bybit V5 REST client for USDT-M linear perpetual futures."""

    BASE_URL      = "https://api.bybit.com"
    TESTNET_URL   = "https://api-testnet.bybit.com"
    RECV_WINDOW   = "5000"
    CATEGORY      = "linear"  # USDT perpetual

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = False,
    ):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.base_url   = self.TESTNET_URL if testnet else self.BASE_URL
        self.testnet    = testnet
        self._session: Optional[aiohttp.ClientSession] = None

    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    async def connect(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def with_credentials(self, api_key: str, api_secret: str) -> "BybitClient":
        """Return new client instance with different credentials (per-user)."""
        return BybitClient(
            api_key=api_key,
            api_secret=api_secret,
            testnet=self.testnet,
        )

    # ── AUTH ──────────────────────────────────────────────────────────────────
    def _sign(self, timestamp: str, params_str: str) -> str:
        """HMAC-SHA256 signature for Bybit V5."""
        payload = f"{timestamp}{self.api_key}{self.RECV_WINDOW}{params_str}"
        return hmac.new(self.api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    def _auth_headers(self, timestamp: str, params_str: str) -> Dict[str, str]:
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": self._sign(timestamp, params_str),
            "X-BAPI-RECV-WINDOW": self.RECV_WINDOW,
            "Content-Type": "application/json",
        }

    # ── REQUEST LAYER ─────────────────────────────────────────────────────────
    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        body: Optional[Dict] = None,
        auth: bool = False,
        retries: int = 3,
    ) -> Dict[str, Any]:
        if self._session is None:
            await self.connect()

        url = f"{self.base_url}{path}"
        timestamp = str(int(time.time() * 1000))

        headers = {"Content-Type": "application/json"}
        if auth:
            import json as _json
            if method == "GET":
                qs = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
                headers = self._auth_headers(timestamp, qs)
            else:
                body_str = _json.dumps(body or {})
                headers = self._auth_headers(timestamp, body_str)

        for attempt in range(retries):
            try:
                if method == "GET":
                    async with self._session.get(url, params=params, headers=headers) as resp:
                        data = await resp.json()
                else:
                    import json as _json
                    async with self._session.post(url, data=_json.dumps(body or {}), headers=headers) as resp:
                        data = await resp.json()

                ret_code = data.get("retCode", -1)
                if ret_code == 0:
                    return data.get("result", {})
                elif ret_code == 10006:  # rate limit
                    wait = 1.0 * (attempt + 1)
                    log.warning(f"[BYBIT] Rate limited, wait {wait}s")
                    await asyncio.sleep(wait)
                    continue
                else:
                    raise BybitAPIError(str(ret_code), data.get("retMsg", "unknown"), data)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    raise

        return {}

    # ── PUBLIC ENDPOINTS ──────────────────────────────────────────────────────
    async def get_all_contracts(self) -> List[Dict[str, Any]]:
        """Get all linear USDT perpetual instruments."""
        result = await self._request("GET", "/v5/market/instruments-info", {
            "category": self.CATEGORY, "limit": "1000"
        })
        return result.get("list", [])

    async def get_mark_price(self, symbol: str) -> float:
        """Get mark price for a symbol (e.g. 'BTCUSDT')."""
        result = await self._request("GET", "/v5/market/tickers", {
            "category": self.CATEGORY, "symbol": symbol
        })
        items = result.get("list", [])
        if items:
            return float(items[0].get("markPrice", 0))
        return 0.0

    async def get_mark_prices_batch(self, symbols: List[str]) -> Dict[str, float]:
        """Get mark prices for all linear tickers (single API call)."""
        result = await self._request("GET", "/v5/market/tickers", {
            "category": self.CATEGORY
        })
        prices = {}
        for item in result.get("list", []):
            sym = item.get("symbol", "")
            if sym in symbols or not symbols:
                prices[sym] = float(item.get("markPrice", 0))
        return prices

    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Get 24h ticker for a symbol."""
        result = await self._request("GET", "/v5/market/tickers", {
            "category": self.CATEGORY, "symbol": symbol
        })
        items = result.get("list", [])
        return items[0] if items else {}

    async def ping(self) -> bool:
        """Test connectivity."""
        try:
            result = await self._request("GET", "/v5/market/time")
            return True
        except Exception:
            return False

    # ── ACCOUNT ENDPOINTS (auth required) ─────────────────────────────────────
    async def get_account(self) -> Dict[str, Any]:
        """Get unified account wallet balance."""
        result = await self._request("GET", "/v5/account/wallet-balance", {
            "accountType": "UNIFIED"
        }, auth=True)
        accounts = result.get("list", [])
        return accounts[0] if accounts else {}

    async def get_all_account_balances(self) -> List[Dict[str, Any]]:
        """Get all coin balances in unified account."""
        acct = await self.get_account()
        return acct.get("coin", [])

    async def get_open_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get open positions."""
        params = {"category": self.CATEGORY, "settleCoin": "USDT"}
        if symbol:
            params["symbol"] = symbol
        result = await self._request("GET", "/v5/position/list", params, auth=True)
        positions = result.get("list", [])
        # Filter only positions with size > 0
        return [p for p in positions if float(p.get("size", 0)) > 0]

    async def set_leverage(self, symbol: str, buy_leverage: int, sell_leverage: int) -> Dict[str, Any]:
        """Set leverage for a symbol."""
        return await self._request("POST", "/v5/position/set-leverage", body={
            "category": self.CATEGORY,
            "symbol": symbol,
            "buyLeverage": str(buy_leverage),
            "sellLeverage": str(sell_leverage),
        }, auth=True)

    async def set_margin_mode(self, symbol: str, mode: str = "ISOLATED_MARGIN") -> Dict[str, Any]:
        """Set margin mode: ISOLATED_MARGIN or REGULAR_MARGIN (cross)."""
        return await self._request("POST", "/v5/position/switch-isolated", body={
            "category": self.CATEGORY,
            "symbol": symbol,
            "tradeMode": 1 if mode == "ISOLATED_MARGIN" else 0,
            "buyLeverage": "15",
            "sellLeverage": "15",
        }, auth=True)

    async def set_position_mode(self, mode: str = "BothSide") -> Dict[str, Any]:
        """Set position mode: MergedSingle (one-way) or BothSide (hedge)."""
        # Bybit V5: 0=MergedSingle, 3=BothSide
        coin = "USDT"
        return await self._request("POST", "/v5/position/switch-mode", body={
            "category": self.CATEGORY,
            "coin": coin,
            "mode": 3 if mode == "BothSide" else 0,
        }, auth=True)

    # ── ORDER ENDPOINTS ───────────────────────────────────────────────────────
    async def place_order(
        self,
        symbol: str,
        side: str,           # "Buy" or "Sell"
        qty: str,            # quantity string
        order_type: str = "Market",
        price: str = "",
        reduce_only: bool = False,
        position_idx: int = 0,  # 0=one-way, 1=buy-side hedge, 2=sell-side hedge
    ) -> Dict[str, Any]:
        """Place an order."""
        body = {
            "category": self.CATEGORY,
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": qty,
            "positionIdx": position_idx,
        }
        if price and order_type == "Limit":
            body["price"] = price
        if reduce_only:
            body["reduceOnly"] = True
        return await self._request("POST", "/v5/order/create", body=body, auth=True)

    async def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        """Cancel an order."""
        return await self._request("POST", "/v5/order/cancel", body={
            "category": self.CATEGORY,
            "symbol": symbol,
            "orderId": order_id,
        }, auth=True)

    async def get_pending_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get open/pending orders."""
        params = {"category": self.CATEGORY}
        if symbol:
            params["symbol"] = symbol
        result = await self._request("GET", "/v5/order/realtime", params, auth=True)
        return result.get("list", [])

    async def get_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        """Get order detail."""
        result = await self._request("GET", "/v5/order/realtime", {
            "category": self.CATEGORY,
            "symbol": symbol,
            "orderId": order_id,
        }, auth=True)
        items = result.get("list", [])
        return items[0] if items else {}

    async def place_tpsl_order(
        self,
        symbol: str,
        side: str,           # "Buy" or "Sell" (opposite of position side for SL/TP)
        sl_price: str = "",
        tp_price: str = "",
        position_idx: int = 0,
    ) -> Dict[str, Any]:
        """Set trading stop (SL/TP) on existing position."""
        body = {
            "category": self.CATEGORY,
            "symbol": symbol,
            "positionIdx": position_idx,
        }
        if sl_price:
            body["stopLoss"] = sl_price
        if tp_price:
            body["takeProfit"] = tp_price
        return await self._request("POST", "/v5/position/trading-stop", body=body, auth=True)

    async def cancel_tpsl_order(self, symbol: str, position_idx: int = 0) -> Dict[str, Any]:
        """Cancel SL/TP by setting them to empty."""
        return await self.place_tpsl_order(symbol, "", position_idx=position_idx)

    async def verify_credentials(self) -> Tuple[bool, str]:
        """Verify API credentials are valid."""
        try:
            acct = await self.get_account()
            if acct:
                return True, "OK"
            return False, "Empty account response"
        except BybitAPIError as e:
            return False, f"API error: {e.msg}"
        except Exception as e:
            return False, str(e)
