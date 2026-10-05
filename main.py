import asyncio
import hashlib
import os
import re
import random
import logging
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.executors.asyncio import AsyncIOExecutor
from analyze_trends import collect_trending_posts, extract_trend_mechanism
from catalog_fetcher import fetch_promotions, fetch_new_products
from threads_client import ThreadsClient
from ai_handler import AIHandler, format_activity_context, AIGenerationError
from database import Database
from notifier import send_telegram
from token_manager import get_token, refresh_token

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

OWN_USERNAME = os.getenv("THREADS_USERNAME", "dilovakovbasa.ua")


def make_client() -> ThreadsClient:
    return ThreadsClient(
        access_token=get_token(),
        user_id=os.environ["THREADS_USER_ID"],
    )


async def refresh_threads_token():
    """Weekly: extend the 60-day Threads token. If it ever fails, tell the owner
    right away: an expired token means no posts at all (happened 2026-10-01)."""
    log.info("=== Refresh Threads token ===")
    ok, msg = await asyncio.to_thread(refresh_token)
    if not ok:
        await send_telegram(
            "⚠️ Не вдалося продовжити токен Threads. Якщо не виправити, пости перестануть "
            "виходити, коли токен закінчиться (до 60 днів).\n\n"
            f"Причина: {msg[:300]}\n\n"
            "Потрібна повторна авторизація Threads (напишіть Claude)."
        )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

async def comment_one_post():
    """Every 2 hours: find one relevant post and comment on it."""
    log.info("=== Comment run ===")
    db = Database()
    ai = AIHandler(api_key=os.environ["ANTHROPIC_API_KEY"])
    client = make_client()
    recent_context = format_activity_context(db.get_recent_activity())

    try:
        posts = collect_trending_posts(limit=40)
        random.shuffle(posts)
        log.info(f"Feed posts available: {len(posts)}")

        for post in posts:
            if not post.get("id") or not post.get("text"):
                continue

            post_id = post["id"]
            url_key = "url:" + hashlib.sha256((post.get("url") or post_id).encode()).hexdigest()[:16]

            if db.already_processed(post_id) or db.already_processed(url_key):
                continue

            reply = ai.generate_reply(post["text"], recent_context=recent_context)
            if not reply:
                db.mark_replied(post_id)
                db.mark_replied(url_key)
                continue

            log.info(f"Commenting on @{post.get('username')}: {reply}")
            success = client.reply_to_post(post_id, reply)

            db.mark_replied(post_id)
            db.mark_replied(url_key)
            if success:
                db.log_activity(
                    "comment_on_feed",
                    f"@{post.get('username', '?')}: {reply[:150]}",
                )
            else:
                log.warning(f"Reply to {post_id} failed — marked replied anyway")
            return
    finally:
        db.close()

    log.info("=== Comment run done ===")


async def reply_to_feed_post():
    """Twice a day (10:00, 19:00): find ONE relevant post on the live feed and reply
    to it, using a LOGGED-IN browser session (unofficial). Deliberately tiny volume
    to limit ban risk. Reuses the existing reply prompt (generate_reply/REPLY_SYSTEM).
    Sends an operational log to Telegram either way."""
    log.info("=== Reply to feed post ===")
    db = Database()
    ai = AIHandler(api_key=os.environ["ANTHROPIC_API_KEY"])
    try:
        from feed_replier import reply_to_one_relevant_post
        result = await reply_to_one_relevant_post(ai, db)
        status = result.get("status")

        if status == "replied":
            log.info(f"Replied to {result['url']}: {result['reply']}")
            await send_telegram(
                "💬 Бот відповів на чужий пост у Threads (@dilovakovbasa.ua)\n\n"
                f"Кому: @{result['username']}\n"
                f"Наша відповідь: {result['reply']}\n\n"
                f"{result['url']}"
            )
        elif status == "none":
            log.info("No suitable post to reply to this run.")
            await send_telegram(
                "ℹ️ Відповідь на чужий пост: цього разу не знайшлося релевантного "
                "поста (нічого по темі їжі/ковбаси/сиру). Пропускаю."
            )
        elif status == "no_session":
            log.warning("Feed replier has no session configured.")
            await send_telegram(
                "⚠️ Відповіді на чужі пости не працюють: не налаштована сесія входу.\n\n"
                "Треба один раз локально запустити capture_session.py і додати вміст "
                "threads_session.json у змінну THREADS_SESSION_JSON на Railway."
            )
        else:
            err = result.get("error", "unknown")
            log.error(f"Feed reply failed: {err}")
            await send_telegram(
                "⚠️ Не вдалося відповісти на чужий пост.\n\n"
                f"Причина: {str(err)[:300]}"
                + ("\n\nСхоже, сесія входу застаріла, треба оновити THREADS_SESSION_JSON "
                   "(перезапустити capture_session.py)." if err in ("session_expired", "not_logged_in") else "")
            )
    except Exception as e:
        log.error(f"reply_to_feed_post unexpected error: {e}")
        await send_telegram(f"⚠️ Помилка в програмі під час відповіді на чужий пост: {str(e)[:300]}")
    finally:
        db.close()
    log.info("=== Reply to feed post done ===")


