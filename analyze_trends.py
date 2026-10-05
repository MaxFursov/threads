import os
import logging
import anthropic

log = logging.getLogger(__name__)

FOOD_KEYWORDS = ["ковбаса", "шашлик", "сосиски", "м'ясо", "барбекю", "делікатеси"]


def collect_trending_posts(limit: int = 30) -> list[dict]:
    """Search for relevant food/meat posts using the official Threads keyword search API."""
    from threads_client import ThreadsClient

    client = ThreadsClient(
        access_token=os.environ["THREADS_ACCESS_TOKEN"],
        user_id=os.environ["THREADS_USER_ID"],
    )

    posts: list[dict] = []
    seen_ids: set[str] = set()

    for keyword in FOOD_KEYWORDS:
        if len(posts) >= limit:
            break
        results = client.search_posts(keyword, limit=20)
        for post in results:
            if post["id"] not in seen_ids and post.get("text"):
                seen_ids.add(post["id"])
                posts.append(post)

    log.info(f"collect_trending_posts: {len(posts)} posts found")
    return posts[:limit]


def extract_trend_mechanism(posts: list[dict]) -> str | None:
    """Analyse top posts and return the dominant engagement mechanism."""
    if not posts:
        return None

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    posts_text = "\n\n".join(f"{p['text']}" for p in posts[:15])

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": (
                f"Ось популярні пости з Threads сьогодні:\n\n{posts_text}\n\n"
                "Визнач один психологічний механізм який найчастіше зустрічається в цих постах "
                "(впізнаваність, гумор, провокація, несподіваний кут, особиста історія тощо).\n"
                "Відповідай одним реченням: що саме робить ці пости популярними. "
                "Без заголовків, без markdown."
            ),
        }],
    )
    return response.content[0].text.strip()
