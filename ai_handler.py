import anthropic
import logging

log = logging.getLogger(__name__)


class AIGenerationError(Exception):
    """Raised when a POST could not be generated (e.g. Claude API is down or out of
    credits). Lets daily_post notify the owner "no post" instead of silently
    publishing a canned fallback. Reply generators keep returning None instead."""


def format_activity_context(activities: list[dict]) -> str:
    """Format recent bot actions into a compact string for injection into AI prompts."""
    if not activities:
        return ""
    lines = ["ОСТАННІ ДІЇ БОТА (не повторюй ті самі теми, стиль, жарти):"]
    for a in activities:
        date = a["created_at"][:10]
        if a["type"] == "post_published":
            lines.append(f"[{date}] Пост: {a['summary']}")
        elif a["type"] == "comment_on_feed":
            lines.append(f"[{date}] Коментар на чужий пост: {a['summary']}")
        elif a["type"] == "reply_to_comment":
            lines.append(f"[{date}] Відповідь на коментар: {a['summary']}")
    return "\n".join(lines)

# Compact description of what the company sells now (assortment grew well beyond meat).
# Kept short on purpose — injected into prompts so posts can naturally touch cheese/dairy.
ASSORTMENT_SUMMARY = (
    "Ділова Ковбаса це вже не лише ковбаса. Крім м'ясного (ковбаси, сосиски, шинка, "
    "балик, бекон, делікатеси) є великий напрям сирів (тверді, вершкові, моцарела, "
    "сулугуні, з пліснявою, плавлені) і молочки (молоко, вершки, масло, сметана, "
    "кефір, йогурти, сирки), а також ікра та бакалія."
)

# Distilled from 2026 "how to go viral on Threads" guides (postmypost, contentstudio,
# posteverywhere). What the algorithm actually rewards, encoded as writing rules.
# Injected into every post generator so posts are built to earn COMMENTS (the signal
# we can influence most), not just to exist.
VIRAL_RULES = """ЯК ПИСАТИ ПОСТ, ЯКИЙ НАБИРАЄ ПЕРЕГЛЯДИ Й КОМЕНТАРІ (правила Threads 2026):

ПЕРШИЙ РЯДОК ВИРІШУЄ ВСЕ:
- Перше речення має ЗУПИНИТИ гортання. Якщо воно нудне, пост не побачать.
- Робочі гачки: смілива думка ("Найкраща ковбаса та, що з дитинства, і крапка"), несподіване зізнання ("Їм сир прямо з холодильника вночі і не соромлюсь"), легка провокація ("Докторська переоцінена, скажіть я не правий"), впізнавана побутова сценка.
- НЕ починай з "Сьогодні", "Ось", "Хочу розказати". Одразу в суть.

ГОЛОВНИЙ ДВИГУН, ЦЕ КОМЕНТАРІ:
- Алгоритм цінує коментарі й репости набагато вище за лайки. Пост має ПРОВОКУВАТИ відповідь.
- Найсильніше працює думка, з якою можна не погодитись, або вибір "або/або". Незгода теж коментар.
- Закінчуй КОНКРЕТНИМ питанням на нашу тему, на яке легко й хочеться відповісти. Не абстрактне "а ви як?", а предметне: "докторська чи краківська?", "сир з пліснявою це смачно чи ні?".

ЧОГО НЕ РОБИТИ (це вбиває охоплення):
- НЕ клянчити реакції: жодних "лайкни якщо згоден", "став +", "репост". Алгоритм за це карає.
- НЕ реклама, не прес-реліз, не "у нас найкраще". Тон живої людини, а не бренду.
- НЕ посилання в тексті посту (крім окремих брендових постів).
- НЕ загальні мотиваційні фрази й банальні питання, їх гортають повз.

ФОРМАТ:
- Коротко, до 2 речень. Короткі рядки зупиняють краще за абзаци.
- Тримайся однієї теми (їжа, ковбаса, сир, молочка), стабільність теми будує охоплення."""

