from datetime import datetime, timezone

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
