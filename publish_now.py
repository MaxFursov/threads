"""
publish_now.py — ручний запуск: генерує і публікує один пост прямо зараз.

Запуск:
    python publish_now.py               # тільки генерація, показати текст
    python publish_now.py --publish     # згенерувати і одразу опублікувати
    python publish_now.py consumer      # consumer-пост (без каталогу)
    python publish_now.py consumer --publish
"""

import asyncio
import sys
import os
import re
import logging
from dotenv import load_dotenv

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


async def main():
    from threads_client import ThreadsClient
    from ai_handler import AIHandler
    from database import Database
    from catalog_fetcher import fetch_new_products, fetch_promotions
    from analyze_trends import collect_trending_posts, extract_trend_mechanism

    args = sys.argv[1:]
    mode = next((a for a in args if not a.startswith("--")), "catalog")
    do_publish = "--publish" in args

    client = ThreadsClient(
        access_token=os.environ["THREADS_ACCESS_TOKEN"],
        user_id=os.environ["THREADS_USER_ID"],
    )
    db = Database()
    # migrate local DB if needed
    try:
        db.conn.execute("ALTER TABLE published_posts ADD COLUMN thread_id TEXT DEFAULT NULL")
        db.conn.commit()
    except Exception:
        pass
    ai = AIHandler(api_key=os.environ["ANTHROPIC_API_KEY"])

    try:
        if mode == "consumer":
            log.info("Mode: consumer post")
            trend_posts = collect_trending_posts(limit=30)
            trend_mechanism = extract_trend_mechanism(trend_posts) if trend_posts else None
            if trend_mechanism:
                log.info(f"Trend mechanism: {trend_mechanism}")
            insight = db.get_insight()
            recent_posts = db.get_recent_post_texts(days=14)
            post_text = ai.generate_daily_post(
                insight=insight,
                recent_posts=recent_posts,
                trend_mechanism=trend_mechanism,
            )
        else:
            log.info("Mode: catalog post (new products + promotions)")
            promotions = await fetch_promotions()
            new_products = await fetch_new_products()
            log.info(f"Fetched: {len(new_products)} new products, {len(promotions)} promotions")

            recently_mentioned_promos = db.get_recently_mentioned("promotions")
            recently_mentioned_products = db.get_recently_mentioned("new_products")

            fresh_promos = [p for p in promotions if p["name"] not in recently_mentioned_promos]
            fresh_products = [p for p in new_products if p["name"] not in recently_mentioned_products]

            if not fresh_promos and not fresh_products:
                log.info("No fresh catalog items — falling back to consumer post")
                insight = db.get_insight()
                recent_posts = db.get_recent_post_texts(days=14)
                post_text = ai.generate_daily_post(insight=insight, recent_posts=recent_posts)
            else:
                post_text = ai.generate_catalog_post(
                    new_products=fresh_products[:3] if fresh_products else None,
                    promotions=fresh_promos[:4] if fresh_promos else None,
                )
                if post_text:
                    db.save_recently_mentioned("promotions", [p["name"] for p in fresh_promos[:4]])
                    db.save_recently_mentioned("new_products", [p["name"] for p in fresh_products[:3]])

        if not post_text:
            log.error("AI returned empty post — aborting.")
            return

        log.info(f"\n{'='*60}\nПОСТ:\n{post_text}\n{'='*60}")

        if not do_publish:
            log.info("Запустіть з --publish щоб опублікувати.")
            return

        log.info("Публікуємо...")
        post_url = client.create_post(post_text)

        if post_url:
            thread_id = re.search(r"/post/([A-Za-z0-9_-]+)", post_url or "")
            db.mark_daily_post()
            db.save_published_post(
                post_url, post_text,
                thread_id=thread_id.group(1) if thread_id else None,
            )
            db.log_activity("post_published", post_text[:200])
            log.info(f"Опубліковано: {post_url}")
        else:
            log.error("Не вдалося опублікувати пост.")
    finally:
        db.close()


asyncio.run(main())
