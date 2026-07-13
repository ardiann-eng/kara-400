"""Fail-closed static validation for execution configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


BYBIT_MAINNET_ACK_VALUE = "I_UNDERSTAND_BYBIT_MAINNET_RISK"


class StartupConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class BybitPreflightResult:
    credentials_valid: bool
    can_read_account: bool
    can_trade_contracts: bool
    withdrawal_enabled: Optional[bool]
    account_type: str
    position_mode: str
    testnet: bool
    available_usdt: float


def validate_bybit_preflight(result: BybitPreflightResult) -> List[str]:
    """Validate read-only checks performed later by BybitClient."""
    errors = []
    if not result.credentials_valid:
        errors.append("Bybit API credentials invalid")
    if not result.can_read_account:
        errors.append("Bybit API key cannot read account")
    if not result.can_trade_contracts:
        errors.append("Bybit API key lacks contract trading permission")
    if result.withdrawal_enabled is True:
        errors.append("Bybit API key must not have withdrawal permission")
    if result.account_type.upper() != "UNIFIED":
        errors.append("Bybit account must use UNIFIED account type")
    if result.position_mode.lower() != "one_way":
        errors.append("Bybit account must use one-way position mode")
    if result.available_usdt < 0:
        errors.append("Bybit available USDT cannot be negative")
    return errors


def validate_startup_config(cfg) -> None:
    """Reject unsafe or contradictory environment settings before connecting."""
    errors = []
    trade_mode = str(cfg.TRADE_MODE).lower()
    data_source = str(cfg.DATA_SOURCE).lower()
    exchange = str(cfg.EXECUTION_EXCHANGE).lower()

    if trade_mode not in ("paper", "live"):
        errors.append("KARA_TRADE_MODE must be 'paper' or 'live'")
    if data_source not in ("mainnet", "testnet"):
        errors.append("KARA_DATA_SOURCE must be 'mainnet' or 'testnet'")
    if exchange != "bybit":
        errors.append(
            "KARA_EXECUTION_EXCHANGE must be 'bybit'; Hyperliquid is read-only market data"
        )

    if exchange == "bybit":
        if cfg.BYBIT_ACCOUNT_TYPE != "UNIFIED":
            errors.append("BYBIT_ACCOUNT_TYPE must be UNIFIED")
        if cfg.BYBIT_CATEGORY != "linear":
            errors.append("BYBIT_CATEGORY must be linear")
        if cfg.BYBIT_SETTLE_COIN != "USDT":
            errors.append("BYBIT_SETTLE_COIN must be USDT")
        if not 1000 <= cfg.BYBIT_RECV_WINDOW <= 10000:
            errors.append("BYBIT_RECV_WINDOW must be between 1000 and 10000")
        if not 0 < cfg.BYBIT_MAX_PRICE_GAP_PCT <= 0.02:
            errors.append("BYBIT_MAX_PRICE_GAP_PCT must be within (0, 0.02]")
        if not 0 < cfg.BYBIT_MAX_SLIPPAGE_PCT <= 0.02:
            errors.append("BYBIT_MAX_SLIPPAGE_PCT must be within (0, 0.02]")
        allowlist = tuple(getattr(cfg, "BYBIT_LIVE_ASSET_ALLOWLIST", ()))
        if not allowlist or any(not str(asset).strip() for asset in allowlist):
            errors.append("BYBIT_LIVE_ASSET_ALLOWLIST must not be empty")
        if not 1 <= getattr(cfg, "BYBIT_LIVE_MAX_LEVERAGE", 0) <= 100:
            errors.append("BYBIT_LIVE_MAX_LEVERAGE must be within [1, 100]")
        if not 1 <= getattr(cfg, "BYBIT_LIVE_MAX_POSITIONS", 0) <= 20:
            errors.append("BYBIT_LIVE_MAX_POSITIONS must be within [1, 20]")
        per_trade_risk = getattr(cfg, "BYBIT_LIVE_MAX_RISK_PER_TRADE_PCT", 0)
        total_risk = getattr(cfg, "BYBIT_LIVE_MAX_TOTAL_RISK_PCT", 0)
        if not 0 < per_trade_risk <= 0.10:
            errors.append("BYBIT_LIVE_MAX_RISK_PER_TRADE_PCT must be within (0, 0.10]")
        if not per_trade_risk <= total_risk <= 0.50:
            errors.append("BYBIT_LIVE_MAX_TOTAL_RISK_PCT must cover per-trade risk and be <= 0.50")
        symbol_notional = getattr(cfg, "BYBIT_LIVE_MAX_SYMBOL_NOTIONAL_PCT", 0)
        total_notional = getattr(cfg, "BYBIT_LIVE_MAX_TOTAL_NOTIONAL_PCT", 0)
        if symbol_notional <= 0 or total_notional < symbol_notional:
            errors.append("Bybit total notional cap must cover positive symbol notional cap")
        if not 1 <= getattr(cfg, "BYBIT_LIVE_MAX_SIGNAL_AGE_S", 0) <= 300:
            errors.append("BYBIT_LIVE_MAX_SIGNAL_AGE_S must be within [1, 300]")
        if not 1 <= getattr(cfg, "BYBIT_LIVE_MAX_QUOTE_AGE_S", 0) <= 30:
            errors.append("BYBIT_LIVE_MAX_QUOTE_AGE_S must be within [1, 30]")
        spread = getattr(cfg, "BYBIT_LIVE_MAX_SPREAD_PCT", 0)
        if not 0 < spread <= cfg.BYBIT_MAX_SLIPPAGE_PCT:
            errors.append("BYBIT_LIVE_MAX_SPREAD_PCT must be positive and <= slippage cap")
        if getattr(cfg, "BYBIT_LIVE_MIN_DEPTH_RATIO", 0) < 1:
            errors.append("BYBIT_LIVE_MIN_DEPTH_RATIO must be >= 1")

        if trade_mode == "live":
            if not cfg.FERNET_KEY:
                errors.append("Bybit live execution requires FERNET_KEY")
            if not cfg.BYBIT_TESTNET and cfg.BYBIT_MAINNET_ACK != BYBIT_MAINNET_ACK_VALUE:
                errors.append(
                    "Bybit mainnet requires BYBIT_MAINNET_ACK="
                    f"{BYBIT_MAINNET_ACK_VALUE}"
                )
            if getattr(cfg, "BYBIT_TESTNET_ONLY", True) and not cfg.BYBIT_TESTNET:
                errors.append("Bybit mainnet is locked while BYBIT_TESTNET_ONLY=true")

    if errors:
        raise StartupConfigurationError("; ".join(errors))
