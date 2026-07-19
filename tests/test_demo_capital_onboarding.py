from decimal import Decimal
from types import SimpleNamespace
import sys

import pytest

sys.modules.setdefault("eth_account", SimpleNamespace())

from core.capital_allocation import (
    CapitalAllocationError,
    apply_allocation,
    convert_allocation_idr,
    parse_allocation_idr,
    sizing_equity,
)
from data.bybit_client import BybitClient, BybitError
from execution.demo_universe import exact_demo_universe, is_demo_execution_eligible
from execution.exchange_client import VenueAccount
from execution.symbol_registry import BybitSymbolRegistry
from models.schemas import User


def test_idr_allocation_conversion_and_sizing_equity():
    allocation = convert_allocation_idr(1_000_000, 16_000)
    assert allocation.usd == 62.5
    assert sizing_equity(999, allocation.usd) == 62.5


def test_demo_preflight_http_403_explains_network_block_not_bad_credentials():
    from notify.telegram import bybit_preflight_failure_message

    text = bybit_preflight_failure_message(
        BybitError("Bybit returned non-JSON HTTP 403"), "demo"
    )

    assert "bukan bukti API Key atau API Secret salah" in text
    assert "tidak ada order dibuat" in text
    assert "credential baru dulu" in text


@pytest.mark.asyncio
async def test_access_code_registration_starts_demo_environment_onboarding(monkeypatch):
    from notify.telegram import KaraTelegram, WAITING_CAPITAL_ALLOCATION

    user = User(chat_id="1", paper_balance_usd=100)
    bot = KaraTelegram.__new__(KaraTelegram)
    bot._authorized_chat_ids = set()
    bot.bot_app = None
    bot._save_state = lambda: None
    replies = []
    message = SimpleNamespace(text="CODE", reply_html=lambda text, **kwargs: replies.append((text, kwargs)))
    async def reply_html(text, **kwargs):
        replies.append((text, kwargs))
    message.reply_html = reply_html
    update = SimpleNamespace(effective_chat=SimpleNamespace(id="1"), effective_user=SimpleNamespace(username="u", first_name="u"), message=message, effective_message=message)
    monkeypatch.setattr("notify.telegram.user_db.get_user", lambda _: user)
    monkeypatch.setattr("notify.telegram.user_db.update_user", lambda _: None)
    monkeypatch.setattr("notify.telegram.config.ALL_ACCESS_CODES", ("CODE",))
    ctx = SimpleNamespace(user_data={})
    assert await bot.cmd_access_code(update, ctx) == WAITING_CAPITAL_ALLOCATION
    assert ctx.user_data["pending_execution_environment"] == "demo"
    assert "Setup Bybit Demo" in replies[0][0]
    assert "langkah 1/4" in replies[0][0]


@pytest.mark.asyncio
async def test_existing_paper_user_requires_explicit_demo_setup(monkeypatch):
    from notify.telegram import KaraTelegram, WAITING_CAPITAL_ALLOCATION

    user = User(chat_id="1", paper_balance_usd=100, is_authorized=True, tos_agreed=True)
    bot = KaraTelegram.__new__(KaraTelegram)
    bot._is_authorized = lambda _: True
    bot.bot_app = None
    replies = []

    class Message:
        async def reply_html(self, text, **kwargs):
            replies.append(text)

    update = SimpleNamespace(effective_chat=SimpleNamespace(id="1"), effective_message=Message())
    monkeypatch.setattr("notify.telegram.user_db.get_user", lambda _: user)
    ctx = SimpleNamespace(user_data={})

    assert await bot.cmd_demo(update, ctx) == WAITING_CAPITAL_ALLOCATION
    assert user.config.bot_mode.value == "paper"
    assert ctx.user_data["pending_execution_environment"] == "demo"
    assert "Setup Bybit Demo" in replies[0]
    assert "langkah 1/4" in replies[0]


