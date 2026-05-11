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

            await self.page.get_by_role("button", name="Post").click()
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

    async def create_post(self, text: str) -> bool:
        await self.page.goto(BASE + "/", wait_until="load", timeout=30000)
        await asyncio.sleep(4)

        try:
            # Click "What's new?" area to open composer
            whats_new = self.page.get_by_text("What's new?")
            await whats_new.click()
            await asyncio.sleep(2)

            editor = await self.page.query_selector('[contenteditable="true"]')
            if not editor:
                log.error("Post editor not found")
                return False

            await editor.click()
            await editor.type(text, delay=30)
            await asyncio.sleep(1)

            # Click Post button
            post_btn = self.page.get_by_role("button", name="Post", exact=True)
            await post_btn.click()
            await asyncio.sleep(3)

            log.info(f"Post created: {text[:60]}...")
            return True

        except Exception as e:
            log.error(f"Post failed: {e}")
            return False
