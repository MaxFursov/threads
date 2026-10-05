"""Keeps the long-lived Threads access token alive without manual work.

Threads tokens last 60 days and can only be refreshed while still VALID (and at
least 24h old). Once expired, a human must redo the OAuth flow. The bot used to
rely on a hand-set env var, so the token silently expired (2026-10-01) and
publishing stopped. Now:

- The freshest token is persisted next to the DB (volume /app/data, or ./ locally).
- `get_token()` returns it; main.make_client() uses it for every job.
- `refresh_token()` runs weekly and extends the token by another 60 days.
- If the env var THREADS_ACCESS_TOKEN is changed by hand (a brand-new token after
  re-auth), the stored token is based on a different env token, so it is
  discarded and the env token wins. Re-auth therefore stays a one-step change.
"""

import os
import json
import hashlib
import logging
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

REFRESH_URL = "https://graph.threads.net/refresh_access_token"


def _store_path() -> str:
    d = "/app/data" if os.path.isdir("/app/data") else "."
    return os.path.join(d, "threads_token.json")


def _fp(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()[:16]


def _read_store() -> dict | None:
    try:
        with open(_store_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_store(data: dict) -> None:
    try:
        with open(_store_path(), "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(_store_path(), 0o600)
    except Exception as e:
        log.error(f"token_manager: could not persist token: {e}")


def get_token() -> str:
    """Freshest valid token: the persisted refreshed one, unless the env token was
    replaced by hand since (then the env token wins)."""
    env_token = os.environ["THREADS_ACCESS_TOKEN"]
    store = _read_store()
    if store and store.get("env_fp") == _fp(env_token) and store.get("token"):
        return store["token"]
    return env_token


def refresh_token() -> tuple[bool, str]:
    """Extend the token by 60 days. Returns (ok, message). Never raises.
    A 'too recent' refusal (token < 24h old) counts as OK, nothing to do."""
    env_token = os.environ["THREADS_ACCESS_TOKEN"]
    current = get_token()
    try:
        r = requests.get(
            REFRESH_URL,
            params={"grant_type": "th_refresh_token", "access_token": current},
            timeout=30,
        )
        body = r.json() if r.content else {}
    except Exception as e:
        return False, f"network error: {type(e).__name__}"

    if r.status_code == 200 and body.get("access_token"):
        _write_store({
            "env_fp": _fp(env_token),
            "token": body["access_token"],
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
            "expires_in": body.get("expires_in"),
        })
        days = int((body.get("expires_in") or 0) / 86400)
        log.info(f"token_manager: token refreshed, valid ~{days} more days")
        return True, f"токен продовжено, діє ще ~{days} днів"

    msg = (body.get("error") or {}).get("message", f"HTTP {r.status_code}")
    if "24 hours" in msg or "too recent" in msg.lower():
        log.info("token_manager: token too new to refresh, skipping")
        return True, "токен ще занадто свіжий, оновлення не потрібне"
    log.error(f"token_manager: refresh failed: {msg}")
    return False, msg
