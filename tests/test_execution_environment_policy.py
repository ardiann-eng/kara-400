from models.schemas import BotMode, ExecutionEnvironment, User
from core.execution_environment_policy import requires_demo_onboarding


def test_legacy_paper_requires_demo_onboarding():
    user = User(chat_id="1", paper_balance_usd=100)
    assert requires_demo_onboarding(user) is True


def test_demo_and_mainnet_do_not_match_paper_entry_block():
    user = User(chat_id="1", paper_balance_usd=100)
    user.config.bot_mode = BotMode.LIVE
    user.bybit_environment = ExecutionEnvironment.DEMO
    assert requires_demo_onboarding(user) is False
    user.bybit_environment = ExecutionEnvironment.MAINNET
    assert requires_demo_onboarding(user) is False
