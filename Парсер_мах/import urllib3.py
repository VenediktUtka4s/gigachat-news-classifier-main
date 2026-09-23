import urllib3
from gigachat import GigaChat

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
GIGACHAT_CREDENTIALS = "MDE5YmFjZDMtMzFhYi03N2Y4LTkzODQtZDkwNjAzZjgxMTMxOjE1MGJiYTcyLTRlYjMtNDNiZS05NjA3LTgwZDlmYzc4MTU1MA=="

try:
    with GigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False) as giga:
        models = giga.get_models()
        print("Структура ответа:")
        # Выводим все атрибуты первого объекта модели, чтобы понять, как к нему обращаться
        if hasattr(models, 'data') and len(models.data) > 0:
            first_model = models.data[0]
            print(dir(first_model))
            # Пытаемся вывести популярные названия атрибутов
            print(f"Name: {getattr(first_model, 'name', 'Нет атрибута name')}")
            print(f"Model: {getattr(first_model, 'model', 'Нет атрибута model')}")
            
            print("\nВсе доступные модели:")
            for m in models.data:
                # В новых версиях API поле обычно называется name
                print(getattr(m, 'name', str(m)))
        else:
            print(models)
except Exception as e:
    print(f"Ошибка: {e}")