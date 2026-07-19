from types import SimpleNamespace
import sys
import ast
from pathlib import Path

import pytest

from models.schemas import BotMode

sys.modules.setdefault("eth_account", SimpleNamespace())

from notify.telegram import KaraTelegram
from execution.symbol_registry import BybitSymbolRegistry


def test_config_has_single_fernet_assignment_with_hl_fallback():
    tree = ast.parse(
        (Path(__file__).parents[1] / "config.py").read_text(encoding="utf-8")
    )
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "FERNET_KEY" for target in node.targets)
    ]

    assert len(assignments) == 1
    source = ast.unparse(assignments[0])
    assert "HL_FERNET_KEY" in source
    assert "FERNET_KEY" in source


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_html(self, text, **kwargs):
        self.replies.append((text, kwargs))


class FakeBotApp:
    def __init__(self, session):
        self.session = session

    async def get_session(self, chat_id):
        return self.session


def test_bybit_chart_url_uses_exact_registry_symbol_not_asset_guess():
    registry = BybitSymbolRegistry(aliases={"KBONK": "1000BONKUSDT"})
    registry.load([{
        "symbol": "1000BONKUSDT", "status": "Trading",
        "contractType": "LinearPerpetual", "settleCoin": "USDT", "baseCoin": "1000BONK",
        "priceFilter": {"tickSize": "0.000001"},
        "lotSizeFilter": {"qtyStep": "1", "minOrderQty": "1", "minNotionalValue": "5"},
        "leverageFilter": {"maxLeverage": "20"},
    }])

    url = KaraTelegram._bybit_chart_url("kBONK", SimpleNamespace(registry=registry))

    assert url == "https://www.bybit.com/en/trade/usdt/1000BONKUSDT"
    assert KaraTelegram._bybit_chart_url("UNKNOWN", SimpleNamespace(registry=registry)) is None


def test_bybit_chart_label_distinguishes_demo_from_live():
    demo = SimpleNamespace(bybit_environment=SimpleNamespace(value="demo"))
    mainnet = SimpleNamespace(bybit_environment=SimpleNamespace(value="mainnet"))

    assert KaraTelegram._bybit_chart_label(demo) == "Chart Bybit Demo"
    assert KaraTelegram._bybit_chart_label(mainnet) == "Chart Bybit Live"


def test_positions_view_does_not_build_bybit_chart_buttons():
    source = (Path(__file__).parents[1] / "notify" / "telegram.py").read_text(
        encoding="utf-8"
    )
    positions_start = source.index("async def cmd_positions")
    positions_end = source.index("def _fmt_hold_duration", positions_start)
    positions_source = source[positions_start:positions_end]

    assert "_bybit_chart_url(pos.asset" not in positions_source
    assert "Chart Bybit" not in positions_source


