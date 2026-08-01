"""Opt-in Bybit testnet lifecycle drill. Never imported by bot startup."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import getpass
import json
import math
import os
from pathlib import Path
import sys
from typing import Callable, Optional

from data.bybit_client import BybitClient, BybitError
from data.bybit_private_ws import BybitPrivateWebSocket
from core.startup_validation import validate_bybit_preflight
from execution.exchange_client import ExecutionOrderStatus, InstrumentSpec, VenueOrder
from models.schemas import Side
from utils.helpers import gen_id


ALLOWED_SYMBOLS = {"BTC", "ETH"}
MAX_PARTIAL_DRILL_NOTIONAL_USDT = 250.0


class DrillSafetyError(RuntimeError):
    pass


@dataclass
class DrillEvidence:
    timestamp_utc: str
    environment: str
    account_masked: str
    symbol: str
    side: str
    quantity: float
    scenario: str = "lifecycle"
    entry_order_link_id_masked: str = ""
    entry_fill_price: float = 0.0
    partial_fill_price: float = 0.0
    partial_filled_qty: float = 0.0
    protected_remainder_size: float = 0.0
    exit_fill_price: float = 0.0
    fee: float = 0.0
    hard_sl_present: bool = False
    observed_stop_loss: float = 0.0
    missing_sl_detected: bool = False
    emergency_close_attempted: bool = False
    ws_reconnect_count: int = 0
    forced_rest_reconciliation: bool = False
    multi_position_symbols: Optional[list[str]] = None
    close_all_closed_symbols: Optional[list[str]] = None
    probe_observations: Optional[dict] = None
    partial_tp_clears_full_sl: Optional[bool] = None
    partial_calls_accumulate: Optional[bool] = None
    full_sl_reapply_clears_partial_tp: Optional[bool] = None
    partial_tp_visible_as_stop_order: Optional[bool] = None
    native_tp_order_ids_captured: Optional[bool] = None
    native_tp_targets_live: Optional[int] = None
    native_tp_trigger_prices: Optional[list] = None
    native_tp_planned_prices: Optional[list] = None
    reconciliation_result: str = "not_run"
    ws_state: str = "not_used"
    final_position_size: float = 0.0
    result: str = "failed"
    error: str = ""


def mask(value: str, visible: int = 4) -> str:
    value = str(value or "")
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}...{value[-visible:]}"


def smallest_valid_quantity(spec: InstrumentSpec, mark_price: float) -> float:
    if mark_price <= 0:
        raise DrillSafetyError("Bybit mark price must be positive")
    notional_steps = math.ceil(
        (spec.min_notional / mark_price) / spec.qty_step - 1e-12
    )
    minimum_steps = math.ceil(spec.min_qty / spec.qty_step - 1e-12)
    steps = max(notional_steps, minimum_steps, 1)
    quantity = steps * spec.qty_step
    return float(f"{quantity:.12g}")


def partial_drill_quantity(spec: InstrumentSpec, mark_price: float) -> float:
    """Return two independently valid slices, capped for controlled drills."""
    quantity = smallest_valid_quantity(spec, mark_price) * 2
    notional = quantity * mark_price
    if notional > MAX_PARTIAL_DRILL_NOTIONAL_USDT:
        raise DrillSafetyError(
            f"Partial drill notional {notional:.2f} exceeds "
            f"{MAX_PARTIAL_DRILL_NOTIONAL_USDT:.2f} USDT cap"
        )
    return float(f"{quantity:.12g}")


def native_target_drill_quantity(spec: InstrumentSpec, mark_price: float) -> float:
    """Smallest size whose TP1 slice still clears the venue step.

    KARA closes 25% at TP1, so a native target needs roughly four steps of size.
    Anything smaller legitimately skips native targets, which this drill is not
    trying to exercise.
    """
    base = smallest_valid_quantity(spec, mark_price)
    steps = math.ceil(spec.qty_step / (0.25 * spec.qty_step))
    quantity = max(base, steps * spec.qty_step)
    notional = quantity * mark_price
    if notional > MAX_PARTIAL_DRILL_NOTIONAL_USDT:
        raise DrillSafetyError(
            f"Native target drill notional {notional:.2f} exceeds "
            f"{MAX_PARTIAL_DRILL_NOTIONAL_USDT:.2f} USDT cap"
        )
    return float(f"{quantity:.12g}")


def validate_environment(*, environment: str, confirmed: bool, full_auto: bool) -> None:
    if environment not in {"testnet", "demo"}:
        raise DrillSafetyError("Drill environment must be testnet or demo")
    if not confirmed:
        raise DrillSafetyError(f"Required flag missing: --confirm-{environment}")
    if full_auto:
        raise DrillSafetyError("Drill refuses KARA_FULL_AUTO=true")


def read_drill_credentials(
    environment: str,
    *,
    environ=os.environ,
    prompt: Callable[[str], str] = getpass.getpass,
) -> tuple[str, str]:
    """Read environment-specific credentials or one hidden comma-separated prompt."""
    prefix = f"BYBIT_{environment.upper()}"
    key = str(environ.get(f"{prefix}_API_KEY", "")).strip()
    secret = str(environ.get(f"{prefix}_API_SECRET", "")).strip()
    if bool(key) != bool(secret):
        raise DrillSafetyError(
            f"Set both {prefix}_API_KEY and {prefix}_API_SECRET, or neither"
        )
    if key:
        return key, secret
    credential_pair = prompt(
        f"Bybit {environment} API key,API secret: "
    ).strip()
    if credential_pair.count(",") != 1:
        raise DrillSafetyError(
            "Enter Demo credentials as API_KEY,API_SECRET with exactly one comma"
        )
    key, secret = (part.strip() for part in credential_pair.split(",", 1))
    if not key or not secret:
        raise DrillSafetyError("Bybit API key and API secret are required")
    return key, secret


async def wait_for_fill(
    client: BybitClient,
    symbol: str,
    client_order_id: str,
    *,
    attempts: int = 20,
    delay_s: float = 0.5,
) -> VenueOrder:
    latest = None
    for _ in range(attempts):
        latest = await client.get_order(symbol, client_order_id)
        if latest.status in (
            ExecutionOrderStatus.FILLED,
            ExecutionOrderStatus.CANCELLED,
            ExecutionOrderStatus.REJECTED,
        ):
            return latest
        await asyncio.sleep(delay_s)
    raise BybitError(f"Order did not reach terminal state: {client_order_id}")


async def place_and_confirm(
    client: BybitClient,
    *,
    symbol: str,
    side: Side,
    quantity: float,
    prefix: str,
    reduce_only: bool = False,
) -> VenueOrder:
    order_link_id = gen_id(prefix)
    await client.place_order(
        symbol=symbol,
        side=side,
        quantity=quantity,
        client_order_id=order_link_id,
        reduce_only=reduce_only,
    )
    return await wait_for_fill(client, symbol, order_link_id)


async def close_exchange_position(
    client: BybitClient,
    *,
    symbol: str,
    entry_side: Side,
) -> Optional[VenueOrder]:
    positions = await client.get_positions(symbol)
    venue = next((item for item in positions if item.side == entry_side), None)
    if not venue or venue.size <= 0:
        return None
    close_side = Side.SHORT if entry_side == Side.LONG else Side.LONG
    fill = await place_and_confirm(
        client,
        symbol=symbol,
        side=close_side,
        quantity=venue.size,
        prefix="KARA-DRILL-CLEANUP",
        reduce_only=True,
    )
    if fill.status != ExecutionOrderStatus.FILLED:
        raise BybitError("Cleanup close was not filled")
    return fill


async def prepare_drill(
    client: BybitClient,
    *,
    asset: str,
    environment: str,
    require_funds: bool,
) -> tuple[InstrumentSpec, float, float]:
    asset = asset.upper()
    if asset not in ALLOWED_SYMBOLS:
        raise DrillSafetyError("Only BTC and ETH are allowed for initial drills")
    await client.connect()
    await client.sync_clock()
    await client.load_instruments()
    preflight = await client.preflight()
    errors = validate_bybit_preflight(preflight)
    if errors:
        raise DrillSafetyError("; ".join(errors))
    expected_testnet = environment == "testnet"
    if preflight.testnet != expected_testnet or client.testnet != expected_testnet:
        raise DrillSafetyError("Preflight did not confirm Bybit testnet")
    if environment == "demo" and not getattr(client, "demo", False):
        raise DrillSafetyError("Preflight did not confirm Bybit Demo Trading")
    if require_funds and preflight.available_usdt <= 0:
        raise DrillSafetyError(
            f"{environment.capitalize()} available USDT must be positive before drill"
        )
    spec = await client.get_instrument(asset)
    return spec, await client.get_mark_price(spec.symbol), preflight.available_usdt


def environment_label(environment: str) -> str:
    return "BYBIT TESTNET" if environment == "testnet" else "BYBIT DEMO TRADING"


async def run_hold_protected(
    client: BybitClient,
    *,
    asset: str,
    side: Side,
    environment: str = "testnet",
    confirm: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> DrillEvidence:
    spec, mark_price, _ = await prepare_drill(
        client, asset=asset, environment=environment, require_funds=True
    )
    if await client.get_positions(spec.symbol):
        raise DrillSafetyError(f"Refusing hold: existing {spec.symbol} position")
    quantity = smallest_valid_quantity(spec, mark_price)
    evidence = DrillEvidence(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        environment=environment_label(environment),
        account_masked=mask(client.api_key),
        symbol=spec.symbol,
        side=side.value,
        quantity=quantity,
        scenario="hold_protected",
    )
    output(
        f"Environment: {evidence.environment} | Account: {evidence.account_masked} | "
        f"Scenario: HOLD PROTECTED | Symbol: {spec.symbol} | Quantity: {quantity}"
    )
    if confirm(f"Type {environment.upper()} to hold protected {side.value.upper()} {quantity} {spec.symbol}: ").strip() != environment.upper():
        raise DrillSafetyError("Operator confirmation rejected")
    total_fee = 0.0
    try:
        await client.set_leverage(spec.symbol, 1)
        entry = await place_and_confirm(
            client, symbol=spec.symbol, side=side, quantity=quantity, prefix="KARA-DRILL-HOLD"
        )
        evidence.entry_order_link_id_masked = mask(entry.client_order_id)
        evidence.entry_fill_price = entry.average_fill_price
        total_fee += entry.fee_paid
        if entry.status != ExecutionOrderStatus.FILLED or entry.filled_qty != quantity:
            raise BybitError("Protected hold entry was not fully filled")
        sl_raw = entry.average_fill_price * (0.99 if side == Side.LONG else 1.01)
        await client.set_protection(
            symbol=spec.symbol,
            side=side,
            stop_loss=client.symbol_registry.normalize_price(spec, sl_raw),
        )
        positions = await client.get_positions(spec.symbol)
        venue = next((item for item in positions if item.side == side), None)
        evidence.hard_sl_present = bool(venue and venue.stop_loss)
        evidence.observed_stop_loss = float(venue.stop_loss or 0) if venue else 0.0
        evidence.final_position_size = venue.size if venue else 0.0
        if not venue or venue.size != quantity or not venue.stop_loss:
            raise BybitError("Protected hold verification failed")
        evidence.reconciliation_result = "exchange_protected"
        evidence.result = "held_protected"
    except Exception as exc:
        evidence.error = f"{type(exc).__name__}: {exc}"
        try:
            cleanup = await close_exchange_position(client, symbol=spec.symbol, entry_side=side)
            if cleanup:
                evidence.exit_fill_price = cleanup.average_fill_price
                total_fee += cleanup.fee_paid
        except Exception as cleanup_exc:
            evidence.error = f"{evidence.error}; cleanup: {type(cleanup_exc).__name__}: {cleanup_exc}"
        positions = await client.get_positions(spec.symbol)
        evidence.final_position_size = sum(item.size for item in positions)
        if evidence.final_position_size:
            evidence.reconciliation_result = "unresolved_position"
    evidence.fee = total_fee
    return evidence


async def run_partial_tp_mode_probe(
    client: BybitClient,
    *,
    asset: str,
    side: Side,
    environment: str = "testnet",
    confirm: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> DrillEvidence:
    """Settle, by observation, how Bybit V5 tpslMode Full and Partial interact.

    Bybit's set-trading-stop documentation does not state whether installing a
    Partial TP clears a Full-mode stopLoss, whether repeated Partial calls add or
    overwrite, or whether position.stopLoss reflects Partial stops. KARA cannot
    install native partial TPs without those answers, because a wrong assumption
    silently removes hard protection from a live position.
    """
    spec, mark_price, _ = await prepare_drill(
        client, asset=asset, environment=environment, require_funds=True
    )
    if await client.get_positions(spec.symbol):
        raise DrillSafetyError(f"Refusing probe: existing {spec.symbol} position")
    quantity = partial_drill_quantity(spec, mark_price)
    evidence = DrillEvidence(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        environment=environment_label(environment),
        account_masked=mask(client.api_key),
        symbol=spec.symbol,
        side=side.value,
        quantity=quantity,
        scenario="partial_tp_mode_probe",
    )
    output(
        f"Environment: {evidence.environment} | Account: {evidence.account_masked} | "
        f"Scenario: PARTIAL TP MODE PROBE | Symbol: {spec.symbol} | Quantity: {quantity}"
    )
    prompt = (
        f"Type {environment.upper()} to probe tpslMode with {side.value.upper()} "
        f"{quantity} {spec.symbol}: "
    )
    if confirm(prompt).strip() != environment.upper():
        raise DrillSafetyError("Operator confirmation rejected")

    registry = client.symbol_registry
    observations: dict = {}
    total_fee = 0.0

    async def snapshot(stage: str) -> dict:
        positions = await client.get_positions(spec.symbol)
        venue = next((item for item in positions if item.side == side), None)
        try:
            stop_orders = await client.get_open_orders(
                spec.symbol, order_filter="StopOrder"
            )
        except Exception as exc:
            stop_orders = []
            observations.setdefault("stop_order_read_errors", []).append(
                f"{stage}: {type(exc).__name__}: {exc}"
            )
        record = {
            "position_size": venue.size if venue else 0.0,
            "position_stop_loss": float(venue.stop_loss or 0) if venue else 0.0,
            "position_take_profit": float(venue.take_profit or 0) if venue else 0.0,
            "stop_order_count": len(stop_orders),
            "stop_orders": [
                {
                    "stopOrderType": row.get("stopOrderType"),
                    "tpslMode": row.get("tpslMode"),
                    "triggerPrice": row.get("triggerPrice"),
                    "qty": row.get("qty"),
                    "reduceOnly": row.get("reduceOnly"),
                }
                for row in stop_orders
            ],
        }
        observations[stage] = record
        output(
            f"  [{stage}] position SL={record['position_stop_loss']} "
            f"TP={record['position_take_profit']} "
            f"stop_orders={record['stop_order_count']}"
        )
        return record

    try:
        await client.set_leverage(spec.symbol, 1)
        entry = await place_and_confirm(
            client,
            symbol=spec.symbol,
            side=side,
            quantity=quantity,
            prefix="KARA-DRILL-TPMODE",
        )
        evidence.entry_order_link_id_masked = mask(entry.client_order_id)
        evidence.entry_fill_price = entry.average_fill_price
        total_fee += entry.fee_paid
        if entry.status != ExecutionOrderStatus.FILLED or entry.filled_qty != quantity:
            raise BybitError("Probe entry was not fully filled")

        fill = entry.average_fill_price
        long_side = side == Side.LONG
        stop_loss = registry.normalize_price(spec, fill * (0.98 if long_side else 1.02))
        tp1 = registry.normalize_price(spec, fill * (1.02 if long_side else 0.98))
        tp2 = registry.normalize_price(spec, fill * (1.04 if long_side else 0.96))
        slice_qty = registry.normalize_quantity(spec, entry.filled_qty / 2)
        if slice_qty <= 0 or slice_qty >= entry.filled_qty:
            raise DrillSafetyError("Probe requires two non-empty quantity slices")

        # Stage 1 establishes the baseline: a Full-mode stop on the whole position.
        await client.set_protection(symbol=spec.symbol, side=side, stop_loss=stop_loss)
        baseline = await snapshot("1_after_full_sl")
        evidence.hard_sl_present = bool(baseline["position_stop_loss"])
        evidence.observed_stop_loss = baseline["position_stop_loss"]
        if not evidence.hard_sl_present:
            raise BybitError("Probe baseline failed: Full-mode stop loss not visible")

        # Stage 2 is the decisive question for KARA.
        await client.add_partial_tp_sl(
            symbol=spec.symbol,
            side=side,
            take_profit=tp1,
            stop_loss=stop_loss,
            quantity=slice_qty,
        )
        after_first = await snapshot("2_after_partial_tp1")

        # Stage 3 shows whether a second Partial call adds or replaces.
        await client.add_partial_tp_sl(
            symbol=spec.symbol,
            side=side,
            take_profit=tp2,
            stop_loss=stop_loss,
            quantity=registry.normalize_quantity(spec, entry.filled_qty - slice_qty),
        )
        after_second = await snapshot("3_after_partial_tp2")

        # Stage 4 shows whether KARA's own reconcile reinstall would wipe the targets.
        await client.set_protection(symbol=spec.symbol, side=side, stop_loss=stop_loss)
        after_reapply = await snapshot("4_after_full_sl_reapplied")

        evidence.partial_tp_clears_full_sl = bool(
            baseline["position_stop_loss"] and not after_first["position_stop_loss"]
        )
        evidence.partial_tp_visible_as_stop_order = (
            after_first["stop_order_count"] > baseline["stop_order_count"]
        )
        evidence.partial_calls_accumulate = (
            after_second["stop_order_count"] > after_first["stop_order_count"]
        )
        evidence.full_sl_reapply_clears_partial_tp = (
            after_first["stop_order_count"] > 0
            and after_reapply["stop_order_count"] < after_second["stop_order_count"]
        )
        evidence.probe_observations = observations
        evidence.result = "passed"
    except Exception as exc:
        evidence.error = f"{type(exc).__name__}: {exc}"
        evidence.probe_observations = observations
    finally:
        try:
            cleanup = await close_exchange_position(
                client, symbol=spec.symbol, entry_side=side
            )
            if cleanup:
                evidence.exit_fill_price = cleanup.average_fill_price
                total_fee += cleanup.fee_paid
        except Exception as cleanup_exc:
            evidence.error = (
                f"{evidence.error}; cleanup: "
                f"{type(cleanup_exc).__name__}: {cleanup_exc}"
            ).lstrip("; ")
        remaining = await client.get_positions(spec.symbol)
        evidence.final_position_size = sum(item.size for item in remaining)
        evidence.reconciliation_result = (
            "unresolved_position" if evidence.final_position_size else "exchange_zero"
        )
        if evidence.final_position_size:
            evidence.result = "failed"
    evidence.fee = total_fee
    return evidence


class _DrillRiskManager:
    """Forces one pre-validated quantity so the drill exercises the real executor
    entry path without pulling in live risk configuration."""

    def __init__(self, contracts: float, leverage: int = 1):
        self.status = {
            "peak_balance": 0, "daily_pnl": 0, "paused": False, "kill_switch": False
        }
        self._contracts = contracts
        self._leverage = leverage

    def pre_trade_check(self, signal, account, positions):
        return True, "drill"

    def calculate_position_size(self, signal, equity):
        return 0.0, self._contracts, self._leverage

    def check_tp_trail(self, position, price, market_state=None):
        return None

    def record_pnl(self, pnl, balance):
        return None


async def run_native_tp_lifecycle(
    client: BybitClient,
    *,
    asset: str,
    side: Side,
    environment: str = "testnet",
    confirm: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> DrillEvidence:
    """Drive BybitExecutor.open_position against the venue and prove the native
    targets it installs are actually live and attributable.

    This exercises production code, not the raw client, so a passing run is
    evidence that a real KARA entry leaves visible TP1/TP2 on Bybit.
    """
    from execution.bybit_executor import BybitExecutor
    from execution.price_bridge import HyperliquidBybitPriceBridge
    from models.schemas import (
        MarketRegime, ScoreBreakdown, SignalStrength, TradeSignal,
    )

    spec, mark_price, _ = await prepare_drill(
        client, asset=asset, environment=environment, require_funds=True
    )
    if await client.get_positions(spec.symbol):
        raise DrillSafetyError(f"Refusing drill: existing {spec.symbol} position")
    quantity = native_target_drill_quantity(spec, mark_price)
    evidence = DrillEvidence(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        environment=environment_label(environment),
        account_masked=mask(client.api_key),
        symbol=spec.symbol,
        side=side.value,
        quantity=quantity,
        scenario="native_tp_lifecycle",
    )
    output(
        f"Environment: {evidence.environment} | Account: {evidence.account_masked} | "
        f"Scenario: NATIVE TP LIFECYCLE | Symbol: {spec.symbol} | Quantity: {quantity}"
    )
    prompt = (
        f"Type {environment.upper()} to run executor entry with "
        f"{side.value.upper()} {quantity} {spec.symbol}: "
    )
    if confirm(prompt).strip() != environment.upper():
        raise DrillSafetyError("Operator confirmation rejected")

    long_side = side == Side.LONG
    executor = BybitExecutor(
        chat_id="drill",
        client=client,
        risk_manager=_DrillRiskManager(quantity),
        symbol_registry=client.symbol_registry,
        price_bridge=HyperliquidBybitPriceBridge(0.01),
        fill_timeout_s=10.0,
        poll_interval_s=0.5,
    )
    signal = TradeSignal(
        signal_id="drill-native-tp",
        asset=asset.upper(),
        side=side,
        score=70,
        strength=SignalStrength.MODERATE,
        regime=MarketRegime.NORMAL,
        breakdown=ScoreBreakdown(),
        entry_price=mark_price,
        stop_loss=mark_price * (0.98 if long_side else 1.02),
        tp1=mark_price * (1.02 if long_side else 0.98),
        tp2=mark_price * (1.04 if long_side else 0.96),
        suggested_leverage=1,
    )

    total_fee = 0.0
    try:
        position = await executor.open_position(signal)
        if position is None:
            raise BybitError("Executor declined the drill entry")
        evidence.entry_fill_price = position.entry_price
        evidence.hard_sl_present = bool(position.stop_loss)
        total_fee += position.entry_fee_paid
        evidence.native_tp_planned_prices = [position.tp1, position.tp2]
        evidence.native_tp_order_ids_captured = bool(
            position.native_tp1_order_id and position.native_tp2_order_id
        )

        rows = await client.get_open_orders(spec.symbol, order_filter="StopOrder")
        targets = [
            row
            for row in rows
            if str(row.get("stopOrderType", "")).endswith("TakeProfit")
        ]
        evidence.native_tp_targets_live = len(targets)
        evidence.native_tp_trigger_prices = sorted(
            float(row.get("triggerPrice") or 0) for row in targets
        )
        evidence.partial_tp_visible_as_stop_order = len(targets) == 2

        venue_positions = await client.get_positions(spec.symbol)
        venue = next((p for p in venue_positions if p.side == side), None)
        evidence.observed_stop_loss = float(venue.stop_loss or 0) if venue else 0.0
        evidence.partial_tp_clears_full_sl = not evidence.observed_stop_loss

        live_ids = {str(row.get("orderId") or "") for row in targets}
        ids_match = {
            position.native_tp1_order_id, position.native_tp2_order_id
        } <= live_ids
        evidence.probe_observations = {
            "planned_tp1": position.tp1,
            "planned_tp2": position.tp2,
            "live_target_triggers": evidence.native_tp_trigger_prices,
            "stored_ids_match_live_orders": ids_match,
            "position_stop_loss": evidence.observed_stop_loss,
            "targets": [
                {
                    "stopOrderType": row.get("stopOrderType"),
                    "tpslMode": row.get("tpslMode"),
                    "triggerPrice": row.get("triggerPrice"),
                    "qty": row.get("qty"),
                    "orderId_masked": mask(str(row.get("orderId", ""))),
                }
                for row in targets
            ],
        }
        output(
            f"  planned TP1/TP2 = {position.tp1} / {position.tp2}; "
            f"live target triggers = {evidence.native_tp_trigger_prices}; "
            f"position SL = {evidence.observed_stop_loss}"
        )
        if len(targets) != 2 or not ids_match or not evidence.observed_stop_loss:
            raise BybitError(
                "Native TP lifecycle verification failed: "
                f"targets={len(targets)} ids_match={ids_match} "
                f"sl={evidence.observed_stop_loss}"
            )
        evidence.result = "passed"
    except Exception as exc:
        evidence.error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            cleanup = await close_exchange_position(
                client, symbol=spec.symbol, entry_side=side
            )
            if cleanup:
                evidence.exit_fill_price = cleanup.average_fill_price
                total_fee += cleanup.fee_paid
        except Exception as cleanup_exc:
            evidence.error = (
                f"{evidence.error}; cleanup: "
                f"{type(cleanup_exc).__name__}: {cleanup_exc}"
            ).lstrip("; ")
        remaining = await client.get_positions(spec.symbol)
        evidence.final_position_size = sum(item.size for item in remaining)
        evidence.reconciliation_result = (
            "unresolved_position" if evidence.final_position_size else "exchange_zero"
        )
        if evidence.final_position_size:
            evidence.result = "failed"
    evidence.fee = total_fee
    return evidence


async def run_partial_tp_solo_probe(
    client: BybitClient,
    *,
    asset: str,
    side: Side,
    environment: str = "testnet",
    confirm: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> DrillEvidence:
    """Check whether a partial take-profit installs without a paired partial stop,
    and whether each installed target exposes a retrievable venue order id.

    KARA needs both answers: the pairing requirement stacks redundant stops on top
    of the full-position stop, and without order ids a native target fill cannot be
    attributed to a slice.
    """
    spec, mark_price, _ = await prepare_drill(
        client, asset=asset, environment=environment, require_funds=True
    )
    if await client.get_positions(spec.symbol):
        raise DrillSafetyError(f"Refusing probe: existing {spec.symbol} position")
    quantity = partial_drill_quantity(spec, mark_price)
    evidence = DrillEvidence(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        environment=environment_label(environment),
        account_masked=mask(client.api_key),
        symbol=spec.symbol,
        side=side.value,
        quantity=quantity,
        scenario="partial_tp_solo_probe",
    )
    output(
        f"Environment: {evidence.environment} | Account: {evidence.account_masked} | "
        f"Scenario: PARTIAL TP SOLO PROBE | Symbol: {spec.symbol} | Quantity: {quantity}"
    )
    prompt = (
        f"Type {environment.upper()} to probe solo partial TP with "
        f"{side.value.upper()} {quantity} {spec.symbol}: "
    )
    if confirm(prompt).strip() != environment.upper():
        raise DrillSafetyError("Operator confirmation rejected")

    registry = client.symbol_registry
    observations: dict = {}
    total_fee = 0.0

    async def snapshot(stage: str) -> dict:
        positions = await client.get_positions(spec.symbol)
        venue = next((item for item in positions if item.side == side), None)
        rows = await client.get_open_orders(spec.symbol, order_filter="StopOrder")
        record = {
            "position_size": venue.size if venue else 0.0,
            "position_stop_loss": float(venue.stop_loss or 0) if venue else 0.0,
            "position_take_profit": float(venue.take_profit or 0) if venue else 0.0,
            "stop_order_count": len(rows),
            "stop_orders": [
                {
                    "orderId_masked": mask(str(row.get("orderId", ""))),
                    "has_order_id": bool(row.get("orderId")),
                    "stopOrderType": row.get("stopOrderType"),
                    "tpslMode": row.get("tpslMode"),
                    "triggerPrice": row.get("triggerPrice"),
                    "qty": row.get("qty"),
                }
                for row in rows
            ],
        }
        observations[stage] = record
        output(
            f"  [{stage}] SL={record['position_stop_loss']} "
            f"TP={record['position_take_profit']} orders={record['stop_order_count']}"
        )
        return record

    try:
        await client.set_leverage(spec.symbol, 1)
        entry = await place_and_confirm(
            client,
            symbol=spec.symbol,
            side=side,
            quantity=quantity,
            prefix="KARA-DRILL-TPSOLO",
        )
        evidence.entry_order_link_id_masked = mask(entry.client_order_id)
        evidence.entry_fill_price = entry.average_fill_price
        total_fee += entry.fee_paid
        if entry.status != ExecutionOrderStatus.FILLED or entry.filled_qty != quantity:
            raise BybitError("Solo probe entry was not fully filled")

        fill = entry.average_fill_price
        long_side = side == Side.LONG
        stop_loss = registry.normalize_price(spec, fill * (0.98 if long_side else 1.02))
        tp1 = registry.normalize_price(spec, fill * (1.02 if long_side else 0.98))
        tp2 = registry.normalize_price(spec, fill * (1.04 if long_side else 0.96))
        slice_qty = registry.normalize_quantity(spec, entry.filled_qty / 2)

        await client.set_protection(symbol=spec.symbol, side=side, stop_loss=stop_loss)
        baseline = await snapshot("1_after_full_sl")
        evidence.hard_sl_present = bool(baseline["position_stop_loss"])
        evidence.observed_stop_loss = baseline["position_stop_loss"]

        # Decisive call: a target with no paired stop.
        await client.add_partial_tp_sl(
            symbol=spec.symbol, side=side, take_profit=tp1, quantity=slice_qty
        )
        solo_first = await snapshot("2_after_solo_tp1")
        await client.add_partial_tp_sl(
            symbol=spec.symbol,
            side=side,
            take_profit=tp2,
            quantity=registry.normalize_quantity(spec, entry.filled_qty - slice_qty),
        )
        solo_second = await snapshot("3_after_solo_tp2")

        targets = [
            row
            for row in solo_second["stop_orders"]
            if str(row["stopOrderType"] or "").endswith("TakeProfit")
        ]
        extra_stops = [
            row
            for row in solo_second["stop_orders"]
            if str(row["stopOrderType"] or "") == "PartialStopLoss"
        ]
        observations["verdict"] = {
            "solo_tp_accepted": len(targets) >= 1,
            "both_targets_installed": len(targets) == 2,
            "paired_stop_created_anyway": len(extra_stops) > 0,
            "every_target_has_order_id": bool(targets)
            and all(row["has_order_id"] for row in targets),
            "full_stop_survived": bool(solo_first["position_stop_loss"]),
        }
        evidence.partial_tp_clears_full_sl = not solo_first["position_stop_loss"]
        evidence.partial_tp_visible_as_stop_order = len(targets) >= 1
        evidence.partial_calls_accumulate = len(targets) == 2
        evidence.probe_observations = observations
        evidence.result = "passed"
    except Exception as exc:
        evidence.error = f"{type(exc).__name__}: {exc}"
        evidence.probe_observations = observations
    finally:
        try:
            cleanup = await close_exchange_position(
                client, symbol=spec.symbol, entry_side=side
            )
            if cleanup:
                evidence.exit_fill_price = cleanup.average_fill_price
                total_fee += cleanup.fee_paid
        except Exception as cleanup_exc:
            evidence.error = (
                f"{evidence.error}; cleanup: "
                f"{type(cleanup_exc).__name__}: {cleanup_exc}"
            ).lstrip("; ")
        remaining = await client.get_positions(spec.symbol)
        evidence.final_position_size = sum(item.size for item in remaining)
        evidence.reconciliation_result = (
            "unresolved_position" if evidence.final_position_size else "exchange_zero"
        )
        if evidence.final_position_size:
            evidence.result = "failed"
    evidence.fee = total_fee
    return evidence


async def run_recover_protected(
    client: BybitClient,
    *,
    asset: str,
    side: Side,
    environment: str = "testnet",
    confirm: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> DrillEvidence:
    spec, _, _ = await prepare_drill(client, asset=asset, environment=environment, require_funds=False)
    positions = await client.get_positions(spec.symbol)
    venue = next((item for item in positions if item.side == side), None)
    if len(positions) != 1 or not venue:
        raise DrillSafetyError(f"Recovery requires exactly one {side.value} {spec.symbol} position")
    evidence = DrillEvidence(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        environment=environment_label(environment),
        account_masked=mask(client.api_key),
        symbol=spec.symbol,
        side=side.value,
        quantity=venue.size,
        scenario="recover_protected",
        hard_sl_present=bool(venue.stop_loss),
        observed_stop_loss=float(venue.stop_loss or 0),
        protected_remainder_size=venue.size,
    )
    if not venue.stop_loss:
        raise DrillSafetyError("Recovery refuses unprotected position; inspect exchange and emergency-close manually")
    output(
        f"Environment: {evidence.environment} | Account: {evidence.account_masked} | "
        f"Scenario: RECOVER PROTECTED | Symbol: {spec.symbol} | Side: {side.value} | "
        f"Quantity: {venue.size} | Native SL: {venue.stop_loss}"
    )
    if confirm(f"Type {environment.upper()} to reduce-only close recovered {side.value.upper()} {venue.size} {spec.symbol}: ").strip() != environment.upper():
        raise DrillSafetyError("Operator confirmation rejected")
    try:
        close = await close_exchange_position(client, symbol=spec.symbol, entry_side=side)
        if not close:
            raise BybitError("Recovered position disappeared before close")
        evidence.exit_fill_price = close.average_fill_price
        evidence.fee = close.fee_paid
        remaining = await client.get_positions(spec.symbol)
        evidence.final_position_size = sum(item.size for item in remaining)
        if evidence.final_position_size:
            raise BybitError("Recovered position remains after reduce-only close")
        evidence.reconciliation_result = "exchange_zero"
        evidence.result = "passed"
    except Exception as exc:
        evidence.error = f"{type(exc).__name__}: {exc}"
        remaining = await client.get_positions(spec.symbol)
        evidence.final_position_size = sum(item.size for item in remaining)
        evidence.reconciliation_result = "unresolved_position"
    return evidence


async def run_missing_sl_emergency_close(
    client: BybitClient,
    *,
    asset: str,
    side: Side,
    environment: str = "testnet",
    confirm: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> DrillEvidence:
    """Deliberately remove SL, prove REST detection, then emergency-close."""
    spec, _, _ = await prepare_drill(client, asset=asset, environment=environment, require_funds=False)
    positions = await client.get_positions(spec.symbol)
    venue = next((item for item in positions if item.side == side), None)
    if len(positions) != 1 or not venue or not venue.stop_loss:
        raise DrillSafetyError(
            f"Missing-SL drill requires exactly one protected {side.value} {spec.symbol} position"
        )
    evidence = DrillEvidence(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        environment=environment_label(environment),
        account_masked=mask(client.api_key),
        symbol=spec.symbol,
        side=side.value,
        quantity=venue.size,
        scenario="missing_sl_emergency_close",
        hard_sl_present=True,
        observed_stop_loss=float(venue.stop_loss),
        protected_remainder_size=venue.size,
    )
    output(
        f"Environment: {evidence.environment} | Account: {evidence.account_masked} | "
        f"Scenario: REMOVE SL THEN EMERGENCY CLOSE | Symbol: {spec.symbol} | "
        f"Side: {side.value} | Quantity: {venue.size} | Native SL: {venue.stop_loss}"
    )
    if confirm(
        f"Type {environment.upper()} to cancel native SL then emergency-close {venue.size} {spec.symbol}: "
    ).strip() != environment.upper():
        raise DrillSafetyError("Operator confirmation rejected")
    try:
        await client.clear_stop_loss(spec.symbol)
        positions = await client.get_positions(spec.symbol)
        venue = next((item for item in positions if item.side == side), None)
        if not venue:
            raise BybitError("Position disappeared after SL cancellation")
        evidence.missing_sl_detected = not bool(venue.stop_loss)
        if not evidence.missing_sl_detected:
            raise BybitError("Native SL still present after requested cancellation")
        evidence.emergency_close_attempted = True
        close = await close_exchange_position(client, symbol=spec.symbol, entry_side=side)
        if not close:
            raise BybitError("Unprotected position disappeared before emergency close")
        evidence.exit_fill_price = close.average_fill_price
        evidence.fee = close.fee_paid
        remaining = await client.get_positions(spec.symbol)
        evidence.final_position_size = sum(item.size for item in remaining)
        if evidence.final_position_size:
            raise BybitError("Unprotected position remains after emergency close")
        evidence.reconciliation_result = "exchange_zero"
        evidence.result = "passed"
    except Exception as exc:
        evidence.error = f"{type(exc).__name__}: {exc}"
        positions = await client.get_positions(spec.symbol)
        venue = next((item for item in positions if item.side == side), None)
        evidence.final_position_size = sum(item.size for item in positions)
        if venue and not venue.stop_loss and not evidence.emergency_close_attempted:
            evidence.emergency_close_attempted = True
            try:
                close = await close_exchange_position(client, symbol=spec.symbol, entry_side=side)
                if close:
                    evidence.exit_fill_price = close.average_fill_price
                    evidence.fee += close.fee_paid
                positions = await client.get_positions(spec.symbol)
                evidence.final_position_size = sum(item.size for item in positions)
            except Exception as cleanup_exc:
                evidence.error = f"{evidence.error}; emergency cleanup: {type(cleanup_exc).__name__}: {cleanup_exc}"
        evidence.reconciliation_result = (
            "exchange_zero" if evidence.final_position_size == 0 else "unresolved_position"
        )
    return evidence


async def run_ws_reconnect_check(
    client: BybitClient,
    *,
    asset: str,
    side: Side,
    environment: str = "testnet",
    confirm: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> DrillEvidence:
    """Close local private WS transport and require reconnect-triggered REST proof."""
    spec, _, _ = await prepare_drill(client, asset=asset, environment=environment, require_funds=False)
    positions = await client.get_positions(spec.symbol)
    venue = next((item for item in positions if item.side == side), None)
    if len(positions) != 1 or not venue or not venue.stop_loss:
        raise DrillSafetyError(
            f"WS drill requires exactly one protected {side.value} {spec.symbol} position"
        )
    evidence = DrillEvidence(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        environment=environment_label(environment),
        account_masked=mask(client.api_key),
        symbol=spec.symbol,
        side=side.value,
        quantity=venue.size,
        scenario="ws_reconnect_rest_reconciliation",
        hard_sl_present=True,
        observed_stop_loss=float(venue.stop_loss),
        protected_remainder_size=venue.size,
        ws_state="connecting",
    )
    output(
        f"Environment: {evidence.environment} | Account: {evidence.account_masked} | "
        f"Scenario: WS RECONNECT + REST RECONCILIATION | Symbol: {spec.symbol} | "
        f"Side: {side.value} | Quantity: {venue.size} | Native SL: {venue.stop_loss}"
    )
    if confirm(f"Type {environment.upper()} to disconnect local private WS and force REST reconciliation: ").strip() != environment.upper():
        raise DrillSafetyError("Operator confirmation rejected")

    reconciled = asyncio.Event()

    async def on_reconnect() -> None:
        account = await client.get_account()
        restored = await client.get_positions(spec.symbol)
        restored_venue = next((item for item in restored if item.side == side), None)
        if account.total_equity < 0 or not restored_venue or not restored_venue.stop_loss:
            raise BybitError("Forced REST reconciliation did not restore protected position")
        evidence.forced_rest_reconciliation = True
        evidence.hard_sl_present = True
        evidence.observed_stop_loss = float(restored_venue.stop_loss)
        evidence.final_position_size = restored_venue.size
        reconciled.set()

    ws = BybitPrivateWebSocket(
        api_key=client.api_key,
        api_secret=client._api_secret,
        testnet=environment == "testnet",
        demo=environment == "demo",
        stale_after_s=5,
        on_reconnect=on_reconnect,
    )
    try:
        await ws.start()
        deadline = asyncio.get_running_loop().time() + 15
        while not ws.connected and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        if not ws.connected or not ws._ws:
            raise BybitError("Private WS did not connect before disconnect drill")
        await ws._ws.close()
        await asyncio.wait_for(reconciled.wait(), timeout=20)
        evidence.ws_reconnect_count = 1
        evidence.ws_state = "reconnected"
        evidence.reconciliation_result = "exchange_protected"
        evidence.result = "held_protected"
    except Exception as exc:
        evidence.error = f"{type(exc).__name__}: {exc}"
        evidence.ws_state = "failed"
        current = await client.get_positions(spec.symbol)
        evidence.final_position_size = sum(item.size for item in current)
        evidence.reconciliation_result = (
            "exchange_protected" if evidence.final_position_size and evidence.hard_sl_present else "unresolved_position"
        )
    finally:
        await ws.stop()
    return evidence


async def run_multi_position_close_all(
    client: BybitClient,
    *,
    environment: str = "testnet",
    confirm: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> DrillEvidence:
    """Open two protected positions, then reduce-only close each and reconcile zero."""
    btc, btc_mark, _ = await prepare_drill(client, asset="BTC", environment=environment, require_funds=True)
    eth, eth_mark, _ = await prepare_drill(client, asset="ETH", environment=environment, require_funds=True)
    if await client.get_positions():
        raise DrillSafetyError("Multi close-all drill requires zero existing exchange positions")
    specs = ((btc, btc_mark), (eth, eth_mark))
    quantities = {spec.symbol: smallest_valid_quantity(spec, mark) for spec, mark in specs}
    evidence = DrillEvidence(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        environment=environment_label(environment),
        account_masked=mask(client.api_key),
        symbol="BTCUSDT,ETHUSDT",
        side="long",
        quantity=sum(quantities.values()),
        scenario="multi_position_close_all",
        multi_position_symbols=[spec.symbol for spec, _ in specs],
        close_all_closed_symbols=[],
    )
    output(
        f"Environment: {evidence.environment} | Account: {evidence.account_masked} | "
        f"Scenario: TWO POSITION CLOSE-ALL | BTC: {quantities[btc.symbol]} | ETH: {quantities[eth.symbol]}"
    )
    if confirm(f"Type {environment.upper()} to open protected BTC and ETH then reduce-only close-all: ").strip() != environment.upper():
        raise DrillSafetyError("Operator confirmation rejected")
    total_fee = 0.0
    opened = []
    try:
        for spec, _ in specs:
            await client.set_leverage(spec.symbol, 1)
            entry = await place_and_confirm(client, symbol=spec.symbol, side=Side.LONG, quantity=quantities[spec.symbol], prefix="KARA-DRILL-CLOSEALL-ENTRY")
            if entry.status != ExecutionOrderStatus.FILLED or entry.filled_qty != quantities[spec.symbol]:
                raise BybitError(f"Close-all entry not fully filled: {spec.symbol}")
            total_fee += entry.fee_paid
            await client.set_protection(symbol=spec.symbol, side=Side.LONG, stop_loss=client.symbol_registry.normalize_price(spec, entry.average_fill_price * 0.99))
            venue = next((item for item in await client.get_positions(spec.symbol) if item.side == Side.LONG), None)
            if not venue or not venue.stop_loss:
                raise BybitError(f"Close-all native SL missing: {spec.symbol}")
            opened.append((spec.symbol, Side.LONG))
        evidence.hard_sl_present = True
        for symbol, side in opened:
            close = await close_exchange_position(client, symbol=symbol, entry_side=side)
            if not close:
                raise BybitError(f"Close-all position missing: {symbol}")
            total_fee += close.fee_paid
            evidence.close_all_closed_symbols.append(symbol)
        evidence.final_position_size = sum(item.size for item in await client.get_positions())
        if evidence.final_position_size:
            raise BybitError("Close-all left exchange position open")
        evidence.reconciliation_result = "exchange_zero"
        evidence.result = "passed"
    except Exception as exc:
        evidence.error = f"{type(exc).__name__}: {exc}"
        for symbol, side in opened:
            try:
                close = await close_exchange_position(client, symbol=symbol, entry_side=side)
                if close:
                    total_fee += close.fee_paid
                    if symbol not in evidence.close_all_closed_symbols:
                        evidence.close_all_closed_symbols.append(symbol)
            except Exception as cleanup_exc:
                evidence.error = f"{evidence.error}; cleanup {symbol}: {type(cleanup_exc).__name__}: {cleanup_exc}"
        evidence.final_position_size = sum(item.size for item in await client.get_positions())
        evidence.reconciliation_result = "exchange_zero" if evidence.final_position_size == 0 else "unresolved_position"
    evidence.fee = total_fee
    return evidence


async def run_lifecycle(
    client: BybitClient,
    *,
    asset: str,
    side: Side,
    partial_close: bool,
    environment: str = "testnet",
    confirm: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> DrillEvidence:
    spec, mark_price, available_usdt = await prepare_drill(
        client, asset=asset, environment=environment, require_funds=True
    )
    quantity = (
        partial_drill_quantity(spec, mark_price)
        if partial_close
        else smallest_valid_quantity(spec, mark_price)
    )
    existing = await client.get_positions(spec.symbol)
    if existing:
        raise DrillSafetyError(f"Refusing drill: existing {spec.symbol} position")

    evidence = DrillEvidence(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        environment=environment_label(environment),
        account_masked=mask(client.api_key),
        symbol=spec.symbol,
        side=side.value,
        quantity=quantity,
    )
    output(
        f"Environment: {evidence.environment} | Account: {evidence.account_masked} | "
        f"Available USDT: {available_usdt} | Symbol: {spec.symbol} | "
        f"Quantity: {quantity}"
    )
    prompt = (
        f"Type {environment.upper()} to place {side.value.upper()} {quantity} {spec.symbol} "
        f"at mark {mark_price}: "
    )
    if confirm(prompt).strip() != environment.upper():
        raise DrillSafetyError("Operator confirmation rejected")

    entry_fill = None
    total_fee = 0.0
    try:
        await client.set_leverage(spec.symbol, 1)
        entry_fill = await place_and_confirm(
            client,
            symbol=spec.symbol,
            side=side,
            quantity=quantity,
            prefix="KARA-DRILL-ENTRY",
        )
        evidence.entry_order_link_id_masked = mask(entry_fill.client_order_id)
        evidence.entry_fill_price = entry_fill.average_fill_price
        total_fee += entry_fill.fee_paid
        if entry_fill.status != ExecutionOrderStatus.FILLED or entry_fill.filled_qty <= 0:
            raise BybitError("Testnet entry was not fully filled")

        sl_raw = (
            entry_fill.average_fill_price * 0.99
            if side == Side.LONG
            else entry_fill.average_fill_price * 1.01
        )
        stop_loss = client.symbol_registry.normalize_price(spec, sl_raw)
        await client.set_protection(
            symbol=spec.symbol,
            side=side,
            stop_loss=stop_loss,
        )
        protected = await client.get_positions(spec.symbol)
        venue = next((item for item in protected if item.side == side), None)
        evidence.hard_sl_present = bool(venue and venue.stop_loss)
        if not evidence.hard_sl_present:
            raise BybitError("Native hard SL missing after entry")

        if partial_close:
            partial_qty = client.symbol_registry.normalize_quantity(spec, quantity / 2)
            close_side = Side.SHORT if side == Side.LONG else Side.LONG
            partial_fill = await place_and_confirm(
                client,
                symbol=spec.symbol,
                side=close_side,
                quantity=partial_qty,
                prefix="KARA-DRILL-PARTIAL",
                reduce_only=True,
            )
            evidence.partial_fill_price = partial_fill.average_fill_price
            evidence.partial_filled_qty = partial_fill.filled_qty
            total_fee += partial_fill.fee_paid
            if partial_fill.status != ExecutionOrderStatus.FILLED or partial_fill.filled_qty != partial_qty:
                raise BybitError("Partial close was not fully filled")
            remaining = await client.get_positions(spec.symbol)
            venue = next((item for item in remaining if item.side == side), None)
            evidence.protected_remainder_size = venue.size if venue else 0.0
            if not venue or venue.size <= 0 or not venue.stop_loss:
                raise BybitError("Partial close did not preserve protected remainder")

        final_fill = await close_exchange_position(
            client, symbol=spec.symbol, entry_side=side
        )
        if final_fill:
            evidence.exit_fill_price = final_fill.average_fill_price
            total_fee += final_fill.fee_paid
        evidence.reconciliation_result = "exchange_zero"
        evidence.result = "passed"
    except Exception as exc:
        evidence.error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            cleanup_fill = await close_exchange_position(
                client, symbol=spec.symbol, entry_side=side
            )
            if cleanup_fill:
                evidence.exit_fill_price = cleanup_fill.average_fill_price
                total_fee += cleanup_fill.fee_paid
        except Exception as cleanup_exc:
            evidence.error = (
                f"{evidence.error}; cleanup: {type(cleanup_exc).__name__}: {cleanup_exc}"
            ).strip("; ")
        try:
            final_positions = await client.get_positions(spec.symbol)
            evidence.final_position_size = sum(item.size for item in final_positions)
        except Exception as final_exc:
            evidence.final_position_size = -1
            evidence.error = (
                f"{evidence.error}; final check: {type(final_exc).__name__}: {final_exc}"
            ).strip("; ")
        evidence.fee = total_fee
        if evidence.final_position_size != 0:
            evidence.result = "failed"
            evidence.reconciliation_result = "unresolved_position"
    return evidence


def write_evidence(report_path: Path, evidence: DrillEvidence) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("a", encoding="utf-8") as report:
        report.write(json.dumps(asdict(evidence), sort_keys=True) + "\n")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("testnet", "demo"), default="testnet")
    parser.add_argument("--confirm-testnet", action="store_true")
    parser.add_argument("--confirm-demo", action="store_true")
    parser.add_argument("--symbol", choices=("BTC", "ETH"), required=True)
    parser.add_argument("--side", choices=("long", "short"), required=True)
    parser.add_argument("--partial-close", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--hold-protected", action="store_true")
    mode.add_argument("--recover-protected", action="store_true")
    mode.add_argument("--simulate-missing-sl", action="store_true")
    mode.add_argument("--ws-reconnect-check", action="store_true")
    mode.add_argument("--multi-close-all", action="store_true")
    mode.add_argument("--partial-tp-mode-probe", action="store_true")
    mode.add_argument("--partial-tp-solo-probe", action="store_true")
    mode.add_argument("--native-tp-lifecycle", action="store_true")
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
    )
    return parser.parse_args(argv)


async def async_main(argv=None) -> int:
    args = parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv()
    testnet = os.getenv("BYBIT_TESTNET", "true").lower() in ("true", "1", "yes")
    testnet_only = os.getenv("BYBIT_TESTNET_ONLY", "true").lower() in (
        "true", "1", "yes"
    )
    full_auto = os.getenv("KARA_FULL_AUTO", "false").lower() == "true"
    recv_window = int(os.getenv("BYBIT_RECV_WINDOW", "5000"))
    validate_environment(
        environment=args.environment,
        confirmed=args.confirm_testnet if args.environment == "testnet" else args.confirm_demo,
        full_auto=full_auto,
    )
    if args.environment == "testnet" and not testnet_only:
        raise DrillSafetyError("Drill requires BYBIT_TESTNET_ONLY=true")
    if args.environment == "testnet" and not testnet:
        raise DrillSafetyError("Drill refuses BYBIT_TESTNET=false or mainnet")

    api_key, api_secret = read_drill_credentials(args.environment)
    if not api_key or not api_secret:
        raise DrillSafetyError(f"Bybit {args.environment} credentials are required")

    client = BybitClient(
        api_key=api_key,
        api_secret=api_secret,
        testnet=args.environment == "testnet",
        demo=args.environment == "demo",
        recv_window=recv_window,
    )
    evidence = None
    try:
        if args.hold_protected:
            if args.partial_close:
                raise DrillSafetyError("--hold-protected cannot combine with --partial-close")
            evidence = await run_hold_protected(
                client, asset=args.symbol, side=Side(args.side), environment=args.environment
            )
        elif args.recover_protected:
            if args.partial_close:
                raise DrillSafetyError("--recover-protected cannot combine with --partial-close")
            evidence = await run_recover_protected(
                client, asset=args.symbol, side=Side(args.side), environment=args.environment
            )
        elif args.simulate_missing_sl:
            if args.partial_close:
                raise DrillSafetyError("--simulate-missing-sl cannot combine with --partial-close")
            evidence = await run_missing_sl_emergency_close(
                client, asset=args.symbol, side=Side(args.side), environment=args.environment
            )
        elif args.ws_reconnect_check:
            if args.partial_close:
                raise DrillSafetyError("--ws-reconnect-check cannot combine with --partial-close")
            evidence = await run_ws_reconnect_check(
                client, asset=args.symbol, side=Side(args.side), environment=args.environment
            )
        elif args.multi_close_all:
            if args.partial_close:
                raise DrillSafetyError("--multi-close-all cannot combine with --partial-close")
            evidence = await run_multi_position_close_all(client, environment=args.environment)
        elif args.native_tp_lifecycle:
            evidence = await run_native_tp_lifecycle(
                client,
                asset=args.symbol,
                side=Side(args.side),
                environment=args.environment,
            )
        elif args.partial_tp_solo_probe:
            evidence = await run_partial_tp_solo_probe(
                client,
                asset=args.symbol,
                side=Side(args.side),
                environment=args.environment,
            )
        elif args.partial_tp_mode_probe:
            if args.partial_close:
                raise DrillSafetyError(
                    "--partial-tp-mode-probe cannot combine with --partial-close"
                )
            evidence = await run_partial_tp_mode_probe(
                client,
                asset=args.symbol,
                side=Side(args.side),
                environment=args.environment,
            )
        else:
            evidence = await run_lifecycle(
                client,
                asset=args.symbol,
                side=Side(args.side),
                partial_close=args.partial_close,
                environment=args.environment,
            )
    finally:
        await client.close()
        api_key = ""
        api_secret = ""

    report_path = args.report or Path(
        f"SESSION_NOTES/bybit_{args.environment}_drills.jsonl"
    )
    write_evidence(report_path, evidence)
    print(json.dumps(asdict(evidence), indent=2, sort_keys=True))
    held = evidence.result == "held_protected" and evidence.hard_sl_present and evidence.final_position_size > 0
    passed = evidence.result == "passed" and evidence.final_position_size == 0
    return 0 if held or passed else 1


def main(argv=None) -> int:
    try:
        return asyncio.run(async_main(argv))
    except (DrillSafetyError, BybitError) as exc:
        print(f"DRILL REFUSED/FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
