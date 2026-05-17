"""
KARA Bot - Bitget Client (REST)

Async REST client untuk Bitget USDT-M Futures (Mix v2 API).

Desain low-latency:
1. httpx.AsyncClient dengan HTTP/2 + connection pool (keep-alive)
2. Public endpoints tanpa auth — bisa dipakai global tanpa user keys
3. Auth (HMAC-SHA256) di-cache per request, signature dihitung sekali
4. Separate concurrency: market data semaphore terpisah dari trading
5. Tidak ada blocking I/O — semua awaitable
6. Per-asset price cache singkat (1.5s TTL) untuk burst de-dupe

Bitget API reference: https://www.bitget.com/api-doc/contract/intro
"""

from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

log = logging.getLogger("kara.bitget_client")


BITGET_BASE_URL = "https://api.bitget.com"

# Product type untuk USDT-M futures di v2 API:
PRODUCT_TYPE_LIVE  = "USDT-FUTURES"
PRODUCT_TYPE_DEMO  = "SUSDT-FUTURES"  # paper/demo trading product type

# Margin coin always USDT for USDT-M futures
MARGIN_COIN = "USDT"


class BitgetAPIError(Exception):
    """Raised when Bitget returns non-success code."""
    def __init__(self, code: str, msg: str, raw: Any = None):
        self.code = code
        self.msg = msg
        self.raw = raw
        super().__init__(f"Bitget API error [{code}]: {msg}")


