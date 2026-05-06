import asyncio
import os
import logging
from datetime import datetime
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from analyze_trends import collect_trending_posts, analyze_and_draft
from database import Database

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DRAFTS_FILE = "drafts.log"


async def daily_draft():
    """Collect trending posts, analyze, save draft. No publishing."""
    log.info("=== Daily draft run ===")
    db = Database()

    if db.posted_today():
        log.info("Draft already generated today, skipping.")
        db.close()
        return

    try:
        posts = await collect_trending_posts(limit=30)
        if not posts:
            log.warning("No trending posts collected.")
            return

        log.info(f"Collected {len(posts)} posts. Analyzing...")
        result = analyze_and_draft(posts)

        # Save draft to file
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(DRAFTS_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"DATE: {timestamp}\n")
            f.write(f"{'='*60}\n")
            f.write(result)
            f.write("\n")

        db.mark_daily_post()
        log.info(f"Draft saved to {DRAFTS_FILE}")
        print("\n" + "="*60)
        print(result)
        print("="*60 + "\n")

    finally:
        db.close()

    log.info("=== Done ===")


def main():
    scheduler = AsyncIOScheduler()
    # Run once per day at 09:00
    scheduler.add_job(daily_draft, "cron", hour=9, minute=0, id="daily_draft")
    scheduler.start()
    log.info("Scheduler started. Daily draft runs at 09:00.")

    loop = asyncio.get_event_loop()
    loop.run_until_complete(daily_draft())  # run immediately on start
    loop.run_forever()


if __name__ == "__main__":
    main()
