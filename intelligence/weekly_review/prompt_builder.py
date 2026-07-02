"""
Prompt composition for the weekly review LLM analyst.

Design principle: the LLM is a *narrator* over deterministic stats. It does
NOT compute numbers, it does NOT invent field names, it does NOT decide to
apply changes. Its output is a hypothesis + rationale + confidence.
"""

from __future__ import annotations

import json


SYSTEM_PROMPT = """You are the weekly strategy analyst for the KARA crypto trading bot.

Your role is STRICTLY ADVISORY:
- You read a deterministic statistical evidence pack and the live config snapshot.
- You narrate patterns and propose config parameter changes as HYPOTHESES.
- You never claim to execute changes. A human reviews and applies each suggestion.

HARD RULES (violating these makes your output rejected):
1. Every `field` in `config_suggestions` MUST appear verbatim in the provided
   `config_schema` list. Never invent, misspell, or paraphrase field names.
2. Statistics are already computed. Never restate or recompute — cite bucket keys.
3. If a bucket has n < min_samples_for_significance, you MAY mention it but MUST
   set confidence="low" and flag insufficient sample size in `risk_notes`.
4. Prefer FEWER, HIGHER-CONFIDENCE suggestions over many low-confidence ones.
   A perfectly acceptable output has 0 suggestions if evidence is weak.
5. Never suggest a change > 50% away from the current value without explicit
   justification in `risk_notes` and `confidence` no higher than "medium".
6. For every suggestion, explain in `rationale` (a) which bucket(s) motivate it,
   (b) the mechanism (e.g. "SL too tight causes noise stop-outs in Asia session"),
   (c) how you'd know it worked next week (falsifiable success criterion).

ANALYSIS COVERAGE — you MUST scan every group of the config:
RISK, SCALPER, SIGNAL, MARKET_SCAN, EXEC, plus top-level flags (ALLOW_SHORT,
BLOCKED_HOURS_UTC, ENABLE_INTELLIGENCE, WATCHED_ASSETS, SCALPER_ASSETS, etc).
If a group has no suggestion, say so briefly in `insights` under topic
"coverage: <group>" — this proves you didn't skip it.

OUTPUT FORMAT: Return ONLY one valid JSON object with exactly these top-level
keys: `insights`, `config_suggestions`, and `flags`. Do not send markdown,
commentary, or prose outside the JSON object.
"""


REVIEW_TOOL_SCHEMA = {
    "name": "submit_weekly_review",
    "description": "Submit the structured weekly review.",
    "input_schema": {
        "type": "object",
        "required": ["insights", "config_suggestions", "flags"],
        "properties": {
            "insights": {
                "type": "array",
                "description": "Narrative findings, one per topic. Include a 'coverage: <group>' entry for any config group you leave untouched.",
                "items": {
                    "type": "object",
                    "required": ["topic", "finding", "confidence"],
                    "properties": {
                        "topic": {"type": "string"},
                        "finding": {"type": "string"},
                        "evidence_ref": {"type": "string", "description": "Bucket key e.g. 'session=Asia | side=SHORT'"},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                },
            },
            "config_suggestions": {
                "type": "array",
                "description": "Proposed parameter changes. Empty array if no strong evidence.",
                "items": {
                    "type": "object",
                    "required": ["field", "current", "suggested", "rationale", "sample_size", "confidence"],
                    "properties": {
                        "field": {"type": "string", "description": "Must exist in config_schema"},
                        "current": {"description": "Current value (echoed from snapshot for drift check)"},
                        "suggested": {"description": "Proposed new value, same type as current"},
                        "rationale": {"type": "string"},
                        "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        "sample_size": {"type": "integer"},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                        "risk_notes": {"type": "string"},
                        "success_criterion": {"type": "string", "description": "Falsifiable metric for next week"},
                    },
                },
            },
            "flags": {
                "type": "array",
                "description": "Meta observations: data quality issues, distribution shifts, or concerns.",
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
        f"min_samples_for_significance = {min_samples}\n\n"
        "## config_schema (WHITELIST — field names allowed in suggestions)\n"
        f"```json\n{json.dumps(list(schema.keys()), indent=2)}\n```\n\n"
        "## config_snapshot (live values)\n"
        f"```json\n{json.dumps(config_snapshot, indent=2, default=str)}\n```\n\n"
        "## evidence_pack (deterministic stats — DO NOT recompute)\n"
        f"```json\n{json.dumps(evidence_pack, indent=2, default=str)}\n```\n\n"
        "Now return the JSON review object. Remember: coverage over every group, "
        "cite buckets, no invented field names, prefer fewer high-confidence suggestions."
    )
