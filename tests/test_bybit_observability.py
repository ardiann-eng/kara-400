import asyncio
import logging
from pathlib import Path
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


@pytest.mark.asyncio
async def test_alert_is_logged_even_when_telegram_sink_is_absent(caplog):
    """Regression: protection alerts existed only in Telegram, so Railway logs held
    no record of when a position lost its hard SL."""
    manager = BybitAlertManager(sink=None)

    with caplog.at_level(logging.CRITICAL, logger="kara.bybit_observability"):
        delivered = await manager.emit(
            "missing_sl:LDOUSDT",
            "CRITICAL BYBIT: posisi LDOUSDT tidak memiliki native hard SL.",
        )

    assert delivered is False
    assert "missing_sl:LDOUSDT" in caplog.text
    assert caplog.records[0].levelno == logging.CRITICAL


@pytest.mark.asyncio
async def test_cooldown_suppressed_alert_is_still_logged(caplog):
    sent = []
    manager = BybitAlertManager(sink=lambda m: _collect(sent, m), cooldown_s=300)
    message = "CRITICAL BYBIT: posisi LDOUSDT tidak memiliki native hard SL."

    assert await manager.emit("missing_sl:LDOUSDT", message) is True
    with caplog.at_level(logging.INFO, logger="kara.bybit_observability"):
        assert await manager.emit("missing_sl:LDOUSDT", message) is False

    assert len(sent) == 1
    assert "BYBIT_ALERT_SUPPRESSED" in caplog.text
    assert "repeat=1" in caplog.text


@pytest.mark.asyncio
async def test_warning_alert_logs_at_warning_level(caplog):
    manager = BybitAlertManager(sink=None)

    with caplog.at_level(logging.WARNING, logger="kara.bybit_observability"):
        await manager.emit(
            "ws_stale",
            "WARNING BYBIT: private WebSocket stale/disconnected; REST fallback tetap aktif.",
        )

    assert caplog.records[0].levelno == logging.WARNING


def test_position_monitor_does_not_send_routine_ws_disconnect_alert():
    source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    start = source.index("async def _update_positions")
    end = source.index("async def _scalper_exit_market_state", start)
    monitor = source[start:end]

    assert '"ws_stale"' not in monitor
    assert "private WebSocket stale/disconnected" not in monitor
    assert "await session.executor.reconcile_if_due()" in monitor
    assert '"reconciliation_failed"' in monitor


async def _collect(bucket, message):
    bucket.append(message)
