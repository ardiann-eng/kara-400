"""
Async weekly scheduler — Monday 06:00 UTC by default.

Wired from main.py:
    from intelligence.weekly_review.scheduler import start_weekly_review_loop
    asyncio.create_task(start_weekly_review_loop(bot.telegram), name="weekly_review")
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import config

from .runner import run_weekly_review

log = logging.getLogger("kara.weekly_review.scheduler")


def _seconds_until_next_monday(now: datetime, hour_utc: int = 6) -> float:
    now_utc = now.astimezone(timezone.utc)
    days_ahead = (7 - now_utc.weekday()) % 7  # Mon=0
    target = (now_utc + timedelta(days=days_ahead)).replace(
        hour=hour_utc, minute=0, second=0, microsecond=0
    )
    if target <= now_utc:
        target += timedelta(days=7)
    return (target - now_utc).total_seconds()


async def _send_summary(bot: Any, summary: str, chat_id: Optional[str] = None) -> None:
    if bot is None:
        log.warning("weekly_review: no bot handle — skipping Telegram send")
        return
    send = getattr(bot, "send_text", None)
    if send is None:
        log.warning("weekly_review: bot has no send_text — skipping Telegram send")
        return
    try:
        if chat_id:
            await send(summary, target_chat_id=chat_id)
        else:
            await send(summary)
    except Exception as e:
        log.warning("weekly_review: telegram send failed: %s", e)


async def run_and_notify(bot: Any = None, stub_llm: bool = False) -> None:
    """One-shot: run audit, send Telegram summary."""
    try:
        wr = config.WEEKLY_REVIEW
        result = await asyncio.to_thread(
            run_weekly_review,
            lookback_days=wr.lookback_days,
            baseline_days=getattr(wr, "baseline_lookback_days", 30),
            min_samples=wr.min_samples_for_significance,
            output_dir=wr.output_dir,
            stub_llm=stub_llm,
        )
        await _send_summary(bot, result.telegram_summary)
        log.info("weekly_review: complete — report=%s", result.report_path)
    except Exception:
        log.error("weekly_review: fatal error\n%s", traceback.format_exc())


async def start_weekly_review_loop(bot: Any = None, hour_utc: Optional[int] = None) -> None:
    """Long-running task: sleep to next Monday HH:00 UTC, run, repeat."""
    if not getattr(config.WEEKLY_REVIEW, "enabled", True):
        log.info("weekly_review scheduler DISABLED (WEEKLY_REVIEW.enabled=false)")
        return

    hour = hour_utc if hour_utc is not None else int(
        getattr(config.WEEKLY_REVIEW, "schedule_hour_utc", 6)
    )
    log.info("weekly_review scheduler started (Mon %02d:00 UTC)", hour)
    while True:
        try:
            delay = _seconds_until_next_monday(datetime.now(timezone.utc), hour)
            log.info(
                "weekly_review: next run in %.1fh (%.0fs)",
                delay / 3600,
                delay,
            )
            await asyncio.sleep(delay)
            await run_and_notify(bot)
        except asyncio.CancelledError:
            log.info("weekly_review scheduler cancelled")
            raise
        except Exception:
            log.error(
                "weekly_review loop crashed, retrying in 1h\n%s",
                traceback.format_exc(),
            )
            await asyncio.sleep(3600)


def main_sync():
    """CLI: py -m intelligence.weekly_review.scheduler --run-now"""
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--run-now", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.run_now:
        asyncio.run(run_and_notify(bot=None, stub_llm=args.dry_run))
    else:
        asyncio.run(start_weekly_review_loop(bot=None))


if __name__ == "__main__":
    main_sync()
