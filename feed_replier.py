"""Unofficial, LOGGED-IN replier to other people's Threads posts.

The official Graph API cannot reply to arbitrary external posts until Meta
approves `threads_keyword_search` (App Review). To engage with relevant posts
now, this module drives a headless browser that is LOGGED IN as our account and
leaves a short reply.

SAFETY / RISK (read this before touching cadence):
- The previous account (@dilovakovbasa.official) was BLOCKED for logged-in
  browser automation. Logged-in automation is the ban vector. This module exists
  only because the owner explicitly accepted that risk in exchange for engaging
  with other posts, on the bet that a very low volume is tolerated.
- KEEP THE VOLUME TINY. One reply per run, twice a day (10:00, 19:00). Do not
  raise this without the owner's say-so.
- We reuse a SAVED SESSION (cookies) instead of logging in with a password each
  run: fresh datacenter logins are the single biggest ban trigger. The session
  is captured once by the owner locally (capture_session.py) and provided via
  env. We never store or type the account password here.
- Reading popular posts is still done logged-out in feed_reader.py; this module
  is the only place we act while authenticated. Publishing our OWN daily posts
  stays on the official Graph API.

Session source (first that exists wins):
    env THREADS_SESSION_JSON   — Playwright storage_state as a JSON string
    env THREADS_SESSION_FILE   — path to a storage_state JSON file
    /app/data/threads_session.json (Railway volume) or ./threads_session.json
"""

import os
import json
import asyncio
import logging
import random
import hashlib
from urllib.parse import quote

from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_KEYWORDS = ["ковбаса", "сир", "шашлик", "сніданок", "їжа", "сосиски"]
OWN_USERNAME = "dilovakovbasa.ua"

# Extract candidate posts INCLUDING their permalink (needed to open + reply).
_EXTRACT_JS = r"""
() => {
    const out = [];
    const conts = document.querySelectorAll('[data-pressable-container]');
    for (const c of conts) {
        const userA = c.querySelector('a[href^="/@"]');
        const username = userA
            ? userA.getAttribute('href').replace(/^\/@/, '').replace(/\/$/, '')
            : '';
        // Permalink: an anchor whose href looks like /@user/post/CODE
        let url = '';
        for (const a of c.querySelectorAll('a[href*="/post/"]')) {
            const h = a.getAttribute('href') || '';
            if (/\/@[^/]+\/post\//.test(h)) { url = h; break; }
        }
        const lines = c.innerText.split('\n').map(s => s.trim()).filter(Boolean);
        if (lines.length < 3 || !username || !url) continue;
        const isNum = (s) => /\d/.test(s) && /^[\d\s  .,kKмтисKM]+$/.test(s);
        const toInt = (s) => parseInt(String(s).replace(/[^\d]/g, ""), 10) || 0;
        let i = lines.length - 1;
        const counts = [];
        while (i > 1 && isNum(lines[i])) { counts.unshift(toInt(lines[i])); i--; }
        const text = lines.slice(2, i + 1).join(' ').trim();
        const t = c.querySelector('time');
        const iso = t ? (t.getAttribute('datetime') || '') : '';
        out.push({ username, text, url, iso, likes: counts.length ? counts[0] : 0 });
    }
    return out;
}
"""


