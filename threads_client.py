import asyncio
import logging
from urllib.parse import unquote, quote
from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

BASE = "https://www.threads.com"


class ThreadsClient:
    def __init__(self, session_id: str, ds_user_id: str, csrf_token: str):
        self.session_id = unquote(session_id)
        self.ds_user_id = ds_user_id
        self.csrf_token = csrf_token
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )

        cookies = []
        for domain in [".threads.net", ".threads.com"]:
            for name, value in [
                ("sessionid", self.session_id),
                ("ds_user_id", self.ds_user_id),
                ("csrftoken", self.csrf_token),
            ]:
                cookies.append({"name": name, "value": value, "domain": domain, "path": "/"})

        await self._context.add_cookies(cookies)
        self.page = await self._context.new_page()

        log.info("Loading Threads...")
        await self.page.goto(BASE + "/", wait_until="load", timeout=30000)
        await asyncio.sleep(2)
        log.info(f"Loaded: {self.page.url}")

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def search_posts(self, keyword: str, limit: int = 20) -> list[dict]:
        posts = []
        captured = []

        async def capture_response(response):
            if "graphql" in response.url and response.status == 200:
                try:
                    body = await response.json()
                    data = body.get("data") or {}
                    if "searchResults" in data and data["searchResults"]:
                        captured.append(data["searchResults"])
                except Exception:
                    pass

        self.page.on("response", capture_response)

        encoded = quote(keyword)
        await self.page.goto(
            f"{BASE}/search/?q={encoded}&serp_type=default",
            wait_until="load",
            timeout=30000,
        )
        await asyncio.sleep(5)
        await self.page.evaluate("window.scrollTo(0, 1000)")
        await asyncio.sleep(2)

        self.page.remove_listener("response", capture_response)

        for result in captured:
            for edge in result.get("edges", []):
                thread = edge.get("node", {}).get("thread", {})
                items = thread.get("thread_items", [])
                if not items:
                    continue
                post = items[0].get("post", {})
                post_id = str(post.get("pk") or "")
                text = (post.get("caption") or {}).get("text", "")
                code = post.get("code", "")
                if post_id and text:
                    posts.append({
                        "id": post_id,
                        "text": text,
                        "url": f"{BASE}/t/{code}" if code else "",
                    })
                if len(posts) >= limit:
                    break

        log.info(f"Search '{keyword}': {len(posts)} posts")
        return posts

    async def reply_to_post(self, post_url: str, reply_text: str) -> bool:
        if not post_url:
            return False

        await self.page.goto(post_url, wait_until="load", timeout=30000)
        await asyncio.sleep(2)

        try:
            # Find the Reply button for the specific post we navigated to.
            # When viewing a reply URL, the page shows parent posts above — we must
            # click the button whose container links to the exact target post, not
            # the first button on the page (which belongs to the parent post).
            target_path = post_url.replace("https://www.threads.com", "")
            btn_idx = await self.page.evaluate(r"""
(targetPath) => {
    const btns = Array.from(document.querySelectorAll('svg[aria-label="Reply"]'));
    for (let i = 0; i < btns.length; i++) {
        let el = btns[i];
        for (let d = 0; d < 20; d++) {
            el = el.parentElement;
            if (!el) break;
            const postLinks = el.querySelectorAll('a[href*="/post/"]');
            if (postLinks.length > 0) {
                for (const a of postLinks) {
                    if (a.getAttribute('href') === targetPath) return i;
                }
                break;
            }
        }
    }
    return 0;
}
""", target_path)

            btns = await self.page.query_selector_all('svg[aria-label="Reply"]')
            if btns:
                await btns[btn_idx].click()
            else:
                await self.page.get_by_role("button", name="Reply").first.click()

            await asyncio.sleep(1.5)

            editor = await self.page.query_selector('[contenteditable="true"][role="textbox"]')
            if not editor:
                editor = await self.page.query_selector('[contenteditable="true"]')

            if not editor:
                log.error("Reply editor not found")
                return False

            await editor.click()
            await editor.type(reply_text, delay=40)
            await asyncio.sleep(1)

            await self.page.get_by_role("button", name="Post", exact=True).first.click()
            await asyncio.sleep(2)

            log.info(f"Replied to {post_url}")
            return True

        except Exception as e:
            log.error(f"Reply failed: {e}")
            return False

    async def get_own_post_replies(self, username: str, limit: int = 10) -> list[dict]:
        """Get recent activity (replies to our posts and comments) from the activity page."""
        own_usernames = {username, "dilovakovbasa.official", "dilovakovbasa.ua"}
        own_set_js = str(list(own_usernames))

        await self.page.goto(f"{BASE}/activity", wait_until="load", timeout=30000)
        await asyncio.sleep(4)

        items = await self.page.evaluate(r"""
(ownList) => {
    const results = [];
    const seen = new Set();
    const OWN = new Set(ownList);

    for (const link of document.querySelectorAll('a[href]')) {
        const href = link.href;
        const m = href.match(/threads\.com\/@([^/]+)\/post\/([A-Za-z0-9_-]+)\b/);
        if (!m) continue;
        const user = m[1];
        const code = m[2];
        if (OWN.has(user)) continue;
        if (seen.has(code)) continue;
        seen.add(code);

        // Walk up while container has only 1 post link (individual notification item)
        let el = link;
        for (let i = 0; i < 12; i++) {
            const parent = el.parentElement;
            if (!parent) break;
            if (parent.querySelectorAll('a[href*="/post/"]').length > 1) break;
            el = parent;
        }

        const text = el.innerText.trim();
        const lines = text.split('\n')
            .map(l => l.trim())
            .filter(l => l && l !== user && !l.match(/^\d+[smhdw]$/) && l.length > 2);
        const cleanText = lines.join(' ').trim();

        if (cleanText) {
            results.push({
                id: code,
                username: user,
                post_url: href,
                text: cleanText.substring(0, 500)
            });
        }
    }
    return results;
}
""", list(own_usernames))

        log.info(f"Activity page: {len(items)} reply items found")
        return items

    async def posted_today_on_profile(self, own_username: str) -> bool:
        """Check the profile page to see if a post was published today."""
        await self.page.goto(f"{BASE}/@{own_username}", wait_until="load", timeout=30000)
        await asyncio.sleep(3)
        # Threads shows relative timestamps like "1h", "2h", "30m" for recent posts
        result = await self.page.evaluate(r"""
() => {
    for (const el of document.querySelectorAll('time, [datetime]')) {
        const dt = el.getAttribute('datetime');
        if (dt) {
            const posted = new Date(dt);
            const now = new Date();
            const hoursAgo = (now - posted) / 3600000;
            if (hoursAgo < 24) return true;
        }
    }
    // fallback: look for relative time labels under 24h
    const texts = document.body.innerText;
    return /\b([1-9]|1\d|2[0-3])\s*[hгч]\b|\b[1-5]?\d\s*min\b|\b[1-5]?\d\s*хв\b|\b[1-5]?\d\s*m\b/.test(texts);
}
""")
        return bool(result)

    async def create_post(self, text: str, own_username: str = "dilovakovbasa") -> str | None:
        """Publish a post and return its URL, or None on failure."""
        await self.page.goto(BASE + "/", wait_until="load", timeout=30000)
        await asyncio.sleep(4)

        try:
            whats_new = self.page.get_by_text("What's new?")
            await whats_new.click()
            await asyncio.sleep(2)

            editor = await self.page.query_selector('[contenteditable="true"]')
            if not editor:
                log.error("Post editor not found")
                return None

            await editor.click()
            await editor.type(text, delay=30)
            await asyncio.sleep(1)

            post_btn = self.page.get_by_role("button", name="Post", exact=True).first
            await post_btn.click()
            await asyncio.sleep(4)

            log.info(f"Post created: {text[:60]}...")

            # Navigate to own profile to grab the URL of the freshly published post
            await self.page.goto(f"{BASE}/@{own_username}", wait_until="load", timeout=30000)
            await asyncio.sleep(2)
            link = await self.page.query_selector('a[href*="/post/"]')
            if link:
                href = await link.get_attribute("href")
                return f"{BASE}{href}" if href and href.startswith("/") else href
            return None

        except Exception as e:
            log.error(f"Post failed: {e}")
            return None

    async def get_post_metrics(self, post_url: str) -> dict:
        """Load a post page and return {likes, comments} extracted from GraphQL responses."""
        metrics = {"likes": 0, "comments": 0}
        captured: list[dict] = []

        async def on_response(response):
            if "graphql" in response.url and response.status == 200:
                try:
                    captured.append(await response.json())
                except Exception:
                    pass

        self.page.on("response", on_response)
        try:
            await self.page.goto(post_url, wait_until="load", timeout=30000)
            await asyncio.sleep(3)
        finally:
            self.page.remove_listener("response", on_response)

        for data in captured:
            found = _extract_metrics(data)
            if found["likes"] or found["comments"]:
                return found

        return metrics


def _extract_metrics(obj, depth: int = 0) -> dict:
    empty = {"likes": 0, "comments": 0}
    if depth > 12:
        return empty
    if isinstance(obj, dict):
        if "like_count" in obj:
            return {
                "likes": int(obj.get("like_count") or 0),
                "comments": int(obj.get("reply_count") or obj.get("text_post_app_reply_count") or 0),
            }
        for v in obj.values():
            r = _extract_metrics(v, depth + 1)
            if r["likes"] or r["comments"]:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _extract_metrics(item, depth + 1)
            if r["likes"] or r["comments"]:
                return r
    return empty
