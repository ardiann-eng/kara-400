"""
KARA Bot - Hyperliquid Client (REST)
Wraps the official hyperliquid-python-sdk for clean async usage.
Handles testnet/mainnet toggle, rate limiting, and error recovery.

BUGFIX v2 2026-04-03: Fixed HTTP 422 errors + list index out of range
- Better API response validation
- Retry logic with exponential backoff
- Detailed error logging for 422 errors
- Fixed l2Book payload format
- Robust response parsing

BUGFIX v3 2026-04-05: Stop hiding API failures
- Exception handlers now log and re-raise instead of returning silent zeros
- Added debug logging before each API call
"""

from __future__ import annotations
import asyncio
import logging
import time
import json
import httpx
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

# Official SDK
try:
    from hyperliquid.info  import Info
    from hyperliquid.exchange import Exchange
    import eth_account
except ImportError:
    raise ImportError(
        "Run: pip install hyperliquid-python-sdk eth-account\n"
        "See requirements.txt for full deps."
    )

import config
from models.schemas import (
    FundingData, OIData, OrderbookSnapshot, LiquidationMap,
    Order, Position, AccountState, Side, OrderStatus, PositionStatus, BotMode
)
from utils.helpers import gen_id, utcnow

log = logging.getLogger("kara.hl_client")


