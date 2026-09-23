from __future__ import annotations

# HTTP transport adapted from Фильтр_новостей/sheet.py.
import time
from pathlib import Path
import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from api import StopProcessing

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

class NewsSheet:
    """Official Sheets API only. No browser, cookies or browser profile."""

    def __init__(self, spreadsheet_id: str, credentials_path: Path, *, session=None,
                 min_interval: float = 1.1):
        self.spreadsheet_id = spreadsheet_id
        self.sheet_id = None
        self.grid_rows = None
        self.min_interval = min_interval
        self._last_request = None
        self.base_url = f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}'
        if session is not None:
            self.session = session
            return
        if not credentials_path.is_file():
            raise StopProcessing('Нет JSON-ключа Google: сохраните ключ сервисного аккаунта в '
                                 'google_credentials.json рядом с main.py. Инструкция: GOOGLE_SETUP.md. '
                                 'Браузер запускаться не будет.')
        try:
            credentials = service_account.Credentials.from_service_account_file(
                str(credentials_path), scopes=SCOPES)
        except (ValueError, KeyError, TypeError, GoogleAuthError):
            raise StopProcessing('Некорректный JSON-ключ сервисного аккаунта Google') from None
        # Crucial: an unauthorized response must not transparently replay a delete.
        self.session = AuthorizedSession(credentials, max_refresh_attempts=0, refresh_timeout=30)

    def _request(self, method: str, url: str, **kwargs) -> dict:
        if self._last_request is not None:
            pause = self.min_interval - (time.monotonic() - self._last_request)
            if pause > 0:
                time.sleep(pause)
        self._last_request = time.monotonic()
        try:
            response = self.session.request(method, url, timeout=(15, 60),
                                            allow_redirects=False, **kwargs)
        except (requests.RequestException, GoogleAuthError):
            raise StopProcessing('Ошибка соединения или авторизации Google Sheets. '
                                 'Запрос не повторяется; если выполнялась запись, проверьте таблицу.') from None
        if response.status_code != 200:
            hints = {
                401: 'проверьте ключ сервисного аккаунта',
                403: 'включите Google Sheets API и предоставьте сервисному аккаунту права редактора',
                404: 'проверьте ссылку таблицы и доступ сервисного аккаунта',
                429: 'исчерпана квота Google Sheets API',
            }
            hint = hints.get(response.status_code, 'запрос не повторяется')
            raise StopProcessing(f'Google Sheets: HTTP {response.status_code}; {hint}')
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            return data
        except ValueError:
            raise StopProcessing('Google Sheets вернул некорректный JSON; дальнейшие операции остановлены') from None


    def close(self):
        self.session.close()
