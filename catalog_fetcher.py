import asyncio
import re
import logging
from playwright.async_api import async_playwright

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


async def _scrape_page(url: str) -> list[dict]:
    """Launch a minimal Playwright browser, scrape one catalog page, return parsed items."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
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
            return _parse_cards(cards_data)
        except Exception as e:
            log.error(f"_scrape_page({url}) error: {e}")
            return []
        finally:
            await browser.close()


async def fetch_promotions() -> list[dict]:
    items = await _scrape_page(f"{CATALOG_BASE}/promotion/aktsiia")
    log.info(f"Promotions fetched: {len(items)}")
    return items


async def fetch_new_products() -> list[dict]:
    items = await _scrape_page(f"{CATALOG_BASE}/promotion/novynky")
    log.info(f"New products fetched: {len(items)}")
    return items