async def reply_to_own_comments():
    """Every 30 min: reply to comments on our activity feed (replies-to-replies)."""
    log.info("=== Reply to own comments ===")
    db = Database()
    ai = AIHandler(api_key=os.environ["ANTHROPIC_API_KEY"])
    client = make_client()
    recent_context = format_activity_context(db.get_recent_activity())

    try:
        replies = client.get_own_post_replies()
        replied_count = 0
        for r in replies:
            if replied_count >= 5:
                break
            if db.already_processed(r["id"]):
                continue
            # Source-of-truth guard: skip if we already replied on Threads itself
            if client.has_own_reply(r["reply_target_id"]):
                log.info(f"Already replied on Threads to @{r['username']}, marking done")
                db.mark_replied(r["id"])
                continue
            response = ai.generate_own_post_reply(
                r["text"], r["username"], recent_context=recent_context
            )
            if not response:
                log.info(f"AI NULL for @{r['username']}: {r['text'][:120]}")
                db.mark_replied(r["id"])
                continue
            log.info(f"Replying to @{r['username']}: {response}")
            success = client.reply_to_post(r["reply_target_id"], response)
            if success:
                db.mark_replied(r["id"])
                db.log_activity(
                    "reply_to_comment",
                    f"@{r['username']}: {response[:150]}",
                )
                replied_count += 1
            else:
                db.mark_skipped(r["id"])
    finally:
        db.close()

    log.info("=== Reply to own comments done ===")


async def scan_own_post_comments():
    """Every 2h: scan the FULL conversation under recent posts (top-level comments AND
    follow-up replies to our own replies) and answer anything we haven't answered yet."""
    log.info("=== Scan own post comments ===")
    db = Database()

    try:
        client = make_client()
        recent_posts = client.get_recent_own_posts(limit=10)

        if not recent_posts:
            log.info("No recent posts found.")
            return

        ai = AIHandler(api_key=os.environ["ANTHROPIC_API_KEY"])
        recent_context = format_activity_context(db.get_recent_activity())
        replied_count = 0

        for post in recent_posts:
            if replied_count >= 5:
                break

            try:
                comments = client.get_post_conversation(post["id"])
            except Exception as e:
                log.error(f"get_post_conversation failed for {post['id']}: {e}")
                continue

            for comment in comments:
                if replied_count >= 5:
                    break

                if db.already_processed(comment["id"]):
                    continue

                # Source-of-truth guard: skip if we already replied UNDER this exact
                # comment on Threads (survives DB loss / parallel runs / local-vs-prod).
                if client.has_own_reply(comment["thread_id"]):
                    db.mark_replied(comment["id"])
                    continue

                response = ai.generate_own_post_reply(
                    comment["text"], comment["username"],
                    recent_context=recent_context,
                )
                if not response:
                    log.info(f"AI NULL for @{comment['username']}: {comment['text'][:80]}")
                    db.mark_replied(comment["id"])
                    continue

                log.info(f"Replying to @{comment['username']} (scan): {response}")
                success = client.reply_to_post(comment["thread_id"], response)

                if success:
                    db.mark_replied(comment["id"])
                    db.log_activity(
                        "reply_to_comment",
                        f"@{comment['username']}: {response[:150]}",
                    )
                    replied_count += 1
                else:
                    db.mark_skipped(comment["id"])
                await asyncio.sleep(3)
    finally:
        db.close()

    log.info("=== Scan own post comments done ===")


