"""One-time helper: capture a logged-in Threads session for feed_replier.py.

Run this ON YOUR OWN COMPUTER.

How it works (and why):
  1. It starts your installed Google Chrome as a NORMAL app (not driven by any
     automation) with its own dedicated profile in ~/.threads-bot-profile,
     separate from your everyday Chrome.
  2. YOU log in to Threads by hand in that window (you type your own password,
     nothing here touches it). Because Chrome is not being controlled while you
     log in, Instagram/Threads treats it like an ordinary browser.
  3. When your feed is visible, you press Enter here. Only then does the script
     attach to that window and save the session cookies to threads_session.json.

Why a separate profile and not your everyday Chrome: Chrome blocks remote
control of its default profile, and reusing the SAME session on your Mac and on
the Railway server at once looks like account sharing and gets accounts flagged.
The profile remembers the login, so next time you usually won't sign in again.

Then give that file's contents to the bot:
    - locally: keep threads_session.json next to the code, or
    - on Railway: set env THREADS_SESSION_JSON to the file's full contents.

Usage:
    python3 capture_session.py
"""

import asyncio
import os
import subprocess
import sys
import time
import urllib.request

from playwright.async_api import async_playwright

OUT = "threads_session.json"
PROFILE_DIR = os.path.expanduser("~/.threads-bot-profile")
PORT = 9222
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _cdp_ready() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
        return True
    except Exception:
        return False


async def main():
    if not os.path.exists(CHROME):
        print("Не знайшов Google Chrome у /Applications. Встановіть його й повторіть.")
        sys.exit(1)

    if _cdp_ready():
        print(f"Порт {PORT} уже зайнятий (схоже, Chrome від попереднього запуску відкритий).")
        print("Закрийте те вікно Chrome (Cmd+Q у ньому) і запустіть скрипт знову.")
        sys.exit(1)

    proc = subprocess.Popen(
        [
            CHROME,
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "https://www.threads.net/",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for _ in range(30):
        if _cdp_ready():
            break
        time.sleep(1)
    else:
        print("Chrome не запустився вчасно. Спробуйте ще раз.")
        proc.terminate()
        sys.exit(1)

    print("\n" + "=" * 64)
    print("1. У вікні Chrome, що відкрилось, увійдіть у Threads як")
    print("   @dilovakovbasa.ua (вхід через Instagram теж підходить).")
    print("   Логін і пароль вводьте САМІ, тут їх ніхто не зберігає.")
    print("2. Дочекайтесь, поки завантажиться ваша стрічка.")
    print("3. Поверніться сюди й натисніть Enter, щоб зберегти сесію.")
    print("=" * 64 + "\n")
    input("Натисніть Enter, коли ви вже увійшли... ")

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        context = browser.contexts[0]
        await context.storage_state(path=OUT)
        await browser.close()

    proc.terminate()
    print(f"\nГотово. Сесію збережено у файл: {OUT}")
    print("Напишіть Claude, що файл готовий, він додасть його на Railway.\n")


if __name__ == "__main__":
    asyncio.run(main())
