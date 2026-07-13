import json
from urllib.parse import parse_qs, urlparse

import pytest

from data.bybit_client import BybitClient
from execution.bybit_executor import BybitExecutor
from execution.price_bridge import HyperliquidBybitPriceBridge
from models.schemas import MarketRegime, ScoreBreakdown, Side, SignalStrength, TradeSignal


INSTRUMENT = {
    "symbol": "BTCUSDT",
    "baseCoin": "BTC",
    "settleCoin": "USDT",
    "status": "Trading",
    "contractType": "LinearPerpetual",
    "priceFilter": {"tickSize": "0.1"},
    "lotSizeFilter": {
        "qtyStep": "0.001",
        "minOrderQty": "0.001",
        "minNotionalValue": "5",
    },
    "leverageFilter": {"maxLeverage": "20"},
}


class Response:
    status = 200

    def __init__(self, result):
        self.payload = {"retCode": 0, "retMsg": "OK", "result": result}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self.payload


class StatefulBybitHTTP:
    """Stateful V5 boundary used by real BybitClient and BybitExecutor code."""

    closed = False

    def __init__(self):
        self.requests = []
        self.orders = {}
        self.position = None
        self.stop_loss = None
        self.order_number = 0

    def request(self, method, url, *, data, headers):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        body = json.loads(data) if data else {}
        self.requests.append((method, parsed.path, query, body, headers))

        if parsed.path == "/v5/account/wallet-balance":
            return Response({"list": [{
                "totalEquity": "1000",
                "totalAvailableBalance": "900",
                "totalInitialMargin": "100",
                "totalPerpUPL": "0",
                "coin": [{"coin": "USDT", "walletBalance": "1000"}],
            }]})
        if parsed.path == "/v5/market/instruments-info":
            return Response({"list": [INSTRUMENT], "nextPageCursor": ""})
        if parsed.path == "/v5/market/tickers":
            return Response({"list": [{"symbol": "BTCUSDT", "markPrice": "100.1"}]})
        if parsed.path == "/v5/position/set-leverage":
            return Response({})
        if parsed.path == "/v5/order/create":
            return Response(self._create_order(body))
        if parsed.path == "/v5/order/realtime":
            order_link_id = query["orderLinkId"][0]
            return Response({"list": [self.orders[order_link_id]]})
        if parsed.path == "/v5/position/trading-stop":
            self.stop_loss = float(body["stopLoss"])
            return Response({})
        if parsed.path == "/v5/position/list":
            return Response({"list": self._position_rows()})
        raise AssertionError(f"Unexpected request: {method} {parsed.path}")

    def _create_order(self, body):
        self.order_number += 1
        quantity = float(body["qty"])
        reduce_only = body["reduceOnly"]
        side = body["side"]
        fill_price = 101.0 if reduce_only else 100.2
        order = {
            "orderId": f"oid-{self.order_number}",
            "orderLinkId": body["orderLinkId"],
            "symbol": body["symbol"],
            "side": side,
            "qty": body["qty"],
            "cumExecQty": body["qty"],
            "avgPrice": str(fill_price),
            "cumExecFee": "0.01",
            "orderStatus": "Filled",
            "reduceOnly": reduce_only,
        }
        self.orders[body["orderLinkId"]] = order
        if reduce_only:
            self.position["size"] = max(0.0, self.position["size"] - quantity)
            if self.position["size"] == 0:
                self.position = None
                self.stop_loss = None
        else:
            self.position = {
                "symbol": body["symbol"],
                "side": side,
                "size": quantity,
                "avgPrice": fill_price,
                "leverage": 10,
            }
        return {"orderId": order["orderId"], "orderLinkId": body["orderLinkId"]}

    def _position_rows(self):
        if not self.position:
            return []
        return [{
            "symbol": self.position["symbol"],
            "side": self.position["side"],
            "size": str(self.position["size"]),
            "avgPrice": str(self.position["avgPrice"]),
            "leverage": str(self.position["leverage"]),
            "stopLoss": str(self.stop_loss or 0),
            "takeProfit": "0",
            "unrealisedPnl": "0",
        }]


class Risk:
    status = {"peak_balance": 1000, "daily_pnl": 0, "paused": False, "kill_switch": False}

    def pre_trade_check(self, signal, account, positions):
        return True, "ok"

    def calculate_position_size(self, signal, equity):
        return 100, 0.1, 10

    def check_tp_trail(self, position, price, market_state=None):
        return None

    def record_pnl(self, pnl, balance):
        self.recorded = (pnl, balance)


def signal():
    return TradeSignal(
        signal_id="signal-http",
        asset="BTC",
        side=Side.LONG,
        score=70,
        strength=SignalStrength.MODERATE,
        regime=MarketRegime.NORMAL,
        breakdown=ScoreBreakdown(),
        entry_price=100,
        stop_loss=99,
        tp1=101,
        tp2=102,
        suggested_leverage=10,
    )


@pytest.mark.asyncio
async def test_full_executor_lifecycle_crosses_stateful_http_boundary():
    transport = StatefulBybitHTTP()
    client = BybitClient(api_key="key", api_secret="secret", session=transport)
    await client.load_instruments()
    executor = BybitExecutor(
        chat_id="1",
        client=client,
        risk_manager=Risk(),
        symbol_registry=client.symbol_registry,
        price_bridge=HyperliquidBybitPriceBridge(0.003),
        fill_timeout_s=0.1,
        poll_interval_s=0,
    )

    position = await executor.open_position(signal())

    assert position.entry_price == 100.2
    assert transport.position["size"] == 0.1
    assert transport.stop_loss == 99.2

    partial = await executor.close_position(
        position.position_id, 101, reason="tp1", close_ratio=0.5
    )
    assert partial["fully_closed"] is False
    assert transport.position["size"] == pytest.approx(0.05)

    closed = await executor.close_position(position.position_id, 101, reason="manual")
    assert closed["fully_closed"] is True
    assert transport.position is None
    assert executor.open_positions == []

    create_requests = [
        body for method, path, query, body, headers in transport.requests
        if path == "/v5/order/create"
    ]
    assert len(create_requests) == 3
    assert [item["reduceOnly"] for item in create_requests] == [False, True, True]
    assert len({item["orderLinkId"] for item in create_requests}) == 3
    assert any(path == "/v5/position/trading-stop" for _, path, _, _, _ in transport.requests)
