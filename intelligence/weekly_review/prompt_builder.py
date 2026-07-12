"""
Prompt composition for the weekly AI strategy auditor.

Design principles:
- LLM is a *professional auditor/narrator* over deterministic stats — not a trader.
- No invented numbers, field names, or causal certainty from small samples.
- Explicit anti-bias + anti-overfitting rules.
- Output is hypotheses + concrete config suggestions for human review only.
"""

from __future__ import annotations

import json


SYSTEM_PROMPT = """You are an independent quantitative auditor for the KARA crypto futures trading bot.

ROLE
- You evaluate strategy health from a deterministic statistical evidence pack and a live config snapshot.
- You produce professional insights and OPTIONAL config-parameter hypotheses.
- You do NOT execute trades or apply config. A human must approve every change.
- You are not a cheerleader: report weaknesses and null findings as carefully as strengths.

EPISTEMIC RULES (non-negotiable)
1. Numbers are precomputed in evidence_pack. NEVER recompute, invent, or "adjust" stats.
2. Cite evidence via bucket keys / group labels already present in the pack (evidence_refs).
3. Every config_suggestions[].field MUST appear verbatim in config_schema. No invented names.
4. Prefer the null hypothesis: if evidence is weak, return ZERO config_suggestions.
5. Sample size: if n < min_samples_for_significance, confidence MUST be "low" and risk_notes
   must mention insufficient sample. Do not propose high-confidence changes from n < 30.
6. Multiple comparisons: many buckets are scanned. Isolated winners/losers can be noise.
   Prefer patterns that appear in ≥2 independent cuts (e.g. side AND session, or week AND baseline).
7. Overfitting guardrails:
   - Do not optimize for last week's luck (single asset, single day, single reason with small n).
   - Prefer small, reversible parameter steps (typically ≤20% relative change).
   - Changes >50% from current require confidence ≤ "medium" and explicit risk_notes.
   - If baseline (longer window) contradicts the 7-day window, say so and lower confidence.
8. Bias guardrails:
   - Do not assume longs > shorts or scalper > standard without data.
   - Separate structural issues (negative expectancy, fat left tail) from temporary regime noise.
   - Call out selection bias, look-ahead, and missing fields (see data_quality).
9. Actionability: every suggestion must include:
   (a) mechanism, (b) evidence_refs, (c) falsifiable success_criterion for next week.
10. Coverage: scan RISK, SCALPER, SIGNAL, MARKET_SCAN, EXEC, and top-level flags.
    For groups with no change, add an insights entry topic="coverage: <GROUP>".

ANALYSIS PRIORITIES (in order)
A. Data quality / integrity issues that invalidate conclusions
B. Structural negative expectancy by side/mode/exit-reason with adequate n
C. Risk of ruin: loss streaks, drawdown, risk sizing vs SL distance
D. Exit quality: SL vs TP vs time_exit vs trail — where edge is given back
E. Session / hour concentration of losses
F. Score calibration: do higher scores earn higher expectancy?
G. Concrete, minimal config hypotheses only after A–F

OUTPUT
Return ONLY one valid JSON object with keys: insights, config_suggestions, flags.
No markdown fences, no prose outside JSON.
"""


REVIEW_TOOL_SCHEMA = {
    "name": "submit_weekly_review",
    "description": "Submit the structured weekly strategy audit.",
    "input_schema": {
        "type": "object",
        "required": ["insights", "config_suggestions", "flags", "executive_summary"],
        "properties": {
            "executive_summary": {
                "type": "string",
                "description": "3-6 sentence neutral summary of weekly health and top risks.",
            },
            "insights": {
                "type": "array",
                "description": "Narrative findings. Include coverage:* entries for untouched config groups.",
                "items": {
                    "type": "object",
                    "required": ["topic", "finding", "confidence"],
                    "properties": {
                        "topic": {"type": "string"},
                        "finding": {"type": "string"},
                        "evidence_ref": {
                            "type": "string",
                            "description": "Bucket key e.g. 'side=SHORT' or 'reason=stop_loss'",
                        },
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                },
            },
            "config_suggestions": {
                "type": "array",
                "description": "Proposed parameter changes. Empty array if evidence is insufficient.",
                "items": {
                    "type": "object",
                    "required": [
                        "field",
                        "current",
                        "suggested",
                        "rationale",
                        "sample_size",
                        "confidence",
                    ],
                    "properties": {
                        "field": {"type": "string", "description": "Must exist in config_schema"},
                        "current": {"description": "Current value echoed from snapshot"},
                        "suggested": {"description": "Proposed new value, same type as current"},
                        "rationale": {"type": "string"},
                        "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        "sample_size": {"type": "integer"},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                        "risk_notes": {"type": "string"},
                        "success_criterion": {
                            "type": "string",
                            "description": "Falsifiable metric for next weekly review",
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["P0", "P1", "P2"],
                            "description": "P0=risk of ruin / structural bleed; P1=material EV; P2=tuning",
                        },
                    },
                },
            },
            "flags": {
                "type": "array",
                "description": "Data quality, regime shift, or process concerns.",
                "items": {
                    "type": "object",
                    "required": ["severity", "message"],
                    "properties": {
                        "severity": {"type": "string", "enum": ["info", "warn", "critical"]},
                        "message": {"type": "string"},
                    },
                },
            },
        },
    },
}


def build_user_message(
    evidence_pack: dict,
    config_snapshot: dict,
    schema: dict,
    min_samples: int,
) -> str:
    return (
        "You are conducting the WEEKLY strategy audit for KARA.\n"
        f"min_samples_for_significance = {min_samples}\n\n"
        "INSTRUCTIONS\n"
        "- Use ONLY the evidence below. Do not invent trades or metrics.\n"
        "- Compare `overall` vs `baseline.overall` when baseline is present; "
        "do not overfit to a 7-day blip.\n"
        "- If total_trades is low, say so in flags and keep suggestions empty or low-confidence.\n"
        "- Prefer concrete, minimal config changes with priority tags.\n"
        "- Return JSON only matching the schema.\n\n"
        "## config_schema (WHITELIST — only these field names may appear in suggestions)\n"
        f"```json\n{json.dumps(list(schema.keys()), indent=2)}\n```\n\n"
        "## config_snapshot (live values)\n"
        f"```json\n{json.dumps(config_snapshot, indent=2, default=str)}\n```\n\n"
        "## evidence_pack (deterministic — DO NOT recompute)\n"
        f"```json\n{json.dumps(evidence_pack, indent=2, default=str)}\n```\n\n"
        "Produce the JSON audit object now."
    )
