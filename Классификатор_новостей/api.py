from __future__ import annotations

# HTTP transport adapted from Фильтр_новостей/classifier.py.
import time
import uuid
from pathlib import Path
import requests

DEFAULT_MODEL = 'GigaChat-3-Ultra'

class StopProcessing(RuntimeError):
    """Stop safely on an API or spreadsheet error."""

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


class GigaChatClient:
    def __init__(self, key_path: Path, prompt: str, model: str = DEFAULT_MODEL,
                 scope: str = 'GIGACHAT_API_PERS', ca_bundle: str | None = None,
                 session=None, on_retry=None):
        key = key_path.read_text(encoding='utf-8-sig').strip()
        if key.startswith('Basic '):
            key = key[6:].strip()
        if not key or any(c.isspace() for c in key):
            raise StopProcessing('В api_key.txt должен быть один ключ авторизации')
        self._key = key
        self.prompt, self.model, self.scope = prompt, model, scope
        self.session = session or requests.Session()
        if ca_bundle:
            self.session.verify = ca_bundle
        self._token, self._expires = '', 0.0
        self.on_retry = on_retry

    def _json_request(self, method: str, url: str, **kwargs) -> dict:
        operation = {
            'https://ngw.devices.sberbank.ru:9443/api/v2/oauth': 'получение токена',
            'https://api.giga.chat/v1/models': 'получение списка моделей',
            'https://api.giga.chat/v1/chat/completions': 'классификация новости',
        }.get(url, 'запрос к API')
        try:
            response = self.session.request(method, url, timeout=(15, 120),
                                            allow_redirects=False, **kwargs)
        except requests.RequestException as error:
            # Do not print request/response bodies or headers: they may contain keys.
            cases = (
                (requests.exceptions.SSLError, 'ошибка TLS; проверьте доверенный PEM через --ca-bundle'),
                (requests.exceptions.ConnectTimeout, 'тайм-аут подключения (лимит 15 секунд)'),
                (requests.exceptions.ReadTimeout, 'тайм-аут ожидания данных ответа (лимит 120 секунд)'),
                (requests.exceptions.ProxyError, 'ошибка подключения через прокси'),
                (requests.exceptions.ConnectionError, 'не удалось установить или сохранить соединение'),
                (requests.exceptions.Timeout, 'превышено время ожидания запроса'),
                (requests.exceptions.ChunkedEncodingError, 'ошибка передачи ответа: соединение прервано или данные повреждены'),
                (requests.exceptions.ContentDecodingError, 'не удалось декодировать ответ сервера'),
            )
            error_type, reason = next(
                ((kind.__name__, explanation) for kind, explanation in cases if isinstance(error, kind)),
                ('RequestException', 'ошибка выполнения сетевого запроса'))
            raise StopProcessing(
                f'Сетевая ошибка GigaChat: {reason} ({error_type}; {operation}); '
                'запрос не завершён',
                details={'network_error': error_type, 'operation': operation}) from None
        if response.status_code != 200:
            raise StopProcessing(f'GigaChat: HTTP {response.status_code}; скрипт остановлен')
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            return data
        except ValueError:
            raise StopProcessing('GigaChat вернул некорректный JSON') from None

    def _authorization(self) -> dict:
        if time.time() >= self._expires - 60:
            data = self._json_request('POST', 'https://ngw.devices.sberbank.ru:9443/api/v2/oauth',
                headers={'Authorization': f'Basic {self._key}', 'RqUID': str(uuid.uuid4()),
                         'Accept': 'application/json'}, data={'scope': self.scope})
            try:
                token, expires = data['access_token'], float(data['expires_at'])
                # The service normally returns milliseconds since epoch.
                if expires > 100_000_000_000:
                    expires /= 1000
                if not isinstance(token, str) or not token or expires <= time.time() + 60:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise StopProcessing('GigaChat вернул некорректный токен или срок его действия') from None
            self._token, self._expires = token, expires
        return {'Authorization': f'Bearer {self._token}', 'Accept': 'application/json'}

    def models(self) -> list[str]:
        data = self._json_request('GET', 'https://api.giga.chat/v1/models', headers=self._authorization())
        try:
            return [item['id'] for item in data['data']]
        except (KeyError, TypeError):
            raise StopProcessing('Некорректный список моделей GigaChat') from None

    def classify(self, text: str) -> dict:
        delays = (2, 5, 10)
        transient_errors = {'ConnectionError', 'ConnectTimeout', 'ReadTimeout',
                            'ProxyError', 'Timeout', 'ChunkedEncodingError',
                            'ContentDecodingError'}
        for attempt in range(len(delays) + 1):
            try:
                return self._classify_once(text)
            except StopProcessing as error:
                if (error.details.get('operation') != 'классификация новости'
                        or error.details.get('network_error') not in transient_errors):
                    raise
                if attempt == len(delays):
                    raise StopProcessing(
                        f'{error}; исчерпаны 3 повтора (4 попытки), скрипт остановлен',
                        details={**error.details, 'attempts': attempt + 1}) from None
                event = {'action': 'classification_retry', 'retry': attempt + 1,
                         'max_retries': len(delays), 'delay_seconds': delays[attempt],
                         'error': str(error), 'details': error.details}
                if self.on_retry is not None:
                    self.on_retry(event)
                else:
                    print(f'GigaChat: повтор {attempt + 1}/3 через {delays[attempt]} с '
                          f'после {error.details["network_error"]}', flush=True)
                time.sleep(delays[attempt])