def _load_storage_state():
    """Return the Playwright storage_state (dict) or None if no session is set up."""
    raw = os.getenv("THREADS_SESSION_JSON")
    if raw:
        try:
            return json.loads(raw)
        except Exception as e:
            log.error(f"feed_replier: THREADS_SESSION_JSON is not valid JSON: {e}")
            return None

    candidates = [
        os.getenv("THREADS_SESSION_FILE"),
        "/app/data/threads_session.json",
        "./threads_session.json",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                log.error(f"feed_replier: failed to read session file {path}: {e}")
    return None


MAX_POST_AGE_HOURS = 24  # only reply to FRESH posts; old ones get no eyes and look like spam


def _search_url(keyword: str) -> str:
    # filter=recent returns newest posts first (default search mixes in months-old ones)
    return f"https://www.threads.net/search?q={quote(keyword)}&serp_type=default&filter=recent"


def _age_hours(iso: str) -> float | None:
    """Post age in hours from an ISO timestamp, or None if unparseable."""
    from datetime import datetime, timezone
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except Exception:
        return None


async def _looks_logged_out(page) -> bool:
    """Heuristic: are we on / redirected to a login wall?"""
    url = page.url or ""
    if "/login" in url or "accounts/login" in url:
        return True
    try:
        # A visible "Log in" / "Увійти" affordance strongly implies logged out.
        for name in ("Log in", "Увійти", "Log In"):
            if await page.get_by_role("button", name=name).count() > 0:
                return True
    except Exception:
        pass
    return False


async def _post_reply(page, post_url: str, text: str, dry_run: bool = False, shot_path: str | None = None) -> bool:
    """Open a post and leave one reply. Returns True on apparent success.
    dry_run: do everything EXCEPT press the final publish button (for testing);
    saves a screenshot to shot_path and returns True."""
    await page.goto(f"https://www.threads.net{post_url}", wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(random.uniform(3, 6))

    if await _looks_logged_out(page):
        raise RuntimeError("not_logged_in")

    # Open the reply composer. Try a few affordances (UA + EN labels).
    opened = False
    for name in ("Відповісти", "Reply", "Відповідь"):
        try:
            loc = page.get_by_role("button", name=name).first
            if await loc.count() > 0:
                await loc.click(timeout=5000)
                opened = True
                break
        except Exception:
            continue
    if not opened:
        # Fallback: click the composer placeholder area.
        for placeholder in ("Відповісти", "Reply", "Напишіть відповідь", "Add a reply"):
            try:
                loc = page.get_by_text(placeholder, exact=False).first
                if await loc.count() > 0:
                    await loc.click(timeout=5000)
                    opened = True
                    break
            except Exception:
                continue
    if not opened:
        raise RuntimeError("reply_composer_not_found")

    await asyncio.sleep(random.uniform(1.5, 3))

    # Type into the contenteditable textbox.
    typed = False
    try:
        box = page.get_by_role("textbox").last
        await box.click(timeout=5000)
        await box.type(text, delay=random.randint(25, 60))
        typed = True
    except Exception:
        pass
    if not typed:
        try:
            box = page.locator('[contenteditable="true"]').last
            await box.click(timeout=5000)
            await box.type(text, delay=random.randint(25, 60))
            typed = True
        except Exception:
            pass
    if not typed:
        raise RuntimeError("textbox_not_found")

    await asyncio.sleep(random.uniform(1, 2.5))

    if dry_run:
        if shot_path:
            await page.screenshot(path=shot_path)
        return True

    # Submit. The publish button is labelled "Опублікувати"/"Post"/"Дописати".
    for name in ("Опублікувати", "Post", "Дописати", "Publish"):
        try:
            btn = page.get_by_role("button", name=name, exact=True).last
            if await btn.count() > 0 and await btn.is_enabled():
                await btn.click(timeout=5000)
                await asyncio.sleep(random.uniform(3, 5))
                return True
        except Exception:
            continue

    raise RuntimeError("post_button_not_found")


async def reply_to_one_relevant_post(ai, db, dry_run: bool = False, shot_path: str | None = None) -> dict:
    """Find ONE relevant post from the live feed and reply to it (logged in).

    Returns a result dict:
        {"status": "replied", "username": ..., "url": ..., "reply": ...}
        {"status": "none"}                 — nothing suitable found
        {"status": "no_session"}           — session cookies not configured
        {"status": "error", "error": ...}  — something went wrong
    Never raises.
    """
    storage_state = _load_storage_state()
    if not storage_state:
        log.warning("feed_replier: no session configured (THREADS_SESSION_JSON / file).")
        return {"status": "no_session"}

    kws = list(DEFAULT_KEYWORDS)
    random.shuffle(kws)
    kws = kws[:3]

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                user_agent=UA, locale="uk-UA", storage_state=storage_state
            )
            page = await context.new_page()
            try:
                # Gather candidates across a few keywords.
                candidates: list[dict] = []
                seen: set[str] = set()
                for kw in kws:
                    try:
                        await page.goto(_search_url(kw), wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(random.uniform(4, 7))
                        if await _looks_logged_out(page):
                            log.error("feed_replier: session appears logged out.")
                            return {"status": "error", "error": "session_expired"}
                        raw = await page.evaluate(_EXTRACT_JS)
                    except Exception as e:
                        log.warning(f"feed_replier: keyword '{kw}' failed: {e}")
                        continue
                    for item in raw or []:
                        username = (item.get("username") or "").strip()
                        text = (item.get("text") or "").strip()
                        url = (item.get("url") or "").strip()
                        if not username or not url or username == OWN_USERNAME:
                            continue
                        if len(text) < 20:
                            continue
                        # Skip replies inside someone else's thread; we want top-level posts.
                        if text.startswith("Відповідь користувачу"):
                            continue
                        age = _age_hours(item.get("iso") or "")
                        if age is None or age < 0 or age > MAX_POST_AGE_HOURS:
                            continue
                        if url in seen:
                            continue
                        seen.add(url)
                        candidates.append({
                            "username": username, "text": text, "url": url,
                            "likes": int(item.get("likes", 0)),
                        })

                # Prefer posts that already have some traction (a reply under a post
                # nobody sees gets no reach); shuffle first so ties vary run to run.
                random.shuffle(candidates)
                candidates.sort(key=lambda c: c["likes"], reverse=True)
                log.info(f"feed_replier: {len(candidates)} fresh candidate posts (<= {MAX_POST_AGE_HOURS}h)")

                failures = 0
                last_error = ""
                for cand in candidates:
                    url_key = "reply:" + hashlib.sha256(cand["url"].encode()).hexdigest()[:16]
                    if db.already_processed(url_key):
                        continue

                    reply = ai.generate_reply(cand["text"])
                    if not reply:
                        db.mark_replied(url_key)  # irrelevant, don't revisit
                        continue

                    log.info(f"feed_replier: replying to @{cand['username']}: {reply}")
                    try:
                        ok = await _post_reply(page, cand["url"], reply, dry_run=dry_run, shot_path=shot_path)
                    except RuntimeError as e:
                        log.error(f"feed_replier: post_reply failed on {cand['url']}: {e}")
                        if shot_path:  # debugging aid for local runs
                            try:
                                await page.screenshot(path=shot_path.replace(".png", "_fail.png"))
                            except Exception:
                                pass
                        if str(e) == "not_logged_in":
                            return {"status": "error", "error": "session_expired"}
                        # Post-specific problem (e.g. replies restricted on that post, or a
                        # different layout). Don't retry this post; try the next one, but
                        # give up after a few so a real UI change gets reported, not looped.
                        db.mark_replied(url_key)
                        failures += 1
                        last_error = str(e)
                        if failures >= 3:
                            return {"status": "error", "error": last_error}
                        continue

                    if dry_run:
                        return {
                            "status": "dry_run",
                            "username": cand["username"],
                            "url": f"https://www.threads.net{cand['url']}",
                            "reply": reply,
                        }

                    db.mark_replied(url_key)
                    if ok:
                        db.log_activity(
                            "comment_on_feed",
                            f"@{cand['username']}: {reply[:150]}",
                        )
                        return {
                            "status": "replied",
                            "username": cand["username"],
                            "url": f"https://www.threads.net{cand['url']}",
                            "reply": reply,
                        }
                    else:
                        return {"status": "error", "error": "publish_unconfirmed"}

                return {"status": "none"}
            finally:
                await context.close()
                await browser.close()
    except Exception as e:
        log.error(f"feed_replier.reply_to_one_relevant_post failed: {e}")
        return {"status": "error", "error": str(e)}
