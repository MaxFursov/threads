import time
import logging
import requests
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

API_BASE = "https://graph.threads.net/v1.0"


def _redact(err) -> str:
    """requests puts the full URL (incl. ?access_token=...) in HTTP error text; never log the token."""
    import re
    return re.sub(r"access_token=[^&\s'\"]+", "access_token=***", str(err))
OWN_USERNAME = "dilovakovbasa.ua"


class ThreadsClient:
    def __init__(self, access_token: str, user_id: str):
        self.token = access_token
        self.user_id = user_id

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        p = dict(params or {})
        p["access_token"] = self.token
        try:
            r = requests.get(f"{API_BASE}/{path}", params=p, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error(f"GET /{path} → {_redact(e)}")
            try:
                log.error(f"  body: {e.response.text[:300]}")  # type: ignore[attr-defined]
            except Exception:
                pass
            return None

    def _post(self, path: str, data: dict | None = None, timeout: int = 15) -> dict | None:
        d = dict(data or {})
        d["access_token"] = self.token
        try:
            r = requests.post(f"{API_BASE}/{path}", data=d, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error(f"POST /{path} → {_redact(e)}")
            try:
                log.error(f"  body: {e.response.text[:300]}")  # type: ignore[attr-defined]
            except Exception:
                pass
            return None

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def create_post(self, text: str) -> str | None:
        """Publish a text post. Returns the permalink URL or None on failure."""
        container = self._post(f"{self.user_id}/threads", {
            "media_type": "TEXT",
            "text": text,
        })
        if not container or "id" not in container:
            log.error(f"create_post: container failed: {container}")
            return None

        time.sleep(3)  # Threads requires a short delay before publishing

        # Publish is the slow step; give it a generous timeout. Even so, the
        # response can time out AFTER Threads has already accepted the post, so a
        # failure here is not proof the post is missing — we reconcile below.
        result = self._post(f"{self.user_id}/threads_publish", {
            "creation_id": container["id"],
        }, timeout=45)
        if not result or "id" not in result:
            log.error(f"create_post: publish failed: {result}; verifying against feed...")
            reconciled = self._find_recent_post_by_text(text)
            if reconciled:
                log.info(f"create_post: post actually published despite error: {reconciled['id']}")
                return reconciled["url"] or f"https://www.threads.net/@{OWN_USERNAME}/post/{reconciled['id']}"
            log.error("create_post: post not found in feed — publish truly failed")
            return None

        thread_id = result["id"]
        log.info(f"Post published: {thread_id}")

        permalink = self._get_permalink(thread_id)
        return permalink or f"https://www.threads.net/@{OWN_USERNAME}/post/{thread_id}"

    def _find_recent_post_by_text(self, text: str) -> dict | None:
        """After a publish error, check whether the post actually went through.
        Matches our most recent posts by exact text (Threads may still be
        settling, so wait briefly first). Returns the post dict or None."""
        time.sleep(5)
        target = (text or "").strip()
        for attempt in range(2):
            for post in self.get_recent_own_posts(limit=5):
                if (post.get("text") or "").strip() == target:
                    return post
            if attempt == 0:
                time.sleep(5)
        return None

    def reply_to_post(self, thread_id: str, text: str) -> bool:
        """Reply to any thread by its numeric ID."""
        container = self._post(f"{self.user_id}/threads", {
            "media_type": "TEXT",
            "text": text,
            "reply_to_id": thread_id,
        })
        if not container or "id" not in container:
            log.error(f"reply_to_post: container failed for {thread_id}: {container}")
            return False

        time.sleep(3)

        result = self._post(f"{self.user_id}/threads_publish", {
            "creation_id": container["id"],
        })
        if result and "id" in result:
            log.info(f"Replied to {thread_id} → new thread {result['id']}")
            return True

        log.error(f"reply_to_post: publish failed for {thread_id}: {result}")
        return False

    # ------------------------------------------------------------------
    # Reading activity
    # ------------------------------------------------------------------

    def get_own_post_replies(self) -> list[dict]:
        """Return replies/comments left on our content (activity feed equivalent)."""
        data = self._get("me/replies", {
            "fields": "id,text,timestamp,username,permalink,replied_to",
        })
        if not data:
            return []

        results = []
        for item in data.get("data", []):
            username = item.get("username", "")
            if username == OWN_USERNAME:
                continue  # skip our own replies
            replied_to = (item.get("replied_to") or {}).get("id", "")
            results.append({
                "id": item["id"],
                "text": item.get("text", ""),
                "username": username,
                "permalink": item.get("permalink", ""),
                # We reply to the comment itself (so our reply is threaded under it)
                "reply_target_id": item["id"],
            })
        log.info(f"get_own_post_replies: {len(results)} items")
        return results

    def get_post_comments(self, thread_id: str) -> list[dict]:
        """Return direct replies to one of our posts (top-level comments)."""
        data = self._get(f"{thread_id}/replies", {
            "fields": "id,text,timestamp,username,permalink",
        })
        if not data:
            return []

        comments = []
        seen_users: set[str] = set()
        for item in data.get("data", []):
            username = item.get("username", "")
            if not username or username == OWN_USERNAME:
                continue
            if username in seen_users:
                continue  # one reply per user per post
            seen_users.add(username)
            text = item.get("text", "")
            if not text:
                continue
            comments.append({
                "id": f"comment:{item['id']}",
                "thread_id": item["id"],          # numeric ID used for reply_to_post
                "text": text,
                "username": username,
                "permalink": item.get("permalink", ""),
            })
        log.info(f"get_post_comments({thread_id}): {len(comments)} comments")
        return comments

    def get_post_conversation(self, thread_id: str) -> list[dict]:
        """Return the ENTIRE conversation under one of our posts, flattened —
        top-level comments AND replies-to-replies (follow-up questions) at any depth.
        Excludes our own messages. No per-user dedup (has_own_reply guards duplicates)."""
        data = self._get(f"{thread_id}/conversation", {
            "fields": "id,text,timestamp,username,permalink",
            "limit": 50,
        })
        if not data:
            return []

        out = []
        for item in data.get("data", []):
            username = item.get("username", "")
            if not username or username == OWN_USERNAME:
                continue
            text = item.get("text", "")
            if not text:
                continue
            out.append({
                "id": f"comment:{item['id']}",
                "thread_id": item["id"],
                "text": text,
                "username": username,
                "permalink": item.get("permalink", ""),
            })
        log.info(f"get_post_conversation({thread_id}): {len(out)} messages from others")
        return out

    def has_own_reply(self, comment_thread_id: str) -> bool:
        """True if our account already has a reply under this comment.
        Source of truth is Threads itself, so this survives DB loss / parallel runs."""
        data = self._get(f"{comment_thread_id}/replies", {"fields": "username"})
        if not data:
            return False
        for item in data.get("data", []):
            if item.get("username", "") == OWN_USERNAME:
                return True
        return False

    def get_recent_own_posts(self, limit: int = 10) -> list[dict]:
        """Return our recent published posts with IDs and permalinks."""
        data = self._get(f"{self.user_id}/threads", {
            "fields": "id,text,timestamp,permalink",
            "limit": limit,
        })
        if not data:
            return []
        return [
            {
                "id": item["id"],
                "url": item.get("permalink", ""),
                "text": item.get("text", ""),
                "timestamp": item.get("timestamp", ""),
            }
            for item in data.get("data", [])
        ]

    # ------------------------------------------------------------------
    # Metrics & status checks
    # ------------------------------------------------------------------

    def get_post_metrics(self, thread_id: str) -> dict:
        """Return {likes, comments} for a thread."""
        data = self._get(thread_id, {"fields": "like_count,replies_count"})
        if not data:
            return {"likes": 0, "comments": 0}
        return {
            "likes": int(data.get("like_count") or 0),
            "comments": int(data.get("replies_count") or 0),
        }

    def posted_today(self) -> bool:
        """True if we already published a post today (Kyiv time)."""
        kyiv = timezone(timedelta(hours=3))
        today = datetime.now(kyiv).date()
        data = self._get(f"{self.user_id}/threads", {
            "fields": "timestamp",
            "limit": 5,
        })
        for item in (data or {}).get("data", []):
            try:
                ts = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
                if ts.astimezone(kyiv).date() == today:
                    return True
            except Exception:
                continue
        return False

    # ------------------------------------------------------------------
    # Discovery (keyword search)
    # ------------------------------------------------------------------

    def search_posts(self, keyword: str, limit: int = 20) -> list[dict]:
        """Search public Threads posts by keyword using threads_keyword_search."""
        data = self._get("keyword_search", {
            "q": keyword,
            "fields": "id,text,username,timestamp,permalink",
            "search_type": "RECENT",
            "limit": limit,
        })
        if not data:
            return []
        posts = []
        for item in data.get("data", []):
            text = item.get("text", "")
            username = item.get("username", "")
            if not text or len(text) < 10 or username == OWN_USERNAME:
                continue
            posts.append({
                "id": item["id"],
                "text": text,
                "username": username,
                "url": item.get("permalink", ""),
            })
        return posts

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_permalink(self, thread_id: str) -> str | None:
        data = self._get(thread_id, {"fields": "permalink"})
        return data.get("permalink") if data else None