@pytest.mark.asyncio
async def test_demo_allocation_confirmation_shows_official_demo_api_guide(monkeypatch):
    from core.capital_allocation import convert_allocation_idr
    from notify.telegram import KaraTelegram, WAITING_BYBIT_KEY

    edits = []
    class Query:
        data = "onboard_allocation_confirm"
        async def answer(self, *args, **kwargs): pass
        async def edit_message_text(self, text, **kwargs):
            edits.append((text, kwargs))

    class BotApp:
        async def get_session(self, _chat_id):
            return SimpleNamespace(executor=SimpleNamespace(open_positions=[]))
    bot = KaraTelegram.__new__(KaraTelegram)
    bot.bot_app = BotApp()
    bot._pending_pnl_cards, bot._pending_signals = {}, {}
    update = SimpleNamespace(
        callback_query=Query(), effective_chat=SimpleNamespace(id="1"),
        effective_message=SimpleNamespace(),
    )
    ctx = SimpleNamespace(user_data={
        "pending_execution_environment": "demo",
        "pending_capital_allocation": convert_allocation_idr(1_000_000, 16_000),
    })

    assert await bot.handle_allocation_confirmation(update, ctx) == WAITING_BYBIT_KEY
    text, kwargs = edits[0]
    assert "langkah 3/4" in text
    assert "Jangan pakai API Key Testnet atau Mainnet" in text
    urls = [button.url for row in kwargs["reply_markup"].inline_keyboard for button in row if button.url]
    assert "https://bybit-exchange.github.io/docs/v5/demo" in urls


@pytest.mark.asyncio
async def test_demo_preflight_403_returns_to_key_step_without_losing_allocation(monkeypatch):
    from core.capital_allocation import convert_allocation_idr
    from notify.telegram import KaraTelegram, WAITING_BYBIT_KEY

    sent, deleted = [], []
    class DemoClient:
        def __init__(self, **kwargs): pass
        async def connect(self): pass
        async def sync_clock(self): pass
        async def close(self): pass
        async def preflight(self):
            raise BybitError("Bybit returned non-JSON HTTP 403")
    class Chat:
        async def send_message(self, text, **kwargs): sent.append(text)
    class CredentialMessage:
        text = "demo-secret"
        async def delete(self): deleted.append(True)

    bot = KaraTelegram.__new__(KaraTelegram)
    bot._is_authorized = lambda _: True
    monkeypatch.setattr("data.bybit_client.BybitClient", DemoClient)
    allocation = convert_allocation_idr(1_000_000, 16_000)
    ctx = SimpleNamespace(user_data={
        "pending_execution_environment": "demo",
        "pending_capital_allocation": allocation,
        "pending_bybit_key": "demo-key",
    })
    update = SimpleNamespace(effective_message=CredentialMessage(), effective_chat=Chat())

    assert await bot.handle_bybit_secret(update, ctx) == WAITING_BYBIT_KEY
    assert deleted == [True]
    assert "pending_bybit_key" not in ctx.user_data
    assert ctx.user_data["pending_capital_allocation"] is allocation
    assert any("bukan bukti API Key atau API Secret salah" in text for text in sent)


@pytest.mark.asyncio
async def test_existing_paper_start_explains_entry_block_and_demo_migration(monkeypatch):
    from notify.telegram import KaraTelegram

    user = User(chat_id="1", paper_balance_usd=100, is_authorized=True, tos_agreed=True)
    replies = []
    class Message:
        async def reply_html(self, text, **kwargs): replies.append(text)
    bot = KaraTelegram.__new__(KaraTelegram)
    bot._authorized_chat_ids = set()
    bot._save_state = lambda: None
    bot.bot_app = None
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id="1"),
        effective_user=SimpleNamespace(username="u", first_name="u"),
        effective_message=Message(),
    )
    monkeypatch.setattr("notify.telegram.user_db.get_user", lambda _: user)
    await bot.cmd_start(update, SimpleNamespace(user_data={}))
    assert "Paper tidak menerima trade baru" in replies[0]
    assert "/demo" in replies[0]


