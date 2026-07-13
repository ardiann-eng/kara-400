import asyncio
from types import SimpleNamespace

import pytest

from core.bybit_observability import BybitAlertManager, BybitTelemetry
from data.bybit_client import BybitClient
from data.bybit_private_ws import BybitPrivateWebSocket
from execution.exchange_client import VenuePosition
from models.schemas import Side


class Response:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return {
            "retCode": 0,
            "result": {"list": [{"symbol": "BTCUSDT", "markPrice": "60000"}]},
        }


class Session:
    closed = False

    def request(self, *args, **kwargs):
        return Response()


def test_snapshot_contains_no_credential_or_order_fields():
    snapshot = BybitTelemetry(environment="BYBIT TESTNET").snapshot()
    keys = " ".join(snapshot).lower()

    assert "api" not in keys
    assert "secret" not in keys
    assert "signature" not in keys
    assert "order" not in keys
    assert snapshot["environment"] == "BYBIT TESTNET"


@pytest.mark.asyncio
async def test_alert_manager_deduplicates_then_allows_after_cooldown():
    sent = []

    async def sink(message):
        sent.append(message)

    alerts = BybitAlertManager(sink, cooldown_s=5)

    assert await alerts.emit("missing_sl:BTCUSDT", "first") is True
    assert await alerts.emit("missing_sl:BTCUSDT", "duplicate") is False
    alerts._last_sent["missing_sl:BTCUSDT"] -= 6
    assert await alerts.emit("missing_sl:BTCUSDT", "after cooldown") is True
    assert sent == ["first", "after cooldown"]


@pytest.mark.asyncio
async def test_alert_delivery_failure_never_raises():
    async def failed_sink(message):
        raise RuntimeError("telegram down")

    alerts = BybitAlertManager(failed_sink, cooldown_s=5)

    assert await alerts.emit("critical", "safe message") is False


@pytest.mark.asyncio
async def test_rest_request_updates_secret_free_health_metrics():
    telemetry = BybitTelemetry()
    client = BybitClient(
        api_key="never-exposed-key",
        api_secret="never-exposed-secret",
        session=Session(),
        telemetry=telemetry,
    )

    assert await client.get_mark_price("BTCUSDT") == 60000
    snapshot = telemetry.snapshot()

    assert snapshot["rest_healthy"] is True
    assert snapshot["rest_last_success_at"] > 0
    assert snapshot["rest_latency_ms"] >= 0
    assert "never-exposed" not in str(snapshot)


def test_ws_message_updates_connection_health_without_payload_storage():
    telemetry = BybitTelemetry()
    ws = BybitPrivateWebSocket(
        api_key="key",
        api_secret="secret",
        telemetry=telemetry,
    )
    ws._connected = True

    ws.handle_message('{"topic":"wallet","data":[]}')

    assert telemetry.ws_connected is True
    assert telemetry.ws_stale is False
    assert telemetry.ws_last_message_at > 0


@pytest.mark.asyncio
async def test_executor_reconciliation_updates_sl_and_mismatch_metrics():
    from tests.test_bybit_executor import FakeClient, make_executor

    telemetry = BybitTelemetry()
    client = FakeClient()
    client.positions = [
        VenuePosition("BTCUSDT", Side.LONG, 0.1, 100, 5, stop_loss=99)
    ]
    executor = make_executor(client)
    executor.telemetry = telemetry

    await executor.reconcile()

    assert telemetry.last_reconciliation_at > 0
    assert telemetry.reconciliation_mismatch_count == 1
    assert telemetry.unknown_recovered_positions == 1
    assert telemetry.hard_sl_healthy_count == 1
