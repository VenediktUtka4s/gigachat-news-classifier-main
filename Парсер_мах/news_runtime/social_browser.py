"""Saved-browser-session collection for VK and Telegram public channel pages.

The selectors deliberately live in one module because both Web VK and Telegram
Web K change their markup independently.  A selector miss is an error, never a
successful empty collection.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, Playwright, sync_playwright

from max_news.models import Post
from max_news.text import first_sentence

MOSCOW = "Europe/Moscow"


class SocialBrowserError(RuntimeError):
    pass


class AuthenticationRequired(SocialBrowserError):
    pass


def telegram_web_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
        raise SocialBrowserError("Ожидалась ссылка Telegram t.me")
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        raise SocialBrowserError("В ссылке Telegram не указан канал")
    if parts[0] == "c" and len(parts) >= 2 and parts[1].isdigit():
        return f"https://web.telegram.org/k/#-100{parts[1]}"
    if parts[0].startswith("+") or parts[0] in {"joinchat", "share"}:
        raise SocialBrowserError("Пригласительные и служебные ссылки Telegram не поддерживаются")
    return f"https://web.telegram.org/k/#@{parts[0].lstrip('@')}"


def _moscow_date(timestamp: str) -> date | None:
    try:
        return datetime.fromtimestamp(int(timestamp), tz=datetime.now().astimezone().tzinfo).astimezone(
            __import__("zoneinfo").ZoneInfo(MOSCOW)
        ).date()
    except (TypeError, ValueError, OverflowError):
        return None


class SocialBrowser(AbstractContextManager["SocialBrowser"]):
    def __init__(self, platform: str, profile_dir: Path, *, headless: bool = True,
                 max_scrolls: int = 200, logger=None) -> None:
        if platform not in {"vk", "telegram"}:
            raise ValueError("platform должен быть 'vk' или 'telegram'")
        if max_scrolls < 1:
            raise ValueError("max_scrolls должен быть положительным")
        self.platform = platform
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.max_scrolls = max_scrolls
        self.logger = logger
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> "SocialBrowser":
        self._ensure_browser()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        if self._context:
            self._context.close()
        if self._playwright:
            self._playwright.stop()
        self._context = self._page = self._playwright = None

    def _ensure_browser(self) -> Page:
        if self._page:
            return self._page
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            self.profile_dir, headless=self.headless, viewport={"width": 1440, "height": 1000},
            locale="ru-RU", timezone_id=MOSCOW,
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    def login(self) -> None:
        if self.headless:
            raise AuthenticationRequired("Для входа запустите браузер без --headless")
        page = self._ensure_browser()
        try:
            page.goto("https://vk.com/" if self.platform == "vk" else "https://web.telegram.org/k/", wait_until="domcontentloaded")
        except PlaywrightError as exc:
            raise AuthenticationRequired(f"Окно браузера закрылось до входа: {exc}") from None

        print(f"  ожидание входа в {self.platform}: авторизуйтесь (QR-код/логин) в открытом окне Chrome; "
              "не закрывайте окно — ожидание до 5 минут")
        deadline = datetime.now().timestamp() + 300
        while datetime.now().timestamp() < deadline:
            try:
                ready = self._is_logged_in(page.content())
            except PlaywrightError as exc:
                raise AuthenticationRequired(
                    f"Окно браузера было закрыто до подтверждения входа в {self.platform} ({exc}). "
                    "Запустите ещё раз и дождитесь сообщения «вход подтверждён» перед тем, как закрывать окно."
                ) from None
            if ready:
                print(f"  вход в {self.platform} подтверждён, сессия сохранена.")
                return
            try:
                page.wait_for_timeout(1000)
            except PlaywrightError as exc:
                raise AuthenticationRequired(
                    f"Окно браузера было закрыто до подтверждения входа в {self.platform} ({exc}). "
                    "Запустите ещё раз и дождитесь сообщения «вход подтверждён» перед тем, как закрывать окно."
                ) from None
        raise AuthenticationRequired(f"Вход в {self.platform} не завершён за 5 минут")

    def _open_collection(self, url: str) -> None:
        page = self._ensure_browser()
        target = telegram_web_url(url) if self.platform == "telegram" else url
        page.goto(target, wait_until="domcontentloaded")

    def _content(self) -> str:
        return self._ensure_browser().content()

    def _scroll_older(self) -> None:
        page = self._ensure_browser()
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(750)

    def _is_logged_in(self, html: str) -> bool:
        """Залогинен ли профиль вообще — без открытой переписки с сообщениями
        (в отличие от _is_ready). После входа по умолчанию открыт список чатов
        БЕЗ единого сообщения на экране, поэтому _is_ready здесь не подходит —
        она ждёт маркеры конкретного открытого чата с постами и никогда не
        сработает на голом списке чатов."""
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True).lower()
        return "log in" not in text and "войти" not in text and "войдите" not in text

    def _is_ready(self, html: str) -> bool:
        """Готовность определяется ТЕМ ЖЕ маркером, что использует extract_*_posts
        (data-post-id/data-mid), а не расплывчатым OR по подстроке класса
        ("wall"/"bubble") — тот совпадает уже с пустым контейнером ленты,
        отрисованным до того, как загрузилось хоть одно сообщение. Из-за этого
        _content_when_ready считала страницу готовой мгновенно, не дожидаясь
        реального контента, а extract_*_posts затем не находила ни одного поста
        и collect() валил это как "разметка изменилась" на каждом источнике."""
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True).lower()
        if self.platform == "vk":
            return "войти" not in text and "data-post-id" in html
        return "log in" not in text and "войти" not in text and "data-mid" in html

    @staticmethod
    def extract_vk_posts(html: str, url: str, start: date, end: date) -> list[Post]:
        soup = BeautifulSoup(html, "html.parser")
        source = urlparse(url).netloc + urlparse(url).path.rstrip("/")
        found: list[Post] = []
        for node in soup.select("[data-post-id]"):
            post_id = node.get("data-post-id", "")
            timestamp = node.get("data-post-date") or node.get("data-date")
            published = _moscow_date(timestamp)
            match = __import__("re").fullmatch(r"(?:club|public|id)?(-?\d+)_([\d]+)", post_id)
            text_node = node.select_one(".wall_post_text, [data-testid='post_text']")
            text = text_node.get_text(" ", strip=True) if text_node else ""
            if not match or not published or not text or not start <= published <= end:
                continue
            owner, number = match.groups()
            if not owner.startswith("-") and post_id.startswith(("club", "public")):
                owner = "-" + owner
            found.append(Post(source, url, published, None, f"https://vk.com/wall{owner}_{number}",
                              first_sentence(text), text))
        return found

    @staticmethod
    def extract_telegram_posts(html: str, url: str, start: date, end: date) -> list[Post]:
        soup = BeautifulSoup(html, "html.parser")
        parts = [part for part in urlparse(url).path.split("/") if part]
        channel = parts[1] if parts[:1] == ["c"] and len(parts) > 1 else (parts[0] if parts else "")
        if not channel:
            return []
        source = urlparse(url).netloc + urlparse(url).path.rstrip("/")
        found: list[Post] = []
        for node in soup.select("[data-mid]"):
            message_id = node.get("data-mid", "")
            # Текущая разметка Telegram Web K не рендерит <time datetime>: дата
            # лежит в data-timestamp (Unix-время) прямо на самом узле сообщения —
            # тот же приём, что уже используется для VK (data-post-date).
            published = _moscow_date(node.get("data-timestamp"))
            body = node.select_one(".message, .text-content, .message-content")
            text = body.get_text(" ", strip=True) if body else ""
            if not message_id.isdigit() or not published or not text or not start <= published <= end:
                continue
            found.append(Post(source, url, published, None, f"https://t.me/{channel}/{message_id}",
                              first_sentence(text), text))
        return found

    def _content_when_ready(self, timeout_s: float = 15.0) -> str:
        """Сразу после навигации SPA (Telegram Web K/VK) может ещё не успеть
        отрендерить ни сообщения, ни признаки экрана входа — мгновенная проверка
        путает "страница ещё грузится" с "нужен вход" и ошибочно валит вход как
        разлогин. Ждём до timeout_s, пока не появится однозначный сигнал —
        либо контент готов, либо страница явно показывает экран входа."""
        deadline = datetime.now().timestamp() + timeout_s
        html = self._content()
        while datetime.now().timestamp() < deadline:
            if self._is_ready(html) or not self._is_logged_in(html):
                return html
            self._ensure_browser().wait_for_timeout(500)
            html = self._content()
        return html

    def collect(self, url: str, start: date, end: date) -> Iterator[Post]:
        if start > end:
            raise ValueError("start не может быть позже end")
        self._open_collection(url)
        seen: set[str] = set()
        selector_seen = False
        for scroll_index in range(self.max_scrolls):
            html = self._content_when_ready() if scroll_index == 0 else self._content()
            if not self._is_ready(html):
                if not self._is_logged_in(html):
                    action = f"--login {self.platform} без --headless"
                    raise AuthenticationRequired(f"Требуется вход: запустите {action}")
                # Залогинены, но конкретный канал не отрендерил содержимое —
                # это проблема ОДНОГО источника, а не сессии целиком, поэтому
                # не должно прерывать сбор по остальным источникам как разлогин.
                raise SocialBrowserError(
                    f"Не удалось загрузить содержимое {self.platform}-канала за отведённое время: {url}"
                )
            extractor = self.extract_vk_posts if self.platform == "vk" else self.extract_telegram_posts
            posts = extractor(html, url, start, end)
            selector_seen = selector_seen or ("data-post-id" in html if self.platform == "vk" else "data-mid" in html)
            added = [post for post in posts if post.direct_url not in seen]
            if not added:
                if selector_seen:
                    return
                raise SocialBrowserError(f"Не найдены ожидаемые элементы {self.platform}; разметка изменилась или источник недоступен")
            for post in added:
                seen.add(post.direct_url)
                yield post
            self._scroll_older()
        if self.logger:
            self.logger.warning("Достигнут предел прокруток %s: %s", self.max_scrolls, url)
