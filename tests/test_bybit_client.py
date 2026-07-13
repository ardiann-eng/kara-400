import hashlib
import hmac

import pytest

from data.bybit_client import BybitClient
from execution.exchange_client import ExecutionOrderStatus
from models.schemas import Side
from core.startup_validation import BybitPreflightResult


def test_get_signature_uses_sorted_url_encoded_query():
    client = BybitClient(api_key="key", api_secret="secret")
    query = client._query_string({"symbol": "BTCUSDT", "category": "linear"})
    timestamp = "1700000000000"

    expected_payload = "1700000000000key5000category=linear&symbol=BTCUSDT"
    expected = hmac.new(
        b"secret", expected_payload.encode(), hashlib.sha256
    ).hexdigest()

    assert query == "category=linear&symbol=BTCUSDT"
    assert client._signature(timestamp, query) == expected


def test_post_signature_uses_exact_compact_body():
    client = BybitClient(api_key="key", api_secret="secret")
    body = client._json_body({"category": "linear", "qty": "0.001"})

    assert body == '{"category":"linear","qty":"0.001"}'
    assert client._signature("1", body) == hmac.new(
        b"secret", f"1key5000{body}".encode(), hashlib.sha256
    ).hexdigest()


def test_order_parser_maps_partial_fill_and_fees():
    order = BybitClient._parse_order(
        {
            "orderId": "exchange-id",
            "orderLinkId": "kara-id",
            "symbol": "BTCUSDT",
            "side": "Sell",
            "qty": "0.01",
            "cumExecQty": "0.004",
            "avgPrice": "60000.5",
            "cumExecFee": "0.12",
            "orderStatus": "PartiallyFilled",
            "reduceOnly": True,
        }
    )

    assert order.side == Side.SHORT
    assert order.status == ExecutionOrderStatus.PARTIALLY_FILLED
    assert order.filled_qty == 0.004
    assert order.fee_paid == 0.12
    assert order.reduce_only is True


@pytest.mark.asyncio
async def test_get_order_falls_back_to_history(monkeypatch):
    client = BybitClient(api_key="key", api_secret="secret")
    paths = []

    async def fake_request(method, path, **kwargs):
        paths.append(path)
        if path == "/v5/order/realtime":
            return {"list": []}
        return {
            "list": [
                {
                    "orderId": "oid",
                    "orderLinkId": "kara-id",
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "qty": "0.001",
                    "cumExecQty": "0.001",
                    "avgPrice": "60000",
                    "orderStatus": "Filled",
                }
            ]
        }

    monkeypatch.setattr(client, "_request", fake_request)
    order = await client.get_order("BTCUSDT", "kara-id")

    assert paths == ["/v5/order/realtime", "/v5/order/history"]
    assert order.status == ExecutionOrderStatus.FILLED


@pytest.mark.asyncio
async def test_place_order_uses_order_link_id_and_no_request_retry(monkeypatch):
    client = BybitClient(api_key="key", api_secret="secret")
    captured = {}

    async def fake_request(method, path, **kwargs):
        captured.update(kwargs)
        return {"orderId": "oid", "orderLinkId": "kara-entry-1"}

    monkeypatch.setattr(client, "_request", fake_request)
    order = await client.place_order(
        symbol="BTCUSDT",
        side=Side.LONG,
        quantity=0.001,
        client_order_id="kara-entry-1",
    )

    assert captured["body"]["orderLinkId"] == "kara-entry-1"
    assert captured["retries"] == 0
    assert captured["ambiguous_order_id"] == "kara-entry-1"
    assert order.status == ExecutionOrderStatus.PENDING


@pytest.mark.asyncio
async def test_set_protection_uses_full_mark_price_stop(monkeypatch):
    client = BybitClient(api_key="key", api_secret="secret")
    captured = {}

    async def fake_request(method, path, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(client, "_request", fake_request)
    await client.set_protection(
        symbol="BTCUSDT",
        side=Side.LONG,
        stop_loss=59000,
    )

    assert captured["body"]["tpslMode"] == "Full"
    assert captured["body"]["slTriggerBy"] == "MarkPrice"
    assert "takeProfit" not in captured["body"]


@pytest.mark.asyncio
async def test_preflight_reads_permissions_account_and_position_mode(monkeypatch):
    client = BybitClient(api_key="key", api_secret="secret", testnet=True)

    async def fake_request(method, path, **kwargs):
        if path == "/v5/user/query-api":
            return {"permissions": {"ContractTrade": ["Order"], "Withdraw": []}}
        if path == "/v5/position/list":
            return {"list": [{"positionIdx": 0}]}
        raise AssertionError(path)

    async def fake_account():
        from execution.exchange_client import VenueAccount
        return VenueAccount(100, 100, 90, 10, 0)

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr(client, "get_account", fake_account)

    result = await client.preflight()

    assert isinstance(result, BybitPreflightResult)
    assert result.can_trade_contracts is True
    assert result.withdrawal_enabled is False
    assert result.position_mode == "one_way"
