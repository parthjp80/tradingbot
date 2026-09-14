"""
Entry point.

  python main.py --once        run a single cycle and exit (good for cron)
  python main.py                run continuously, polling every
                                 POLL_INTERVAL_MINUTES during market hours,
                                 matching the always-on container pattern
                                 you use for market-news.

Market hours check is intentionally simple (US/Eastern, 9:30-16:00, Mon-Fri)
and does not account for early closes/holidays -- extend `is_market_hours()`
with a holiday calendar (e.g. `pandas_market_calendars`) before relying on
it for anything beyond paper trading.
"""
from __future__ import annotations
import argparse
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from bot.orchestrator import Orchestrator
from bot.config import POLL_INTERVAL_MINUTES
from bot.logger_setup import get_logger

log = get_logger("main")
EASTERN = ZoneInfo("America/New_York")


def is_market_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(EASTERN)
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now <= close_t


def run_once(orchestrator: Orchestrator, force: bool = False) -> None:
    if not force and not is_market_hours():
        log.info("Outside market hours, skipping cycle.")
        return
    orchestrator.run_cycle()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument("--force", action="store_true", help="Ignore market-hours check")
    args = parser.parse_args()

    orchestrator = Orchestrator()

    if args.once:
        run_once(orchestrator, force=args.force)
        return

    scheduler = BlockingScheduler(timezone=str(EASTERN))
    scheduler.add_job(run_once, "interval", minutes=POLL_INTERVAL_MINUTES, args=[orchestrator])
    log.info("Starting scheduler: every %d minutes during market hours.", POLL_INTERVAL_MINUTES)

    run_once(orchestrator, force=args.force)  # run immediately on startup too

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down scheduler.")


if __name__ == "__main__":
    main()
