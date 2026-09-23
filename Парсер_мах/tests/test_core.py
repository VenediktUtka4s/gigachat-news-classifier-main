from max_news.text import first_sentence


def test_first_sentence() -> None:
    assert first_sentence("Первая фраза. Вторая фраза.") == "Первая фраза."
    assert first_sentence("Заголовок\n\nОсновной текст") == "Заголовок"
    assert first_sentence("Вопрос? Ответ.") == "Вопрос?"
    assert first_sentence("") == ""
