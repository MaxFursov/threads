# Threds — Threads бот «Ділова Ковбаса»

Автоматизація Threads-акаунту для B2B постачальника м'ясних виробів.

## Що робить

| Час | Задача |
|-----|--------|
| 09:00 | `daily_post` — публікує новий пост (з каталогу або AI) |
| 8,10,12,14,16,18,20 | `scan_own_post_comments` — відповідає на коментарі під постами |
| 22:00 | `collect_post_metrics` — збирає статистику постів |

## Файли

| Файл | Що робить |
|------|-----------|
| `main.py` | Scheduler + всі job-функції |
| `ai_handler.py` | Claude API: генерація постів і відповідей |
| `threads_client.py` | Threads Graph API |
| `catalog_fetcher.py` | Парсить promotions/new_products з сайту |
| `feed_reader.py` | Читає Threads-стрічку для аналізу трендів |
| `analyze_trends.py` | Аналізує тренди для кращих постів |
| `database.py` | SQLite: published_posts, pending_posts |
| `notifier.py` | → Telegram DK_Logs бот |

## Деплой

- **Railway:** проєкт `genuine-forgiveness`, сервіс `threads`
- **Service ID:** `79ed2955-1725-486f-8178-b56a9b86dc70`
- **GitHub:** github.com/MaxFursov/threads
- **Volume:** `threads-volume` → `/app/data` (SQLite там)
- **Region:** US West

## ENV

```
THREADS_ACCESS_TOKEN
THREADS_USER_ID
ANTHROPIC_API_KEY
TG_BOT_TOKEN
TG_NOTIFY_CHAT_ID
```

## Команди

```bash
railway logs --tail 30
railway up --detach
python main.py
```
