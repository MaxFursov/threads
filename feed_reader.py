"""Unofficial, LOGGED-OUT reader of the public Threads feed.

We CANNOT use the official keyword_search API to read other people's posts until
Meta approves `threads_keyword_search` (App Review). So, to learn what is popular
on our topic right now, we read the PUBLIC Threads search page with a headless
browser, WITHOUT logging in.

IMPORTANT (safety): this NEVER logs in with our account. The old account was
blocked for logged-in browser automation, so here we only read anonymous public
search results. Publishing still goes exclusively through the official Graph API
(threads_client.py). Reading is decoupled from posting on purpose.

Public profile pages force a login wall, but the search page
(`https://www.threads.net/search?q=...`) is server-rendered and readable while
logged out, including per-post like/reply counts.
"""

import asyncio
import logging
import random
from urllib.parse import quote

from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Topics we care about. Kept broad — meat, cheese, dairy, food in general —
# so the "popular right now" signal reflects our whole (expanded) assortment.
DEFAULT_KEYWORDS = ["ковбаса", "сир", "шашлик", "сніданок", "їжа"]

OWN_USERNAME = "dilovakovbasa.ua"

# JS that runs in the page and extracts one dict per post card.
_EXTRACT_JS = r"""
() => {
    const isNum = (s) => /\d/.test(s) && /^[\d\s  .,kKмтисKM]+$/.test(s);
    const toInt = (s) => parseInt(String(s).replace(/[^\d]/g, ""), 10) || 0;
    const out = [];
    const conts = document.querySelectorAll('[data-pressable-container]');
    for (const c of conts) {
        const userA = c.querySelector('a[href^="/@"]');
        const username = userA
            ? userA.getAttribute('href').replace(/^\/@/, '').replace(/\/$/, '')
            : '';
        const lines = c.innerText.split('\n').map(s => s.trim()).filter(Boolean);
        if (lines.length < 3) continue;
        // Trailing numeric lines are engagement counts (likes, replies, reposts, shares).
        let i = lines.length - 1;
        const counts = [];
        while (i > 1 && isNum(lines[i])) { counts.unshift(toInt(lines[i])); i--; }
        // lines[0] = username, lines[1] = date/relative-time, middle = post text.
        const text = lines.slice(2, i + 1).join(' ').trim();
        out.push({
            username,
            text,
            likes: counts.length > 0 ? counts[0] : 0,
            replies: counts.length > 1 ? counts[1] : 0,
        });
    }
    return out;
}
"""


def _search_url(keyword: str) -> str:
    return f"https://www.threads.net/search?q={quote(keyword)}&serp_type=default"


async def fetch_popular_posts(
    keywords: list[str] | None = None,
    per_run: int = 3,
    min_likes: int = 40,
    min_len: int = 20,
    limit: int = 12,
) -> list[dict]:
    """Read the public Threads search results (logged out) for a few food keywords
    and return the most-liked posts as {username, text, likes, replies}.

    Returns [] on any failure — callers must fall back to normal generation.
    """
    kws = list(keywords or DEFAULT_KEYWORDS)
    random.shuffle(kws)
    kws = kws[:per_run]

    posts: list[dict] = []
    seen_text: set[str] = set()

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            page = await browser.new_page(user_agent=UA, locale="uk-UA")
            try:
                for kw in kws:
                    try:
                        await page.goto(_search_url(kw), wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(5)
                        raw = await page.evaluate(_EXTRACT_JS)
                    except Exception as e:
                        log.warning(f"feed_reader: keyword '{kw}' failed: {e}")
                        continue

                    for item in raw or []:
                        text = (item.get("text") or "").strip()
                        username = (item.get("username") or "").strip()
                        if username == OWN_USERNAME:
                            continue
                        if len(text) < min_len:
                            continue
                        if item.get("likes", 0) < min_likes:
                            continue
                        key = text[:60].lower()
                        if key in seen_text:
                            continue
                        seen_text.add(key)
                        posts.append({
                            "username": username,
                            "text": text,
                            "likes": int(item.get("likes", 0)),
                            "replies": int(item.get("replies", 0)),
                        })
            finally:
                await browser.close()
    except Exception as e:
        log.error(f"feed_reader.fetch_popular_posts failed: {e}")
        return []

    posts.sort(key=lambda x: x["likes"], reverse=True)
    log.info(f"feed_reader: {len(posts)} popular posts (keywords={kws})")
    return posts[:limit]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(fetch_popular_posts())
    print(f"\n=== {len(result)} popular posts ===")
    for pst in result:
        print(f"\n[{pst['likes']}♥ {pst['replies']}💬] @{pst['username']}")
        print(f"  {pst['text'][:200]}")
