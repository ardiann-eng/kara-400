"""
Async weekly scheduler. KARA has no APScheduler — main.py uses asyncio loops
(see `asyncio.create_task` in main.py). We follow the same pattern.

Fires every Monday 06:00 UTC. Sends Telegram summary + writes report/JSON.

Wire it up in main.py after the bot is initialized, e.g.:

    from intelligence.weekly_review.scheduler import start_weekly_review_loop
    asyncio.create_task(start_weekly_review_loop(bot), name="weekly_review")
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

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
    """
    Best-effort Telegram delivery. Falls back gracefully if the bot doesn't
    expose the expected method. `send_text` uses HTML parse_mode which matches
    the format we emit.
    """
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
    """One-shot: run review, send summary. Safe to call manually for testing."""
    try:
        result = await asyncio.to_thread(run_weekly_review, stub_llm=stub_llm)
        await _send_summary(bot, result.telegram_summary)
        log.info("weekly_review: complete — report=%s", result.report_path)
    except Exception:
        log.error("weekly_review: fatal error\n%s", traceback.format_exc())


async def start_weekly_review_loop(bot: Any = None, hour_utc: int = 6) -> None:
    """Long-running task: sleep to next Monday 06:00 UTC, run, repeat."""
    log.info("weekly_review scheduler started (Mon %02d:00 UTC)", hour_utc)
    while True:
        try:
            delay = _seconds_until_next_monday(datetime.now(timezone.utc), hour_utc)
            log.info("weekly_review: sleeping %.0fh until next run", delay / 3600)
            await asyncio.sleep(delay)
            await run_and_notify(bot)
        except asyncio.CancelledError:
            log.info("weekly_review scheduler cancelled")
            raise
        except Exception:
            log.error("weekly_review loop crashed, retrying in 1h\n%s", traceback.format_exc())
            await asyncio.sleep(3600)


def main_sync():
    """CLI hook: `py -m intelligence.weekly_review.scheduler --run-now`."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-now", action="store_true", help="Run once, ignore schedule.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.run_now:
        asyncio.run(run_and_notify(bot=None, stub_llm=args.dry_run))
    else:
        asyncio.run(start_weekly_review_loop(bot=None))


if __name__ == "__main__":
    main_sync()
