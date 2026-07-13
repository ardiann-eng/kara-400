import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest

from data.bybit_private_ws import BybitPrivateWebSocket
from execution.exchange_client import ExecutionOrderStatus


def test_private_ws_auth_signature():
    expires = 1700000000000
    expected = hmac.new(
        b"secret", f"GET/realtime{expires}".encode(), hashlib.sha256
    ).hexdigest()
    assert BybitPrivateWebSocket.auth_signature("secret", expires) == expected


@pytest.mark.asyncio
async def test_order_event_is_cached_and_wakes_waiter():
    ws = BybitPrivateWebSocket(api_key="key", api_secret="secret")
    ws._connected = True
    ws._last_message_at = time.monotonic()
    waiter = ws.wait_for_order("kara-id", 0.2)
    ws.handle_message(json.dumps({
        "topic": "order",
        "data": [{
            "orderId": "oid",
            "orderLinkId": "kara-id",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "qty": "0.01",
            "cumExecQty": "0.01",
            "avgPrice": "60000",
            "cumExecFee": "0.1",
            "orderStatus": "Filled",
        }],
    }))
    order = await waiter
    assert order.status == ExecutionOrderStatus.FILLED
    assert order.average_fill_price == 60000


@pytest.mark.asyncio
async def test_stale_ws_returns_none_for_rest_fallback():
    ws = BybitPrivateWebSocket(api_key="key", api_secret="secret")
    assert ws.stale is True
    assert await ws.wait_for_order("missing", 1) is None


@pytest.mark.asyncio
async def test_execution_events_are_deduplicated_and_state_events_dispatch():
    seen = []

    async def on_state(topic, row):
        seen.append((topic, row))

    ws = BybitPrivateWebSocket(
        api_key="key", api_secret="secret", on_state_event=on_state
    )
    execution = json.dumps({
        "topic": "execution",
        "data": [{"execId": "exec-1", "symbol": "BTCUSDT"}],
    })
    ws.handle_message(execution)
    ws.handle_message(execution)
    ws.handle_message(json.dumps({
        "topic": "position",
        "data": [{"symbol": "BTCUSDT", "positionIdx": 0, "size": "0"}],
    }))
    ws.handle_message(json.dumps({
        "topic": "wallet",
        "data": [{"accountType": "UNIFIED", "totalEquity": "100"}],
    }))
    await __import__("asyncio").sleep(0)

    assert [topic for topic, _ in seen].count("execution") == 1
    assert [topic for topic, _ in seen].count("position") == 1
    assert [topic for topic, _ in seen].count("wallet") == 1
    assert ws.latest_positions["BTCUSDT:0"]["size"] == "0"
    assert ws.latest_wallet["totalEquity"] == "100"


@pytest.mark.asyncio
async def test_out_of_order_execution_then_order_still_resolves_order_waiter():
    seen = []

    async def on_state(topic, row):
        seen.append((topic, row["execId"]))

    ws = BybitPrivateWebSocket(
        api_key="key", api_secret="secret", on_state_event=on_state
    )
    ws._connected = True
    ws._last_message_at = time.monotonic()
    ws.handle_message(json.dumps({
        "topic": "execution",
        "data": [{"execId": "exec-before-order", "orderLinkId": "kara-id"}],
    }))
    ws.handle_message(json.dumps({
        "topic": "order",
        "data": [{
            "orderId": "oid",
            "orderLinkId": "kara-id",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "qty": "0.01",
            "cumExecQty": "0.01",
            "avgPrice": "60000",
            "orderStatus": "Filled",
        }],
    }))
    await __import__("asyncio").sleep(0)

    order = await ws.wait_for_order("kara-id", 0.1)

    assert order.status == ExecutionOrderStatus.FILLED
    assert seen == [("execution", "exec-before-order")]


class FakeSocket:
    def __init__(self):
        self.closed = False
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive(self):
        return SimpleNamespace(data=json.dumps({"success": True}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class ReconnectingSession:
    def __init__(self, sockets):
        self.sockets = list(sockets)
        self.calls = 0

    async def ws_connect(self, url, heartbeat):
        self.calls += 1
        if not self.sockets:
            raise AssertionError("Unexpected third WS connection")
        return self.sockets.pop(0)


@pytest.mark.asyncio
async def test_ws_reconnect_reauthenticates_resubscribes_and_reconciles_once(monkeypatch):
    sockets = [FakeSocket(), FakeSocket()]
    reconciliations = 0

    async def on_reconnect():
        nonlocal reconciliations
        reconciliations += 1
        ws._running = False

    async def no_wait(_delay):
        return None

    ws = BybitPrivateWebSocket(
        api_key="key", api_secret="secret", on_reconnect=on_reconnect
    )
    ws._session = ReconnectingSession(sockets)
    ws._running = True
    monkeypatch.setattr("data.bybit_private_ws.asyncio.sleep", no_wait)

    await ws._run()

    assert ws._session.calls == 2
    assert reconciliations == 1
    for socket in sockets:
        assert socket.sent[0]["op"] == "auth"
        assert socket.sent[1] == {
            "op": "subscribe",
            "args": ["order", "execution", "position", "wallet"],
        }
