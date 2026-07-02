"""
Emit weekly review artifacts:
- Markdown full report (`weekly_YYYY-MM-DD.md`)
- Machine-readable suggestion JSON (`suggested_config_YYYY-MM-DD.json`)
- Telegram summary string (MarkdownV2-escaped, <4000 chars)
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any


def _fmt_val(v: Any) -> str:
    if isinstance(v, float):
        # Show enough precision for pct-like values
        return f"{v:.6g}"
    return str(v)


def _pct_delta(cur: Any, new: Any) -> str:
    try:
        c, n = float(cur), float(new)
        if c == 0:
            return "n/a"
        return f"{((n - c) / abs(c)) * 100:+.1f}%"
    except (TypeError, ValueError):
        return "—"


def write_markdown(review: dict, evidence_pack: dict, config_snapshot: dict, path: str) -> None:
    lines: list[str] = []
    lines.append(f"# KARA Weekly Review — {datetime.utcnow().strftime('%Y-%m-%d')}")
    lines.append("")

    # Overview
    overall = evidence_pack.get("overall", {})
    window = evidence_pack.get("window", {})
    lines.append("## Overview")
    lines.append(f"- Window: {window.get('first_ts')} → {window.get('last_ts')}")
    lines.append(f"- Trades: **{overall.get('total_trades', 0)}**")
    lines.append(f"- Winrate: **{(overall.get('winrate', 0) * 100):.1f}%**")
    lines.append(f"- Total PnL: **${overall.get('total_pnl', 0):+.2f}**")
    lines.append(f"- Expectancy: **${overall.get('expectancy', 0):+.4f}** per trade")
    if overall.get("profit_factor"):
        lines.append(f"- Profit Factor: **{overall['profit_factor']}**")
    lines.append(f"- Model: `{review.get('meta', {}).get('model', 'n/a')}`")
    lines.append("")

    # Flags
    flags = review.get("flags") or []
    if flags:
        lines.append("## Flags")
        for f in flags:
            lines.append(f"- **[{f.get('severity', 'info').upper()}]** {f.get('message', '')}")
        lines.append("")

    # Suggestions
    sugs = review.get("config_suggestions") or []
    lines.append(f"## Suggested Config Changes ({len(sugs)})")
    lines.append("")
    if not sugs:
        lines.append("_No changes proposed — evidence insufficient or config already well-tuned._")
    else:
        lines.append("| # | Field | Current | Suggested | Δ | Confidence | n |")
        lines.append("|---|-------|---------|-----------|---|-----------|---|")
        for i, s in enumerate(sugs, 1):
            lines.append(
                f"| {i} | `{s.get('field', '')}` | {_fmt_val(s.get('current'))} | "
                f"{_fmt_val(s.get('suggested'))} | {_pct_delta(s.get('current'), s.get('suggested'))} | "
                f"{s.get('confidence', '?')} | {s.get('sample_size', '?')} |"
            )
        lines.append("")
        for i, s in enumerate(sugs, 1):
            lines.append(f"### {i}. `{s.get('field')}`")
            lines.append(f"- **Rationale**: {s.get('rationale', '')}")
            if s.get("evidence_refs"):
                lines.append(f"- **Evidence**: `{', '.join(s['evidence_refs'])}`")
            if s.get("success_criterion"):
                lines.append(f"- **Success criterion**: {s['success_criterion']}")
            if s.get("risk_notes"):
                lines.append(f"- **Risk notes**: {s['risk_notes']}")
            lines.append("")

    # Insights
    insights = review.get("insights") or []
    if insights:
        lines.append("## Insights")
        for ins in insights:
            lines.append(f"- **{ins.get('topic', '')}** ({ins.get('confidence', '?')}): {ins.get('finding', '')}")
            if ins.get("evidence_ref"):
                lines.append(f"  - ref: `{ins['evidence_ref']}`")
        lines.append("")

    # Meta warnings
    warns = review.get("meta", {}).get("warnings") or []
    if warns:
        lines.append("## Validation Warnings")
        for w in warns:
            lines.append(f"- {w}")
        lines.append("")

    # Appendix
    lines.append("## Appendix — Evidence Pack (raw)")
    lines.append("```json")
    lines.append(json.dumps(evidence_pack, indent=2, default=str))
    lines.append("```")
    lines.append("")
    lines.append("## Appendix — Config Snapshot")
    lines.append("```json")
    lines.append(json.dumps(config_snapshot, indent=2, default=str))
    lines.append("```")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_suggested_config_json(review: dict, path: str) -> None:
    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "model": review.get("meta", {}).get("model"),
        "suggestions": review.get("config_suggestions") or [],
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)


def _html_escape(s) -> str:
    s = str(s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_telegram_summary(review: dict, evidence_pack: dict, report_path: str) -> str:
    """Return an HTML-formatted Telegram summary (project uses parse_mode=HTML)."""
    overall = evidence_pack.get("overall", {})
    sugs = review.get("config_suggestions") or []
    top = sorted(
        sugs,
        key=lambda s: {"high": 0, "medium": 1, "low": 2}.get(s.get("confidence", "low"), 3),
    )[:3]

    date = datetime.utcnow().strftime("%Y-%m-%d")
    total_trades = overall.get("total_trades", 0)
    winrate = overall.get("winrate", 0) or 0
    total_pnl = overall.get("total_pnl", 0) or 0
    expectancy = overall.get("expectancy", 0) or 0

    lines = [
        f"<b>KARA Weekly Review — {_html_escape(date)}</b>",
        "",
        f"Trades: <b>{total_trades}</b> | WR: <b>{winrate * 100:.1f}%</b> | "
        f"PnL: <b>${total_pnl:+.2f}</b>",
        f"Expectancy: <b>${expectancy:+.4f}</b>",
        "",
    ]
    if top:
        lines.append(f"<b>Top {len(top)} Suggestions</b>")
    else:
        lines.append("<i>No suggestions this week</i>")

    for i, s in enumerate(top, 1):
        cur = _fmt_val(s.get("current"))
        new = _fmt_val(s.get("suggested"))
        delta = _pct_delta(s.get("current"), s.get("suggested"))
        field = _html_escape(s.get("field", ""))
        conf = _html_escape(s.get("confidence", "?"))
        n = _html_escape(s.get("sample_size", "?"))
        lines.append(
            f"{i}. <code>{field}</code>: {_html_escape(cur)} → "
            f"{_html_escape(new)} ({_html_escape(delta)}) [{conf}, n={n}]"
        )
        rationale = (s.get("rationale") or "")[:220]
        if rationale:
            lines.append(f"   <i>{_html_escape(rationale)}</i>")

    flags = review.get("flags") or []
    crits = [f for f in flags if f.get("severity") == "critical"]
    if crits:
        lines.append("")
        lines.append("<b>Critical flags:</b>")
        for f in crits[:3]:
            lines.append(f"⚠️ {_html_escape(f.get('message', ''))}")

    lines.append("")
    lines.append(f"Report: <code>{_html_escape(report_path)}</code>")
    lines.append("Apply: <code>py -m intelligence.weekly_review.apply_review --file ...</code>")

    out = "\n".join(lines)
    if len(out) > 4000:
        out = out[:3990] + "\n..."
    return out
