import urllib3
from gigachat import GigaChat

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GIGACHAT_CREDENTIALS = "MDE5YmFjZDMtMzFhYi03N2Y4LTkzODQtZDkwNjAzZjgxMTMxOjE1MGJiYTcyLTRlYjMtNDNiZS05NjA3LTgwZDlmYzc4MTU1MA=="

try:
    with GigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False) as giga:
        models = giga.get_models()
        print("Доступные модели для вашего ключа:")
        for model in models.data:
            print(f'"{model.id}"')
except Exception as e:
    print(f"Ошибка: {e}")