@pytest.mark.asyncio
async def test_tp_updates_do_not_offer_final_pnl_card(monkeypatch):
    from models.schemas import Position, PositionStatus, Side

    position = Position(
        position_id="p1", asset="MORPHO", side=Side.LONG,
        entry_price=2.2045, size_initial=10, size_current=6,
        leverage=10, margin_usd=2.2, stop_loss=2.2045,
        tp1=2.21, tp2=2.22, status=PositionStatus.OPEN,
    )
    session = SimpleNamespace(
        executor=SimpleNamespace(_positions={"p1": position}, registry=None),
        user=SimpleNamespace(bybit_environment=SimpleNamespace(value="demo")),
    )
    sent = []
    telegram = KaraTelegram.__new__(KaraTelegram)
    telegram.bot_app = FakeBotApp(session)
    async def send_text(text, **kwargs):
        sent.append((text, kwargs))
    telegram.send_text = send_text

    await telegram.send_position_event({
        "action": "tp1", "position_id": "p1", "pnl_slice": 1,
        "pnl_total": 1, "pnl_pct_slice": 0.05, "exit_price": 2.21,
        "stop_moved_to_entry": True,
    }, {"MORPHO": 2.21}, target_chat_id="1")

    text, kwargs = sent[0]
    assert "KARA UPDATE: Target Reached" in text
    assert "TP1 HIT" in text
    assert "SL digeser ke Entry" in text
    assert "Pnl Card" not in text
    assert kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_full_close_pnl_card_does_not_offer_bybit_chart(monkeypatch):
    from models.schemas import Position, PositionStatus, Side

    position = Position(
        position_id="p1", asset="LTC", side=Side.LONG,
        entry_price=47.06, size_initial=10, size_current=0,
        leverage=20, margin_usd=23.53, stop_loss=46.5,
        tp1=48, tp2=49, status=PositionStatus.CLOSED,
    )

    class CloseSession:
        executor = SimpleNamespace(_positions={"p1": position}, registry=None)
        user = SimpleNamespace(bybit_environment=SimpleNamespace(value="demo"))

        async def get_account_state(self):
            return SimpleNamespace()

    sent = []
    telegram = KaraTelegram.__new__(KaraTelegram)
    telegram.bot_app = FakeBotApp(CloseSession())
    telegram._pending_pnl_cards = {}

    async def send_text(text, **kwargs):
        sent.append((text, kwargs))

    telegram.send_text = send_text
    monkeypatch.setattr(
        KaraTelegram, "_bybit_chart_url", staticmethod(lambda *_: "https://chart.example/LTCUSDT")
    )

    await telegram.send_position_event({
        "action": "time_exit", "position_id": "p1", "fully_closed": True,
        "pnl_total": -2.085, "pnl_pct_total": -0.0135, "exit_price": 47.08,
    }, {"LTC": 47.08}, target_chat_id="1")

    text, kwargs = sent[0]
    assert "Time Exit · Loss" in text
    buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
    assert [button.text for button in buttons] == ["📊 PnL Card"]
    assert [button.callback_data for button in buttons] == ["gen_pnl:p1"]


class ReconcilingExecutor:
    def __init__(self, positions=None, error=None):
        self.open_positions = list(positions or [])
        self.error = error
        self.calls = []

    async def reconcile_if_due(self, force=False):
        self.calls.append(force)
        if self.error:
            raise self.error
        return True


class FakeQuery:
    data = "paper_close_all_confirm"

    def __init__(self):
        self.answers = []
        self.edits = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


@pytest.mark.asyncio
async def test_paper_command_cannot_clear_live_state_with_exchange_position(monkeypatch):
    user = SimpleNamespace(config=SimpleNamespace(bot_mode=BotMode.LIVE))
    position = SimpleNamespace(asset="BTC")
    session = SimpleNamespace(executor=ReconcilingExecutor([position]))
    message = FakeMessage()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id="1"),
        effective_message=message,
    )
    telegram = KaraTelegram.__new__(KaraTelegram)
    telegram.bot_app = FakeBotApp(session)
    telegram._is_authorized = lambda _update: True
    activated = []

    async def activate(*args):
        activated.append(args)

    telegram._activate_paper_mode = activate
    monkeypatch.setattr("notify.telegram.user_db.get_user", lambda chat_id: user)

    await telegram.cmd_paper(update, SimpleNamespace())

    assert activated == []
    assert len(message.replies) == 1
    text, kwargs = message.replies[0]
    assert "Tidak bisa pindah ke Paper" in text
    assert "BTC" in text
    callbacks = [
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert callbacks == ["paper_close_all_confirm", "paper_cancel"]


@pytest.mark.asyncio
async def test_live_command_blocks_open_paper_position(monkeypatch):
    user = SimpleNamespace(config=SimpleNamespace(bot_mode=BotMode.PAPER))
    position = SimpleNamespace(asset="ETH")
    session = SimpleNamespace(executor=SimpleNamespace(open_positions=[position]))
    message = FakeMessage()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id="1"),
        effective_message=message,
    )
    telegram = KaraTelegram.__new__(KaraTelegram)
    telegram.bot_app = FakeBotApp(session)
    telegram._is_authorized = lambda _update: True
    telegram._is_throttled = lambda *args, **kwargs: False
    monkeypatch.setattr("notify.telegram.user_db.get_user", lambda chat_id: user)

    result = await telegram.cmd_live(update, SimpleNamespace(user_data={}))

    assert result is not None
    assert "posisi Paper masih terbuka" in message.replies[-1][0]
    assert "ETH" in message.replies[-1][0]


