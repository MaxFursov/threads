import asyncio
import os
import random
import logging
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
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

KEYWORDS = [
    "ковбаса",
    "сосиски",
    "ковбасні вироби",
    "делікатеси",
    "шашлик",
    "м'ясо купити",
    "сардельки",
]

MAX_REPLIES = int(os.getenv("MAX_REPLIES_PER_RUN", "15"))


async def run_bot():
    log.info("=== Bot run started ===")

    client = ThreadsClient(
        session_id=os.environ["THREADS_SESSION_ID"],
        ds_user_id=os.environ["THREADS_DS_USER_ID"],
        csrf_token=os.environ["THREADS_CSRF_TOKEN"],
    )
    ai = AIHandler(api_key=os.environ["ANTHROPIC_API_KEY"])
    db = Database()

    await client.start()
    replies_sent = 0

    try:
        for keyword in KEYWORDS:
            if replies_sent >= MAX_REPLIES:
                break

            posts = await client.search_posts(keyword)

            for post in posts:
                if replies_sent >= MAX_REPLIES:
                    break

                post_id = post["id"]
                if db.already_processed(post_id):
                    continue

                text = post.get("text", "")
                if not text:
                    db.mark_skipped(post_id)
                    continue

                log.info(f"[{post_id}] {text[:80]}")

                reply = ai.generate_reply(text)
                if not reply:
                    db.mark_skipped(post_id)
                    continue

                log.info(f"Reply: {reply}")

                success = await client.reply_to_post(post["url"], reply)
                if success:
                    db.mark_replied(post_id)
                    replies_sent += 1
                    delay = random.randint(45, 120)
                    log.info(f"Waiting {delay}s before next reply...")
                    await asyncio.sleep(delay)
                else:
                    db.mark_skipped(post_id)

        log.info(f"Replies sent: {replies_sent}")

        if not db.posted_today():
            log.info("Creating daily post...")
            post_text = ai.generate_daily_post()
            success = await client.create_post(post_text)
            if success:
                db.mark_daily_post()

    finally:
        await client.stop()
        db.close()

    log.info("=== Bot run finished ===")


def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_bot, "interval", minutes=20, id="bot_run")
    scheduler.start()
    log.info("Scheduler started. Bot runs every 20 minutes.")

    loop = asyncio.get_event_loop()
    loop.run_until_complete(run_bot())  # run immediately on start
    loop.run_forever()


if __name__ == "__main__":
    main()