@pytest.mark.asyncio
async def test_demo_onboarding_sets_requested_virtual_balance_then_activates(monkeypatch):
    from core.capital_allocation import convert_allocation_idr
    from core.startup_validation import BybitPreflightResult
    from models.schemas import BotMode, ExecutionEnvironment
    from notify.telegram import KaraTelegram

    user = User(chat_id="1", paper_balance_usd=100, is_authorized=True)
    sent, deleted, updates = [], [], []

    class DemoClient:
        def __init__(self, **kwargs):
            assert kwargs["demo"] is True and kwargs["testnet"] is False
        async def connect(self): pass
        async def sync_clock(self): pass
        async def close(self): pass
        async def preflight(self):
            return BybitPreflightResult(True, True, True, None, "UNIFIED", "one_way", False, 100)
        async def set_demo_usdt_balance(self, amount):
            assert amount == 62.5
            return VenueAccount(62.5, 62.5, 62.5, 0, 0)
        async def get_account(self):
            return VenueAccount(100, 100, 100, 0, 0)

    class Chat:
        async def send_message(self, text, **kwargs): sent.append((text, kwargs))
    class CredentialMessage:
        text = "demo-secret"
        async def delete(self): deleted.append(True)
    class Query:
        data = "bybit_live_confirm"
        async def answer(self, *args, **kwargs): pass
        async def edit_message_text(self, text, **kwargs): sent.append((text, kwargs))
    class BotApp:
        async def get_session(self, chat_id):
            return SimpleNamespace(executor=SimpleNamespace(open_positions=[]))
        async def ensure_bybit_public_client(self): pass
        async def close_user_session(self, chat_id): pass

    bot = KaraTelegram.__new__(KaraTelegram)
    bot.bot_app = BotApp()
    bot._pending_pnl_cards, bot._pending_signals = {}, {}
    bot._is_authorized = lambda _: True
    monkeypatch.setattr("data.bybit_client.BybitClient", DemoClient)
    monkeypatch.setattr("notify.telegram.user_db.get_user", lambda _: user)
    monkeypatch.setattr("notify.telegram.user_db.update_user", lambda value: updates.append(value))
    ctx = SimpleNamespace(user_data={
        "pending_execution_environment": "demo",
        "pending_capital_allocation": convert_allocation_idr(1_000_000, 16_000),
        "pending_bybit_key": "demo-key",
    })
    update = SimpleNamespace(effective_message=CredentialMessage(), effective_chat=Chat())
    await bot.handle_bybit_secret(update, ctx)
    assert deleted == [True]
    assert ctx.user_data["pending_bybit_venue_equity"] == 62.5
    confirm_update = SimpleNamespace(callback_query=Query(), effective_chat=SimpleNamespace(id="1"), effective_message=SimpleNamespace())
    await bot.on_callback(confirm_update, ctx)
    assert user.bybit_environment == ExecutionEnvironment.DEMO
    assert user.config.bot_mode == BotMode.LIVE
    assert user.capital_allocation_usd is None
    assert updates == [user]


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "99999"])
def test_invalid_allocation_rejected(raw):
    with pytest.raises(CapitalAllocationError):
        parse_allocation_idr(raw)


def test_allocation_cannot_exceed_venue_or_change_open_position():
    user = User(chat_id="1", paper_balance_usd=100)
    allocation = convert_allocation_idr(2_000_000, 16_000)
    with pytest.raises(CapitalAllocationError, match="exceeds"):
        apply_allocation(user, allocation, 100, has_open_position=False)
    with pytest.raises(CapitalAllocationError, match="open"):
        apply_allocation(user, convert_allocation_idr(1_000_000, 16_000), 100, has_open_position=True)


@pytest.mark.asyncio
async def test_demo_balance_set_reduces_existing_wallet_to_requested_capital(monkeypatch):
    client = BybitClient(api_key="key", api_secret="secret", testnet=False, demo=True)
    calls = []

    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {}

    balances = iter([
        VenueAccount(100, 100, 100, 0, 0),
        VenueAccount(62.5, 62.5, 62.5, 0, 0),
    ])
    async def account():
        return next(balances)

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(client, "get_account", account)
    assert (await client.set_demo_usdt_balance(Decimal("62.50"))).total_equity == 62.5
    assert calls == [("POST", "/v5/account/demo-apply-money", {
        "body": {
            "adjustType": 1,
            "utaDemoApplyMoney": [{"coin": "USDT", "amountStr": "37.50"}],
        }, "auth": True, "retries": 0
    })]


@pytest.mark.asyncio
@pytest.mark.parametrize("testnet,demo", [(False, False), (True, False)])
async def test_demo_fund_rejects_mainnet_and_testnet(testnet, demo):
    client = BybitClient(api_key="key", api_secret="secret", testnet=testnet, demo=demo)
    with pytest.raises(BybitError, match="only"):
        await client.set_demo_usdt_balance("1")


