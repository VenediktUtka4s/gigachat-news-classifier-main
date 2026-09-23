from datetime import date
from pathlib import Path

import pytest

from max_news.models import Post
from news_runtime.social_browser import (
    AuthenticationRequired,
    SocialBrowser,
    SocialBrowserError,
    telegram_web_url,
)


def test_telegram_web_url_routes_public_and_private_links() -> None:
    assert telegram_web_url("https://t.me/example_channel") == (
        "https://web.telegram.org/k/#@example_channel"
    )
    assert telegram_web_url("https://t.me/c/123456/42") == (
        "https://web.telegram.org/k/#-100123456"
    )


def test_telegram_web_url_rejects_non_channel_links() -> None:
    with pytest.raises(SocialBrowserError, match="Telegram"):
        telegram_web_url("https://example.org/channel")


def test_vk_posts_use_stable_post_links_and_moscow_dates() -> None:
    html = """
    <article data-post-id="club42_17" data-post-date="1767214800">
      <div class="wall_post_text">Текст VK-поста</div>
    </article>
    """
    posts = SocialBrowser.extract_vk_posts(
        html, "https://vk.com/club42", date(2026, 1, 1), date(2026, 1, 1)
    )
    assert posts == [Post(
        source="vk.com/club42", source_url="https://vk.com/club42",
        publication_date=date(2026, 1, 1), publication_time=None,
        direct_url="https://vk.com/wall-42_17", title="Текст VK-поста",
        text="Текст VK-поста",
    )]


def test_telegram_posts_read_bubble_id_timestamp_and_permalink() -> None:
    # Реальная разметка Telegram Web K (проверено вживую 2026-09-23): даты нет
    # в дочернем <time datetime>, она в data-timestamp (Unix-время) прямо на
    # узле [data-mid] — тот же приём, что у VK (data-post-date).
    html = """
    <div class="bubble" data-mid="55" data-timestamp="1767214800">
      <div class="message">Первая строка<br>Продолжение</div>
    </div>
    """
    posts = SocialBrowser.extract_telegram_posts(
        html, "https://t.me/example", date(2026, 1, 1), date(2026, 1, 1)
    )
    assert len(posts) == 1
    assert posts[0].direct_url == "https://t.me/example/55"
    assert posts[0].publication_date == date(2026, 1, 1)
    assert posts[0].text == "Первая строка Продолжение"


def test_telegram_posts_without_data_timestamp_are_skipped_not_crashed() -> None:
    # Сообщение без data-timestamp (например, служебное) не должно падать —
    # просто пропускается, как и раньше без валидной даты.
    html = '<div class="bubble" data-mid="56"><div class="message">Текст</div></div>'
    posts = SocialBrowser.extract_telegram_posts(
        html, "https://t.me/example", date(2026, 1, 1), date(2026, 1, 1)
    )
    assert posts == []


def test_collect_stops_after_no_new_posts_without_treating_unknown_dom_as_empty() -> None:
    browser = SocialBrowser("vk", Path("unused"), max_scrolls=5)
    snapshots = iter([
        "<article data-post-id='club1_1' data-post-date='1767214800'><div class='wall_post_text'>Пост</div></article>",
        "<article data-post-id='club1_1' data-post-date='1767214800'><div class='wall_post_text'>Пост</div></article>",
    ])
    browser._open_collection = lambda url: None
    browser._content = lambda: next(snapshots)
    browser._scroll_older = lambda: None
    assert [post.direct_url for post in browser.collect(
        "https://vk.com/club1", date(2026, 1, 1), date(2026, 1, 1)
    )] == ["https://vk.com/wall-1_1"]


def test_collect_requires_visible_login_when_profile_is_missing() -> None:
    browser = SocialBrowser("vk", Path("unused"), headless=True)
    browser._open_collection = lambda url: None
    browser._content = lambda: "<body>Войти</body>"
    with pytest.raises(AuthenticationRequired, match="--login vk"):
        list(browser.collect("https://vk.com/club1", date(2026, 1, 1), date(2026, 1, 1)))


def test_is_ready_false_for_empty_bubble_container_without_any_message() -> None:
    # Контейнер ленты (класс "bubble"/"wall") отрисовывается ДО того, как
    # загрузится хоть одно сообщение — считать это готовностью нельзя: именно
    # так _is_ready раньше давала ложный "готово" на пустой ленте, а
    # extract_telegram_posts потом не находила ни одного [data-mid] и collect()
    # валил это как "разметка изменилась" на каждом источнике подряд.
    telegram = SocialBrowser("telegram", Path("unused"))
    assert telegram._is_ready("<div class='bubbles-inner'></div>") is False
    vk = SocialBrowser("vk", Path("unused"))
    assert vk._is_ready("<div class='wall_module'></div>") is False


def test_is_logged_in_true_on_bare_chat_list_without_open_conversation() -> None:
    # После входа по умолчанию открыт список чатов БЕЗ единого сообщения на
    # экране — именно это раньше принималось за "вход не завершён" навечно,
    # потому что _is_ready (для _is_logged_in теперь отдельная проверка)
    # требует маркеры открытой переписки, которых тут закономерно нет.
    browser = SocialBrowser("telegram", Path("unused"))
    assert browser._is_logged_in("<body>Избранное Сохранённые сообщения</body>") is True


def test_is_logged_in_false_while_login_screen_is_shown() -> None:
    browser = SocialBrowser("telegram", Path("unused"))
    assert browser._is_logged_in("<body>Войдите в Telegram по QR-коду</body>") is False