@pytest.mark.asyncio
async def test_live_command_blocks_credential_rotation_with_live_position(monkeypatch):
    user = SimpleNamespace(config=SimpleNamespace(bot_mode=BotMode.LIVE))
    position = SimpleNamespace(asset="BTC")
    executor = ReconcilingExecutor([position])
    session = SimpleNamespace(executor=executor)
    message = FakeMessage()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id="1"),
        effective_message=message,
    )
    telegram = KaraTelegram.__new__(KaraTelegram)
    telegram.bot_app = FakeBotApp(session)
    telegram._is_authorized = lambda _update: True
    telegram._is_throttled = lambda *args, **kwargs: False
    monkeypatch.setattr("notify.telegram.user_db.get_user", lambda chat_id: user)

    await telegram.cmd_live(update, SimpleNamespace(user_data={}))

    assert executor.calls == [True]
    assert "posisi Live masih terbuka" in message.replies[-1][0]


@pytest.mark.asyncio
async def test_paper_command_fails_closed_when_reconciliation_fails(monkeypatch):
    user = SimpleNamespace(config=SimpleNamespace(bot_mode=BotMode.LIVE))
    executor = ReconcilingExecutor(error=RuntimeError("REST unavailable"))
    session = SimpleNamespace(executor=executor)
    message = FakeMessage()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id="1"),
        effective_message=message,
    )
    telegram = KaraTelegram.__new__(KaraTelegram)
    telegram.bot_app = FakeBotApp(session)
    telegram._is_authorized = lambda _update: True
    activated = []

    async def activate(*args):
        activated.append(args)

    telegram._activate_paper_mode = activate
    monkeypatch.setattr("notify.telegram.user_db.get_user", lambda chat_id: user)

    await telegram.cmd_paper(update, SimpleNamespace())

    assert activated == []
    assert executor.calls == [True]
    assert "belum dapat diverifikasi" in message.replies[-1][0]


@pytest.mark.asyncio
async def test_failed_close_all_callback_cannot_activate_paper(monkeypatch):
    user = SimpleNamespace(config=SimpleNamespace(bot_mode=BotMode.LIVE))
    position = SimpleNamespace(asset="BTC", entry_price=100)

    class Executor:
        open_positions = [position]

        async def reconcile_if_due(self, force=False):
            return True

        async def close_all_positions(self, prices):
            return [{
                "action": "close_all_failed",
                "failed_assets": ["BTC"],
                "fully_closed": False,
            }]

    session = SimpleNamespace(executor=Executor())
    query = FakeQuery()
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id="1"),
        effective_message=SimpleNamespace(),
    )
    telegram = KaraTelegram.__new__(KaraTelegram)
    telegram.bot_app = FakeBotApp(session)
    telegram._pending_pnl_cards = {}
    telegram._pending_signals = {}
    activated = []

    async def mark_price(*args):
        return 100

    async def activate(*args):
        activated.append(args)

    telegram._execution_mark_price = mark_price
    telegram._activate_paper_mode = activate
    monkeypatch.setattr("notify.telegram.user_db.get_user", lambda chat_id: user)

    await telegram.on_callback(update, SimpleNamespace(user_data={}))

    assert activated == []
    assert user.config.bot_mode == BotMode.LIVE
    assert "Gagal pindah Paper" in query.edits[-1][0]
    assert "BTC" in query.edits[-1][0]


@pytest.mark.asyncio
async def test_scalper_warning_uses_actual_live_caps_not_stale_paper_values(monkeypatch):
    message = FakeMessage()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id="1"),
        effective_message=message,
    )
    telegram = KaraTelegram.__new__(KaraTelegram)
    telegram._is_authorized = lambda _update: True
    telegram._is_throttled = lambda *args, **kwargs: False
    monkeypatch.setattr("notify.telegram.config.BYBIT_LIVE_MAX_LEVERAGE", 20)
    monkeypatch.setattr("notify.telegram.config.BYBIT_LIVE_MAX_RISK_PER_TRADE_PCT", 0.035)

    await telegram.cmd_scalper(update, SimpleNamespace())

    text = message.replies[-1][0]
    assert "20x" in text
    assert "3.5%" in text
    assert "25-35x" not in text
    assert "13%" not in text


