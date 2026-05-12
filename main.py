import asyncio
import os
import random
import logging
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.executors.asyncio import AsyncIOExecutor
from analyze_trends import collect_trending_posts, analyze_and_draft
from threads_client import ThreadsClient
from ai_handler import AIHandler
from database import Database

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

KEYWORDS = ["ковбаса", "шашлик", "сосиски", "м'ясо", "делікатеси"]
OWN_USERNAME = os.getenv("THREADS_USERNAME", "dilovakovbasa")


def make_client():
    return ThreadsClient(
        session_id=os.environ["THREADS_SESSION_ID"],
        ds_user_id=os.environ["THREADS_DS_USER_ID"],
        csrf_token=os.environ["THREADS_CSRF_TOKEN"],
    )


async def comment_one_post():
    """Every 2 hours: find one relevant post and comment on it."""
    log.info("=== Comment run ===")
    db = Database()
    ai = AIHandler(api_key=os.environ["ANTHROPIC_API_KEY"])
    client = make_client()
    await client.start()

    try:
        random.shuffle(KEYWORDS)
        for keyword in KEYWORDS:
            posts = await client.search_posts(keyword, limit=10)
            for post in posts:
                post_id = post["id"]
                if db.already_processed(post_id) or not post.get("url"):
                    continue
                reply = ai.generate_reply(post["text"])
                if not reply:
                    db.mark_skipped(post_id)
                    continue
                log.info(f"Commenting on [{post_id}]: {reply}")
                success = await client.reply_to_post(post["url"], reply)
                if success:
                    db.mark_replied(post_id)
                else:
                    db.mark_skipped(post_id)
                return  # one comment per run
    finally:
        await client.stop()
        db.close()

    log.info("=== Comment run done ===")


async def reply_to_own_comments():
    """Every 2 hours: reply to comments left on our own posts."""
    log.info("=== Reply to own comments ===")
    db = Database()
    ai = AIHandler(api_key=os.environ["ANTHROPIC_API_KEY"])
    client = make_client()
    await client.start()

    try:
        replies = await client.get_own_post_replies(OWN_USERNAME)
        replied_count = 0
        for r in replies:
            if replied_count >= 5:
                break
            if db.already_processed(r["id"]):
                continue
            response = ai.generate_reply(r["text"])
            if not response:
                log.info(f"AI NULL for @{r['username']}: {r['text'][:120]}")
                db.mark_skipped(r["id"])
                continue
            log.info(f"Replying to @{r['username']}: {response}")
            success = await client.reply_to_post(r["post_url"], response)
            if success:
                db.mark_replied(r["id"])
                replied_count += 1
            else:
                db.mark_skipped(r["id"])
    finally:
        await client.stop()
        db.close()

    log.info("=== Reply to own comments done ===")


async def daily_post():
    """Every day at 09:00: analyze trends and publish a post."""
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

        result = analyze_and_draft(posts)
        post_text = result.split("ПОСТ:")[-1].strip() if "ПОСТ:" in result else result

        log.info(f"Post text: {post_text}")

        client = make_client()
        await client.start()
        success = await client.create_post(post_text)
        await client.stop()

        if success:
            db.mark_daily_post()
            log.info("Post published.")
        else:
            log.error("Failed to publish post.")
    finally:
        db.close()

    log.info("=== Daily post done ===")


async def main():
    scheduler = AsyncIOScheduler(
        executors={"default": AsyncIOExecutor()},
        timezone="Europe/Kyiv",
    )
    scheduler.add_job(daily_post, "cron", hour=9, minute=0, id="daily_post")
    scheduler.add_job(comment_one_post, "cron", hour="8,10,12,14,16,18,20", minute=0, id="comment")
    scheduler.add_job(reply_to_own_comments, "cron", hour="8-21", minute="0,30", id="own_replies")
    scheduler.start()
    log.info("Scheduler started: daily post 09:00, comments every 2h (8-22), own replies every 30min (8-22).")

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
