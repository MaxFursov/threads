import sqlite3
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

    def close(self):
        self.conn.close()
