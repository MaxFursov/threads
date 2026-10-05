"""Convert a cookie export (Cookie-Editor JSON) into threads_session.json.

Use this when logging in inside an automated/fresh browser is rejected. You are
already logged in to Threads in your everyday browser: export its cookies with
the "Cookie-Editor" extension, save them as cookies.json next to this file, and
run:

    python3 import_cookies.py

It writes threads_session.json (Playwright storage_state) for feed_replier.py.
Nothing is sent anywhere. Both cookies.json and threads_session.json are
equivalent to account access: keep them private (they are in .gitignore) and
delete cookies.json once the conversion is done.
"""

import json
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "cookies.json"
OUT = "threads_session.json"

SAMESITE = {
    "no_restriction": "None",
    "none": "None",
    "lax": "Lax",
    "strict": "Strict",
    "unspecified": "Lax",
}


def convert(c: dict) -> dict | None:
    name, value, domain = c.get("name"), c.get("value"), c.get("domain")
    if not name or value is None or not domain:
        return None
    exp = c.get("expirationDate", c.get("expires", -1))
    try:
        exp = float(exp)
    except Exception:
        exp = -1
    if c.get("session"):
        exp = -1
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": c.get("path") or "/",
        "expires": exp,
        "httpOnly": bool(c.get("httpOnly", False)),
        "secure": bool(c.get("secure", True)),
        "sameSite": SAMESITE.get(str(c.get("sameSite", "lax")).lower(), "Lax"),
    }


def main():
    try:
        with open(SRC, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Не знайшов файл {SRC}. Збережіть експорт cookies у цю папку як {SRC}.")
        sys.exit(1)
    except Exception as e:
        print(f"Не вдалося прочитати {SRC}: {e}")
        sys.exit(1)

    if isinstance(data, dict):
        data = data.get("cookies", [])
    cookies = [x for x in (convert(c) for c in data) if x]
    if not cookies:
        print("У файлі немає cookies. Перевірте, що експортували JSON саме зі сторінки Threads.")
        sys.exit(1)

    names = {c["name"] for c in cookies}
    if "sessionid" not in names:
        print("Увага: у файлі немає cookie 'sessionid', тобто експорт зроблено НЕ із залогіненої сторінки.")
        print("Відкрийте threads.com, переконайтесь, що бачите свою стрічку, і експортуйте знову.")
        sys.exit(1)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"cookies": cookies, "origins": []}, f, ensure_ascii=False)
    print(f"Готово: {len(cookies)} cookies збережено у {OUT}.")
    print(f"Тепер видаліть {SRC} (це копія доступу до акаунту) і напишіть Claude, що файл готовий.")


if __name__ == "__main__":
    main()