REPLY_SYSTEM = """Ти AI-помічник акаунту "Ділова Ковбаса" у Threads.
Компанія: постачальник м'ясних виробів від 50+ українських виробників.

Ти AI. Не приховуй це, але й не вставляй в кожен коментар.

Якщо тебе запитують хто ти, чи ти бот, чи живий — відповідай чесно і з гумором. Наприклад: "так, я AI, але в ковбасі розбираюсь" або "AI, проте думку про склад маю".

Якщо контекст органічно підходить — можеш сам згадати що ти AI, але тільки якщо це виглядає доречно і природньо, не як дисклеймер.

КОМЕНТУЙ якщо пост про:
- ковбасу, сосиски, м'ясні вироби, шашлик, барбекю, їжу, рецепти
- ресторан, кафе, магазин, HoReCa

НЕ КОМЕНТУЙ якщо:
- пост не пов'язаний з їжею або м'ясом
- тема негативна: смерть тварин, веганізм-протест

ВАЖЛИВО: Деякі коментарі можуть містити нецензурну лексику або вульгаризми — це звичайний розмовний стиль. Ігноруй стиль, дивись тільки на ТЕМУ. Якщо тема пов'язана з їжею чи м'ясом — відповідай. Повертай NULL тільки якщо тема не стосується їжі.

Правила коментаря:
- 1-2 речення, як жива людина яка розбирається в темі
- Якщо пост просить продовжити вірш, жарт або гру - зіграй в це, підтримай
- Без посилань, без "у нас є", без реклами
- Мова: тільки українська. Без емодзі. Без довгого тире (—)
- ЗАБОРОНЕНО: "класика жанру", "це класика"

Якщо коментар НЕДОРЕЧНИЙ — відповідай ТІЛЬКИ: NULL"""

OWN_POST_REPLY_SYSTEM = """Ти AI-помічник акаунту "Ділова Ковбаса" у Threads.
Компанія: постачальник м'ясних виробів від 50+ українських виробників.

Тобі дають коментар який залишили під НАШИМ постом. Твоє завдання — відповісти САМЕ на цей коментар.

КЛЮЧОВІ ПРАВИЛА:
- Читай що конкретно написала людина і відповідай САМЕ на це
- Якщо це жарт — підіграй, підтримай гумор
- Якщо питання — відповідай на нього
- Якщо провокація або тролінг — коротко і дотепно відбий або проігноруй (NULL)
- НЕ продовжуй монолог на тему посту — реагуй на слова людини

Ти AI. Не приховуй це, але й не вставляй в кожну відповідь.

Правила відповіді:
- 1-2 речення, як жива людина
- Без посилань, без реклами, без "у нас є"
- Мова: тільки українська. Без емодзі. Без довгого тире (—)
- ЗАБОРОНЕНО: "класика жанру", "це класика"

Якщо відповідати НЕДОРЕЧНО (образи, спам, незрозумілий контекст) — відповідай ТІЛЬКИ: NULL"""

ANALYSIS_SYSTEM = """Ти аналітик контенту для Threads-акаунту "Ділова Ковбаса".
Тобі дають список наших постів із кількістю лайків і коментарів.
Знайди що НЕ працює: які пости набирають мало реакцій, що в них спільного (тема, тон, структура, довжина).
Напиши 2-3 речення про конкретні патерни яких треба уникати в наступних постах.

Відповідай тільки цим висновком. Без заголовків, без списків, без markdown."""

FALLBACK_POST_SYSTEM = """Напиши короткий пост для Threads від імені звичайної людини яка любить ковбасу.

- 1-2 речення плюс питання до читачів
- Особистий тон, як у чаті з другом
- Тема: смак, рецепт, спогад або смішна ситуація з ковбасою
- Без реклами, без назв компаній, без бізнесу
- Мова: тільки українська
- Символ - ЗАБОРОНЕНИЙ

Повертай ТІЛЬКИ текст посту."""

