"""
Orchestrator: load trades → build evidence → snapshot config → LLM review →
validate → write artifacts → return result.

Also exposes `run_weekly_review()` for the scheduler and a `__main__`-style
CLI (see intelligence/weekly_review/__main__.py).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import config

from . import aggregator, config_introspect, llm_client, report_writer

log = logging.getLogger("kara.weekly_review")


DEFAULT_OUTPUT_DIR = os.path.join(config.STORAGE_BASE, "reviews")
DEFAULT_MIN_SAMPLES = 30
DEFAULT_LOOKBACK_DAYS = 7


@dataclass
class ReviewResult:
    report_path: str
    suggestion_path: str
    telegram_summary: str
    evidence_pack: dict
    review: dict
    warnings: list = field(default_factory=list)


def _stub_llm_review() -> dict:
    """Canned response used with --dry-run so we can smoke-test without the API."""
    return {
        "insights": [
            {"topic": "coverage: RISK", "finding": "No changes proposed — recent risk params look consistent with data.", "confidence": "low"},
            {"topic": "coverage: SCALPER", "finding": "Dry-run stub — no real LLM analysis performed.", "confidence": "low"},
            {"topic": "coverage: SIGNAL", "finding": "Dry-run stub.", "confidence": "low"},
            {"topic": "coverage: MARKET_SCAN", "finding": "Dry-run stub.", "confidence": "low"},
            {"topic": "coverage: EXEC", "finding": "Dry-run stub.", "confidence": "low"},
        ],
        "config_suggestions": [],
        "flags": [{"severity": "info", "message": "Dry-run — LLM was NOT called. Suggestions are stubbed empty."}],
        "meta": {"model": "stub", "warnings": [], "retries": 0, "failed": False},
    }


def run_weekly_review(
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    now: Optional[datetime] = None,
    stub_llm: bool = False,
) -> ReviewResult:
    now = now or datetime.now(timezone.utc)
    date_tag = now.strftime("%Y-%m-%d")

    log.info("weekly_review: loading closed trades (%dd)", lookback_days)
    df = aggregator.load_closed_trades(days=lookback_days, now=now)
    log.info("weekly_review: %d closed trades", len(df))

    evidence_pack = aggregator.to_evidence_pack(df)
    snapshot = config_introspect.snapshot_config()
    schema = config_introspect.config_schema()

    if stub_llm:
        log.info("weekly_review: STUB LLM (dry-run)")
        review = _stub_llm_review()
    else:
        log.info("weekly_review: calling LLM")
        review = llm_client.run_llm_review(
            evidence_pack=evidence_pack,
            config_snapshot=snapshot,
            schema=schema,
            min_samples=min_samples,
        )

    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, f"weekly_{date_tag}.md")
    suggestion_path = os.path.join(output_dir, f"suggested_config_{date_tag}.json")

    report_writer.write_markdown(review, evidence_pack, snapshot, report_path)
    report_writer.write_suggested_config_json(review, suggestion_path)
    tg_summary = report_writer.format_telegram_summary(review, evidence_pack, report_path)

    log.info("weekly_review: wrote %s + %s", report_path, suggestion_path)

    return ReviewResult(
        report_path=report_path,
        suggestion_path=suggestion_path,
        telegram_summary=tg_summary,
        evidence_pack=evidence_pack,
        review=review,
        warnings=review.get("meta", {}).get("warnings", []),
    )