class BitgetClient:
    """
    Bitget USDT-M Futures REST client.

    Single instance bisa dipakai untuk:
    - Public data (mark price, candles, ticker) — TANPA credentials
    - Private trading (order, position, balance) — DENGAN credentials per user

    Untuk multi-user, instantiate satu BitgetClient per user dengan
    credentials masing-masing, atau gunakan with_credentials() untuk
    membuat lightweight clone.
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        demo_mode: bool = False,
        timeout: float = 8.0,
    ):
        self.api_key    = api_key.strip()
        self.api_secret = api_secret.strip()
        self.passphrase = passphrase.strip()
        self.demo_mode  = demo_mode
        self.product_type = PRODUCT_TYPE_DEMO if demo_mode else PRODUCT_TYPE_LIVE

        self._timeout = timeout
        # HTTP client lazy-init in connect() (event loop must be running)
        self._http: Optional[httpx.AsyncClient] = None
        self._connect_lock = asyncio.Lock()

        # Low-latency cache untuk burst de-dupe.
        # Mark price TTL singkat (1.5s) cukup untuk batch signal
        # tanpa flooding HTTP.
        self._mark_cache: Dict[str, Tuple[float, float]] = {}  # sym → (price, ts)
        self._mark_cache_ttl = 1.5

        # Semaphore terpisah: data scan (boleh throttled) vs trading (instan)
        self._data_sem: Optional[asyncio.Semaphore] = None  # init in connect()

        # Stats
        self._last_request_ts: float = 0.0
        self._consecutive_errors = 0

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret and self.passphrase)

    async def connect(self) -> None:
        """Init httpx client. Harus dipanggil sekali sebelum first request."""
        async with self._connect_lock:
            if self._http is not None:
                return
            limits = httpx.Limits(
                max_keepalive_connections=20,
                max_connections=40,
                keepalive_expiry=60.0,
            )
            # HTTP/2 dengan keep-alive — kurangi handshake latency
            self._http = httpx.AsyncClient(
                base_url=BITGET_BASE_URL,
                timeout=self._timeout,
                limits=limits,
                http2=True,
                headers={"locale": "en-US"},
            )
            self._data_sem = asyncio.Semaphore(8)
            log.info(
                f"[BITGET] Client connected (product_type={self.product_type}, "
                f"auth={'yes' if self.has_credentials else 'public-only'})"
            )

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    def with_credentials(
        self, api_key: str, api_secret: str, passphrase: str
    ) -> "BitgetClient":
        """
        Buat lightweight clone untuk user lain, share HTTP client supaya
        connection pool dipakai bersama. Tidak buat httpx baru.

        Pattern: global public client + per-user wrapper untuk private calls.
        """
        clone = BitgetClient(
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase,
            demo_mode=self.demo_mode,
            timeout=self._timeout,
        )
        clone._http = self._http  # share HTTP pool!
        clone._data_sem = self._data_sem
        clone._mark_cache = self._mark_cache  # shared price cache
        return clone

    # ──────────────────────────────────────────────────────────────
    # AUTH / SIGNING
    # ──────────────────────────────────────────────────────────────

    def _sign(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        """HMAC-SHA256 signature → base64. Bitget spec."""
        pre = f"{timestamp}{method.upper()}{request_path}{body}"
        sig = hmac.new(
            self.api_secret.encode("utf-8"),
            pre.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(sig).decode("utf-8")

    def _auth_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        if not self.has_credentials:
            raise RuntimeError("Bitget client tidak punya credentials — tambahkan api_key/secret/passphrase")
        ts = str(int(time.time() * 1000))
        return {
            "ACCESS-KEY":        self.api_key,
            "ACCESS-SIGN":       self._sign(ts, method, path, body),
            "ACCESS-TIMESTAMP":  ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type":      "application/json",
            "locale":            "en-US",
        }

    # ──────────────────────────────────────────────────────────────
    # LOW-LEVEL REQUEST
    # ──────────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        auth: bool = False,
        is_data_call: bool = False,
    ) -> Any:
        """
        Low-level HTTP wrapper. Pakai semaphore HANYA untuk market data
        (scoring), TIDAK untuk order execution — order harus instan.
        """
        if self._http is None:
            await self.connect()

        # Build query string
        query = ""
        if params:
            # Bitget mau parameter di query string untuk GET
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                query = "?" + "&".join(f"{k}={v}" for k, v in filtered.items())

        body_str = json.dumps(body, separators=(",", ":")) if body else ""

        headers = {"Content-Type": "application/json", "locale": "en-US"}
        if auth:
            headers.update(self._auth_headers(method, path + query, body_str))

        url = path + query

        async def _do_request():
            assert self._http is not None
            t0 = time.monotonic()
            try:
                if method.upper() == "GET":
                    resp = await self._http.get(url, headers=headers)
                elif method.upper() == "POST":
                    resp = await self._http.post(url, headers=headers, content=body_str)
                elif method.upper() == "DELETE":
                    resp = await self._http.delete(url, headers=headers)
                else:
                    raise ValueError(f"Method tidak didukung: {method}")
            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                self._consecutive_errors += 1
                log.warning(f"[BITGET] {method} {path} network error: {e}")
                raise
            latency_ms = (time.monotonic() - t0) * 1000.0
            self._last_request_ts = time.time()

            try:
                data = resp.json()
            except Exception:
                log.error(f"[BITGET] {method} {path} non-JSON response: {resp.text[:200]}")
                raise BitgetAPIError("HTTP", f"non-JSON response {resp.status_code}", resp.text)

            code = str(data.get("code", ""))
            if code == "00000":
                self._consecutive_errors = 0
                if latency_ms > 500:
                    log.warning(f"[BITGET] slow request {method} {path} {latency_ms:.0f}ms")
                else:
                    log.debug(f"[BITGET] {method} {path} {latency_ms:.0f}ms")
                return data.get("data")

            self._consecutive_errors += 1
            msg = data.get("msg", "unknown")
            log.warning(f"[BITGET] {method} {path} → code={code} msg={msg}")
            raise BitgetAPIError(code, msg, data)

        if is_data_call and self._data_sem is not None:
            async with self._data_sem:
                return await _do_request()
        return await _do_request()

    # ──────────────────────────────────────────────────────────────
    # PUBLIC: MARKET DATA
    # ──────────────────────────────────────────────────────────────

    async def get_all_contracts(self) -> List[Dict[str, Any]]:
        """List semua perpetual contracts (USDT-M)."""
        try:
            data = await self._request(
                "GET", "/api/v2/mix/market/contracts",
                params={"productType": self.product_type},
                is_data_call=True,
            )
            if isinstance(data, list):
                return data
            return []
        except Exception as e:
            log.error(f"[BITGET] get_all_contracts failed: {e}")
            return []

    async def get_mark_price(self, symbol: str) -> float:
        """
        Mark price untuk satu symbol. Pakai cache 1.5s TTL untuk de-dupe
        burst calls dari signal generator.
        """
        if not symbol:
            return 0.0

        # Cache hit?
        cached = self._mark_cache.get(symbol)
        if cached and (time.time() - cached[1]) < self._mark_cache_ttl:
            return cached[0]

        try:
            data = await self._request(
                "GET", "/api/v2/mix/market/symbol-price",
                params={"symbol": symbol, "productType": self.product_type},
                is_data_call=True,
            )
            # Response format: [{"symbol":"BTCUSDT","price":"78000.5","ts":"..."}] or single dict
            if isinstance(data, list) and data:
                px = float(data[0].get("price", 0))
            elif isinstance(data, dict):
                px = float(data.get("price", 0))
            else:
                px = 0.0

            if px > 0:
                self._mark_cache[symbol] = (px, time.time())
            return px
        except Exception as e:
            log.debug(f"[BITGET] get_mark_price({symbol}) failed: {e}")
            return 0.0

    async def get_mark_prices_batch(self, symbols: List[str]) -> Dict[str, float]:
        """Batch fetch — pakai endpoint all-tickers (1 HTTP call untuk semua)."""
        try:
            data = await self._request(
                "GET", "/api/v2/mix/market/tickers",
                params={"productType": self.product_type},
                is_data_call=True,
            )
            result: Dict[str, float] = {}
            now = time.time()
            if isinstance(data, list):
                wanted = set(symbols)
                for t in data:
                    sym = t.get("symbol", "")
                    if sym in wanted:
                        try:
                            px = float(t.get("markPrice") or t.get("lastPr") or t.get("last") or 0)
                            if px > 0:
                                result[sym] = px
                                self._mark_cache[sym] = (px, now)
                        except (ValueError, TypeError):
                            pass
            return result
        except Exception as e:
            log.warning(f"[BITGET] get_mark_prices_batch failed: {e}")
            return {}

    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Detail ticker — last, bid, ask, funding, OI."""
        try:
            data = await self._request(
                "GET", "/api/v2/mix/market/ticker",
                params={"symbol": symbol, "productType": self.product_type},
                is_data_call=True,
            )
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict):
                return data
            return {}
        except Exception as e:
            log.debug(f"[BITGET] get_ticker({symbol}) failed: {e}")
            return {}

    # ──────────────────────────────────────────────────────────────
    # PRIVATE: ACCOUNT
    # ──────────────────────────────────────────────────────────────

    async def get_account(self) -> Dict[str, Any]:
        """Akun USDT-M futures: balance, equity, margin used."""
        data = await self._request(
            "GET", "/api/v2/mix/account/account",
            params={
                "symbol":      "BTCUSDT",   # required oleh v2; data return tetap akun
                "productType": self.product_type,
                "marginCoin":  MARGIN_COIN,
            },
            auth=True,
        )
        return data if isinstance(data, dict) else {}

    async def get_all_account_balances(self) -> List[Dict[str, Any]]:
        """List balance per margin coin."""
        try:
            data = await self._request(
                "GET", "/api/v2/mix/account/accounts",
                params={"productType": self.product_type},
                auth=True,
            )
            return data if isinstance(data, list) else []
        except Exception as e:
            log.error(f"[BITGET] get_all_account_balances failed: {e}")
            return []

    async def get_open_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Open positions. Jika symbol=None → semua position."""
        if symbol:
            path = "/api/v2/mix/position/single-position"
            params = {
                "symbol":      symbol,
                "productType": self.product_type,
                "marginCoin":  MARGIN_COIN,
            }
        else:
            path = "/api/v2/mix/position/all-position"
            params = {
                "productType": self.product_type,
                "marginCoin":  MARGIN_COIN,
            }
        try:
            data = await self._request("GET", path, params=params, auth=True)
            if isinstance(data, list):
                return [p for p in data if float(p.get("total", 0) or 0) > 0]
            return []
        except Exception as e:
            log.error(f"[BITGET] get_open_positions failed: {e}")
            return []

    # ──────────────────────────────────────────────────────────────
    # PRIVATE: ORDERS
    # ──────────────────────────────────────────────────────────────

    async def set_leverage(
        self,
        symbol: str,
        leverage: int,
        hold_side: str = "long",
        margin_mode: str = "isolated",
    ) -> Dict[str, Any]:
        """
        Set leverage untuk symbol+side. Bitget hedge mode butuh per-side.
        margin_mode: "isolated" (default) atau "crossed"
        """
        body = {
            "symbol":      symbol,
            "productType": self.product_type,
            "marginCoin":  MARGIN_COIN,
            "leverage":    str(leverage),
            "holdSide":    hold_side,
        }
        try:
            return await self._request(
                "POST", "/api/v2/mix/account/set-leverage",
                body=body, auth=True,
            ) or {}
        except BitgetAPIError as e:
            # 40762 = leverage sama (no change) — treat as success
            if e.code in ("40762", "45110"):
                log.debug(f"[BITGET] leverage {symbol} {hold_side}={leverage}x already set")
                return {"leverage": leverage}
            raise

    async def set_margin_mode(self, symbol: str, mode: str = "isolated") -> Dict[str, Any]:
        """mode: 'isolated' | 'crossed'"""
        body = {
            "symbol":      symbol,
            "productType": self.product_type,
            "marginCoin":  MARGIN_COIN,
            "marginMode":  mode,
        }
        try:
            return await self._request(
                "POST", "/api/v2/mix/account/set-margin-mode",
                body=body, auth=True,
            ) or {}
        except BitgetAPIError as e:
            if e.code in ("45117", "40760"):  # already set
                return {"marginMode": mode}
            raise

    async def set_position_mode(self, mode: str = "hedge_mode") -> Dict[str, Any]:
        """
        mode: 'one_way_mode' | 'hedge_mode'.
        Default hedge_mode (Bitget standard). Hanya perlu di-set sekali per account.
        """
        body = {
            "productType": self.product_type,
            "posMode":     mode,
        }
        try:
            return await self._request(
                "POST", "/api/v2/mix/account/set-position-mode",
                body=body, auth=True,
            ) or {}
        except BitgetAPIError as e:
            if e.code in ("45117", "40760", "45110"):  # already set
                return {"posMode": mode}
            raise

    async def place_order(
        self,
        symbol: str,
        side: str,            # "buy" | "sell"
        order_type: str,      # "limit" | "market"
        size: float,
        trade_side: str,      # "open" | "close"   (hedge mode)
        price: float = 0.0,
        reduce_only: bool = False,
        client_oid: Optional[str] = None,
        force: str = "gtc",   # gtc | post_only | ioc | fok
    ) -> Dict[str, Any]:
        """
        Place USDT-M futures order.

        Hedge mode mapping:
          LONG open:  side="buy",  trade_side="open"
          LONG close: side="sell", trade_side="close"
          SHORT open: side="sell", trade_side="open"
          SHORT close:side="buy",  trade_side="close"
        """
        body = {
            "symbol":      symbol,
            "productType": self.product_type,
            "marginCoin":  MARGIN_COIN,
            "size":        str(size),
            "side":        side,
            "tradeSide":   trade_side,
            "orderType":   order_type,
            "force":       force,
        }
        if order_type == "limit" and price > 0:
            body["price"] = str(price)
        if reduce_only:
            body["reduceOnly"] = "YES"
        if client_oid:
            body["clientOid"] = client_oid

        return await self._request(
            "POST", "/api/v2/mix/order/place-order",
            body=body, auth=True,
        ) or {}

    async def cancel_order(
        self, symbol: str, order_id: Optional[str] = None, client_oid: Optional[str] = None
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "symbol":      symbol,
            "productType": self.product_type,
            "marginCoin":  MARGIN_COIN,
        }
        if order_id:
            body["orderId"] = order_id
        if client_oid:
            body["clientOid"] = client_oid
        try:
            return await self._request(
                "POST", "/api/v2/mix/order/cancel-order",
                body=body, auth=True,
            ) or {}
        except Exception as e:
            log.debug(f"[BITGET] cancel_order({symbol}, {order_id}) failed: {e}")
            return {}

    async def get_pending_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"productType": self.product_type}
        if symbol:
            params["symbol"] = symbol
        try:
            data = await self._request(
                "GET", "/api/v2/mix/order/orders-pending",
                params=params, auth=True,
            )
            if isinstance(data, dict):
                return data.get("entrustedList") or []
            return data if isinstance(data, list) else []
        except Exception as e:
            log.error(f"[BITGET] get_pending_orders failed: {e}")
            return []

    async def get_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        try:
            data = await self._request(
                "GET", "/api/v2/mix/order/detail",
                params={
                    "symbol":      symbol,
                    "productType": self.product_type,
                    "orderId":     order_id,
                },
                auth=True,
            )
            return data if isinstance(data, dict) else {}
        except Exception as e:
            log.debug(f"[BITGET] get_order failed: {e}")
            return {}

    # ──────────────────────────────────────────────────────────────
    # PRIVATE: STOP-LOSS / TAKE-PROFIT trigger orders
    # ──────────────────────────────────────────────────────────────

    async def place_tpsl_order(
        self,
        symbol: str,
        plan_type: str,        # "pos_loss" (SL) | "pos_profit" (TP) | "loss_plan" | "profit_plan"
        trigger_price: float,
        hold_side: str,        # "long" | "short" (posisi yang mau diproteksi)
        size: Optional[float] = None,    # untuk partial; None = full position
        execute_price: float = 0.0,      # 0 = market on trigger
        client_oid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Place stop-loss atau take-profit trigger order untuk posisi yang ada.

        plan_type:
          "pos_loss"   → posisi-level SL (auto-resize jika partial close)
          "pos_profit" → posisi-level TP
        Recommended pakai posisi-level supaya tidak harus update setiap kali partial close.
        """
        body: Dict[str, Any] = {
            "symbol":       symbol,
            "productType":  self.product_type,
            "marginCoin":   MARGIN_COIN,
            "planType":     plan_type,
            "triggerPrice": str(trigger_price),
            "triggerType":  "mark_price",
            "holdSide":     hold_side,
        }
        if execute_price > 0:
            body["executePrice"] = str(execute_price)
        if size is not None:
            body["size"] = str(size)
        if client_oid:
            body["clientOid"] = client_oid

        return await self._request(
            "POST", "/api/v2/mix/order/place-tpsl-order",
            body=body, auth=True,
        ) or {}

    async def cancel_tpsl_order(
        self,
        symbol: str,
        order_id_list: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "symbol":      symbol,
            "productType": self.product_type,
            "marginCoin":  MARGIN_COIN,
        }
        if order_id_list:
            body["orderIdList"] = order_id_list
        try:
            return await self._request(
                "POST", "/api/v2/mix/order/cancel-plan-order",
                body=body, auth=True,
            ) or {}
        except Exception as e:
            log.debug(f"[BITGET] cancel_tpsl_order failed: {e}")
            return {}

    # ──────────────────────────────────────────────────────────────
    # HEALTH / CONNECTIVITY CHECK
    # ──────────────────────────────────────────────────────────────

    async def ping(self) -> bool:
        """Public server time check — verifikasi koneksi tanpa auth."""
        try:
            data = await self._request("GET", "/api/v2/public/time")
            return bool(data)
        except Exception:
            return False

    async def verify_credentials(self) -> Tuple[bool, str]:
        """Try fetch account — return (ok, error_msg)."""
        if not self.has_credentials:
            return False, "API key / secret / passphrase belum lengkap"
        try:
            await self.get_account()
            return True, "ok"
        except BitgetAPIError as e:
            return False, f"[{e.code}] {e.msg}"
        except Exception as e:
            return False, str(e)
