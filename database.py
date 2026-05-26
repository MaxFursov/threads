import sqlite3
import json
from datetime import date
import logging

log = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str = "/app/data/bot.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS processed_posts (
                post_id TEXT PRIMARY KEY,
                replied INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS daily_posts (
                post_date TEXT PRIMARY KEY,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS published_posts (
                post_url TEXT PRIMARY KEY,
                post_text TEXT,
                likes INTEGER DEFAULT NULL,
                comments INTEGER DEFAULT NULL,
                published_at TEXT DEFAULT CURRENT_TIMESTAMP,
                metrics_updated_at TEXT DEFAULT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.conn.commit()

    def already_processed(self, post_id: str) -> bool:
        # Replied items are permanently skipped.
        # Skipped items (reply failed or AI returned None) are retried after 6 hours.
        cur = self.conn.execute(
            """SELECT 1 FROM processed_posts WHERE post_id = ?
               AND (replied = 1 OR created_at > datetime('now', '-6 hours'))""",
            (post_id,)
        )
        return cur.fetchone() is not None

    def mark_replied(self, post_id: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO processed_posts (post_id, replied) VALUES (?, 1)",
            (post_id,),
        )
        self.conn.commit()

    def mark_skipped(self, post_id: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO processed_posts (post_id, replied) VALUES (?, 0)",
            (post_id,),
        )
        self.conn.commit()

    def posted_today(self) -> bool:
        today = date.today().isoformat()
        cur = self.conn.execute(
            "SELECT 1 FROM daily_posts WHERE post_date = ?", (today,)
        )
        return cur.fetchone() is not None

    def mark_daily_post(self):
        today = date.today().isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO daily_posts (post_date) VALUES (?)", (today,)
        )
        self.conn.commit()

    def save_published_post(self, url: str, text: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO published_posts (post_url, post_text) VALUES (?, ?)",
            (url, text),
        )
        self.conn.commit()

    def get_posts_needing_metrics(self, max_count: int = 20) -> list[dict]:
        cur = self.conn.execute(
            """SELECT post_url FROM published_posts
               WHERE metrics_updated_at IS NULL
               AND published_at > datetime('now', '-30 days')
               ORDER BY published_at DESC LIMIT ?""",
            (max_count,),
        )
        return [{"url": row[0]} for row in cur.fetchall()]

    def update_post_metrics(self, url: str, likes: int, comments: int):
        self.conn.execute(
            """UPDATE published_posts
               SET likes = ?, comments = ?, metrics_updated_at = CURRENT_TIMESTAMP
               WHERE post_url = ?""",
            (likes, comments, url),
        )
        self.conn.commit()

    def get_all_posts_with_metrics(self) -> list[dict]:
        cur = self.conn.execute(
            """SELECT post_text, likes, comments FROM published_posts
               WHERE likes IS NOT NULL
               ORDER BY published_at DESC""",
        )
        return [
            {"text": row[0], "likes": row[1], "comments": row[2]}
            for row in cur.fetchall()
        ]

    def save_insight(self, insight: str):
        self.conn.execute(
            """INSERT OR REPLACE INTO settings (key, value, updated_at)
               VALUES ('performance_insight', ?, CURRENT_TIMESTAMP)""",
            (insight,),
        )
        self.conn.commit()

    def get_catalog_state(self, key: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (f"catalog_{key}",)
        )
        row = cur.fetchone()
        return json.loads(row[0]) if row else []

    def save_catalog_state(self, key: str, items: list[dict]):
        self.conn.execute(
            """INSERT OR REPLACE INTO settings (key, value, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)""",
            (f"catalog_{key}", json.dumps(items, ensure_ascii=False)),
        )
        self.conn.commit()

    def find_new_catalog_items(self, key: str, current: list[dict]) -> list[dict]:
        previous_names = {item["name"] for item in self.get_catalog_state(key)}
        return [item for item in current if item["name"] not in previous_names]

    def get_recently_mentioned(self, key: str) -> set[str]:
        cur = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (f"mentioned_{key}",)
        )
        row = cur.fetchone()
        return set(json.loads(row[0])) if row else set()

    def save_recently_mentioned(self, key: str, names: list[str]):
        self.conn.execute(
            """INSERT OR REPLACE INTO settings (key, value, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)""",
            (f"mentioned_{key}", json.dumps(names, ensure_ascii=False)),
        )
        self.conn.commit()

    def get_recent_post_texts(self, days: int = 14) -> list[str]:
        cur = self.conn.execute(
            """SELECT post_text FROM published_posts
               WHERE published_at > datetime('now', ? || ' days')
               ORDER BY published_at DESC""",
            (f"-{days}",),
        )
        return [row[0] for row in cur.fetchall() if row[0]]

    def get_insight(self) -> str | None:
        cur = self.conn.execute(
            "SELECT value FROM settings WHERE key = 'performance_insight'"
        )
        row = cur.fetchone()
        return row[0] if row else None

    def close(self):
        self.conn.close()
