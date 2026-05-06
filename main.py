import asyncio
import os
import logging
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.executors.asyncio import AsyncIOExecutor
from analyze_trends import collect_trending_posts, analyze_and_draft
from threads_client import ThreadsClient
from database import Database

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


async def daily_post():
    log.info("=== Daily post run ===")
    db = Database()

    if db.posted_today():
        log.info("Already posted today, skipping.")
        db.close()
        return

    try:
        posts = await collect_trending_posts(limit=30)
        if not posts:
            log.warning("No trending posts collected.")
            return

        log.info(f"Collected {len(posts)} posts. Generating post...")
        result = analyze_and_draft(posts)

        # Extract only the post text
        post_text = result
        if "ПОСТ:" in result:
            post_text = result.split("ПОСТ:")[-1].strip()

        log.info(f"Post text: {post_text}")

        client = ThreadsClient(
            session_id=os.environ["THREADS_SESSION_ID"],
            ds_user_id=os.environ["THREADS_DS_USER_ID"],
            csrf_token=os.environ["THREADS_CSRF_TOKEN"],
        )
        await client.start()
        success = await client.create_post(post_text)
        await client.stop()

        if success:
            db.mark_daily_post()
            log.info("Post published successfully.")
        else:
            log.error("Failed to publish post.")

    finally:
        db.close()

    log.info("=== Done ===")


def main():
    scheduler = AsyncIOScheduler(
        executors={"default": AsyncIOExecutor()},
        timezone="Europe/Kyiv",
    )
    scheduler.add_job(daily_post, "cron", hour=9, minute=0, id="daily_post")
    scheduler.start()
    log.info("Scheduler started. Posts daily at 09:00 Kyiv time.")

    loop = asyncio.get_event_loop()
    loop.run_forever()


if __name__ == "__main__":
    main()
