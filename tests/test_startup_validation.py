from types import SimpleNamespace

import pytest

from core.startup_validation import (
    BYBIT_MAINNET_ACK_VALUE,
    BybitPreflightResult,
    StartupConfigurationError,
    validate_bybit_preflight,
    validate_startup_config,
)


def make_config(**overrides):
    values = {
        "TRADE_MODE": "paper",
        "DATA_SOURCE": "mainnet",
        "EXECUTION_EXCHANGE": "bybit",
        "PRIVATE_KEY": "",
        "BYBIT_TESTNET": True,
        "BYBIT_ACCOUNT_TYPE": "UNIFIED",
        "BYBIT_CATEGORY": "linear",
        "BYBIT_SETTLE_COIN": "USDT",
        "BYBIT_RECV_WINDOW": 5000,
        "BYBIT_MAX_PRICE_GAP_PCT": 0.003,
        "BYBIT_MAX_SLIPPAGE_PCT": 0.002,
        "BYBIT_MAINNET_ACK": "",
        "BYBIT_TESTNET_ONLY": True,
        "BYBIT_LIVE_ASSET_ALLOWLIST": ("BTC", "ETH"),
        "BYBIT_LIVE_MAX_LEVERAGE": 20,
        "BYBIT_LIVE_MAX_POSITIONS": 3,
        "BYBIT_LIVE_MAX_RISK_PER_TRADE_PCT": 0.035,
        "BYBIT_LIVE_MAX_TOTAL_RISK_PCT": 0.105,
        "BYBIT_LIVE_MAX_SYMBOL_NOTIONAL_PCT": 7.0,
        "BYBIT_LIVE_MAX_TOTAL_NOTIONAL_PCT": 21.0,
        "BYBIT_LIVE_MAX_SIGNAL_AGE_S": 30,
        "BYBIT_LIVE_MAX_QUOTE_AGE_S": 5,
        "BYBIT_LIVE_MAX_SPREAD_PCT": 0.0015,
        "BYBIT_LIVE_MIN_DEPTH_RATIO": 1.0,
        "FERNET_KEY": "fernet-present",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_paper_mode_needs_no_execution_credentials():
    validate_startup_config(make_config())


def test_hyperliquid_execution_is_always_rejected():
    with pytest.raises(StartupConfigurationError, match="read-only market data"):
        validate_startup_config(make_config(EXECUTION_EXCHANGE="hyperliquid"))


def test_bybit_live_requires_credentials():
    with pytest.raises(StartupConfigurationError, match="FERNET_KEY"):
        validate_startup_config(
            make_config(
                TRADE_MODE="live",
                EXECUTION_EXCHANGE="bybit",
                FERNET_KEY="",
            )
        )


def test_bybit_testnet_live_does_not_require_mainnet_ack():
    validate_startup_config(
        make_config(
            TRADE_MODE="live",
            EXECUTION_EXCHANGE="bybit",
            BYBIT_TESTNET=True,
        )
    )


def test_bybit_mainnet_requires_exact_acknowledgement():
    cfg = make_config(
        TRADE_MODE="live",
        EXECUTION_EXCHANGE="bybit",
        BYBIT_TESTNET=False,
    )
    with pytest.raises(StartupConfigurationError, match="BYBIT_MAINNET_ACK"):
        validate_startup_config(cfg)

    cfg.BYBIT_MAINNET_ACK = BYBIT_MAINNET_ACK_VALUE
    with pytest.raises(StartupConfigurationError, match="BYBIT_TESTNET_ONLY"):
        validate_startup_config(cfg)

    cfg.BYBIT_TESTNET_ONLY = False
    validate_startup_config(cfg)


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("BYBIT_LIVE_ASSET_ALLOWLIST", (), "ALLOWLIST"),
        ("BYBIT_LIVE_MAX_LEVERAGE", 0, "MAX_LEVERAGE"),
        ("BYBIT_LIVE_MAX_POSITIONS", 0, "MAX_POSITIONS"),
        ("BYBIT_LIVE_MAX_RISK_PER_TRADE_PCT", 0.2, "RISK_PER_TRADE"),
        ("BYBIT_LIVE_MAX_SIGNAL_AGE_S", 0, "SIGNAL_AGE"),
        ("BYBIT_LIVE_MAX_SPREAD_PCT", 0.003, "SPREAD"),
        ("BYBIT_LIVE_MIN_DEPTH_RATIO", 0.5, "DEPTH"),
    ],
)
def test_phase_10_live_limits_fail_closed(field, value, match):
    with pytest.raises(StartupConfigurationError, match=match):
        validate_startup_config(make_config(**{field: value}))


def test_preflight_rejects_dangerous_permissions_and_account_mode():
    result = BybitPreflightResult(
        credentials_valid=True,
        can_read_account=True,
        can_trade_contracts=True,
        withdrawal_enabled=True,
        account_type="UNIFIED",
        position_mode="hedge",
        testnet=True,
        available_usdt=100.0,
    )

    errors = validate_bybit_preflight(result)

    assert "withdrawal permission" in " ".join(errors)
    assert "one-way" in " ".join(errors)
