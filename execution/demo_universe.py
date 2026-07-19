"""Exact Demo candidate intersection. Never derives symbols from asset names."""

from __future__ import annotations

from typing import Iterable, List

from execution.symbol_registry import BybitSymbolRegistry, UnknownBybitSymbol


def exact_demo_universe(hyperliquid_top_assets: Iterable[str], registry: BybitSymbolRegistry) -> List[str]:
    """Retain top-100 assets only when active Bybit metadata resolves exactly."""
    eligible = []
    for asset in hyperliquid_top_assets:
        try:
            registry.resolve(asset)
        except UnknownBybitSymbol:
            continue
        eligible.append(asset)
    return eligible


def is_demo_execution_eligible(asset: str, registry: BybitSymbolRegistry) -> bool:
    """True only for an exact active Bybit linear-USDT metadata resolution."""
    try:
        registry.resolve(asset)
    except UnknownBybitSymbol:
        return False
    return True