def test_top_100_requires_exact_active_bybit_metadata():
    registry = BybitSymbolRegistry(aliases={"KBONK": "1000BONKUSDT"})
    registry.load([
        {"symbol": "BTCUSDT", "status": "Trading", "contractType": "LinearPerpetual", "settleCoin": "USDT", "baseCoin": "BTC", "priceFilter": {"tickSize": "0.1"}, "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "minNotionalValue": "5"}, "leverageFilter": {"maxLeverage": "50"}},
        {"symbol": "BADUSDT", "status": "PreLaunch", "contractType": "LinearPerpetual", "settleCoin": "USDT", "baseCoin": "BAD", "priceFilter": {"tickSize": "0.1"}, "lotSizeFilter": {"qtyStep": "1", "minOrderQty": "1"}, "leverageFilter": {"maxLeverage": "1"}},
    ])
    assert exact_demo_universe(["BTC", "ETH", "BAD"], registry) == ["BTC"]
    assert is_demo_execution_eligible("BTC", registry) is True
    assert is_demo_execution_eligible("ETH", registry) is False


def test_demo_execution_gate_is_wired_before_per_user_signal_selection():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    gate = source.index("[DEMO-UNIVERSE-BLOCK]")
    selection = source.index("base_signal = signals_dict.get", gate)
    assert gate < selection


@pytest.mark.asyncio
async def test_mainnet_executor_sizing_uses_allocation_not_full_venue_equity(monkeypatch):
    from execution.bybit_executor import BybitExecutor
    from execution.exchange_client import InstrumentSpec
    from models.schemas import Side, TradeSignal, SignalStrength, MarketRegime, ScoreBreakdown

    registry = BybitSymbolRegistry()
    registry.load([{"symbol": "BTCUSDT", "status": "Trading", "contractType": "LinearPerpetual", "settleCoin": "USDT", "baseCoin": "BTC", "priceFilter": {"tickSize": "0.1"}, "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "minNotionalValue": "5"}, "leverageFilter": {"maxLeverage": "50"}}])
    user = User(chat_id="1", paper_balance_usd=100, capital_allocation_usd=50)
    from models.schemas import ExecutionEnvironment
    user.bybit_environment = ExecutionEnvironment.MAINNET

    class Risk:
        status = {}
        def pre_trade_check(self, signal, account, positions):
            assert account.total_equity == 50
            return False, "stop after sizing check"

    executor = BybitExecutor(chat_id="1", client=object(), risk_manager=Risk(), symbol_registry=registry, price_bridge=object(), user=user)
    async def account():
        from models.schemas import AccountState, BotMode, ExecutionMode
        return AccountState(total_equity=100, wallet_balance=100, available=100, used_margin=0, unrealized_pnl=0, daily_pnl=0, daily_pnl_pct=0, peak_balance=100, current_drawdown_pct=0, positions=[], mode=BotMode.LIVE, execution_mode=ExecutionMode.FULL_AUTO)
    monkeypatch.setattr(executor, "get_account_state", account)
    signal = TradeSignal(signal_id="s", asset="BTC", side=Side.LONG, score=60, strength=SignalStrength.MODERATE, regime=MarketRegime.NORMAL, breakdown=ScoreBreakdown(), entry_price=100, stop_loss=99, tp1=101, tp2=102, suggested_leverage=1)
    assert await executor.open_position(signal) is None


@pytest.mark.asyncio
async def test_demo_executor_uses_full_demo_balance(monkeypatch):
    from execution.bybit_executor import BybitExecutor
    from models.schemas import ExecutionEnvironment, Side, TradeSignal, SignalStrength, MarketRegime, ScoreBreakdown

    registry = BybitSymbolRegistry()
    registry.load([{"symbol": "BTCUSDT", "status": "Trading", "contractType": "LinearPerpetual", "settleCoin": "USDT", "baseCoin": "BTC", "priceFilter": {"tickSize": "0.1"}, "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "minNotionalValue": "5"}, "leverageFilter": {"maxLeverage": "50"}}])
    user = User(chat_id="1", paper_balance_usd=100, capital_allocation_usd=50)
    user.bybit_environment = ExecutionEnvironment.DEMO

    class Risk:
        status = {}
        def pre_trade_check(self, signal, account, positions):
            assert account.total_equity == 100
            return False, "stop after demo balance check"

    executor = BybitExecutor(chat_id="1", client=object(), risk_manager=Risk(), symbol_registry=registry, price_bridge=object(), user=user)
    async def account():
        from models.schemas import AccountState, BotMode, ExecutionMode
        return AccountState(total_equity=100, wallet_balance=100, available=100, used_margin=0, unrealized_pnl=0, daily_pnl=0, daily_pnl_pct=0, peak_balance=100, current_drawdown_pct=0, positions=[], mode=BotMode.LIVE, execution_mode=ExecutionMode.FULL_AUTO)
    monkeypatch.setattr(executor, "get_account_state", account)
    signal = TradeSignal(signal_id="s", asset="BTC", side=Side.LONG, score=60, strength=SignalStrength.MODERATE, regime=MarketRegime.NORMAL, breakdown=ScoreBreakdown(), entry_price=100, stop_loss=99, tp1=101, tp2=102, suggested_leverage=1)
    assert await executor.open_position(signal) is None
