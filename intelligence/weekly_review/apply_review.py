"""
Interactive CLI to apply weekly review suggestions to config.py.

USAGE:
    py -m intelligence.weekly_review.apply_review \\
        --file data/reviews/suggested_config_2026-07-06.json \\
        [--dry-run] \\
        [--only SCALPER.sl_pct,SIGNAL.min_score_to_signal]

Guardrails:
- Never runs non-interactively; every suggestion needs y/N confirmation.
- Backs up config.py to config.py.bak.YYYYMMDD-HHMMSS before writing.
- Verifies live `current` value matches JSON `current` (drift check) — abort item on mismatch.
- Field names validated against introspected schema (rejects hallucinations).
- Exits non-zero if any suggestion could not be applied.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import sys
from datetime import datetime

from . import config_introspect

# Regex per group: matches the line that assigns the field within a @dataclass
# definition. We treat dataclass bodies as having lines like
#   field_name: type = value    # comment
# and top-level module assignments like
#   FIELD_NAME = value
_DATACLASS_BODIES = {
    "RISK": "RiskConfig",
    "SCALPER": "ScalperConfig",
    "SIGNAL": "SignalConfig",
    "MARKET_SCAN": "MarketScanConfig",
    "EXEC": "ExecConfig",
}


def _config_path() -> str:
    import config as _cfg
    return _cfg.__file__


def _find_dataclass_span(text: str, class_name: str) -> tuple[int, int] | None:
    """
    Return (start_idx, end_idx) covering the class body. End is the first blank
    line at column 0 after the class declaration.
    """
    m = re.search(rf"^class\s+{re.escape(class_name)}\b.*?:\s*$", text, re.MULTILINE)
    if not m:
        return None
    start = m.end()
    tail = text[start:]
    end_rel = re.search(r"\n(?=[A-Za-z_])", tail)
    if end_rel is None:
        return (start, len(text))
    return (start, start + end_rel.start())


def _replace_dataclass_field(text: str, class_name: str, field: str, new_repr: str, current_repr: str) -> tuple[str | None, str]:
    span = _find_dataclass_span(text, class_name)
    if span is None:
        return None, f"class {class_name} not found in config.py"
    body_start, body_end = span
    body = text[body_start:body_end]
    # Match:  field: type = value    (with optional comment)
    line_pat = re.compile(
        rf"(^(\s+){re.escape(field)}\s*:\s*[^=\n]+=\s*)(?P<val>[^\n#]+?)(\s*(#.*)?)$",
        re.MULTILINE,
    )
    match = line_pat.search(body)
    if not match:
        return None, f"field `{field}` not found in class {class_name}"
    current_in_source = match.group("val").strip()
    if current_in_source != current_repr.strip():
        # Try normalized comparison via ast
        try:
            if ast.literal_eval(current_in_source) != ast.literal_eval(current_repr):
                return None, f"drift on {class_name}.{field}: source={current_in_source} expected={current_repr}"
        except Exception:
            return None, f"drift on {class_name}.{field}: source={current_in_source} expected={current_repr}"
    new_line = match.group(1) + new_repr + (match.group(4) or "")
    new_body = body[:match.start()] + new_line + body[match.end():]
    return text[:body_start] + new_body + text[body_end:], "ok"


def _replace_top_level(text: str, field: str, new_repr: str, current_repr: str) -> tuple[str | None, str]:
    # Match top-of-line assignment (allow leading whitespace 0)
    line_pat = re.compile(
        rf"(^{re.escape(field)}\s*=\s*)(?P<val>[^\n#]+?)(\s*(#.*)?)$",
        re.MULTILINE,
    )
    match = line_pat.search(text)
    if not match:
        return None, f"top-level `{field}` not found"
    current_in_source = match.group("val").strip()
    if current_in_source != current_repr.strip():
        try:
            if ast.literal_eval(current_in_source) != ast.literal_eval(current_repr):
                return None, f"drift on {field}: source={current_in_source} expected={current_repr}"
        except Exception:
            return None, f"drift on {field}: source={current_in_source} expected={current_repr}"
    new_line = match.group(1) + new_repr + (match.group(4) or "")
    return text[:match.start()] + new_line + text[match.end():], "ok"


def _to_repr(value):
    return repr(value)


def _prompt_yes_no(msg: str) -> bool:
    while True:
        ans = input(f"{msg} [y/N] ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("", "n", "no"):
            return False


def apply_review(json_path: str, dry_run: bool = False, only: set[str] | None = None) -> int:
    with open(json_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    suggestions = payload.get("suggestions", [])
    if not suggestions:
        print("No suggestions in file.")
        return 0

    schema = config_introspect.config_schema()
    cfg_path = _config_path()
    with open(cfg_path, "r", encoding="utf-8") as f:
        original_text = f.read()
    text = original_text

    applied: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []

    for i, sug in enumerate(suggestions, 1):
        field = sug.get("field", "")
        if only and field not in only:
            continue
        if field not in schema:
            failed.append((field, "unknown field (not in schema)"))
            continue

        current = sug.get("current")
        suggested = sug.get("suggested")

        # Drift check against live values
        try:
            live_val = config_introspect.resolve_current_value(field)
        except KeyError as e:
            failed.append((field, f"resolve failed: {e}"))
            continue
        if live_val != current:
            print(f"[{i}] {field}: DRIFT — file says current={current!r} but live={live_val!r}. Skipping.")
            failed.append((field, f"drift: live={live_val!r} json_current={current!r}"))
            continue

        # Preview
        print("")
        print(f"── Suggestion {i}/{len(suggestions)} ──")
        print(f"  field         : {field}")
        print(f"  current       : {current!r}")
        print(f"  suggested     : {suggested!r}")
        print(f"  confidence    : {sug.get('confidence', '?')}")
        print(f"  sample_size   : {sug.get('sample_size', '?')}")
        print(f"  rationale     : {sug.get('rationale', '')}")
        if sug.get("risk_notes"):
            print(f"  risk_notes    : {sug['risk_notes']}")

        if dry_run:
            print("  (dry-run: not prompting, not applying)")
            skipped.append(field)
            continue

        if not _prompt_yes_no("Apply this change?"):
            skipped.append(field)
            continue

        # Perform replacement
        cur_repr = _to_repr(current)
        new_repr = _to_repr(suggested)
        if "." in field:
            group, name = field.split(".", 1)
            class_name = _DATACLASS_BODIES.get(group)
            if not class_name:
                failed.append((field, f"unknown group {group}"))
                continue
            updated, msg = _replace_dataclass_field(text, class_name, name, new_repr, cur_repr)
        else:
            updated, msg = _replace_top_level(text, field, new_repr, cur_repr)

        if updated is None:
            print(f"  FAILED: {msg}")
            failed.append((field, msg))
            continue
        text = updated
        applied.append(field)
        print(f"  applied ✓")

    print("")
    print("── Summary ──")
    print(f"Applied: {applied}")
    print(f"Skipped: {skipped}")
    print(f"Failed:  {failed}")

    if applied and not dry_run:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{cfg_path}.bak.{stamp}"
        shutil.copy2(cfg_path, backup)
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Config written. Backup at: {backup}")

    return 1 if failed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Apply KARA weekly review suggestions.")
    ap.add_argument("--file", required=True, help="Path to suggested_config_*.json")
    ap.add_argument("--dry-run", action="store_true", help="Show diffs, don't prompt or write.")
    ap.add_argument("--only", default="", help="Comma-separated field names to include.")
    args = ap.parse_args(argv)
    only = {f.strip() for f in args.only.split(",") if f.strip()} or None
    return apply_review(args.file, dry_run=args.dry_run, only=only)


if __name__ == "__main__":
    sys.exit(main())
