"""Wait for the MAX web app, allowing manual login in the persistent profile."""
from __future__ import annotations

import time as time_module

from playwright.sync_api import Error as PlaywrightError, Page


def is_login_page(page: Page, body: str) -> bool:
    markers = (
        "Войти в MAX",
        "Введите номер телефона",
        "Войти по номеру",
        "QR-код",
        "Отсканируйте",
    )
    return any(marker in body for marker in markers) or "login" in page.url.lower()


def ensure_max_ready(page: Page, *, headless: bool = False) -> None:
    try:
        page.goto(
            "https://web.max.ru/",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
    except PlaywrightError:
        if page.is_closed():
            raise

    deadline = time_module.monotonic() + 300
    last_reload = time_module.monotonic()
    login_announced = False
    login_finished_announced = False
    while time_module.monotonic() < deadline:
        search = page.get_by_placeholder("Найти").first
        if search.count() and search.is_visible():
            return

        body = page.locator("body").inner_text(timeout=5_000)
        login_page = is_login_page(page, body)
        if login_page:
            if headless:
                raise RuntimeError(
                    "профиль MAX требует повторного входа; "
                    "запустите без --headless"
                )
            if not login_announced:
                print(
                    "  требуется вход в MAX: авторизуйтесь по QR-коду "
                    "в открытом Chrome; ожидание до 5 минут"
                )
                login_announced = True
            page.wait_for_timeout(1_000)
            continue

        if login_announced and not login_finished_announced:
            print("  вход подтверждён, ожидаю загрузку главной страницы MAX…")
            login_finished_announced = True

        if time_module.monotonic() - last_reload >= 10:
            try:
                page.reload(wait_until="domcontentloaded", timeout=60_000)
            except PlaywrightError:
                if page.is_closed():
                    raise
            last_reload = time_module.monotonic()
        page.wait_for_timeout(1_000)

    raise RuntimeError(
        "главная страница MAX не загрузилась за 5 минут; повторите запуск"
    )
