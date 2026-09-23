from types import SimpleNamespace

import pytest

from max_news.auth import ensure_max_ready
import max_news.auth as auth


class FakePage:
    url = 'https://web.max.ru/'

    def __init__(self, ready=False):
        self.ready = ready
        self.waits = 0

    def goto(self, url, **kwargs):
        assert url == 'https://web.max.ru/'

    def get_by_placeholder(self, name):
        assert name == 'Найти'
        return SimpleNamespace(first=SimpleNamespace(
            count=lambda: int(self.ready), is_visible=lambda: self.ready))

    def locator(self, selector):
        assert selector == 'body'
        return SimpleNamespace(inner_text=lambda **kwargs: 'Войти в MAX: QR-код')

    def wait_for_timeout(self, delay):
        self.waits += 1
        self.ready = True


def test_saved_login_needs_no_wait():
    page = FakePage(ready=True)
    ensure_max_ready(page, headless=True)
    assert page.waits == 0


def test_headless_requires_visible_relogin():
    page = FakePage()
    with pytest.raises(RuntimeError, match='без --headless'):
        ensure_max_ready(page, headless=True)
    assert page.waits == 0


def test_visible_login_continues_after_user_signs_in(capsys):
    page = FakePage()
    ensure_max_ready(page)
    assert page.waits == 1
    assert 'QR-коду' in capsys.readouterr().out


def test_login_timeout_has_no_false_archive_claim(monkeypatch):
    ticks = iter([0, 0, 301])
    monkeypatch.setattr(auth.time_module, 'monotonic', lambda: next(ticks))
    with pytest.raises(RuntimeError, match='не загрузилась за 5 минут'):
        ensure_max_ready(FakePage())