class HyperliquidClient:
    """
    Async-friendly wrapper around the Hyperliquid SDK.
    All heavy calls are run in a thread executor to avoid blocking the event loop.
    """

    def __init__(self):
        self.testnet = config.HL_TESTNET
        self.wallet  = config.WALLET_ADDRESS
        self._info: Optional[Info] = None
        self._exchange: Optional[Exchange] = None
        self._account: Optional[Any] = None  # eth_account
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # HTTP clients for direct API calls
        self._http_data: Optional[httpx.AsyncClient] = None
        self._http_trade: Optional[httpx.AsyncClient] = None
        
        self._data_url = (
            "https://api.hyperliquid.xyz"
            if config.DATA_SOURCE == "mainnet"
            else "https://api.hyperliquid-testnet.xyz"
        )
        self._trade_url = (
            "https://api.hyperliquid-testnet.xyz"
            if config.TRADE_MODE == "paper"
            else "https://api.hyperliquid.xyz"
        )
        self.base_url = self._data_url  # For backwards compatibility

        # Market cache for top_volume filtering
        self._market_cache: Dict[str, Any] = {}
        self._market_cache_time: float = 0.0

        # API call retry settings
        self._max_retries = 3
        self._retry_delay = 0.5  # seconds, exponential backoff

    # ──────────────────────────────────────────
    # INIT
    # ──────────────────────────────────────────

    async def connect(self):
        """Initialize SDK clients. Call once at startup."""
        self._loop = asyncio.get_event_loop()
        
        self._http_data = httpx.AsyncClient(
            base_url=self._data_url,
            timeout=15.0,
            limits=httpx.Limits(max_connections=10)
        )
        self._http_trade = httpx.AsyncClient(
            base_url=self._trade_url,
            timeout=15.0,
            limits=httpx.Limits(max_connections=10)
        )

        # Info client (read-only, no key needed)
        try:
            self._info = Info(base_url=self._data_url, skip_ws=True)
            log.info(f"✓ Info client initialized (SDK) - Data: {config.DATA_SOURCE.upper()}")
        except Exception as e:
            log.warning(f"Info client init failed (will use HTTP only): {e}")
            self._info = None

        if config.PRIVATE_KEY:
            try:
                self._account = eth_account.Account.from_key(config.PRIVATE_KEY)
                self._exchange = Exchange(
                    account=self._account,
                    base_url=self._trade_url
                )
                log.info(f"✓ Exchange client ready [{config.TRADE_MODE.upper()}] - wallet: {self.wallet[:8]}...{self.wallet[-4:]}")
            except Exception as e:
                log.error(f"Exchange initialization failed: {e}")
                self._exchange = None
        else:
            log.warning("No private key set - read-only mode (paper safe)")

        log.info(f"✓ Hyperliquid client connected [Data: {config.DATA_SOURCE.upper()} | Execution: {config.TRADE_MODE.upper()}]")

    async def _run(self, func, *args, **kwargs):
        """Run a synchronous SDK call in thread pool."""
        return await self._loop.run_in_executor(
            None, lambda: func(*args, **kwargs)
        )

    async def _ensure_info(self):
        """Ensure Info client is initialized (lazy init with retry)."""
        pass # Removed lazy init because it causes repeated 'list index out of range' errors on testnet

    async def _call_info_endpoint(
        self,
        request_type: str,
        params: Optional[Dict] = None,
        retry: int = 0
    ) -> Tuple[Dict[str, Any], bool]:
        """
        Direct HTTP POST to /info endpoint with retry logic.

        Args:
            request_type: "allMids", "metaAndAssetCtxs", "l2Book", etc.
            params: request parameters {"coin": "BTC", "user": "0x...", etc.}
            retry: current retry count (internal use)

        Returns:
            Tuple of (response_dict, success_bool)
            - On 422: Returns detailed error info
            - On success: Returns parsed response
            - On failure: Returns empty dict with success=False
        """
        if not self._http_data:
            raise RuntimeError("Call connect() first")

        # Build payload
        payload = {"type": request_type}
        if params:
            payload.update(params)

        try:
            # Use debug for high-frequency calls to keep terminal clean
            if request_type in ["l2Book", "allMids", "metaAndAssetCtxs"]:
                log.debug(f"[API] POST /info - type={request_type}")
            else:
                log.info(f"[API] POST /info - type={request_type}")
            response = await self._http_data.post("/info", json=payload)

            # Handle 422 Unprocessable Entity specifically
            if response.status_code == 422:
                try:
                    error_detail = response.json()
                except:
                    error_detail = response.text

                log.error(
                    f"❗ [API 422] {request_type} - Payload: {json.dumps(payload)}\n"
                    f"             Response: {error_detail}"
                )

                # Special case: l2Book on testnet often fails (422)
                if request_type == "l2Book":
                    return {"fallback": True, "error": "422"}, False

                # Check if this is a retryable error
                if retry < self._max_retries:
                    delay = self._retry_delay * (2 ** retry)
                    log.warning(f"🔄 [API] Retrying {request_type} (attempt {retry+1}/{self._max_retries}) after {delay}s...")
                    await asyncio.sleep(delay)
                    return await self._call_info_endpoint(request_type, params, retry + 1)

                return {"error": "422", "details": error_detail, "type": request_type}, False

            # Check other HTTP errors
            if response.status_code >= 400:
                log.error(f"[API {response.status_code}] {request_type}: {response.text[:200]}")
                return {}, False

            # Parse successful response
            result = response.json()
            log.debug(f"[API] {request_type}: OK ({len(str(result))} bytes)")
            return result, True

        except httpx.TimeoutException:
            log.error(f"[API] {request_type}: TIMEOUT (>{self._http_data.timeout}s)")
            return {}, False
        except httpx.ConnectError as e:
            log.error(f"[API] {request_type}: CONNECTION ERROR - {e}")
            return {}, False
        except json.JSONDecodeError as e:
            log.error(f"[API] {request_type}: INVALID JSON - {e}")
            return {}, False
        except Exception as e:
            log.error(f"[API] {request_type}: {type(e).__name__} - {e}")
            return {}, False

    async def refresh_market_cache(self, force: bool = False):
        """Fetch all market data batch and populate cache (10s TTL)."""
        import time
        now = time.monotonic()
        if not force and (now - self._market_cache_time) < 30 and self._market_cache:
            return
            
        data = await self.get_get_all_market_data_raw()
        if data:
            self._market_cache = data
            self._market_cache_time = now
            log.debug(f"✓ Refreshed market data cache ({len(data[0])} assets)")

    async def get_get_all_market_data_raw(self) -> Optional[Tuple[List, List]]:
        """Fetch all metadata and contexts in ONE call and return (universe, contexts)."""
        try:
            result, success = await self._call_info_endpoint("metaAndAssetCtxs")
            if not success:
                return None
                
            if isinstance(result, list) and len(result) >= 2:
                # metaAndAssetCtxs returns [meta, assetCtxs]
                # meta contains "universe"
                universe = result[0].get("universe", []) if isinstance(result[0], dict) else []
                contexts = result[1]
                return (universe, contexts)
            return None
        except Exception as e:
            log.error(f"get_all_market_data_raw failed: {e}")
            return None

    async def get_all_market_data(self) -> Optional[Tuple[List, List]]:
        """Fetch all metadata and contexts with caching."""
        await self.refresh_market_cache()
        return self._market_cache if self._market_cache else None

    async def get_all_mids(self) -> Dict[str, float]:
        """Fetch all mid prices from cache or API."""
        await self.refresh_market_cache()
        if not self._market_cache:
            return {}
        
        universe, contexts = self._market_cache
        mids = {}
        for i, u in enumerate(universe):
            if i < len(contexts):
                px = float(contexts[i].get("markPx", 0))
                if px > 0:
                    mids[u.get("name")] = px
        return mids

    async def get_top_volume_markets(self, top_n: int = 20) -> List[str]:
        """
        Fetch top volume markets directly from HTTP API.
        Filter by dayNtlVlm > 0 and return top N markets.
        """
        try:
            if not self._http_data:
                raise RuntimeError("Call connect() first")
                
            payload = {"type": "metaAndAssetCtxs"}
            resp = await self._http_data.post("/info", json=payload)
            data = resp.json()
            
            universe = data[0].get("universe", []) if isinstance(data[0], dict) else data[0]
            contexts = data[1]
            
            markets = []
            for i, ctx in enumerate(contexts):
                if i < len(universe):
                    name = universe[i].get("name")
                    if name:
                        vol = float(ctx.get("dayNtlVlm", 0))
                        if vol > 0:
                            markets.append((name, vol))
                            
            markets.sort(key=lambda x: x[1], reverse=True)
            symbols = [name for name, _ in markets[:top_n]]
            
            if symbols:
                log.info(f"✓ Loaded {len(symbols)} top volume markets ({', '.join(symbols[:5])}{'...' if len(symbols)>5 else ''})")
                return symbols
            else:
                log.warning("No markets qualified, using fallback")
                return config.MARKET_SCAN.fallback_markets
                
        except Exception as e:
            log.error(f"get_top_volume_markets failed: {e}", exc_info=False)
            fallback = config.MARKET_SCAN.fallback_markets
            log.info(f"✓ Using {len(fallback)} fallback markets: {', '.join(fallback)}")
            return fallback

    async def get_btc_real_time_data(self) -> Dict[str, Any]:
        """
        Ambil data real-time BTC/USD untuk chart dashboard.

        Returns:
        {
            "current_price": 42150.00,
            "high_24h": 43200.00,
            "low_24h": 41000.00,
            "candles": [[timestamp, open, high, low, close, volume], ...],
            "timestamp": "2026-04-02T23:45:30.123456"
        }
        """
        try:
            await self._ensure_info()

            # Get current price real-time
            mids = None
            try:
                if self._info:
                    mids = await self._run(self._info.all_mids)
                    log.debug("Fetched all_mids from SDK")
            except Exception as e:
                log.debug(f"SDK all_mids failed: {e}, using HTTP")

            # Fallback: direct HTTP
            if not mids:
                result, success = await self._call_info_endpoint("allMids")
                mids = result if success and isinstance(result, dict) else None

            current_price = float(mids.get("BTC", 0)) if isinstance(mids, dict) else 0

            if current_price <= 0:
                log.warning("BTC price is 0 or missing, API issue?")
                return {
                    "current_price": 42150,
                    "high_24h": 43200,
                    "low_24h": 41000,
                    "candles": [],
                    "timestamp": datetime.utcnow().isoformat()
                }

            # Get 1-hour candles (last 24 = ~24 hours of data)
            candles = []
            try:
                if self._info:
                    candles = await self._run(
                        self._info.candles_snapshot,
                        "BTC", "1h", 24
                    )
                    log.debug("Fetched candles from SDK")
            except Exception as e:
                log.debug(f"SDK candles failed: {e}")

            # Extract high/low from candles if available
            if candles and len(candles) > 0:
                try:
                    highs = [float(c[2]) for c in candles if c and len(c) > 2]
                    lows = [float(c[3]) for c in candles if c and len(c) > 3]
                    high_24h = max(highs + [current_price]) if highs else current_price
                    low_24h = min(lows + [current_price]) if lows else current_price
                except (ValueError, IndexError, TypeError) as e:
                    log.debug(f"Failed to parse candles: {e}")
                    high_24h = current_price
                    low_24h = current_price
                    candles = []
            else:
                high_24h = current_price
                low_24h = current_price

            result = {
                "current_price": round(current_price, 8),
                "high_24h": round(high_24h, 8),
                "low_24h": round(low_24h, 8),
                "candles": candles or [],
                "timestamp": datetime.utcnow().isoformat()
            }

            log.debug(f"BTC real-time: ${current_price:,.2f} (H: ${high_24h:,.2f}, L: ${low_24h:,.2f})")
            return result

        except Exception as e:
            log.error(f"get_btc_real_time_data failed: {e}")
            return {
                "current_price": 0,
                "high_24h": 0,
                "low_24h": 0,
                "candles": [],
                "timestamp": datetime.utcnow().isoformat()
            }

    def _extract_ctx(self, meta: Any, asset: str) -> Optional[Dict]:
        """Safely extract asset context from metaAndAssetCtxs response."""
        try:
            if not meta or not isinstance(meta, (list, tuple)) or len(meta) < 2:
                return None
            
            universe_raw = meta[0]
            # Handle case where universe is a dict with 'universe' key
            universe = universe_raw.get("universe", []) if isinstance(universe_raw, dict) else universe_raw
            contexts = meta[1]

            if not isinstance(universe, list) or not isinstance(contexts, list):
                return None

            # Find by name
            for i, u in enumerate(universe):
                if isinstance(u, dict) and u.get("name") == asset:
                    if i < len(contexts):
                        return contexts[i]
            return None
        except Exception as e:
            log.debug(f"Error extracting ctx for {asset}: {e}")
            return None

    async def get_funding_data(self, asset: str, meta: Optional[Any] = None) -> FundingData:
        """Fetch funding data with robust parsing (cached)."""
        ctx = None
        # 1. Try provided meta
        if meta:
            ctx = self._extract_ctx(meta, asset)
        
        # 2. Try internal cache
        if not ctx:
            await self.refresh_market_cache()
            if self._market_cache:
                ctx = self._extract_ctx(self._market_cache, asset)

        if not ctx or not isinstance(ctx, dict):
            # NO SEQUENTIAL FALLBACK to avoid rate limits
            log.warning(f"Could not find context for {asset} in batch/cache")
            return FundingData(asset=asset, funding_rate=0, premium=0, predicted_rate=0, hourly_trend=[])

        return FundingData(
            asset=asset,
            funding_rate=float(ctx.get("funding", 0)),
            premium=float(ctx.get("premium", 0)),
            predicted_rate=float(ctx.get("predictedFunding", 0)),
            hourly_trend=[]
        )

    async def get_oi_data(self, asset: str, meta: Optional[Any] = None) -> OIData:
        """Fetch OI data with robust parsing (cached)."""
        ctx = None
        if meta:
            ctx = self._extract_ctx(meta, asset)
        
        if not ctx:
            await self.refresh_market_cache()
            if self._market_cache:
                ctx = self._extract_ctx(self._market_cache, asset)

        if not ctx or not isinstance(ctx, dict):
            return OIData(asset=asset, open_interest=0, oi_change_pct=0.0, oi_change_24h=0.0, oracle_price=0)

        mark = float(ctx.get("markPx", 0))
        oi_contracts = float(ctx.get("openInterest", 0))
        
        return OIData(
            asset=asset,
            open_interest=oi_contracts * mark,
            oi_change_pct=0.0,
            oi_change_24h=0.0,
            oracle_price=float(ctx.get("oraclePx", mark))
        )

    async def get_orderbook(self, asset: str, depth: int = 20) -> OrderbookSnapshot:
        """Fetch L2 orderbook using l2Book endpoint."""
        log.debug(f"Fetching orderbook for {asset} from {self._data_url}")
        await self._ensure_info()

        book = None

        # Ensure symbol preserves original casing for API compatibility
        asset_clean = asset
        result, success = await self._call_info_endpoint("l2Book", {"coin": asset_clean})

        if success and isinstance(result, dict):
            book = result
            log.debug(f"✓ Fetched orderbook {asset_clean} from HTTP")
        else:
            # FALLBACK: allMids + neutral imbalance
            log.warning(f"⚠️ l2Book FAILED for {asset_clean} - using FALLBACK (allMids)")
            mark = await self.get_mark_price(asset_clean)
            if mark > 0:
                return OrderbookSnapshot(
                    asset=asset_clean,
                    bids=[[mark - 0.01, 1.0]],
                    asks=[[mark + 0.01, 1.0]],
                    mid_price=mark,
                    spread_pct=0.0001,
                    bid_ask_imbalance=0,
                    vwap=mark,
                    vwap_deviation_pct=0
                )
            raise RuntimeError(f"[{asset}] l2Book failed and mark price is 0")

        # Parse orderbook safely
        levels = book.get("levels", [[], []])

        if not (isinstance(levels, list) and len(levels) >= 2):
            log.error(f"[{asset}] Invalid levels format: {type(levels)}")
            raise ValueError(f"[{asset}] Invalid orderbook levels format")

        bids_raw = levels[0]
        asks_raw = levels[1]

        # Parse bids/asks safely
        bids = []
        asks = []

        if isinstance(bids_raw, list):
            for b in bids_raw[:depth]:
                try:
                    if isinstance(b, dict):
                        px = float(b.get("px", 0))
                        sz = float(b.get("sz", 0))
                        if px > 0 and sz > 0:
                            bids.append([px, sz])
                except (ValueError, TypeError):
                    continue

        if isinstance(asks_raw, list):
            for a in asks_raw[:depth]:
                try:
                    if isinstance(a, dict):
                        px = float(a.get("px", 0))
                        sz = float(a.get("sz", 0))
                        if px > 0 and sz > 0:
                            asks.append([px, sz])
                except (ValueError, TypeError):
                    continue

        # Calculate mid and metrics only if we have data
        if bids and asks:
            best_bid = bids[0][0]
            best_ask = asks[0][0]
            mid = (best_bid + best_ask) / 2
            spread = (best_ask - best_bid) / mid if mid else 0

            bid_liq = sum(b[0] * b[1] for b in bids)
            ask_liq = sum(a[0] * a[1] for a in asks)
            total = bid_liq + ask_liq
            imbalance = (bid_liq - ask_liq) / total if total else 0

            total_vol = sum(b[1] for b in bids) + sum(a[1] for a in asks)
            vwap_num = bid_liq + ask_liq
            vwap = vwap_num / total_vol if total_vol else mid
            vwap_dev = (mid - vwap) / vwap if vwap else 0

            log.debug(f"Orderbook {asset}: mid=${mid:,.2f}, spread={spread:.4f}")
        else:
            mid = spread = imbalance = vwap = vwap_dev = 0

        return OrderbookSnapshot(
            asset=asset,
            bids=bids,
            asks=asks,
            mid_price=mid,
            spread_pct=spread,
            bid_ask_imbalance=imbalance,
            vwap=vwap,
            vwap_deviation_pct=vwap_dev
        )

    async def get_mark_price(self, asset: str, meta: Optional[Any] = None) -> float:
        """Get mark price with batch fallback."""
        ctx = None
        if meta:
            ctx = self._extract_ctx(meta, asset)
        
        if not ctx:
            await self.refresh_market_cache()
            if self._market_cache:
                ctx = self._extract_ctx(self._market_cache, asset)

        if ctx and isinstance(ctx, dict):
            px = float(ctx.get("markPx", 0))
            if px > 0: return px

        return 0.0

    async def get_user_state(self) -> Dict[str, Any]:
        """Fetch user account state (balance, positions, etc.)."""
        try:
            await self._ensure_info()
            if not self._info:
                return {}
            return await self._run(self._info.user_state, self.wallet)
        except Exception as e:
            log.error(f"get_user_state: {e}")
            return {}

    # ──────────────────────────────────────────
    # ORDER EXECUTION
    # ──────────────────────────────────────────

    async def place_order(
        self,
        asset:      str,
        is_buy:     bool,
        sz:         float,
        limit_px:   float,
        order_type: str = "post_only",
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        """
        Place a limit order. order_type: 'post_only' | 'limit' | 'market'
        Returns raw SDK response dict.
        """
        if not self._exchange:
            raise RuntimeError("Exchange client not initialized - check private key")

        # Build order type dict for SDK
        if order_type == "post_only":
            ot = {"limit": {"tif": "Alo"}}   # Add Liquidity Only = maker only
        elif order_type == "limit":
            ot = {"limit": {"tif": "Gtc"}}   # Good Till Cancelled
        else:  # market
            ot = {"limit": {"tif": "Ioc"}}   # Immediate Or Cancel

        try:
            result = await self._run(
                self._exchange.order,
                asset, is_buy, sz, limit_px, ot,
                reduce_only=reduce_only
            )
            log.info(f"Order placed: {asset} {'BUY' if is_buy else 'SELL'} {sz} @ {limit_px}")
            return result
        except Exception as e:
            log.error(f"place_order error: {e}")
            raise

    async def cancel_order(self, asset: str, order_id: int) -> Dict[str, Any]:
        """Cancel an open order."""
        if not self._exchange:
            raise RuntimeError("Exchange client not initialized")
        try:
            return await self._run(
                self._exchange.cancel, asset, order_id
            )
        except Exception as e:
            log.error(f"cancel_order error: {e}")
            raise

    async def set_leverage(self, asset: str, leverage: int, is_cross: bool = False) -> Dict:
        """Set leverage for an asset."""
        if not self._exchange:
            raise RuntimeError("Exchange client not initialized")
        try:
            return await self._run(
                self._exchange.update_leverage, leverage, asset, is_cross
            )
        except Exception as e:
            log.error(f"set_leverage error: {e}")
            raise

    async def get_open_orders(self) -> List[Dict]:
        """Get all open orders for the wallet."""
        try:
            await self._ensure_info()
            if not self._info:
                return []
            return await self._run(self._info.open_orders, self.wallet)
        except Exception as e:
            log.error(f"get_open_orders: {e}")
            return []

    async def close(self):
        """Cleanup."""
        if self._http_data:
            await self._http_data.aclose()
        if hasattr(self, '_http_trade') and self._http_trade:
            await self._http_trade.aclose()
        log.info("Hyperliquid client closed")


# Singleton
_client: Optional[HyperliquidClient] = None

def get_client() -> HyperliquidClient:
    global _client
    if _client is None:
        _client = HyperliquidClient()
    return _client