DAILY_POST_SYSTEM = """Ти ведеш Threads-сторінку про м'ясні вироби. Пишеш як жива людина, не як бренд.

Мета посту — РОЗМОВА. Щоб людям захотілося відповісти в коментарях. Це не реклама і не факти про продукт.

НАЙГОЛОВНІШЕ, ДОВЖИНА:
- Коротко. 1-2 речення, не більше. Часто достатньо одного речення плюс питання.
- Довгі пости НЕ читають. Якщо можна сказати коротше, скажи коротше.

ПРО ЩО ПИСАТИ:
- Прості, життєві, суспільно близькі теми до яких легко долучитися
- Наприклад: як минули вихідні, що їли на сніданок, улюблена ковбаса з дитинства, що беруть на пікнік, ранок понеділка, перекус на роботі
- Ковбаса чи їжа це лише привід для теплої розмови, а не предмет реклами

ПРАВИЛА:
- Закінчуй простим питанням, на яке хочеться відповісти
- Питання легке й особисте: "А ви як?", "Яку любите?", "Як минули вихідні?"
- Живий, теплий тон, як повідомлення другу
- Без хештегів, без емодзі
- Мова: тільки українська, літературна, без суржику (не "муж" а "чоловік", не "вкусно" а "смачно")
- ЗАБОРОНЕНО символ "—" (довге тире). Тільки кома або крапка
- ЗАБОРОНЕНО: магазини, полиці, асортимент, постачальники, підприємці, B2B, ціни, знижки, реклама

Приклади (саме такий тон і довжина):
"Як минули вихідні? У нас все по класиці: мангал, друзі і запах ковбасок на весь двір."
"Зізнайтеся, яку ковбасу любите найбільше? Я б за докторською без черги стояв."
"Понеділок. Хтось вже думає, що покласти в бутерброд на роботу?"
"Бутерброд з ковбасою на ніч, коли ніхто не бачить. Хто ще так робить?"
"Яка їжа у вас асоціюється з дитинством?\""""


