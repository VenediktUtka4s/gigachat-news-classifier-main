from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import sync_playwright

from max_news.auth import ensure_max_ready

ROOT = Path(__file__).resolve().parent


def main() -> None:
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=ROOT / ".data" / "max-profile",
            headless=False,
            viewport={"width": 1440, "height": 1000},
            locale="ru-RU",
            timezone_id="Europe/Moscow",
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            ensure_max_ready(page)
            print("Вход в MAX подтверждён. Сессия сохранена для следующих запусков.")
        finally:
            context.close()


if __name__ == "__main__":
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".playwright-browsers")
    )
    main()
