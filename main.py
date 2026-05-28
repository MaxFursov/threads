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
OWN_USERNAME = os.getenv("THREADS_USERNAME", "dilovakovbasa.official")


def make_client():
    return ThreadsClient(
        session_id=os.environ["THREADS_SESSION_ID"],
        ds_user_id=os.environ["THREADS_DS_USER_ID"],
        csrf_token=os.environ["THREADS_CSRF_TOKEN"],
    )


async def comment_one_post():
    """Every 2 hours: find one relevant post in For You feed and comment on it."""
    log.info("=== Comment run ===")
    db = Database()
    ai = AIHandler(api_key=os.environ["ANTHROPIC_API_KEY"])

    posts = await collect_trending_posts(limit=40)
    random.shuffle(posts)
    log.info(f"Feed posts available: {len(posts)}")

    client = make_client()
    await client.start()

    try:
        for post in posts:
            post_id = post.get("id") or str(hash(post["text"][:80]))
            if db.already_processed(post_id) or not post.get("url"):
                continue
            reply = ai.generate_reply(post["text"])
            if not reply:
                db.mark_skipped(post_id)
                continue
            log.info(f"Commenting on @{post.get('username')}: {reply}")
            success = await client.reply_to_post(post["url"], reply)
            if success:
                db.mark_replied(post_id)
            else:
                db.mark_skipped(post_id)
            return
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


async def _publish_consumer_post(db: Database, ai: AIHandler, trend_posts: list):
    """Generate a consumer-focused trend post."""
    trend_mechanism = extract_trend_mechanism(trend_posts) if trend_posts else None
    if trend_mechanism:
        log.info(f"Trend mechanism: {trend_mechanism}")
    insight = db.get_insight()
    recent_posts = db.get_recent_post_texts(days=14)
    return ai.generate_daily_post(insight=insight, recent_posts=recent_posts, trend_mechanism=trend_mechanism)


async def _publish_catalog_post(db: Database, client, ai: AIHandler):
    """Generate and publish a promotions/new products post from the site."""
    promotions = await fetch_promotions(client.page)
    new_products = await fetch_new_products(client.page)

    recently_mentioned_promos = db.get_recently_mentioned("promotions")
    recently_mentioned_products = db.get_recently_mentioned("new_products")

    fresh_promos = [p for p in promotions if p["name"] not in recently_mentioned_promos]
    fresh_products = [p for p in new_products if p["name"] not in recently_mentioned_products]

    if not fresh_promos and not fresh_products:
        log.info("All promotions recently mentioned, falling back to consumer post.")
        return await _publish_consumer_post(db, ai, [])

    post_text = ai.generate_catalog_post(
        new_products=fresh_products[:3] if fresh_products else None,
        promotions=fresh_promos[:4] if fresh_promos else None,
    )
    if post_text:
        db.save_recently_mentioned("promotions", [p["name"] for p in fresh_promos[:4]])
        db.save_recently_mentioned("new_products", [p["name"] for p in fresh_products[:3]])
    return post_text


async def daily_post():
    """Every day at 09:00: alternate between consumer post and catalog post."""
    log.info("=== Daily post run ===")
    db = Database()

    if db.posted_today():
        log.info("Already posted today (db), skipping.")
        db.close()
        return

    try:
        trend_posts = await collect_trending_posts(limit=30)

        client = make_client()
        await client.start()
        already_posted = await client.posted_today_on_profile(OWN_USERNAME)
        if already_posted:
            log.info("Already posted today (profile check), skipping.")
            await client.stop()
            db.mark_daily_post()
            db.close()
            return

        from datetime import date
        use_catalog = date.today().toordinal() % 2 == 0
        log.info(f"Post type today: {'catalog' if use_catalog else 'consumer'}")

        ai = AIHandler(api_key=os.environ["ANTHROPIC_API_KEY"])
        if use_catalog:
            post_text = await _publish_catalog_post(db, client, ai)
        else:
            post_text = await _publish_consumer_post(db, ai, trend_posts)

        log.info(f"Post text: {post_text}")
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
    scheduler.add_job(comment_one_post, "cron", hour="8,10,12,14,16,18,20", minute=0, id="comment")
    scheduler.add_job(reply_to_own_comments, "cron", hour="8-21", minute="0,30", id="own_replies")
    scheduler.add_job(collect_post_metrics, "cron", hour=22, minute=0, id="metrics")
    scheduler.start()
    log.info("Scheduler started: daily post 09:00 (alternating consumer/catalog), comments every 2h (8-20), own replies every 30min (8-21), metrics 22:00.")

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
