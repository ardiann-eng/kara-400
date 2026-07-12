"""
Introspect the KARA config surface so the LLM analyst can never invent field
names. Every suggestion is validated against the schema produced here.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Any

import config


# Groups we introspect via dataclasses. Extend here if a new @dataclass gets
# added to config.py.
_DATACLASS_GROUPS = {
    "RISK": config.RISK,
    "SCALPER": config.SCALPER,
    "SIGNAL": config.SIGNAL,
    "MARKET_SCAN": config.MARKET_SCAN,
    "EXEC": config.EXEC,
}

# Top-level module attributes that are also tunable. Kept explicit — do NOT
# auto-scan module globals (leaks secrets like TELEGRAM_TOKEN, PRIVATE_KEY).
_TOP_LEVEL_FIELDS = [
    "ALLOW_SHORT",
    "BLOCKED_HOURS_UTC",
    "ENABLE_INTELLIGENCE",
    "INTELLIGENCE_RETRAIN_MIN_SAMPLES",
    "INTELLIGENCE_RETRAIN_INTERVAL_HOURS",
    "WATCHED_ASSETS",
    "SCALPER_ASSETS",
    "TRADING_MODE",
    "FORCE_SCALPER_ONLY",
    "STANDARD_SIGNAL_AS_SCALPER_FALLBACK",
    "TRADE_MODE",
    "FULL_AUTO",
    "DATA_SOURCE",
]


def _describe_value(v: Any) -> tuple[str, Any]:
    if isinstance(v, bool):
        return "bool", v
    if isinstance(v, int):
        return "int", v
    if isinstance(v, float):
        return "float", v
    if isinstance(v, str):
        return "str", v
    if isinstance(v, (list, tuple)):
        return "list", list(v)
    return type(v).__name__, v


def snapshot_config() -> dict:
    """Full config snapshot: value + type per field. Fed to LLM as context."""
    out = {"groups": {}, "top_level": {}}
    for group_name, obj in _DATACLASS_GROUPS.items():
        if not is_dataclass(obj):
            continue
        entries = {}
        for f in fields(obj):
            val = getattr(obj, f.name)
            typ, v = _describe_value(val)
            entries[f.name] = {"value": v, "type": typ}
        out["groups"][group_name] = entries
    for name in _TOP_LEVEL_FIELDS:
        if hasattr(config, name):
            typ, v = _describe_value(getattr(config, name))
            out["top_level"][name] = {"value": v, "type": typ}
    return out


def config_schema() -> dict:
    """
    Field-name whitelist for LLM. Any suggestion whose `field` is not in this
    schema gets dropped as hallucination.

    Format: {"GROUP.field_name": "type", ...} plus top-level flat names.
    """
    schema: dict[str, str] = {}
    snap = snapshot_config()
    for group_name, entries in snap["groups"].items():
        for fname, meta in entries.items():
            schema[f"{group_name}.{fname}"] = meta["type"]
    for fname, meta in snap["top_level"].items():
        schema[fname] = meta["type"]
    return schema


def resolve_current_value(field_path: str) -> Any:
    """Read the live value of a schema field path. Used at apply-time drift check."""
    if "." in field_path:
        group, name = field_path.split(".", 1)
        obj = _DATACLASS_GROUPS.get(group)
        if obj is None:
            raise KeyError(f"Unknown group: {group}")
        if not hasattr(obj, name):
            raise KeyError(f"Unknown field: {field_path}")
        return getattr(obj, name)
    if hasattr(config, field_path):
        return getattr(config, field_path)
    raise KeyError(f"Unknown field: {field_path}")
