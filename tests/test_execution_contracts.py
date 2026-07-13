import ast
import inspect
from pathlib import Path

from execution.base_executor import BaseExecutor
from execution.exchange_client import (
    ExecutionClient,
    ExecutionOrderStatus,
    InstrumentSpec,
    LivePositionStatus,
)

def test_existing_executors_declare_common_contract():
    root = Path(__file__).parents[1]
    for relative_path, class_name in (
        ("execution/paper_executor.py", "PaperExecutor"),
    ):
        tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
        class_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        )
        assert any(
            isinstance(base, ast.Name) and base.id == "BaseExecutor"
            for base in class_node.bases
        )


def test_execution_client_contract_cannot_be_instantiated():
    assert inspect.isabstract(ExecutionClient)


def test_instrument_spec_keeps_venue_constraints():
    spec = InstrumentSpec(
        asset="BTC",
        symbol="BTCUSDT",
        tick_size=0.1,
        qty_step=0.001,
        min_qty=0.001,
        min_notional=5.0,
        max_leverage=100,
    )

    assert spec.symbol == "BTCUSDT"
    assert spec.qty_step == 0.001
    assert spec.max_leverage == 100


def test_live_lifecycle_has_unprotected_and_reconciliation_states():
    assert LivePositionStatus.OPEN_UNPROTECTED.value == "open_unprotected"
    assert LivePositionStatus.RECONCILIATION_REQUIRED.value == "reconciliation_required"
    assert ExecutionOrderStatus.PARTIALLY_FILLED.value == "partially_filled"
