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
from core.startup_validation import validate_bybit_preflight
from execution.exchange_client import ExecutionOrderStatus, InstrumentSpec, VenueOrder
from models.schemas import Side
from utils.helpers import gen_id


ALLOWED_SYMBOLS = {"BTC", "ETH"}


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
    entry_order_link_id_masked: str = ""
    entry_fill_price: float = 0.0
    exit_fill_price: float = 0.0
    fee: float = 0.0
    hard_sl_present: bool = False
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


def validate_environment(*, confirm_testnet: bool, testnet: bool, full_auto: bool) -> None:
    if not confirm_testnet:
        raise DrillSafetyError("Required flag missing: --confirm-testnet")
    if not testnet:
        raise DrillSafetyError("Drill refuses BYBIT_TESTNET=false or mainnet")
    if full_auto:
        raise DrillSafetyError("Drill refuses KARA_FULL_AUTO=true")


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


async def run_lifecycle(
    client: BybitClient,
    *,
    asset: str,
    side: Side,
    partial_close: bool,
    confirm: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> DrillEvidence:
    asset = asset.upper()
    if asset not in ALLOWED_SYMBOLS:
        raise DrillSafetyError("Only BTC and ETH are allowed for initial drills")

    await client.connect()
    await client.sync_clock()
    await client.load_instruments()
    preflight = await client.preflight()
    preflight_errors = validate_bybit_preflight(preflight)
    if preflight_errors:
        raise DrillSafetyError("; ".join(preflight_errors))
    if not preflight.testnet or not client.testnet:
        raise DrillSafetyError("Preflight did not confirm Bybit testnet")

    spec = await client.get_instrument(asset)
    mark_price = await client.get_mark_price(spec.symbol)
    quantity = smallest_valid_quantity(spec, mark_price)
    existing = await client.get_positions(spec.symbol)
    if existing:
        raise DrillSafetyError(f"Refusing drill: existing {spec.symbol} position")

    evidence = DrillEvidence(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        environment="BYBIT TESTNET",
        account_masked=mask(client.api_key),
        symbol=spec.symbol,
        side=side.value,
        quantity=quantity,
    )
    output(
        f"Environment: {evidence.environment} | Account: {evidence.account_masked} | "
        f"Available USDT: {preflight.available_usdt} | Symbol: {spec.symbol} | "
        f"Quantity: {quantity}"
    )
    prompt = (
        f"Type TESTNET to place {side.value.upper()} {quantity} {spec.symbol} "
        f"at mark {mark_price}: "
    )
    if confirm(prompt).strip() != "TESTNET":
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

        if partial_close and quantity >= spec.qty_step * 2:
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
            total_fee += partial_fill.fee_paid
            remaining = await client.get_positions(spec.symbol)
            venue = next((item for item in remaining if item.side == side), None)
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
    parser.add_argument("--confirm-testnet", action="store_true")
    parser.add_argument("--symbol", choices=("BTC", "ETH"), required=True)
    parser.add_argument("--side", choices=("long", "short"), required=True)
    parser.add_argument("--partial-close", action="store_true")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("SESSION_NOTES/bybit_testnet_drills.jsonl"),
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
        confirm_testnet=args.confirm_testnet,
        testnet=testnet,
        full_auto=full_auto,
    )
    if not testnet_only:
        raise DrillSafetyError("Drill requires BYBIT_TESTNET_ONLY=true")

    api_key = getpass.getpass("Bybit testnet API key: ").strip()
    api_secret = getpass.getpass("Bybit testnet API secret: ").strip()
    if not api_key or not api_secret:
        raise DrillSafetyError("Bybit testnet credentials are required")

    client = BybitClient(
        api_key=api_key,
        api_secret=api_secret,
        testnet=True,
        recv_window=recv_window,
    )
    evidence = None
    try:
        evidence = await run_lifecycle(
            client,
            asset=args.symbol,
            side=Side(args.side),
            partial_close=args.partial_close,
        )
    finally:
        await client.close()
        api_key = ""
        api_secret = ""

    write_evidence(args.report, evidence)
    print(json.dumps(asdict(evidence), indent=2, sort_keys=True))
    return 0 if evidence.result == "passed" and evidence.final_position_size == 0 else 1


def main(argv=None) -> int:
    try:
        return asyncio.run(async_main(argv))
    except (DrillSafetyError, BybitError) as exc:
        print(f"DRILL REFUSED/FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
