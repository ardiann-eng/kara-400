from datetime import datetime, timezone

import pytest

from models.schemas import AccountState, BotMode, ExecutionMode
from risk.risk_manager import RiskManager
from tests.test_bybit_executor import make_signal


def test_kill_switch_never_auto_resets_when_drawdown_recovers(monkeypatch):
    risk = RiskManager.__new__(RiskManager)
    risk._chat_id = ""
    risk._daily_pnl = 0
    risk._peak_balance = 1000
    risk._session_start_balance = 1000
    risk._last_reset_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    risk._cooldown_until = None
    risk._kill_switch = True
    risk._paused = False
    risk._latest_score = {}
    account = AccountState(
        total_equity=1000,
        wallet_balance=1000,
        available=1000,
        used_margin=0,
        unrealized_pnl=0,
        daily_pnl=0,
        daily_pnl_pct=0,
        peak_balance=1000,
        current_drawdown_pct=0,
        mode=BotMode.LIVE,
        execution_mode=ExecutionMode.SEMI_AUTO,
    )
    monkeypatch.setattr(risk, "_cfg", lambda: __import__("config").SCALPER)

    approved, reason = risk.pre_trade_check(make_signal(), account, [])

    assert approved is False
    assert "KILL SWITCH ACTIVE" in reason
    assert risk._kill_switch is True


def test_kill_switch_reset_requires_admin_and_persists(monkeypatch):
    risk = RiskManager.__new__(RiskManager)
    risk._chat_id = "user-1"
    risk._kill_switch = True
    persisted = []
    risk._persist_risk_state = lambda: persisted.append(risk._kill_switch)
    monkeypatch.setenv("ADMIN_CHAT_ID", "admin-1")

    with pytest.raises(PermissionError, match="Hanya admin"):
        risk.reset_kill_switch("not-admin")
    assert risk._kill_switch is True
    assert persisted == []

    risk.reset_kill_switch("admin-1")
    assert risk._kill_switch is False
    assert persisted == [False]
