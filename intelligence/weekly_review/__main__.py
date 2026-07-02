"""
CLI entry point:
    py -m intelligence.weekly_review [--dry-run] [--live] [--lookback 7]
"""

import argparse
import logging
import sys

from .runner import run_weekly_review, DEFAULT_LOOKBACK_DAYS, DEFAULT_MIN_SAMPLES, DEFAULT_OUTPUT_DIR


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run KARA weekly AI review.")
    ap.add_argument("--dry-run", action="store_true", help="Skip real LLM call; use stub response.")
    ap.add_argument("--live", action="store_true", help="Force real LLM call (default). Kept for clarity vs --dry-run.")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_DAYS)
    ap.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    ap.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    result = run_weekly_review(
        lookback_days=args.lookback,
        min_samples=args.min_samples,
        output_dir=args.output_dir,
        stub_llm=args.dry_run,
    )

    print("── Weekly Review complete ──")
    print(f"Report:      {result.report_path}")
    print(f"Suggestions: {result.suggestion_path}")
    print(f"Trades in window: {result.evidence_pack.get('window', {}).get('trade_count', 0)}")
    n_sug = len(result.review.get('config_suggestions') or [])
    print(f"Suggestions: {n_sug}")
    if result.warnings:
        print(f"Warnings: {result.warnings}")
    print("── Telegram preview ──")
    print(result.telegram_summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
