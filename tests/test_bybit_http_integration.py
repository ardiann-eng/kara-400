import asyncio
import hashlib
import hmac
import json

import pytest

from data.bybit_client import (
    BybitAmbiguousOrderError,
    BybitClient,
    BybitError,
)
from execution.exchange_client import ExecutionOrderStatus
from models.schemas import Side


class ScriptedResponse:
    def __init__(self, payload=None, *, status=200, error=None):
        self.payload = payload
        self.status = status
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self.payload


class ScriptedHTTPSession:
    """Small aiohttp-compatible transport with deterministic request scripts."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    def request(self, method, url, *, data, headers):
        self.requests.append({
            "method": method,
            "url": url,
            "data": data,
            "headers": headers,
        })
        if not self.responses:
            raise AssertionError(f"Unexpected HTTP request: {method} {url}")
        return self.responses.pop(0)


def ok(result):
    return ScriptedResponse({"retCode": 0, "retMsg": "OK", "result": result})


def filled_order(order_link_id, *, source="realtime"):
    return {
        "list": [{
            "orderId": f"oid-{source}",
            "orderLinkId": order_link_id,
            "symbol": "BTCUSDT",
            "side": "Buy",
            "qty": "0.001",
            "cumExecQty": "0.001",
            "avgPrice": "60000",
            "cumExecFee": "0.03",
            "orderStatus": "Filled",
            "reduceOnly": False,
        }]
    }


@pytest.mark.asyncio
async def test_signed_get_reaches_expected_url_and_headers(monkeypatch):
    session = ScriptedHTTPSession(ok({
        "list": [{
            "totalEquity": "1000",
            "totalAvailableBalance": "900",
            "totalInitialMargin": "100",
            "totalPerpUPL": "0",
            "coin": [{"coin": "USDT", "walletBalance": "1000"}],
        }]
    }))
    client = BybitClient(api_key="key", api_secret="secret", session=session)
    monkeypatch.setattr(client, "_timestamp", lambda: "1700000000000")

    account = await client.get_account()

    request = session.requests[0]
    query = "accountType=UNIFIED&coin=USDT"
    signature_payload = f"1700000000000key5000{query}"
    expected_signature = hmac.new(
        b"secret", signature_payload.encode(), hashlib.sha256
    ).hexdigest()
    assert account.total_equity == 1000
    assert request["method"] == "GET"
    assert request["url"] == (
        "https://api-testnet.bybit.com/v5/account/wallet-balance?" + query
    )
    assert request["data"] is None
    assert request["headers"]["X-BAPI-SIGN"] == expected_signature


@pytest.mark.asyncio
async def test_signed_post_reaches_expected_endpoint_with_exact_compact_body(monkeypatch):
    session = ScriptedHTTPSession(ok({}))
    client = BybitClient(api_key="key", api_secret="secret", session=session)
    monkeypatch.setattr(client, "_timestamp", lambda: "1")

    await client.set_leverage("BTCUSDT", 3)

    request = session.requests[0]
    expected_body = (
        '{"category":"linear","symbol":"BTCUSDT",'
        '"buyLeverage":"3","sellLeverage":"3"}'
    )
    expected_signature = hmac.new(
        b"secret", f"1key5000{expected_body}".encode(), hashlib.sha256
    ).hexdigest()
    assert request["method"] == "POST"
    assert request["url"].endswith("/v5/position/set-leverage")
    assert request["data"] == expected_body
    assert json.loads(request["data"])["symbol"] == "BTCUSDT"
    assert request["headers"]["X-BAPI-SIGN"] == expected_signature


@pytest.mark.asyncio
async def test_read_rate_limit_retries_then_succeeds(monkeypatch):
    session = ScriptedHTTPSession(
        ScriptedResponse({"retCode": 10006, "retMsg": "Too many visits"}),
        ok({"list": [{"markPrice": "60001"}]}),
    )
    client = BybitClient(api_key="key", api_secret="secret", session=session)
    sleeps = []

    async def no_wait(delay):
        sleeps.append(delay)

    monkeypatch.setattr("data.bybit_client.asyncio.sleep", no_wait)

    assert await client.get_mark_price("BTCUSDT") == 60001
    assert len(session.requests) == 2
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_read_timeout_retries_then_succeeds(monkeypatch):
    session = ScriptedHTTPSession(
        ScriptedResponse(error=asyncio.TimeoutError()),
        ok({"list": [{"markPrice": "60002"}]}),
    )
    client = BybitClient(api_key="key", api_secret="secret", session=session)

    async def no_wait(_delay):
        return None

    monkeypatch.setattr("data.bybit_client.asyncio.sleep", no_wait)

    assert await client.get_mark_price("BTCUSDT") == 60002
    assert len(session.requests) == 2


@pytest.mark.asyncio
async def test_create_order_timeout_is_ambiguous_and_never_retried():
    session = ScriptedHTTPSession(
        ScriptedResponse(error=asyncio.TimeoutError()),
        ok({"orderId": "must-not-be-used"}),
    )
    client = BybitClient(api_key="key", api_secret="secret", session=session)

    with pytest.raises(BybitAmbiguousOrderError) as error:
        await client.place_order(
            symbol="BTCUSDT",
            side=Side.LONG,
            quantity=0.001,
            client_order_id="KARA-ENTRY-1",
        )

    assert error.value.client_order_id == "KARA-ENTRY-1"
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_ambiguous_lookup_finds_same_order_link_id_in_realtime():
    session = ScriptedHTTPSession(ok(filled_order("KARA-ENTRY-1")))
    client = BybitClient(api_key="key", api_secret="secret", session=session)

    order = await client.get_order("BTCUSDT", "KARA-ENTRY-1")

    assert order.status == ExecutionOrderStatus.FILLED
    assert order.client_order_id == "KARA-ENTRY-1"
    assert len(session.requests) == 1
    assert "orderLinkId=KARA-ENTRY-1" in session.requests[0]["url"]
    assert "/v5/order/realtime?" in session.requests[0]["url"]


@pytest.mark.asyncio
async def test_ambiguous_lookup_falls_back_to_history_with_same_id():
    session = ScriptedHTTPSession(
        ok({"list": []}),
        ok(filled_order("KARA-ENTRY-1", source="history")),
    )
    client = BybitClient(api_key="key", api_secret="secret", session=session)

    order = await client.get_order("BTCUSDT", "KARA-ENTRY-1")

    assert order.status == ExecutionOrderStatus.FILLED
    assert len(session.requests) == 2
    assert "/v5/order/realtime?" in session.requests[0]["url"]
    assert "/v5/order/history?" in session.requests[1]["url"]
    assert all("orderLinkId=KARA-ENTRY-1" in item["url"] for item in session.requests)


@pytest.mark.asyncio
async def test_ambiguous_lookup_not_found_fails_without_create_request():
    session = ScriptedHTTPSession(ok({"list": []}), ok({"list": []}))
    client = BybitClient(api_key="key", api_secret="secret", session=session)

    with pytest.raises(BybitError, match="Bybit order not found"):
        await client.get_order("BTCUSDT", "KARA-ENTRY-1")

    assert len(session.requests) == 2
    assert all(item["method"] == "GET" for item in session.requests)
    assert all("/v5/order/create" not in item["url"] for item in session.requests)


@pytest.mark.asyncio
async def test_execution_quote_calculates_spread_depth_and_side_vwap():
    ticker = ok({"list": [{"markPrice": "100"}]})
    book = ok({
        "b": [["99.9", "2"], ["99.8", "5"]],
        "a": [["100.1", "1"], ["100.2", "5"]],
    })
    session = ScriptedHTTPSession(ticker, book)
    client = BybitClient(api_key="key", api_secret="secret", session=session)

    result = await client.get_execution_quote("BTCUSDT", Side.LONG, 2)

    assert result.best_bid == 99.9
    assert result.best_ask == 100.1
    assert result.spread_pct == pytest.approx(0.002)
    assert result.estimated_fill_price == pytest.approx(100.15)
    assert result.estimated_slippage_pct == pytest.approx(0.0015)
    assert result.available_quantity == 6
    assert {request["method"] for request in session.requests} == {"GET"}
