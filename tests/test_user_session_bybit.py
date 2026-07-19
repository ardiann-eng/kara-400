import pytest

from core.user_session import UserSession
from execution.bybit_executor import BybitExecutor
from execution.symbol_registry import BybitSymbolRegistry
from models.schemas import BotMode, User


class FakeClient:
    pass


class FakePersistence:
    def load_risk_state(self, chat_id):
        return None

    def load_bybit_positions(self, chat_id):
        return []


def live_user():
    user = User(chat_id="1", paper_balance_usd=100)
    user.config.bot_mode = BotMode.LIVE
    user.bybit_api_key = "user-key"
    user.bybit_api_secret = "user-secret"
    user.bybit_authorized = True
    user.bybit_testnet = True
    return user


def live_user_with_credentials(chat_id, api_key, api_secret):
    user = live_user()
    user.chat_id = chat_id
    user.bybit_api_key = api_key
    user.bybit_api_secret = api_secret
    return user


def test_live_session_requires_bybit_dependencies():
    with pytest.raises(RuntimeError, match="Bybit live dependencies"):
        UserSession(live_user())


def test_live_session_builds_only_bybit_executor():
    session = UserSession(
        live_user(),
        bybit_client=FakeClient(),
        bybit_registry=BybitSymbolRegistry(),
        persistence=FakePersistence(),
    )

    assert isinstance(session.executor, BybitExecutor)
    assert session.bybit_client.api_key == "user-key"
    status = session.bybit_status()
    assert status["live_risk_limits"]["max_leverage"] == 20
    assert status["live_risk_limits"]["max_positions"] == 3
    assert status["live_risk_limits"]["max_risk_per_trade_pct"] == 0.035
    assert "user-key" not in str(status)


def test_live_session_refuses_credential_environment_mismatch(monkeypatch):
    user = live_user()
    user.bybit_testnet = False
    monkeypatch.setattr("config.BYBIT_TESTNET", True)

    with pytest.raises(RuntimeError, match="environment does not match"):
        UserSession(
            user,
            bybit_client=FakeClient(),
            bybit_registry=BybitSymbolRegistry(),
            persistence=FakePersistence(),
        )


def test_live_sessions_never_share_credentials_or_clients():
    registry = BybitSymbolRegistry()
    user_a = live_user_with_credentials("1", "key-a", "secret-a")
    user_b = live_user_with_credentials("2", "key-b", "secret-b")

    session_a = UserSession(
        user_a,
        bybit_client=FakeClient(),
        bybit_registry=registry,
        persistence=FakePersistence(),
    )
    session_b = UserSession(
        user_b,
        bybit_client=FakeClient(),
        bybit_registry=registry,
        persistence=FakePersistence(),
    )

    assert session_a.bybit_client is not session_b.bybit_client
    assert session_a.bybit_ws is not session_b.bybit_ws
    assert session_a.executor.client is session_a.bybit_client
    assert session_b.executor.client is session_b.bybit_client
    assert session_a.bybit_client.api_key == "key-a"
    assert session_b.bybit_client.api_key == "key-b"
    assert session_a.bybit_ws.api_key == "key-a"
    assert session_b.bybit_ws.api_key == "key-b"


@pytest.mark.asyncio
async def test_ws_state_event_and_reconnect_force_reconciliation():
    session = UserSession(
        live_user(),
        bybit_client=FakeClient(),
        bybit_registry=BybitSymbolRegistry(),
        persistence=FakePersistence(),
    )
    calls = []

    async def reconcile_if_due(force=False):
        calls.append(force)
        return True

    session.executor.reconcile_if_due = reconcile_if_due

    await session._handle_bybit_state_event("order", {})
    await session._handle_bybit_state_event("execution", {})
    await session._handle_bybit_state_event("position", {})
    await session._handle_bybit_state_event("wallet", {})
    await session._reconcile_after_ws_reconnect()

    assert calls == [True, True, True, True]