async def _publish_consumer_post(db: Database, ai: AIHandler, trend_posts: list) -> str:
    recent_posts = db.get_recent_post_texts(days=14)
    recent_context = format_activity_context(db.get_recent_activity())

    # Preferred path: read the LIVE public Threads feed (logged out, unofficial) for
    # popular food posts and write our own in the same spirit. If reading the feed or
    # generating from it fails for any reason, fall back to plain generation below.
    examples: list = []
    try:
        from feed_reader import fetch_popular_posts
        examples = await fetch_popular_posts()
        if examples:
            feed_text = ai.generate_post_from_feed(
                examples, recent_posts=recent_posts, recent_context=recent_context
            )
            if feed_text:
                log.info(f"Post generated from live feed ({len(examples)} examples)")
                return feed_text
            log.info("Feed post rejected by guards, falling back to standard generation.")
        else:
            log.info("Feed reader returned no posts, falling back to standard generation.")
    except Exception as e:
        log.error(f"Feed-based generation failed, falling back: {e}")

    # Trend signal for the fallback path: analyse the REAL popular posts we just read
    # from the live feed (examples). Only if the feed was empty do we fall back to the
    # keyword-search list (trend_posts), which is weak (returns mostly our own posts).
    trend_source = examples or trend_posts
    trend_mechanism = extract_trend_mechanism(trend_source) if trend_source else None
    if trend_mechanism:
        log.info(f"Trend mechanism: {trend_mechanism}")
    insight = db.get_insight()
    return ai.generate_daily_post(
        insight=insight,
        recent_posts=recent_posts,
        trend_mechanism=trend_mechanism,
        recent_context=recent_context,
    )


async def _publish_catalog_post(db: Database, ai: AIHandler) -> str:
    promotions = await fetch_promotions()
    new_products = await fetch_new_products()

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
    """Every day at 09:00: publish a short, conversational consumer post."""
    log.info("=== Daily post run ===")
    db = Database()

    if db.posted_today():
        log.info("Already posted today (db), skipping.")
        db.close()
        return

    try:
        client = make_client()

        if client.posted_today():
            log.info("Already posted today (API check), skipping.")
            db.mark_daily_post()
            db.close()
            return

        # EVERY day → short conversational CONSUMER post built on the viral rules
        # (live-feed hook analysis + comment-driving question).
        # B2B outreach and branded site-link posts were REMOVED from the rotation
        # (user, 2026-09-22): they target business owners / carry a link, get ~zero
        # engagement, and drag the whole account's reach down. `generate_b2b_post`
        # and `generate_site_post` are kept in ai_handler for manual use only.
        ai = AIHandler(api_key=os.environ["ANTHROPIC_API_KEY"])
        try:
            log.info("Post type today: consumer (viral rules)")
            post_text = await _publish_consumer_post(db, ai, [])
        except AIGenerationError as e:
            # Post text could not be generated (Claude API down / out of credits).
            # Do NOT publish a canned fallback — tell the owner there's no post.
            log.error(f"Post generation failed (Claude API): {e}")
            await send_telegram(
                "⚠️ Пост НЕ додано сьогодні.\n\n"
                "Схоже на проблему з Claude API, можливо закінчились кошти на ключі "
                "або сервіс тимчасово недоступний. Текст посту не згенеровано.\n\n"
                f"Деталі: {str(e)[:300]}"
            )
            return

        log.info(f"Post text: {post_text}")
        post_url = client.create_post(post_text)

        if post_url:
            # The post is LIVE at this point. A bookkeeping failure (DB) must never be
            # reported to the owner as "no post".
            try:
                thread_id = re.search(r"/post/([A-Za-z0-9_-]+)", post_url or "")
                db.mark_daily_post()
                db.save_published_post(
                    post_url, post_text,
                    thread_id=thread_id.group(1) if thread_id else None,
                )
                db.log_activity("post_published", post_text[:200])
            except Exception as e:
                log.error(f"Post is published but saving it to the DB failed: {e}")
            log.info(f"Post published: {post_url}")
            await send_telegram(
                f"✅ Новий пост у Threads (@dilovakovbasa.ua)\n\n{post_text}\n\n{post_url}"
            )
        else:
            log.error("Failed to publish post.")
            await send_telegram(
                "⚠️ Пост НЕ додано сьогодні.\n\n"
                "Текст згенеровано, але Threads не прийняв публікацію, можливо "
                "протермінувався токен доступу або API Threads недоступне.\n\n"
                f"Текст, який намагались опублікувати:\n{post_text[:300]}"
            )
    except Exception as e:
        log.error(f"daily_post unexpected error: {e}")
        await send_telegram(
            "⚠️ Пост НЕ додано сьогодні.\n\n"
            f"Сталася помилка в програмі бота: {str(e)[:300]}"
        )
    finally:
        db.close()

    log.info("=== Daily post done ===")


