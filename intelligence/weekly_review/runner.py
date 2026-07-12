"""
Orchestrator: load all trade data → evidence pack → config snapshot → LLM audit
→ validate → write artifacts → Telegram summary.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import config

from . import aggregator, config_introspect, llm_client, report_writer

log = logging.getLogger("kara.weekly_review")


DEFAULT_OUTPUT_DIR = os.path.join(config.STORAGE_BASE, "reviews")
DEFAULT_MIN_SAMPLES = int(getattr(config.WEEKLY_REVIEW, "min_samples_for_significance", 30))
DEFAULT_LOOKBACK_DAYS = int(getattr(config.WEEKLY_REVIEW, "lookback_days", 7))
DEFAULT_BASELINE_DAYS = int(getattr(config.WEEKLY_REVIEW, "baseline_lookback_days", 30))


@dataclass
class ReviewResult:
    report_path: str
    suggestion_path: str
    telegram_summary: str
    evidence_pack: dict
    review: dict
    warnings: list = field(default_factory=list)


def _stub_llm_review() -> dict:
    return {
        "executive_summary": "Dry-run stub — LLM was not called.",
        "insights": [
            {"topic": "coverage: RISK", "finding": "Dry-run stub.", "confidence": "low"},
            {"topic": "coverage: SCALPER", "finding": "Dry-run stub.", "confidence": "low"},
            {"topic": "coverage: SIGNAL", "finding": "Dry-run stub.", "confidence": "low"},
            {"topic": "coverage: MARKET_SCAN", "finding": "Dry-run stub.", "confidence": "low"},
            {"topic": "coverage: EXEC", "finding": "Dry-run stub.", "confidence": "low"},
        ],
        "config_suggestions": [],
        "flags": [{"severity": "info", "message": "Dry-run — LLM was NOT called."}],
        "meta": {"model": "stub", "warnings": [], "retries": 0, "failed": False},
    }


def run_weekly_review(
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    baseline_days: int = DEFAULT_BASELINE_DAYS,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    now: Optional[datetime] = None,
    stub_llm: bool = False,
) -> ReviewResult:
    now = now or datetime.now(timezone.utc)
    date_tag = now.strftime("%Y-%m-%d")

    log.info(
        "weekly_review: loading trades lookback=%dd baseline=%dd",
        lookback_days,
        baseline_days,
    )
    df, evidence_pack = aggregator.build_full_evidence(
        lookback_days=lookback_days,
        baseline_days=baseline_days,
        now=now,
    )
    log.info(
        "weekly_review: window_trades=%d baseline_trades=%s sources=%s",
        len(df),
        (evidence_pack.get("baseline") or {}).get("overall", {}).get("total_trades"),
        (evidence_pack.get("data_quality") or {}).get("sources"),
    )

    snapshot = config_introspect.snapshot_config()
    schema = config_introspect.config_schema()

    if stub_llm:
        log.info("weekly_review: STUB LLM (dry-run)")
        review = _stub_llm_review()
    else:
        log.info("weekly_review: calling LLM auditor")
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
