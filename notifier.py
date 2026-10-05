"""Operational notifications to the owner via a dedicated Telegram bot.

Uses the Telegram Bot API over plain HTTPS (no user session, no telethon), so it
never conflicts with the facebook_dk account session. The owner creates a bot via
@BotFather, sends it /start once, and we message his private chat by numeric id.

Config (env):
    TG_BOT_TOKEN       — token from @BotFather (required)
    TG_NOTIFY_CHAT_ID  — one or more numeric chat ids, comma-separated (required;
                         each obtained via getUpdates after that person sends
                         /start to the bot). Every listed recipient gets the log.

Best-effort: send_telegram never raises. A failed notification must never break
the bot itself.
"""

import os
import logging
import requests

log = logging.getLogger(__name__)


async def send_telegram(text: str) -> bool:
    """Send one message to every configured recipient via the notification bot.
    Returns True if at least one send succeeded. Never raises."""
    token = os.getenv("TG_BOT_TOKEN")
    raw = os.getenv("TG_NOTIFY_CHAT_ID")
    if not token or not raw:
        log.warning("notifier: TG_BOT_TOKEN / TG_NOTIFY_CHAT_ID missing, skipping notification")
        return False

    chat_ids = [c.strip() for c in raw.split(",") if c.strip()]
    if not chat_ids:
        log.warning("notifier: TG_NOTIFY_CHAT_ID empty after parsing, skipping notification")
        return False

    any_ok = False
    for chat_id in chat_ids:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": False,
                },
                timeout=15,
            )
            r.raise_for_status()
            log.info(f"notifier: sent Telegram notification to {chat_id}")
            any_ok = True
        except Exception as e:
            log.error(f"notifier: failed to send Telegram message to {chat_id}: {e}")
            try:
                log.error(f"  body: {e.response.text[:300]}")  # type: ignore[attr-defined]
            except Exception:
                pass
    return any_ok