async def collect_post_metrics():
    """Every day at 22:00: collect metrics for recent posts, then run analysis."""
    log.info("=== Collect post metrics ===")
    db = Database()
    posts_to_check = db.get_posts_needing_metrics()

    if posts_to_check:
        client = make_client()
        for p in posts_to_check:
            metrics = client.get_post_metrics(p["thread_id"])
            db.update_post_metrics(p["url"], metrics["likes"], metrics["comments"])
            log.info(f"Metrics for {p['url']}: {metrics['likes']} likes, {metrics['comments']} comments")
            await asyncio.sleep(2)
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


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

async def main():
    _db = Database()

    # v2: clear stale replied=1 entries from old broken Playwright deploy
    _cur = _db.conn.execute("SELECT 1 FROM settings WHERE key='v2_reply_cleanup_done'")
    if not _cur.fetchone():
        deleted = _db.conn.execute("DELETE FROM processed_posts WHERE replied=1").rowcount
        _db.conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('v2_reply_cleanup_done','1')")
        _db.conn.commit()
        log.info(f"v2 cleanup: cleared {deleted} stale replied=1 entries")

    # v3: clear scan entries falsely blocked during "mark_replied on failure" period
    _cur = _db.conn.execute("SELECT 1 FROM settings WHERE key='v3_scan_cleanup_done'")
    if not _cur.fetchone():
        deleted = _db.conn.execute(
            """DELETE FROM processed_posts
               WHERE (post_id LIKE 'comment:%' OR post_id LIKE 'user_on_post:%')
               AND created_at > datetime('now', '-48 hours')"""
        ).rowcount
        _db.conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('v3_scan_cleanup_done','1')")
        _db.conn.commit()
        log.info(f"v3 cleanup: cleared {deleted} falsely-blocked scan entries")

    # Add thread_id column if migrating from old schema
    try:
        _db.conn.execute("ALTER TABLE published_posts ADD COLUMN thread_id TEXT DEFAULT NULL")
        _db.conn.commit()
        log.info("Migrated: added thread_id column to published_posts")
    except Exception:
        pass  # column already exists

    _db.close()

    scheduler = AsyncIOScheduler(
        executors={"default": AsyncIOExecutor()},
        timezone="Europe/Kyiv",
    )
    scheduler.add_job(daily_post, "cron", hour=9, minute=0, id="daily_post")
    # comment_one_post вимкнено: keyword search чекає App Review від Meta
    # scheduler.add_job(comment_one_post, "cron", hour="8,10,12,14,16,18,20", minute=0, id="comment")
    # reply_to_own_comments вимкнено: me/replies повертає тільки наші власні відповіді (мертвий).
    # Уся обробка коментарів тепер через scan_own_post_comments (повна гілка розмови).
    scheduler.add_job(scan_own_post_comments, "cron", hour="8,10,12,14,16,18,20", minute=0, id="scan_post_comments")
    # First-hour engagement window: Threads weighs how fast a fresh post gathers
    # comments (first 30-60 min). Reply quickly right after the 09:00 post so early
    # commenters get an answer inside that window (extends the evaluation window).
    scheduler.add_job(scan_own_post_comments, "cron", hour=9, minute=25, id="scan_post_comments_fast1")
    scheduler.add_job(scan_own_post_comments, "cron", hour=9, minute=55, id="scan_post_comments_fast2")
    # Reply to OTHER people's relevant posts, logged-in (unofficial). Tiny volume on
    # purpose: twice a day only, to limit ban risk (see feed_replier.py safety note).
    scheduler.add_job(reply_to_feed_post, "cron", hour=10, minute=0, id="reply_feed_morning")
    scheduler.add_job(reply_to_feed_post, "cron", hour=19, minute=0, id="reply_feed_evening")
    scheduler.add_job(collect_post_metrics, "cron", hour=22, minute=0, id="metrics")
    # Threads token lives 60 days and can only be refreshed while valid: renew weekly.
    scheduler.add_job(refresh_threads_token, "cron", day_of_week="sun", hour=12, minute=0, id="refresh_token")
    scheduler.start()
    log.info(
        "Scheduler started (official Threads API): "
        "daily post 09:00, conversation scan+reply every 2h (8-20), metrics 22:00. "
        "Replies to other posts (unofficial, logged-in session): 10:00 and 19:00."
    )

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