@pytest.mark.asyncio
async def test_live_status_shows_bybit_health_without_credentials():
    account = SimpleNamespace(
        mode=BotMode.LIVE,
        is_paused=False,
        kill_switch_active=False,
        unrealized_pnl=0,
        daily_pnl=0,
        daily_pnl_pct=0,
        wallet_balance=100,
        total_equity=100,
        available=90,
        current_drawdown_pct=0,
        positions=[],
    )

    class Session:
        user = SimpleNamespace(chat_id="1")
        risk_mgr = SimpleNamespace(status={})

        async def get_account_state(self):
            return account

        def bybit_status(self):
            return {
                "environment": "BYBIT TESTNET",
                "rest_healthy": True,
                "rest_latency_ms": 12,
                "ws_connected": True,
                "ws_stale": False,
                "last_reconciliation_at": 0,
                "reconciliation_mismatch_count": 0,
                "hard_sl_healthy_count": 1,
                "hard_sl_missing_count": 0,
                "entry_latency_ms": 20,
                "fill_latency_ms": 10,
                "close_latency_ms": 0,
                "price_bridge_gap_pct": 0.001,
                "circuit_open": False,
                "circuit_remaining_s": 0,
            }

    telegram = KaraTelegram.__new__(KaraTelegram)

    text, _keyboard = await telegram._get_status_content(Session())

    assert "Koneksi Bybit: <b>SEHAT</b>" in text
    assert "WS CONNECTED" in text
    assert "api" not in text.lower()
    assert "secret" not in text.lower()


@pytest.mark.asyncio
async def test_demo_status_shows_environment_and_sizing_limit():
    account = SimpleNamespace(
        mode=BotMode.LIVE, is_paused=False, kill_switch_active=False,
        unrealized_pnl=0, daily_pnl=0, daily_pnl_pct=0, wallet_balance=100,
        total_equity=100, available=90, current_drawdown_pct=0, positions=[],
    )
    class Session:
        user = SimpleNamespace(chat_id="1")
        risk_mgr = SimpleNamespace(status={})
        async def get_account_state(self): return account
        def bybit_status(self):
            return {
                "environment": "BYBIT DEMO", "rest_healthy": True,
                "ws_connected": True, "ws_stale": False,
                "capital_allocation_idr": 1_000_000,
                "capital_allocation_usd": 62.5, "sizing_equity": 62.5,
            }

    telegram = KaraTelegram.__new__(KaraTelegram)
    text, _keyboard = await telegram._get_status_content(Session())

    assert "Mode: <b>BYBIT DEMO</b>" in text
    assert "Saldo Demo untuk trading: <b>$100.00</b>" in text


@pytest.mark.asyncio
async def test_live_confirm_bootstraps_metadata_and_closes_old_ws_and_rest(monkeypatch):
    events = []
    user = SimpleNamespace(
        bybit_api_key=None,
        bybit_api_secret=None,
        bybit_testnet=True,
        bybit_authorized=False,
        config=SimpleNamespace(bot_mode=BotMode.PAPER),
    )

    class OldWS:
        async def stop(self):
            events.append("old_ws")

    class OldREST:
        async def close(self):
            events.append("old_rest")

    class BotApp:
        sessions = {"1": SimpleNamespace(bybit_ws=OldWS(), bybit_client=OldREST())}

        async def ensure_bybit_public_client(self):
            events.append("metadata")

        async def close_user_session(self, chat_id):
            old = self.sessions.pop(chat_id, None)
            if old:
                await old.bybit_ws.stop()
                await old.bybit_client.close()

        async def get_session(self, chat_id):
            if chat_id in self.sessions:
                return self.sessions[chat_id]
            events.append("new_session")
            self.sessions[chat_id] = SimpleNamespace()
            return self.sessions[chat_id]

    query = FakeQuery()
    query.data = "bybit_live_confirm"
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id="1"),
        effective_message=SimpleNamespace(),
    )
    telegram = KaraTelegram.__new__(KaraTelegram)
    telegram.bot_app = BotApp()
    telegram._pending_pnl_cards = {}
    telegram._pending_signals = {}
    updates = []
    monkeypatch.setattr("notify.telegram.user_db.get_user", lambda chat_id: user)
    monkeypatch.setattr("notify.telegram.user_db.update_user", lambda value: updates.append(value.config.bot_mode))

    await telegram.on_callback(
        update,
        SimpleNamespace(user_data={
            "pending_bybit_key": "new-api-key",
            "pending_bybit_secret": "new-api-secret",
            "pending_bybit_testnet": True,
        }),
    )

    assert events == ["metadata", "old_ws", "old_rest", "new_session"]
    assert user.config.bot_mode == BotMode.LIVE
    assert user.bybit_authorized is True
    assert updates == [BotMode.LIVE]


