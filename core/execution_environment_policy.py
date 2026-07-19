"""Environment migration policy. Blocks new Paper entries, never Paper exits."""

from __future__ import annotations

from models.schemas import BotMode, ExecutionEnvironment


def requires_demo_onboarding(user) -> bool:
    """Legacy Paper users must complete Demo setup before any new entry."""
    return (
        user.config.bot_mode == BotMode.PAPER
        and getattr(user, "bybit_environment", ExecutionEnvironment.PAPER)
        == ExecutionEnvironment.PAPER
    )
