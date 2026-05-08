import asyncio
import os
import json
import logging
from urllib.parse import unquote
from playwright.async_api import async_playwright
from dotenv import load_dotenv
import anthropic

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


async def collect_trending_posts(limit: int = 30) -> list[dict]:
    """Collect popular posts from Threads For You feed."""
    posts = []

    p = await async_playwright().start()
    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox"],
    )
    ctx = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
    )

    session_id = unquote(os.environ["THREADS_SESSION_ID"])
    cookies = []
    for domain in [".threads.net", ".threads.com"]:
        for name, value in [
            ("sessionid", session_id),
            ("ds_user_id", os.environ["THREADS_DS_USER_ID"]),
            ("csrftoken", os.environ["THREADS_CSRF_TOKEN"]),
        ]:
            cookies.append({"name": name, "value": value, "domain": domain, "path": "/"})

    await ctx.add_cookies(cookies)
    page = await ctx.new_page()

    captured_threads = []

    async def capture_response(response):
        if "graphql" not in response.url or response.status != 200:
            return
        try:
            body = await response.json()
            data = body.get("data") or {}

            # For You feed
            feed = data.get("feedData")
            if feed and isinstance(feed, dict):
                for edge in feed.get("edges", []):
                    node = edge.get("node", {})
                    thread = node.get("text_post_app_thread") or node.get("thread")
                    if thread:
                        captured_threads.append(thread)
        except Exception:
            pass

    page.on("response", capture_response)

    log.info("Loading For You feed...")
    await page.goto("https://www.threads.com/", wait_until="load", timeout=30000)
    await asyncio.sleep(4)

    # Scroll to load more posts
    for _ in range(4):
        await page.evaluate("window.scrollBy(0, 1200)")
        await asyncio.sleep(2)

    page.remove_listener("response", capture_response)
    await browser.close()
    await p.stop()

    log.info(f"Raw threads captured: {len(captured_threads)}")

    # Extract post data with engagement metrics
    for thread in captured_threads:
        # Feed uses text_post_app_thread, search uses thread
        items = thread.get("thread_items", [])
        if not items:
            continue
        post = items[0].get("post", {})
        text = (post.get("caption") or {}).get("text", "")
        if not text or len(text) < 10:
            continue

        like_count = post.get("like_count", 0) or 0
        info = post.get("text_post_app_info") or {}
        reply_count = info.get("direct_reply_count", 0) or 0
        repost_count = info.get("repost_count", 0) or 0
        username = (post.get("user") or {}).get("username", "")

        posts.append({
            "text": text,
            "username": username,
            "likes": like_count,
            "replies": reply_count,
            "reposts": repost_count,
            "score": like_count + reply_count * 3 + repost_count * 2,
        })

        if len(posts) >= limit:
            break

    # Sort by engagement score
    posts.sort(key=lambda x: x["score"], reverse=True)
    return posts


def analyze_and_draft(posts: list[dict]) -> str:
    """Send posts to Claude, analyze trends, generate a draft post."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    posts_text = "\n\n".join(
        f"[Лайки: {p['likes']}, Відповіді: {p['replies']}, Репости: {p['reposts']}]\n{p['text']}"
        for p in posts[:20]
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": f"""Ось популярні пости з Threads (відсортовані за залученістю):

{posts_text}

КРОК 1. Для кожного з топ-3 постів визнач одним реченням: який психологічний механізм робить його популярним (впізнаваність, провокація, несподіваний кут, гумор тощо).

КРОК 2. Обери один механізм і напиши пост про ковбасу або м'ясні продукти, використовуючи саме його.

Контекст акаунту: Ділова Ковбаса - постачальник м'ясних виробів від українських виробників. Але пост не має звучати як реклама. Пиши як жива людина яка розбирається в темі.

Правила:
- 1-3 речення максимум
- Без "В асортименті є", "у нас є", само-презентацій
- Без посилань на сайт
- Без емодзі
- Тільки короткий дефіс (-), без довгого тире (—)
- Без markdown
- ЗАБОРОНЕНО починати речення з "Більшість людей"

Формат відповіді:
МЕХАНІЗМИ: [топ-3 аналіз]
ПОСТ: [текст]"""
        }],
    )

    return response.content[0].text.strip()


async def main():
    log.info("Collecting trending posts...")
    posts = await collect_trending_posts(limit=30)

    if not posts:
        print("No posts collected. Check cookies or connection.")
        return

    print(f"\n=== TOP {min(5, len(posts))} POSTS BY ENGAGEMENT ===")
    for i, p in enumerate(posts[:5], 1):
        print(f"\n#{i} [likes:{p['likes']} replies:{p['replies']} reposts:{p['reposts']}]")
        print(p["text"][:150])

    print("\n=== CLAUDE ANALYSIS + DRAFT POST ===\n")
    result = analyze_and_draft(posts)
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
