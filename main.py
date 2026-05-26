import asyncio
import os
import random
import logging
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.executors.asyncio import AsyncIOExecutor
from analyze_trends import collect_trending_posts, extract_trend_mechanism
from catalog_fetcher import fetch_promotions, fetch_new_products
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
            response = ai.generate_own_post_reply(r["text"], r["username"])
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
        log.info("Already posted today (db), skipping.")
        db.close()
        return

    try:
        check_client = make_client()
        await check_client.start()
        already_posted = await check_client.posted_today_on_profile(OWN_USERNAME)
        await check_client.stop()
        if already_posted:
            log.info("Already posted today (profile check), skipping.")
            db.mark_daily_post()
            db.close()
            return

        posts = await collect_trending_posts(limit=30)
        trend_mechanism = extract_trend_mechanism(posts) if posts else None
        if trend_mechanism:
            log.info(f"Trend mechanism: {trend_mechanism}")
        else:
            log.warning("No trending posts or mechanism found, using default topic.")

        insight = db.get_insight()
        recent_posts = db.get_recent_post_texts(days=14)
        ai = AIHandler(api_key=os.environ["ANTHROPIC_API_KEY"])
        post_text = ai.generate_daily_post(insight=insight, recent_posts=recent_posts, trend_mechanism=trend_mechanism)

        log.info(f"Post text: {post_text}")

        client = make_client()
        await client.start()
        post_url = await client.create_post(post_text, OWN_USERNAME)
        await client.stop()

        if post_url:
            db.mark_daily_post()
            db.save_published_post(post_url, post_text)
            log.info(f"Post published: {post_url}")
        else:
            log.error("Failed to publish post.")
    finally:
        db.close()

    log.info("=== Daily post done ===")


async def check_catalog_and_post():
    """Daily at 11:00: check site for new promotions/products and post if anything changed."""
    log.info("=== Catalog check ===")
    db = Database()
    client = make_client()
    await client.start()

    try:
        promotions = await fetch_promotions(client.page)
        new_products = await fetch_new_products(client.page)

        new_promos = db.find_new_catalog_items("promotions", promotions)
        new_items = db.find_new_catalog_items("new_products", new_products)

        if new_promos or new_items:
            log.info(f"Catalog changes: {len(new_promos)} new promos, {len(new_items)} new products")
            ai = AIHandler(api_key=os.environ["ANTHROPIC_API_KEY"])
            post_text = ai.generate_catalog_post(
                new_products=new_items if new_items else None,
                promotions=new_promos if new_promos else None,
            )
            if post_text:
                url = await client.create_post(post_text, OWN_USERNAME)
                if url:
                    db.save_published_post(url, post_text)
                    log.info(f"Catalog post published: {url}")
        else:
            log.info("No catalog changes.")

        db.save_catalog_state("promotions", promotions)
        db.save_catalog_state("new_products", new_products)
    finally:
        await client.stop()
        db.close()

    log.info("=== Catalog check done ===")


async def collect_post_metrics():
    """Every day at 22:00: collect metrics for unparsed posts, then run performance analysis."""
    log.info("=== Collect post metrics ===")
    db = Database()
    posts_to_check = db.get_posts_needing_metrics()

    if posts_to_check:
        client = make_client()
        await client.start()
        try:
            for p in posts_to_check:
                metrics = await client.get_post_metrics(p["url"])
                db.update_post_metrics(p["url"], metrics["likes"], metrics["comments"])
                log.info(f"Metrics for {p['url']}: {metrics['likes']} likes, {metrics['comments']} comments")
                await asyncio.sleep(3)
        finally:
            await client.stop()
    else:
        log.info("No posts need metrics update.")

    all_posts = db.get_all_posts_with_metrics()
    if len(all_posts) >= 3:
        ai = AIHandler(api_key=os.environ["ANTHROPIC_API_KEY"])
        insight = ai.analyze_post_performance(all_posts)
        if insight:
            db.save_insight(insight)
            log.info(f"Performance insight saved: {insight[:120]}...")

    db.close()
    log.info("=== Collect post metrics done ===")


async def main():
    scheduler = AsyncIOScheduler(
        executors={"default": AsyncIOExecutor()},
        timezone="Europe/Kyiv",
    )
    scheduler.add_job(daily_post, "cron", hour=9, minute=0, id="daily_post")
    scheduler.add_job(check_catalog_and_post, "cron", hour=11, minute=0, id="catalog")
    scheduler.add_job(comment_one_post, "cron", hour="8,10,12,14,16,18,20", minute=0, id="comment")
    scheduler.add_job(reply_to_own_comments, "cron", hour="8-21", minute="0,30", id="own_replies")
    scheduler.add_job(collect_post_metrics, "cron", hour=22, minute=0, id="metrics")
    scheduler.start()
    log.info("Scheduler started: daily post 09:00, comments every 2h (8-20), own replies every 30min (8-21), metrics 22:00.")

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
