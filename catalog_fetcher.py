import asyncio
import re
import logging
from playwright.async_api import Page

log = logging.getLogger(__name__)

CATALOG_BASE = "https://www.dilovakovbasa.ua"


def _parse_price(text: str) -> float | None:
    m = re.search(r"([\d]+\.[\d]+)", text)
    return float(m.group(1)) if m else None


def _parse_cards(cards_data: list[dict]) -> list[dict]:
    items = []
    for card in cards_data:
        name = card["name"].removeprefix("АКЦІЯ ").strip()
        text = card["text"]

        retail = old_retail = None
        parts = text.split("|")
        try:
            ri = next(i for i, p in enumerate(parts) if "Роздріб" in p)
            retail = _parse_price(parts[ri + 1]) if ri + 1 < len(parts) else None
            old_retail = _parse_price(parts[ri + 2]) if ri + 2 < len(parts) else None
        except (StopIteration, IndexError):
            pass

        if name:
            items.append({
                "name": name,
                "price": retail,
                "old_price": old_retail if old_retail and old_retail != retail else None,
            })
    return items


async def fetch_promotions(page: Page) -> list[dict]:
    try:
        await page.goto(f"{CATALOG_BASE}/promotion/aktsiia", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)
        cards_data = await page.evaluate("""
        () => {
            const cards = document.querySelectorAll(".product-cart-wrap");
            return Array.from(cards).map(card => ({
                name: (card.querySelector("h2 a") || card.querySelector("h2") || {innerText: ""}).innerText.trim(),
                text: card.innerText.replace(/\\n+/g, "|")
            }));
        }
        """)
        items = _parse_cards(cards_data)
        log.info(f"Promotions fetched: {len(items)}")
        return items
    except Exception as e:
        log.error(f"fetch_promotions error: {e}")
        return []


async def fetch_new_products(page: Page) -> list[dict]:
    try:
        await page.goto(f"{CATALOG_BASE}/promotion/novynky", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)
        cards_data = await page.evaluate("""
        () => {
            const cards = document.querySelectorAll(".product-cart-wrap");
            return Array.from(cards).map(card => ({
                name: (card.querySelector("h2 a") || card.querySelector("h2") || {innerText: ""}).innerText.trim(),
                text: card.innerText.replace(/\\n+/g, "|")
            }));
        }
        """)
        items = _parse_cards(cards_data)
        log.info(f"New products fetched: {len(items)}")
        return items
    except Exception as e:
        log.error(f"fetch_new_products error: {e}")
        return []