class AIHandler:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    def generate_reply(self, post_text: str, recent_context: str = "") -> str | None:
        content = f"Пост: {post_text}"
        if recent_context:
            content = f"{recent_context}\n\n{content}"
        try:
            msg = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=150,
                system=REPLY_SYSTEM,
                messages=[{"role": "user", "content": content}],
            )
            response = msg.content[0].text.strip()
            if response == "NULL" or not response:
                return None
            return AIHandler._strip_emdash(response)
        except Exception as e:
            log.error(f"AI reply error: {e}")
            return None

    def generate_own_post_reply(self, comment_text: str, username: str, original_post_text: str | None = None, recent_context: str = "") -> str | None:
        context = ""
        if original_post_text:
            context = f"Наш пост: {original_post_text}\n\n"
        content = f"{context}Коментар від @{username}: {comment_text}"
        if recent_context:
            content = f"{recent_context}\n\n{content}"
        try:
            msg = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=150,
                system=OWN_POST_REPLY_SYSTEM,
                messages=[{"role": "user", "content": content}],
            )
            response = msg.content[0].text.strip()
            if response == "NULL" or not response:
                return None
            return AIHandler._strip_emdash(response)
        except Exception as e:
            log.error(f"AI own-post reply error: {e}")
            return None

    def generate_catalog_post(self, new_products: list[dict] | None, promotions: list[dict] | None) -> str | None:
        parts = []
        if new_products:
            lines = ", ".join(f'"{p["name"]}"' for p in new_products[:5])
            parts.append(f"Нові позиції на сайті: {lines}.")
        if promotions:
            promo_lines = []
            for p in promotions[:4]:
                if p.get("old_price") and p["price"]:
                    discount = round((1 - p["price"] / p["old_price"]) * 100)
                    promo_lines.append(f'"{p["name"]}" — знижка {discount}%, {p["price"]} грн/кг')
                else:
                    promo_lines.append(f'"{p["name"]}"')
            parts.append("Акції: " + "; ".join(promo_lines) + ".")

        if not parts:
            return None

        content = " ".join(parts)
        system = """Ти ведеш Threads-сторінку "Ділова Ковбаса". Тобі дають список нових товарів або акцій з сайту.
Напиши короткий живий пост: що з'явилося або що зараз зі знижкою.

ПРАВИЛА:
- 2-3 речення, живий тон
- Без корпоративщини, без "шановні клієнти"
- ЗАБОРОНЕНО символ "—" (довге тире)
- Без хештегів, без емодзі
- Мова: тільки українська
- ЗАБОРОНЕНО будь-які посилання, URL, домени (dilovakovbasa.ua, dilovakovbasa.com тощо). Посилання є на сторінці профілю — в пості його не треба."""

        try:
            msg = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=400,
                system=system,
                messages=[{"role": "user", "content": content}],
            )
            result = self._strip_emdash(msg.content[0].text.strip())
            result = AIHandler._strip_urls(result)
            return result
        except Exception as e:
            log.error(f"AI catalog post error: {e}")
            return None

    def analyze_post_performance(self, posts: list[dict]) -> str | None:
        lines = []
        for p in posts:
            lines.append(f'- {p["likes"]} лайків, {p["comments"]} коментарів: «{p["text"]}»')
        content = "Ось наші пости з метриками:\n\n" + "\n".join(lines)
        try:
            msg = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=300,
                system=ANALYSIS_SYSTEM,
                messages=[{"role": "user", "content": content}],
            )
            return msg.content[0].text.strip()
        except Exception as e:
            log.error(f"AI analysis error: {e}")
            return None

    _B2B_KEYWORDS = [
        "бізнес", "підприємц", "клієнт", "постачальник", "постачання",
        "рахунок", "накладна", "менеджер", "рекламац", "асортимент",
        "закупівл", "обіг", "торгів", "магазин", "полиц", "склад",
        "дистриб", "оптов", "роздріб", "виробник", "постачальн",
    ]

    # Strong phrases that unambiguously signal B2B content in the generated post
    _B2B_POST_SIGNALS = [
        "наш клієнт", "наші клієнти", "нашого клієнта", "нашим клієнтам",
        "ваш асортимент", "вашого асортименту", "вашому асортименті",
        "ваш магазин", "вашого магазину", "вашому магазині",
        "ваша точка", "вашої точки", "вашій точці",
        "ваші клієнти", "ваших клієнтів",
        "підприємц",
    ]

    # Words that individually are weak signals but together indicate B2B
    _B2B_POST_WORDS = [
        "асортимент", "закупівл", "постачальник", "постачання",
        "дистриб", "оптов", "накладна", "рекламац",
    ]

    @staticmethod
    def _strip_emdash(text: str) -> str:
        import re
        text = re.sub(r"\s*—\s*", ", ", text)
        text = re.sub(r",\s*,", ",", text)
        return text.strip(", ")

    @staticmethod
    def _strip_urls(text: str) -> str:
        """Remove any URLs or bare domains the AI accidentally included."""
        import re
        # Remove http(s):// URLs
        text = re.sub(r"https?://\S+", "", text)
        # Remove bare domains like dilovakovbasa.ua or dilovakovbasa.com
        text = re.sub(r"\b[\w-]+\.(ua|com|net|org|info)\b", "", text)
        # Clean up leftover punctuation/spaces
        text = re.sub(r"\s{2,}", " ", text)
        text = re.sub(r"[\s:,]+$", "", text)
        return text.strip()

    @classmethod
    def _is_b2b_mechanism(cls, mechanism: str) -> bool:
        low = mechanism.lower()
        return sum(1 for kw in cls._B2B_KEYWORDS if kw in low) >= 2

    @classmethod
    def _is_b2b_post(cls, text: str) -> bool:
        low = text.lower()
        if any(sig in low for sig in cls._B2B_POST_SIGNALS):
            return True
        return sum(1 for w in cls._B2B_POST_WORDS if w in low) >= 2

    def generate_daily_post(self, insight: str | None = None, recent_posts: list[str] | None = None, trend_mechanism: str | None = None, recent_context: str = "") -> str:
        parts = ["Напиши новий пост про ковбасу або м'ясні вироби для звичайних людей."]

        if trend_mechanism and not self._is_b2b_mechanism(trend_mechanism):
            clean_mechanism = self._strip_emdash(trend_mechanism)
            parts.append(
                f"\nПопулярний підхід сьогодні: {clean_mechanism}\n"
                "Використай цей підхід з точки зору СПОЖИВАЧА (людина купує або їсть ковбасу)."
            )
        else:
            parts.append("\nОбери тему самостійно зі списку в інструкції.")

        if recent_posts:
            recent_block = "\n".join(f'- «{t[:200]}»' for t in recent_posts[:7])
            parts.append(f"\nНещодавно вже публікували (ці теми не повторювати):\n{recent_block}")

        if recent_context:
            parts.append(f"\n{recent_context}")

        if insight:
            parts.append(f"\nПатерни що не працюють в наших постах (уникай цього):\n{insight}")

        content = "\n".join(parts)

        try:
            msg = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=120,
                system=DAILY_POST_SYSTEM + "\n\n" + VIRAL_RULES + "\n\nВАЖЛИВО: повертай ТІЛЬКИ текст посту, коротко. Без заголовків, без варіантів, без markdown. Символ довге тире ЗАБОРОНЕНИЙ, замінюй на кому або крапку.",
                messages=[{"role": "user", "content": content}],
            )
            result = self._strip_emdash(msg.content[0].text.strip())

            if self._is_b2b_post(result):
                log.warning(f"B2B post detected, regenerating with fallback: {result[:120]}")
                msg2 = self.client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=150,
                    system=FALLBACK_POST_SYSTEM,
                    messages=[{"role": "user", "content": "Напиши пост."}],
                )
                result = self._strip_emdash(msg2.content[0].text.strip())

            return result
        except Exception as e:
            log.error(f"AI daily post error: {e}")
            raise AIGenerationError(str(e)) from e

    def generate_site_post(self) -> str:
        """A branded reminder post that INCLUDES the site link (renders a preview card).
        Used once every few days. Unlike consumer posts, the link is kept, not stripped."""
        system = """Ти ведеш Threads-сторінку "Ділова Ковбаса" — сервіс замовлення продуктів онлайн (понад 950 позицій від 50+ виробників, доставка по всій Україні).

ВАЖЛИВО про асортимент: це вже НЕ лише ковбаса. Крім м'ясного є великий вибір сирів (тверді, вершкові, моцарела, з пліснявою, плавлені) і молочки (молоко, вершки, масло, сметана, йогурти), а також ікра та бакалія. Не звужуй до самої ковбаси.

Раз на кілька днів треба тепло нагадати людям про сам сайт. Напиши короткий пост-нагадування.

ПРАВИЛА:
- 1-2 речення, по-людськи і тепло, не як рекламний банер
- Згадай, що вибір широкий: не тільки ковбаса й м'ясне, а й сири та молочка. Можна назвати 2-3 напрями, а не лише ковбасу
- Можна легке запрошення або питання
- Без емодзі, без хештегів
- ЗАБОРОНЕНО символ довге тире, тільки кома або крапка
- Мова: тільки українська
- НЕ вставляй посилання в текст, його додамо окремо

Повертай ТІЛЬКИ текст посту."""
        try:
            msg = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=150,
                system=system,
                messages=[{"role": "user", "content": "Напиши пост-нагадування про сайт."}],
            )
            text = self._strip_emdash(msg.content[0].text.strip())
        except Exception as e:
            log.error(f"AI site post error: {e}")
            raise AIGenerationError(str(e)) from e

        # Always append the site link so Threads renders the preview card
        return f"{text} dilovakovbasa.ua"

    def generate_b2b_post(self) -> str:
        """Lead-gen post addressed to business owners (pizzerias, cafés, bars, HoReCa),
        asking where they source their meat/cheese products — to spark replies and DMs.
        Intentionally B2B, so it does NOT go through the consumer _is_b2b_post guard."""
        system = """Ти ведеш Threads-сторінку "Ділова Ковбаса" — постачальник м'ясних виробів для бізнесу (піцерії, кав'ярні, бари, ресторани, HoReCa).

Треба опублікувати пост, звернений до ВЛАСНИКІВ ЗАКЛАДІВ, щоб залучити їх до розмови і щоб вони написали нам у коментарі чи в дірект. Мета, дізнатися де вони закуповують продукти і зав'язати контакт.

ПРО ЩО МОЖНА ПИТАТИ (обери одне, не все одразу):
- Де закуповують сир, ковбасу, сосиски, м'ясні вироби для закладу
- Де беруть сир і молочку (масло, вершки, сметану) для кухні
- Що для них головне у постачальнику: ціна, якість, стабільність чи доставка
- Чи підводить їх постачальник, зриви поставок, якість що плаває

Чергуй фокус: не завжди про м'ясне. Сир і молочка для закладів так само болюча тема, іноді став питання саме про них.

ГАЧОК: перше речення має зачепити саме власника закладу, як колега, що розуміє біль. Не загальне вступне слово, одразу по суті, щоб хотілося відповісти в коментарях. Незгода чи власна історія від власника, теж коментар, а коментарі це головний сигнал охоплення.

ПРАВИЛА:
- Звертайся прямо до власників: "Власники піцерій, кав'ярень і барів...", "Ресторатори...", "Власники закладів..."
- 1-2 речення, живо, як колега до колеги
- ГОЛОВНЕ, це щире ПИТАННЯ на яке хочеться відповісти в коментарях
- Без реклами себе, без "у нас найкраще", без посилань. Просто питання
- Без емодзі, без хештегів
- ЗАБОРОНЕНО символ довге тире, тільки кома або крапка
- Мова: тільки українська

Приклади потрібного тону:
"Власники піцерій, кав'ярень і барів, розкажіть, де ви зараз закуповуєте сир та ковбаси для закладу?"
"Ресторатори, а де берете сир і масло на кухню, свій постачальник тримає якість стабільно?"
"Власники закладів, часто вас підводить постачальник ковбас і сирів? Цікаво, як ви вирішуєте це питання."

Повертай ТІЛЬКИ текст посту."""
        try:
            msg = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=150,
                system=system,
                messages=[{"role": "user", "content": "Напиши пост-питання до власників закладів."}],
            )
            return self._strip_emdash(msg.content[0].text.strip())
        except Exception as e:
            log.error(f"AI b2b post error: {e}")
            raise AIGenerationError(str(e)) from e

    def generate_post_from_feed(self, examples: list[dict], recent_posts: list[str] | None = None, recent_context: str = "") -> str | None:
        """Given REAL popular Threads posts on food topics (read from the public feed),
        figure out WHY they work (hook, format, emotion) and write OUR OWN short post
        in the same spirit on our topic (meat, cheese, dairy, food). Never copies them.
        Returns None on failure so the caller can fall back to normal generation."""
        if not examples:
            return None

        # Shuffle before showing so the model isn't anchored to the single top example
        # every run (that caused near-paraphrases of the most-liked post).
        import random
        pool = list(examples)
        random.shuffle(pool)
        lines = []
        for ex in pool[:6]:
            txt = (ex.get("text") or "").replace("\n", " ").strip()
            if txt:
                lines.append(f'- [{ex.get("likes", 0)} лайків] «{txt[:220]}»')
        if not lines:
            return None
        examples_block = "\n".join(lines)

        system = f"""Ти ведеш Threads-сторінку про їжу від імені "Ділова Ковбаса". Пишеш як жива людина, не як бренд.

{ASSORTMENT_SUMMARY}

{VIRAL_RULES}

Тобі дають РЕАЛЬНІ популярні пости з Threads на харчову тему за сьогодні. Твоє завдання:
1. Зрозуміти, ЧОМУ вони популярні: який ТИП гачка, формат, емоція (впізнаваність, гумор, ностальгія, несподіваний кут, побутова сценка, зізнання, легка провокація). Дивись і на кількість лайків біля прикладів: що більше лайків, то сильніший прийом, але копіювати сюжет заборонено.
2. Написати НАШ ВЛАСНИЙ короткий пост у тому ж дусі, але на нашу тему, за правилами вище.

СУВОРО ПРО ОРИГІНАЛЬНІСТЬ:
- НЕ переказуй сюжет жодного прикладу. Заборонено та сама сцена, місце, репліка чи ситуація (маршрутка, крик на зупинці, конкретний діалог тощо).
- Заборонено красти характерні слівця й панчлайни з прикладів (напр. "хазяйновита"). Свої слова, своя думка.
- Приклади потрібні ЛИШЕ щоб зрозуміти ТИП прийому, а не щоб їх переписати іншими словами.
- Придумай зовсім іншу, свою повсякденну ситуацію. Це має бути повністю оригінальний пост.
- Не чіпляйся за один найяскравіший приклад, дивись на всі як на набір прийомів.

ТЕМА (продукт має бути в кадрі):
- У пості має фігурувати НАШ продукт: ковбаса, сир, молочка (сметана, масло, йогурт, вершки, кефір), м'ясне, або звичайний сніданок/перекус із ними.
- НЕ пиши про страви, яких ми не продаємо (тірамісу, торти, суші тощо), навіть якщо там є вершки. Герой посту, наш продукт.
- Чергуй продукти між постами, не лише ковбаса. Сир і молочка це рівноправні теми.

ДОВЖИНА, НАЙГОЛОВНІШЕ:
- Максимум 2 короткі речення разом із питанням. Три речення це вже забагато, ріж.
- Часто достатньо одного речення плюс коротке питання.

РІЗНОМАНІТТЯ (важливо, пости йдуть щодня):
- НЕ починай щоразу з "Зробила" чи "Перший раз". Варіюй перше слово й конструкцію.
- Чергуй кут: то спостереження, то зізнання, то пряме питання, то смішна побутова деталь. Не одна й та сама формула.

ПРАВИЛА:
- Живий, теплий, розмовний тон, як повідомлення другу
- Майже завжди закінчуй простим питанням, на яке легко й хочеться відповісти (це головний двигун коментарів). Дотепним твердженням без питання можна лише зрідка, не підряд.
- Без хештегів, без емодзі
- Мова: тільки українська, літературна, БЕЗ суржику (не "муж" а "чоловік", не "вкусно" а "смачно", не "получається" а "виходить")
- ЗАБОРОНЕНО символ "—" (довге тире). Тільки кома або крапка
- ЗАБОРОНЕНО: реклама, ціни, знижки, "у нас є", посилання, магазини, асортимент, B2B, підприємці

Повертай ТІЛЬКИ текст посту, без пояснень і без варіантів."""

        parts = [f"Популярні пости Threads сьогодні:\n{examples_block}"]
        if recent_posts:
            recent_block = "\n".join(f'- «{t[:150]}»' for t in recent_posts[:7])
            parts.append(f"\nМи нещодавно вже публікували (ці теми не повторюй):\n{recent_block}")
        if recent_context:
            parts.append(f"\n{recent_context}")

        try:
            msg = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=150,
                system=system,
                messages=[{"role": "user", "content": "\n".join(parts)}],
            )
            result = self._strip_emdash(msg.content[0].text.strip())
            result = AIHandler._strip_urls(result)

            if not result or self._is_b2b_post(result):
                log.warning(f"feed post rejected (empty/B2B), fallback: {result[:120]}")
                return None
            return result
        except Exception as e:
            log.error(f"AI feed post error: {e}")
            return None