@pytest.mark.asyncio
async def test_failed_reactivation_restores_previous_live_user_state(monkeypatch):
    user = SimpleNamespace(
        bybit_api_key="old-key",
        bybit_api_secret="old-secret",
        bybit_testnet=True,
        bybit_authorized=True,
        config=SimpleNamespace(bot_mode=BotMode.LIVE),
    )

    class BotApp:
        sessions = {"1": SimpleNamespace(executor=ReconcilingExecutor())}
        attempts = 0

        async def ensure_bybit_public_client(self):
            return None

        async def close_user_session(self, chat_id):
            self.sessions.pop(chat_id, None)

        async def get_session(self, chat_id):
            self.attempts += 1
            if self.attempts == 1:
                return self.sessions[chat_id]
            if self.attempts == 2:
                raise RuntimeError("new private session failed")
            restored = SimpleNamespace(executor=ReconcilingExecutor())
            self.sessions[chat_id] = restored
            return restored

    query = FakeQuery()
    query.data = "bybit_live_confirm"
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id="1"),
        effective_message=SimpleNamespace(),
    )
    telegram = KaraTelegram.__new__(KaraTelegram)
    telegram.bot_app = BotApp()
    telegram._pending_pnl_cards = {}
    telegram._pending_signals = {}
    monkeypatch.setattr("notify.telegram.user_db.get_user", lambda chat_id: user)
    monkeypatch.setattr("notify.telegram.user_db.update_user", lambda value: None)

    await telegram.on_callback(
        update,
        SimpleNamespace(user_data={
            "pending_bybit_key": "new-key",
            "pending_bybit_secret": "new-secret",
            "pending_bybit_testnet": True,
        }),
    )

    assert user.bybit_api_key == "old-key"
    assert user.bybit_api_secret == "old-secret"
    assert user.bybit_authorized is True
    assert user.config.bot_mode == BotMode.LIVE
    assert telegram.bot_app.attempts == 3
    assert "Aktivasi gagal" in query.edits[-1][0]


@pytest.mark.asyncio
async def test_live_confirm_refuses_environment_changed_after_preflight(monkeypatch):
    user = SimpleNamespace(
        bybit_api_key=None,
        bybit_api_secret=None,
        bybit_testnet=True,
        bybit_authorized=False,
        config=SimpleNamespace(bot_mode=BotMode.PAPER),
    )
    query = FakeQuery()
    query.data = "bybit_live_confirm"
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id="1"),
        effective_message=SimpleNamespace(),
    )
    telegram = KaraTelegram.__new__(KaraTelegram)
    telegram.bot_app = FakeBotApp(SimpleNamespace(executor=ReconcilingExecutor()))
    telegram._pending_pnl_cards = {}
    telegram._pending_signals = {}
    monkeypatch.setattr("notify.telegram.user_db.get_user", lambda chat_id: user)
    monkeypatch.setattr("notify.telegram.config.BYBIT_TESTNET", False)

    await telegram.on_callback(
        update,
        SimpleNamespace(user_data={
            "pending_bybit_key": "new-key",
            "pending_bybit_secret": "new-secret",
            "pending_bybit_testnet": True,
        }),
    )

    assert user.config.bot_mode == BotMode.PAPER
    assert user.bybit_authorized is False
    assert "environment berubah" in query.edits[-1][0]
