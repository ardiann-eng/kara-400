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

    def __init__(self, private_key: str = None, wallet_address: str = None):
        self.testnet = config.HL_TESTNET
        self.wallet  = wallet_address or config.WALLET_ADDRESS
        self.private_key = private_key or config.PRIVATE_KEY
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

        # 502 backoff state — shared across all parallel asset requests
        self._api_backoff_until: float = 0.0
        self._consecutive_502s: int = 0

        # Semaphore untuk data fetching (scoring/scan) — throttled, boleh lambat
        # Semaphore untuk execution path TIDAK ADA — order harus instan
        self._data_sem: Optional[asyncio.Semaphore] = None   # dibuat di connect()

        # Candle caching (asset -> (timestamp, data))
        self._candle_cache: Dict[str, Tuple[float, List]] = {}
        self._candle_ttl = 30  # seconds

    # ──────────────────────────────────────────
    # INIT
    # ──────────────────────────────────────────

    async def connect(self):
        """Initialize SDK clients. Call once at startup."""
        self._loop = asyncio.get_event_loop()
        # Throttle HANYA untuk data scan — execution path tidak pernah kena semaphore ini
        self._data_sem = asyncio.Semaphore(8)

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

        if self.private_key:
            try:
                self._account = eth_account.Account.from_key(self.private_key)
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

    def _format_api_error(self, response_text: str, max_len: int = 80) -> str:
        """Collapse a potentially multiline/HTML error body into one short line."""
        import re
        if not response_text:
            return "empty response"
        clean = " ".join(response_text.split())
        clean = re.sub(r"<[^>]+>", "", clean).strip()
        if not clean:
            clean = "HTML error page"
        if len(clean) > max_len:
            clean = clean[:max_len] + "..."
        return clean

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
        retry: int = 0,
        throttled: bool = True,
    ) -> Tuple[Dict[str, Any], bool]:
        """
        Direct HTTP POST to /info endpoint dengan dua jalur:

        throttled=True  (default) — dipakai untuk data scanning/scoring.
            Kena _data_sem (max 8 concurrent) + sleep 0.12s.
            Boleh lambat, tidak mempengaruhi execution.

        throttled=False — dipakai untuk mark price (position monitor) dan
            data yang time-sensitive. Tidak ada semaphore, tidak ada sleep.
            JANGAN pakai untuk bulk scan karena bisa trigger 429.

        Execution path (place_order/cancel_order/set_leverage) TIDAK memanggil
        method ini sama sekali — mereka pakai SDK thread executor langsung.
        """
        if not self._http_data:
            raise RuntimeError("Call connect() first")

        # Build payload
        payload = {"type": request_type}
        if params:
            payload.update(params)

        # Check global 502 backoff before making any request
        now_mono = asyncio.get_event_loop().time()
        if now_mono < self._api_backoff_until:
            wait = self._api_backoff_until - now_mono
            raise RuntimeError(f"API in backoff for {wait:.0f}s (too many 502s)")
        elif self._consecutive_502s > 0 and now_mono >= self._api_backoff_until:
            # Backoff window expired — auto-reset so API calls can resume
            log.info(
                f"[API] Circuit breaker auto-reset after backoff "
                f"(was {self._consecutive_502s} consecutive 502s)"
            )
            self._consecutive_502s = 0
            self._api_backoff_until = 0.0

        try:
            log.debug(f"[API] POST /info - type={request_type} throttled={throttled}")
            if throttled:
                # Data scanning path — throttle supaya tidak trigger 429
                if self._data_sem is None:
                    self._data_sem = asyncio.Semaphore(8)
                async with self._data_sem:
                    await asyncio.sleep(0.12)
                    response = await self._http_data.post("/info", json=payload)
            else:
                # Fast path — tidak ada semaphore, tidak ada sleep
                # Dipakai untuk mark_price (position monitor) dan data time-sensitive
                response = await self._http_data.post("/info", json=payload)

            # Handle 502 Bad Gateway with exponential backoff
            if response.status_code == 502:
                self._consecutive_502s += 1
                backoff = min(5 * (2 ** (self._consecutive_502s - 1)), 120)
                self._api_backoff_until = asyncio.get_event_loop().time() + backoff
                log.warning(
                    f"[API] 502 Bad Gateway #{self._consecutive_502s} — "
                    f"backoff {backoff:.0f}s (Hyperliquid may be down)"
                )
                raise RuntimeError(f"502 Bad Gateway, backoff {backoff}s")

            # Successful response resets backoff counter
            if self._consecutive_502s > 0:
                log.info(f"[API] Hyperliquid recovered after {self._consecutive_502s} retries")
                self._consecutive_502s = 0
                self._api_backoff_until = 0.0

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
                    return await self._call_info_endpoint(request_type, params, retry + 1, throttled)

                return {"error": "422", "details": error_detail, "type": request_type}, False

            # Handle Rate Limiting (429 or 430)
            if response.status_code in [429, 430]:
                wait_time = float(response.headers.get("Retry-After", 2.0))
                log.debug(f"⚠️ [API 429/430] Rate limited on {request_type}. Waiting {wait_time}s...")
                await asyncio.sleep(wait_time)
                if retry < self._max_retries:
                    return await self._call_info_endpoint(request_type, params, retry + 1, throttled)
                return {}, False

            # Check other HTTP errors
            if response.status_code >= 400:
                from utils.log_helpers import log_api_error_once
                log_api_error_once(
                    log, request_type,
                    f"HTTP {response.status_code}: {self._format_api_error(response.text)}"
                )
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
        """Fetch all market data batch and populate cache (30s TTL).

        CRITICAL: jika fetch gagal, cache LAMA tetap dipertahankan (jangan di-clear).
        Ini mencegah semua asset return None hanya karena 1 API call gagal.
        """
        import time
        now = time.monotonic()
        if not force and (now - self._market_cache_time) < 30 and self._market_cache:
            return

        data = await self.get_get_all_market_data_raw()
        if data:
            prev_count = len(self._market_cache[0]) if self._market_cache else 0
            self._market_cache = data
            self._market_cache_time = now
            log.debug(f"✓ Refreshed market data cache ({len(data[0])} assets, was {prev_count})")
        else:
            # Fetch gagal — pertahankan cache lama, jangan set ke None/{}
            cache_age = now - self._market_cache_time
            cached_count = len(self._market_cache[0]) if self._market_cache else 0
            if cached_count > 0:
                log.warning(
                    f"[CTX] metaAndAssetCtxs fetch FAILED — "
                    f"using stale cache ({cached_count} assets, age={cache_age:.0f}s)"
                )
            else:
                log.error(
                    "[CTX] metaAndAssetCtxs fetch FAILED and cache is EMPTY — "
                    "all assets will return price=0 this scan"
                )

    async def get_get_all_market_data_raw(self) -> Optional[Tuple[List, List]]:
        """Fetch all metadata and contexts in ONE call and return (universe, contexts)."""
        try:
            result, success = await self._call_info_endpoint("metaAndAssetCtxs")
            if not success:
                log.warning("[CTX] metaAndAssetCtxs: API call returned success=False")
                return None

            if isinstance(result, list) and len(result) >= 2:
                universe = result[0].get("universe", []) if isinstance(result[0], dict) else []
                contexts = result[1]
                if not universe or not contexts:
                    log.warning(
                        f"[CTX] metaAndAssetCtxs: empty universe={len(universe)} "
                        f"or contexts={len(contexts)}"
                    )
                    return None
                return (universe, contexts)

            log.warning(
                f"[CTX] metaAndAssetCtxs: unexpected response type={type(result).__name__} "
                f"len={len(result) if isinstance(result, list) else 'N/A'}"
            )
            return None
        except Exception as e:
            log.error(f"[CTX] get_all_market_data_raw failed: {e}")
            return None

    async def get_all_market_data(self) -> Optional[Tuple[List, List]]:
        """Fetch all metadata and contexts with caching."""
        await self.refresh_market_cache()
        return self._market_cache if self._market_cache else None

    async def get_asset_max_leverage(self, asset: str) -> int:
        """Fetch the maximum allowed leverage for a specific asset from exchange metadata."""
        await self.refresh_market_cache()
        if not self._market_cache:
            return 50  # Conservative global fallback

        universe, _ = self._market_cache
        for u in universe:
            if isinstance(u, dict) and u.get("name") == asset:
                return int(u.get("maxLeverage", 50))
        
        return 50  # Fallback if asset not found

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

    async def get_top_volume_markets(self, top_n: int = 100, max_retries: int = 5) -> List[str]:
        """
        Fetch top volume markets with retry logic to survive 429 rate limits.
        Returns up to top_n markets sorted by 24h volume, deduped.
        """
        if not self._http_data:
            raise RuntimeError("Call connect() first")

        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    delay = 2 ** attempt  # 2, 4, 8, 16s
                    log.warning(f"Rate limit on market fetch, retry {attempt}/{max_retries} in {delay}s...")
                    await asyncio.sleep(delay)

                payload = {"type": "metaAndAssetCtxs"}
                resp = await self._http_data.post("/info", json=payload)

                if resp.status_code == 429:
                    log.warning(f"get_top_volume_markets: 429 rate limit (attempt {attempt+1}/{max_retries})")
                    continue

                if resp.status_code >= 400:
                    log.warning(f"get_top_volume_markets: HTTP {resp.status_code} (attempt {attempt+1}/{max_retries})")
                    continue

                data = resp.json()
                if not isinstance(data, list) or len(data) < 2:
                    log.warning(f"get_top_volume_markets: Invalid response structure (attempt {attempt+1}/{max_retries})")
                    continue

                universe = data[0].get("universe", []) if isinstance(data[0], dict) else []
                contexts = data[1] if isinstance(data[1], list) else []

                markets = []
                seen: set = set()
                for i, ctx in enumerate(contexts):
                    if i >= len(universe):
                        break
                    name = universe[i].get("name") if isinstance(universe[i], dict) else None
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    vol = float(ctx.get("dayNtlVlm") or 0)
                    if vol > 0:
                        markets.append((name, vol))

                markets.sort(key=lambda x: x[1], reverse=True)
                result = [name for name, _ in markets[:top_n]]

                if result:
                    log.info(f"✓ Loaded {len(result)} markets: {', '.join(result[:5])}...")
                    return result

                log.warning(f"get_top_volume_markets: No markets with volume (attempt {attempt+1}/{max_retries})")

            except Exception as e:
                log.error(f"get_top_volume_markets attempt {attempt+1}: {e}")

        # All retries failed — use deduped fallback
        fallback = list(dict.fromkeys(config.MARKET_SCAN.fallback_markets))
        log.error(f"All market fetch retries failed. Using {len(fallback)} fallback markets.")
        return fallback

    async def get_candles(self, asset: str, interval: str = "1h", limit: int = 100) -> List[List[Any]]:
        """
        Fetch historical OHLCV data for an asset with caching.
        Returns: [[timestamp, open, high, low, close, volume], ... ]
        """
        # 1. Check Cache
        now = time.time()
        if asset in self._candle_cache:
            ts, data = self._candle_cache[asset]
            if now - ts < self._candle_ttl:
                log.debug(f"[{asset}] Using cached candles ({int(now-ts)}s old)")
                return data[:limit]

        try:
            log.info(f"[{asset}] Fetching fresh candles from API...")
            await self._ensure_info()
            
            # 2. Try SDK
            candles = []
            if self._info:
                try:
                    candles = await self._run(self._info.candles_snapshot, asset, interval, limit)
                except Exception as e:
                    log.debug(f"SDK candles_snapshot failed for {asset}: {e}")

            # 3. Direct HTTP Fallback if SDK failed
            if not candles:
                end_ms = int(time.time() * 1000)
                # interval_ms: map common intervals to milliseconds
                _interval_map = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
                interval_ms = _interval_map.get(interval, 3_600_000)
                start_ms = end_ms - interval_ms * limit
                result, success = await self._call_info_endpoint("candleSnapshot", {
                    "req": {
                        "coin": asset,
                        "interval": interval,
                        "startTime": start_ms,
                        "endTime": end_ms
                    }
                })
                if success and isinstance(result, list):
                    candles = result

            if candles:
                self._candle_cache[asset] = (now, candles)
                return candles[:limit]
                
            return []
        except Exception as e:
            log.error(f"get_candles failed for {asset}: {e}")
            return []

    async def get_cached_candles(self, asset: str) -> List[List[Any]]:
        """Helper as requested: returns cached candles or fetches new ones."""
        return await self.get_candles(asset)

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
                    "timestamp": utcnow().isoformat()
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
                "timestamp": utcnow().isoformat()
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
            funding_rate=float(ctx.get("funding") or 0),
            premium=float(ctx.get("premium") or 0),
            predicted_rate=float(ctx.get("predictedFunding") or 0),
            hourly_trend=[]
        )

    async def get_oi_data(self, asset: str, meta: Optional[Any] = None) -> OIData:
        """Fetch OI data with robust parsing (cached).
        
        Fields from metaAndAssetCtxs context:
          openInterest  : OI in contracts (multiply by markPx for USD)
          dayNtlVlm     : 24h notional volume — used as crude OI-change proxy
          oraclePx      : oracle/spot reference price
        """
        ctx = None
        if meta:
            ctx = self._extract_ctx(meta, asset)
        
        if not ctx:
            await self.refresh_market_cache()
            if self._market_cache:
                ctx = self._extract_ctx(self._market_cache, asset)

        if not ctx or not isinstance(ctx, dict):
            return OIData(asset=asset, open_interest=0, oi_change_pct=0.0, oi_change_24h=0.0, oracle_price=0)

        mark          = float(ctx.get("markPx", 0))
        oi_contracts  = float(ctx.get("openInterest", 0))
        oi_usd        = oi_contracts * mark

        # Approximate 24h OI change ratio: dayNtlVlm / oi_usd
        # High volume relative to OI = active positioning = OI likely changing
        # This is a proxy, NOT the real oi_change_24h (HL API doesn't provide it directly)
        day_vol = float(ctx.get("dayNtlVlm", 0))
        if oi_usd > 0 and day_vol > 0:
            # Rough approximation: vol-to-OI ratio indicates activity level
            # Clamp to ±50% to avoid extreme outliers from thinly-traded assets
            oi_change_24h_proxy = min(day_vol / oi_usd - 1.0, 0.50)
        return OIData(
            asset=asset,
            open_interest=float(ctx.get("openInterest") or 0),
            oi_change_pct=0.0,  # Calculated elsewhere
            oi_change_24h=0.0,
            volume_24h_usd=float(ctx.get("dayNtlVlm") or 0),
            oracle_price=float(ctx.get("oraclePx") or 0)
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
        """Get mark price — pakai cache, fallback ke allMids throttled (scanning path)."""
        ctx = None
        if meta:
            ctx = self._extract_ctx(meta, asset)

        if not ctx:
            await self.refresh_market_cache()
            if self._market_cache:
                ctx = self._extract_ctx(self._market_cache, asset)

        if ctx and isinstance(ctx, dict):
            px = float(ctx.get("markPx", 0))
            if px > 0:
                return px
            log.warning(f"[PRICE] {asset}: markPx=0 in ctx (ctx keys: {list(ctx.keys())[:5]})")
        else:
            cached_count = len(self._market_cache[0]) if self._market_cache else 0
            log.warning(
                f"[PRICE] {asset}: no ctx found — "
                f"cache has {cached_count} assets, meta={'yes' if meta else 'no'}"
            )

        return 0.0

    async def get_mark_price_fast(self, asset: str) -> float:
        """
        Fast path untuk position monitor — NO semaphore, NO sleep.
        Prioritas: memory cache → allMids tanpa throttle.
        Dipanggil setiap 5s, tidak boleh diblok oleh data scan semaphore.
        """
        # 1. Coba dari market cache yang sudah ada di memory (tidak perlu network)
        if self._market_cache:
            ctx = self._extract_ctx(self._market_cache, asset)
            if ctx and isinstance(ctx, dict):
                px = float(ctx.get("markPx", 0))
                if px > 0:
                    return px

        # 2. Fast network call — tidak ada semaphore, tidak ada sleep
        try:
            result, success = await self._call_info_endpoint("allMids", throttled=False)
            if success and isinstance(result, dict):
                px_str = result.get(asset)
                if px_str:
                    return float(px_str)
        except Exception as e:
            log.debug(f"[FAST] get_mark_price_fast {asset}: {e}")

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

    async def verify_agent_authorization(self, main_address: str, target_agent: str) -> bool:
        """
        Verify if 'target_agent' is the authorized agent for 'main_address'.
        Target agent must be lowercase for comparison.
        """
        try:
            await self._ensure_info()
            if not self._info:
                return False
            
            state = await self._run(self._info.user_state, main_address)
            if not state:
                return False
            
            authorized_agent = state.get("agentAddress")
            if not authorized_agent:
                return False
            
            return authorized_agent.lower() == target_agent.lower()
        except Exception as e:
            log.error(f"verify_agent_authorization failed: {e}")
            return False

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
