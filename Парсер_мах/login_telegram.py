"""Отдельный вход в Telegram Web — по аналогии с login_max.py.

Сессия сохраняется в .data/telegram-profile и переиспользуется
mega_parser.py при последующих запусках без повторного входа.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from news_runtime.social_browser import AuthenticationRequired, SocialBrowser

ROOT = Path(__file__).resolve().parent


def main() -> int:
    try:
        with SocialBrowser("telegram", ROOT / ".data" / "telegram-profile", headless=False) as browser:
            browser.login()
    except AuthenticationRequired as exc:
        print(f"Вход не подтверждён: {exc}")
        return 1
    print("Вход в Telegram подтверждён. Сессия сохранена для следующих запусков.")
    return 0


if __name__ == "__main__":
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".playwright-browsers")
    )
    sys.exit(main())
