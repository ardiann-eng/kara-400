"""Async Bybit V5 client for USDT linear execution."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Optional
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlencode

import aiohttp

from execution.exchange_client import (
    ExecutionClient,
    ExecutionOrderStatus,
    InstrumentSpec,
    VenueAccount,
    VenueOrder,
    VenuePosition,
)
from execution.symbol_registry import BybitSymbolRegistry
from execution.live_risk_gate import ExecutionQuote
from models.schemas import Side
from core.startup_validation import BybitPreflightResult


log = logging.getLogger("kara.bybit_client")


class BybitError(RuntimeError):
    pass


class BybitAPIError(BybitError):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"Bybit API error {code}: {message}")


class BybitAmbiguousOrderError(BybitError):
    """Order request outcome unknown; reconcile by client order ID before retry."""

    def __init__(self, client_order_id: str):
        self.client_order_id = client_order_id
        super().__init__(
            f"Bybit order outcome unknown for orderLinkId={client_order_id}"
        )


class BybitClient(ExecutionClient):
    MAINNET_URL = "https://api.bybit.com"
    TESTNET_URL = "https://api-testnet.bybit.com"
    DEMO_URL = "https://api-demo.bybit.com"
    LEVERAGE_UNCHANGED_CODE = 110043
    PROTECTION_UNCHANGED_CODE = 34040
    MAX_ORDER_LINK_ID_LENGTH = 45
    DEMO_APPLY_MONEY_MAX_USDT = Decimal("100000")
    # Bybit Demo currently rejects USDT demo-fund amounts with more than one
    # decimal place (resultCode 3410020), despite outer retCode=0.
    DEMO_USDT_AMOUNT_STEP = Decimal("0.1")
    DEMO_BALANCE_TOLERANCE = Decimal("0.05")
    DEMO_BALANCE_READBACK_ATTEMPTS = 6
    DEMO_BALANCE_READBACK_INTERVAL_SEC = 1.0

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        demo: bool = False,
        recv_window: int = 5000,
        session: Optional[aiohttp.ClientSession] = None,
        telemetry=None,
    ):
        self.api_key = api_key
        self._api_secret = api_secret
        if demo and testnet:
            raise ValueError("Bybit demo and testnet cannot both be enabled")
        self.testnet = testnet
        self.demo = demo
        self.recv_window = recv_window
        self.base_url = (
            self.DEMO_URL if demo else self.TESTNET_URL if testnet else self.MAINNET_URL
        )
        self._session = session
        self._owns_session = session is None
        self._clock_offset_ms = 0
        self._registry = BybitSymbolRegistry()
        self.telemetry = telemetry

    async def connect(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _json_body(body: Optional[Dict[str, Any]]) -> str:
        return json.dumps(body or {}, separators=(",", ":"), ensure_ascii=True)

    @staticmethod
    def _query_string(params: Optional[Dict[str, Any]]) -> str:
        pairs = sorted(
            (str(key), str(value))
            for key, value in (params or {}).items()
            if value is not None
        )
        return urlencode(pairs)

    def _timestamp(self) -> str:
        return str(int(time.time() * 1000) + self._clock_offset_ms)

    def _signature(self, timestamp: str, payload: str) -> str:
        raw = f"{timestamp}{self.api_key}{self.recv_window}{payload}"
        return hmac.new(
            self._api_secret.encode("utf-8"),
            raw.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _auth_headers(self, timestamp: str, payload: str) -> Dict[str, str]:
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": str(self.recv_window),
            "X-BAPI-SIGN": self._signature(timestamp, payload),
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        auth: bool = False,
        retries: int = 2,
        ambiguous_order_id: Optional[str] = None,
        response_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        request_started_at = time.monotonic()
        await self.connect()
        method = method.upper()
        query = self._query_string(params)
        body_text = self._json_body(body)
        payload = query if method == "GET" else body_text
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        attempts = retries + 1 if method == "GET" else 1
        for attempt in range(attempts):
            timestamp = self._timestamp()
            headers = (
                self._auth_headers(timestamp, payload)
                if auth
                else {"Content-Type": "application/json"}
            )
            try:
                async with self._session.request(
                    method,
                    url,
                    data=body_text if method != "GET" else None,
                    headers=headers,
                ) as response:
                    try:
                        data = await response.json()
                    except (json.JSONDecodeError, aiohttp.ContentTypeError) as exc:
                        raise BybitError(
                            f"Bybit returned non-JSON HTTP {response.status}"
                        ) from exc

                    if response.status >= 500 and method == "GET" and attempt < retries:
                        await asyncio.sleep(0.25 * (attempt + 1))
                        continue
                    if response.status >= 400:
                        if self.telemetry:
                            self.telemetry.record_rest_error(request_started_at)
                        raise BybitError(f"Bybit HTTP error {response.status}")

                    code = int(data.get("retCode", -1))
                    if code == 0:
                        if response_metadata is not None:
                            response_metadata.update({
                                "http_status": response.status,
                                "ret_code": code,
                                "ret_msg": str(data.get("retMsg", "")),
                                "ret_ext_info": data.get("retExtInfo") or {},
                                "result": data.get("result") or {},
                                "server_time_ms": data.get("time"),
                                "trace_id": response.headers.get("Traceid", ""),
                                "rate_limit_status": response.headers.get(
                                    "X-Bapi-Limit-Status", ""
                                ),
                                "rate_limit_reset_timestamp": response.headers.get(
                                    "X-Bapi-Limit-Reset-Timestamp", ""
                                ),
                                "response_elapsed_ms": round(
                                    (time.monotonic() - request_started_at) * 1000, 1
                                ),
                            })
                        if self.telemetry:
                            self.telemetry.record_rest_success(request_started_at)
                        return data.get("result") or {}
                    if code == 10006 and method == "GET" and attempt < retries:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    if self.telemetry:
                        self.telemetry.record_rest_error(request_started_at)
                    raise BybitAPIError(code, str(data.get("retMsg", "Unknown error")))
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if ambiguous_order_id:
                    raise BybitAmbiguousOrderError(ambiguous_order_id) from exc
                if method == "GET" and attempt < retries:
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                if self.telemetry:
                    self.telemetry.record_rest_error(request_started_at)
                raise BybitError(f"Bybit request failed: {type(exc).__name__}") from exc

        raise BybitError("Bybit request exhausted retries")

    async def sync_clock(self) -> None:
        result = await self._request("GET", "/v5/market/time", retries=2)
        server_ms = int(result.get("timeNano", 0)) // 1_000_000
        if server_ms <= 0:
            server_ms = int(result.get("timeSecond", 0)) * 1000
        if server_ms <= 0:
            raise BybitError("Bybit server time missing")
        self._clock_offset_ms = server_ms - int(time.time() * 1000)

    async def load_instruments(self) -> int:
        instruments = []
        cursor = None
        while True:
            result = await self._request(
                "GET",
                "/v5/market/instruments-info",
                params={"category": "linear", "limit": 1000, "cursor": cursor},
            )
            instruments.extend(result.get("list") or [])
            cursor = result.get("nextPageCursor") or None
            if not cursor:
                break
        self._registry.load(instruments)
        return len(instruments)

    async def get_instrument(self, asset: str) -> InstrumentSpec:
        try:
            return self._registry.resolve(asset)
        except ValueError:
            await self.load_instruments()
            return self._registry.resolve(asset)

    async def get_mark_price(self, symbol: str) -> float:
        result = await self._request(
            "GET",
            "/v5/market/tickers",
            params={"category": "linear", "symbol": symbol},
        )
        rows = result.get("list") or []
        return float(rows[0].get("markPrice", 0)) if rows else 0.0

    async def get_execution_quote(
        self, symbol: str, side: Side, quantity: float
    ) -> ExecutionQuote:
        if quantity <= 0:
            raise ValueError("Execution quote quantity must be positive")
        ticker, book = await asyncio.gather(
            self._request(
                "GET",
                "/v5/market/tickers",
                params={"category": "linear", "symbol": symbol},
            ),
            self._request(
                "GET",
                "/v5/market/orderbook",
                params={"category": "linear", "symbol": symbol, "limit": 50},
            ),
        )
        ticker_rows = ticker.get("list") or []
        mark_price = float(ticker_rows[0].get("markPrice", 0) or 0) if ticker_rows else 0
        bids = book.get("b") or []
        asks = book.get("a") or []
        if mark_price <= 0 or not bids or not asks:
            raise BybitError("Bybit execution quote missing mark price or orderbook")
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2
        if min(best_bid, best_ask, mid) <= 0 or best_ask < best_bid:
            raise BybitError("Bybit execution quote has invalid bid/ask")

        levels = asks if side == Side.LONG else bids
        remaining = quantity
        filled = 0.0
        quote_notional = 0.0
        available = sum(
            float(level[1])
            for level in levels
            if len(level) >= 2 and float(level[0]) > 0 and float(level[1]) > 0
        )
        for raw_price, raw_size, *_ in levels:
            price = float(raw_price)
            size = float(raw_size)
            if price <= 0 or size <= 0:
                continue
            take = min(size, remaining)
            quote_notional += take * price
            filled += take
            remaining -= take
            if remaining <= 1e-12:
                break
        if filled <= 0:
            raise BybitError("Bybit execution quote has no usable depth")
        estimated_fill = quote_notional / filled
        return ExecutionQuote(
            symbol=symbol,
            mark_price=mark_price,
            best_bid=best_bid,
            best_ask=best_ask,
            spread_pct=(best_ask - best_bid) / mid,
            estimated_fill_price=estimated_fill,
            estimated_slippage_pct=abs(estimated_fill - mid) / mid,
            available_quantity=available,
            received_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
        )

    async def get_account(self) -> VenueAccount:
        result = await self._request(
            "GET",
            "/v5/account/wallet-balance",
            params={"accountType": "UNIFIED", "coin": "USDT"},
            auth=True,
        )
        rows = result.get("list") or []
        if not rows:
            raise BybitError("Bybit account response empty")
        account = rows[0]
        coins = account.get("coin") or []
        usdt = next((coin for coin in coins if coin.get("coin") == "USDT"), {})
        return VenueAccount(
            total_equity=float(account.get("totalEquity", 0) or 0),
            wallet_balance=float(usdt.get("walletBalance", 0) or 0),
            available_balance=float(account.get("totalAvailableBalance", 0) or 0),
            used_margin=float(account.get("totalInitialMargin", 0) or 0),
            unrealized_pnl=float(account.get("totalPerpUPL", 0) or 0),
        )

    @staticmethod
    def _demo_funding_root_cause_classification(
        *,
        transaction_rows: List[Dict[str, Any]],
        transaction_log_error: str,
        requested_at_ms: int,
        amount: Decimal,
        adjustment: str,
    ) -> tuple[str, str]:
        """Classify only observable wallet/ledger evidence; never infer Bybit internals."""
        if transaction_log_error:
            return (
                "insufficient_evidence_transaction_log_unavailable",
                "Wallet did not change; Bybit transaction log could not be read",
            )
        relevant_rows = [
            row for row in transaction_rows
            if str(row.get("currency", "")).upper() == "USDT"
            and int(str(row.get("transactionTime", "0") or "0")) >= requested_at_ms - 60_000
        ]
        deltas = []
        for row in relevant_rows:
            try:
                deltas.append(
                    Decimal(str(row.get("change", "0") or "0"))
                    + Decimal(str(row.get("bonusChange", "0") or "0"))
                )
            except (InvalidOperation, ValueError):
                continue
        expected_sign = 1 if adjustment == "add" else -1
        matching_direction = any(value * expected_sign > 0 for value in deltas)
        matching_amount = any(
            abs(abs(value) - amount) <= BybitClient.DEMO_BALANCE_TOLERANCE
            for value in deltas
        )
        if matching_amount:
            return (
                "ledger_records_requested_demo_fund_wallet_readback_stale",
                "Bybit ledger records requested USDT change but wallet did not update in readback window",
            )
        if matching_direction:
            return (
                "ledger_records_different_usdt_change",
                "Bybit ledger records USDT change with requested direction but different amount",
            )
        if relevant_rows:
            return (
                "ledger_has_no_requested_demo_fund_credit",
                "Bybit ledger has USDT activity but no matching requested fund change",
            )
        return (
            "bybit_accepted_fund_request_without_usdt_ledger_record",
            "Bybit accepted request but no matching USDT transaction record or wallet change appeared",
        )

    async def set_demo_usdt_balance(self, target_usdt: Decimal | str | float) -> VenueAccount:
        """Set Demo USDT wallet balance with one documented add or reduce request."""
        if not self.demo or self.testnet:
            raise BybitError("Demo virtual fund endpoint is permitted only for Bybit Demo")
        try:
            target = Decimal(str(target_usdt))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("Demo USDT target must be a positive decimal") from exc
        if not target.is_finite() or target <= 0 or target > self.DEMO_APPLY_MONEY_MAX_USDT:
            raise ValueError("Demo USDT target is outside Bybit permitted range")
        before = await self.get_account()
        current = Decimal(str(before.wallet_balance))
        delta = target - current
        if abs(delta) <= self.DEMO_BALANCE_TOLERANCE:
            return before
        amount = abs(delta).quantize(
            self.DEMO_USDT_AMOUNT_STEP, rounding=ROUND_HALF_UP
        )
        funding_response: Dict[str, Any] = {}
        funding_requested_at_ms = int(time.time() * 1000)
        await self._request(
            "POST",
            "/v5/account/demo-apply-money",
            body={
                "adjustType": 0 if delta > 0 else 1,
                "utaDemoApplyMoney": [{
                    "coin": "USDT",
                    "amountStr": format(amount, "f"),
                }],
            },
            auth=True,
            retries=0,
            response_metadata=funding_response,
        )
        funding_result = funding_response.get("result") or {}
        if str(funding_result.get("orderStatus", "")).upper() == "FAIL":
            result_code = funding_result.get("resultCode", "unknown")
            result_message = str(funding_result.get("retMsg", "Unknown error"))
            log.warning(
                "DEMO_FUNDING_REJECTED result_code=%s result_message=%s trace_id=%s",
                result_code,
                result_message,
                funding_response.get("trace_id", ""),
            )
            raise BybitError(
                f"Bybit Demo funding rejected: resultCode={result_code} {result_message}"
            )
        # Bybit accepts fund requests before the wallet endpoint reflects them.
        # Polling only reads the wallet; it never repeats the rate-limited request.
        readbacks = []
        for attempt in range(self.DEMO_BALANCE_READBACK_ATTEMPTS):
            after = await self.get_account()
            wallet_balance = Decimal(str(after.wallet_balance))
            readbacks.append(wallet_balance)
            if abs(wallet_balance - target) <= self.DEMO_BALANCE_TOLERANCE:
                return after
            if attempt < self.DEMO_BALANCE_READBACK_ATTEMPTS - 1:
                await asyncio.sleep(self.DEMO_BALANCE_READBACK_INTERVAL_SEC)
        positions = await self.get_positions()
        transaction_log: Dict[str, Any] = {}
        transaction_log_error = ""
        try:
            transaction_log = await self._request(
                "GET",
                "/v5/account/transaction-log",
                params={
                    "accountType": "UNIFIED",
                    "currency": "USDT",
                    "startTime": funding_requested_at_ms - 60_000,
                    "endTime": int(time.time() * 1000),
                    "limit": 10,
                },
                auth=True,
            )
        except BybitError as exc:
            transaction_log_error = f"{type(exc).__name__}: {exc}"
        log.warning(
            "DEMO_WALLET_READBACK_MISMATCH target_usdt=%s before_wallet_usdt=%s "
            "before_equity_usdt=%s before_available_usdt=%s before_used_margin_usdt=%s "
            "readback_wallet_usdt=%s final_equity_usdt=%s final_available_usdt=%s "
            "final_used_margin_usdt=%s open_position_count=%s adjustment=%s amount_usdt=%s",
            target,
            before.wallet_balance,
            before.total_equity,
            before.available_balance,
            before.used_margin,
            ",".join(format(value, "f") for value in readbacks),
            after.total_equity,
            after.available_balance,
            after.used_margin,
            len(positions),
            "add" if delta > 0 else "reduce",
            amount,
        )
        transaction_rows = transaction_log.get("list") or []
        safe_transaction_rows = [
            {
                key: row.get(key, "")
                for key in (
                    "transactionTime", "type", "transSubType", "currency", "change",
                    "cashFlow", "funding", "fee", "bonusChange", "cashBalance",
                )
            }
            for row in transaction_rows[:10]
            if isinstance(row, dict)
        ]
        root_cause_code, root_cause_evidence = self._demo_funding_root_cause_classification(
            transaction_rows=transaction_rows,
            transaction_log_error=transaction_log_error,
            requested_at_ms=funding_requested_at_ms,
            amount=amount,
            adjustment="add" if delta > 0 else "reduce",
        )
        log.warning(
            "DEMO_FUNDING_RESPONSE endpoint=/v5/account/demo-apply-money "
            "http_status=%s ret_code=%s ret_msg=%s ret_ext_info=%s result=%s "
            "server_time_ms=%s trace_id=%s rate_limit_status=%s "
            "rate_limit_reset_timestamp=%s response_elapsed_ms=%s",
            funding_response.get("http_status"),
            funding_response.get("ret_code"),
            funding_response.get("ret_msg"),
            json.dumps(funding_response.get("ret_ext_info") or {}, sort_keys=True),
            json.dumps(funding_response.get("result") or {}, sort_keys=True),
            funding_response.get("server_time_ms"),
            funding_response.get("trace_id"),
            funding_response.get("rate_limit_status"),
            funding_response.get("rate_limit_reset_timestamp"),
            funding_response.get("response_elapsed_ms"),
        )
        log.warning(
            "DEMO_FUNDING_TRANSACTION_LOG requested_at_ms=%s row_count=%s rows=%s error=%s",
            funding_requested_at_ms,
            len(safe_transaction_rows),
            json.dumps(safe_transaction_rows, sort_keys=True),
            transaction_log_error,
        )
        log.warning(
            "DEMO_FUNDING_ROOT_CAUSE classification=%s evidence=%s",
            root_cause_code,
            root_cause_evidence,
        )
        raise BybitError("Demo wallet readback does not match requested capital")

    async def apply_demo_money(self, amount_usdt: Decimal | str | float) -> VenueAccount:
        """Compatibility wrapper: add virtual USDT to current Demo wallet."""
        before = await self.get_account()
        return await self.set_demo_usdt_balance(
            Decimal(str(before.wallet_balance)) + Decimal(str(amount_usdt))
        )

    async def preflight(self) -> BybitPreflightResult:
        account = await self.get_account()
        raw_positions = await self._request(
            "GET",
            "/v5/position/list",
            params={"category": "linear", "settleCoin": "USDT"},
            auth=True,
        )
        position_rows = raw_positions.get("list") or []
        one_way = all(int(row.get("positionIdx", 0) or 0) == 0 for row in position_rows)
        if self.demo:
            # Demo API does not support /v5/user/query-api. Account and position
            # access prove this key is accepted; order permission is checked by its
            # explicitly confirmed, smallest-size order in the drill.
            can_trade_contracts = True
            withdrawal_enabled = None
        else:
            api_info = await self._request(
                "GET", "/v5/user/query-api", auth=True
            )
            permissions = api_info.get("permissions") or {}
            can_trade_contracts = bool(permissions.get("ContractTrade") or [])
            withdrawal_enabled = bool(permissions.get("Withdraw") or [])
        return BybitPreflightResult(
            credentials_valid=True,
            can_read_account=True,
            can_trade_contracts=can_trade_contracts,
            withdrawal_enabled=withdrawal_enabled,
            account_type="UNIFIED",
            position_mode="one_way" if one_way else "hedge",
            testnet=self.testnet,
            available_usdt=account.available_balance,
        )

    @property
    def symbol_registry(self) -> BybitSymbolRegistry:
        return self._registry

    async def get_positions(self, symbol: Optional[str] = None) -> List[VenuePosition]:
        params = {"category": "linear", "settleCoin": "USDT", "symbol": symbol}
        result = await self._request(
            "GET", "/v5/position/list", params=params, auth=True
        )
        positions = []
        for row in result.get("list") or []:
            size = float(row.get("size", 0) or 0)
            if size <= 0:
                continue
            positions.append(
                VenuePosition(
                    symbol=str(row.get("symbol", "")),
                    side=Side.LONG if row.get("side") == "Buy" else Side.SHORT,
                    size=size,
                    entry_price=float(row.get("avgPrice", 0) or 0),
                    leverage=int(float(row.get("leverage", 1) or 1)),
                    stop_loss=float(row.get("stopLoss", 0) or 0) or None,
                    take_profit=float(row.get("takeProfit", 0) or 0) or None,
                    unrealized_pnl=float(row.get("unrealisedPnl", 0) or 0),
                )
            )
        return positions

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        try:
            await self._request(
                "POST",
                "/v5/position/set-leverage",
                body={
                    "category": "linear",
                    "symbol": symbol,
                    "buyLeverage": str(leverage),
                    "sellLeverage": str(leverage),
                },
                auth=True,
            )
        except BybitAPIError as exc:
            # Bybit uses 110043 when requested leverage already applies.
            # Desired venue state is therefore satisfied without a retry.
            if exc.code != self.LEVERAGE_UNCHANGED_CODE:
                raise

    @staticmethod
    def _order_status(value: str) -> ExecutionOrderStatus:
        return {
            "Created": ExecutionOrderStatus.PENDING,
            "New": ExecutionOrderStatus.PENDING,
            "PartiallyFilled": ExecutionOrderStatus.PARTIALLY_FILLED,
            "Filled": ExecutionOrderStatus.FILLED,
            "Cancelled": ExecutionOrderStatus.CANCELLED,
            "Rejected": ExecutionOrderStatus.REJECTED,
            "Deactivated": ExecutionOrderStatus.CANCELLED,
        }.get(value, ExecutionOrderStatus.UNKNOWN)

    @classmethod
    def _parse_order(cls, row: Dict[str, Any]) -> VenueOrder:
        return VenueOrder(
            order_id=str(row.get("orderId", "")),
            client_order_id=str(row.get("orderLinkId", "")),
            symbol=str(row.get("symbol", "")),
            side=Side.LONG if row.get("side") == "Buy" else Side.SHORT,
            requested_qty=float(row.get("qty", 0) or 0),
            filled_qty=float(row.get("cumExecQty", 0) or 0),
            average_fill_price=float(row.get("avgPrice", 0) or 0),
            fee_paid=float(row.get("cumExecFee", 0) or 0),
            status=cls._order_status(str(row.get("orderStatus", ""))),
            reduce_only=bool(row.get("reduceOnly", False)),
        )

    async def place_order(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: float,
        client_order_id: str,
        reduce_only: bool = False,
    ) -> VenueOrder:
        if not client_order_id or len(client_order_id) > self.MAX_ORDER_LINK_ID_LENGTH:
            raise ValueError(
                f"Bybit orderLinkId must contain 1-{self.MAX_ORDER_LINK_ID_LENGTH} characters"
            )
        body = {
            "category": "linear",
            "symbol": symbol,
            "side": "Buy" if side == Side.LONG else "Sell",
            "orderType": "Market",
            "qty": str(quantity),
            "positionIdx": 0,
            "reduceOnly": reduce_only,
            "orderLinkId": client_order_id,
        }
        result = await self._request(
            "POST",
            "/v5/order/create",
            body=body,
            auth=True,
            retries=0,
            ambiguous_order_id=client_order_id,
        )
        return VenueOrder(
            order_id=str(result.get("orderId", "")),
            client_order_id=str(result.get("orderLinkId", client_order_id)),
            symbol=symbol,
            side=side,
            requested_qty=quantity,
            filled_qty=0.0,
            average_fill_price=0.0,
            fee_paid=0.0,
            status=ExecutionOrderStatus.PENDING,
            reduce_only=reduce_only,
        )

    async def get_order(self, symbol: str, client_order_id: str) -> VenueOrder:
        params = {
            "category": "linear",
            "symbol": symbol,
            "orderLinkId": client_order_id,
        }
        result = await self._request(
            "GET", "/v5/order/realtime", params=params, auth=True
        )
        rows = result.get("list") or []
        if not rows:
            result = await self._request(
                "GET", "/v5/order/history", params=params, auth=True
            )
            rows = result.get("list") or []
        if not rows:
            raise BybitError(f"Bybit order not found: {client_order_id}")
        return self._parse_order(rows[0])

    async def get_closed_pnl(
        self, symbol: str, *, start_ms: Optional[int] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Realized PnL rows for closed position slices, newest first.

        A venue-side stop leaves no local fill to account for, so this is the only
        record of what a stop-out actually realized.
        """
        params: Dict[str, Any] = {
            "category": "linear",
            "symbol": symbol,
            "limit": limit,
        }
        if start_ms:
            params["startTime"] = int(start_ms)
        result = await self._request(
            "GET", "/v5/position/closed-pnl", params=params, auth=True
        )
        return list(result.get("list") or [])

    async def get_order_by_id(
        self, symbol: str, order_id: str
    ) -> Optional[VenueOrder]:
        """Resolve a venue order by exchange order id, live list first then history.

        Native targets are identified by exchange order id rather than orderLinkId,
        because Bybit creates them from trading-stop and assigns no client id.
        """
        params = {"category": "linear", "symbol": symbol, "orderId": order_id}
        for path in ("/v5/order/realtime", "/v5/order/history"):
            result = await self._request("GET", path, params=params, auth=True)
            rows = result.get("list") or []
            if rows:
                return self._parse_order(rows[0])
        return None

    async def get_open_orders(
        self, symbol: str, *, order_filter: str = "StopOrder"
    ) -> List[Dict[str, Any]]:
        """Read-only listing of live orders, including conditional TP/SL orders.

        Rows are returned unparsed: a caller auditing venue TP/SL behaviour needs
        `stopOrderType`, `tpslMode`, and `triggerPrice`, none of which fit VenueOrder.
        """
        result = await self._request(
            "GET",
            "/v5/order/realtime",
            params={
                "category": "linear",
                "symbol": symbol,
                "orderFilter": order_filter,
            },
            auth=True,
        )
        return list(result.get("list") or [])

    async def cancel_order(self, symbol: str, client_order_id: str) -> None:
        await self._request(
            "POST",
            "/v5/order/cancel",
            body={
                "category": "linear",
                "symbol": symbol,
                "orderLinkId": client_order_id,
            },
            auth=True,
        )

    async def set_protection(
        self,
        *,
        symbol: str,
        side: Side,
        stop_loss: float,
        take_profit: Optional[float] = None,
    ) -> None:
        body = {
            "category": "linear",
            "symbol": symbol,
            "positionIdx": 0,
            "tpslMode": "Full",
            "stopLoss": str(stop_loss),
            "slTriggerBy": "MarkPrice",
        }
        if take_profit is not None:
            body["takeProfit"] = str(take_profit)
            body["tpTriggerBy"] = "MarkPrice"
        try:
            await self._request(
                "POST", "/v5/position/trading-stop", body=body, auth=True
            )
        except BybitAPIError as exc:
            # Bybit 34040 means requested protection already matches venue state.
            if exc.code != self.PROTECTION_UNCHANGED_CODE:
                raise

    async def add_partial_tp_sl(
        self,
        *,
        symbol: str,
        side: Side,
        take_profit: float,
        quantity: float,
        stop_loss: Optional[float] = None,
    ) -> None:
        """Install one partial take-profit slice, optionally paired with a stop.

        Bybit requires `tpSize` to equal `slSize` when both are sent, so pairing a
        stop with every target stacks redundant stops on top of the full-position
        stop KARA already holds. Omitting `stop_loss` sends the target alone.
        """
        if take_profit <= 0 or quantity <= 0:
            raise ValueError("Partial TP price and quantity must be positive")
        if stop_loss is not None and stop_loss <= 0:
            raise ValueError("Partial SL price must be positive when supplied")
        body = {
            "category": "linear",
            "symbol": symbol,
            "positionIdx": 0,
            "tpslMode": "Partial",
            "takeProfit": str(take_profit),
            "tpTriggerBy": "MarkPrice",
            "tpOrderType": "Market",
            "tpSize": str(quantity),
        }
        if stop_loss is not None:
            body.update({
                "stopLoss": str(stop_loss),
                "slTriggerBy": "MarkPrice",
                "slOrderType": "Market",
                "slSize": str(quantity),
            })
        await self._request(
            "POST", "/v5/position/trading-stop", body=body, auth=True
        )

    async def clear_stop_loss(self, symbol: str) -> None:
        """Cancel only full-position SL; caller must reconcile immediately."""
        await self._request(
            "POST",
            "/v5/position/trading-stop",
            body={
                "category": "linear",
                "symbol": symbol,
                "positionIdx": 0,
                "tpslMode": "Full",
                "stopLoss": "0",
            },
            auth=True,
        )
