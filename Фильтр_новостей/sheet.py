from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import quote

import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from classifier import StopProcessing


HEADERS = ['Сборщик', 'Дата сбора', 'Основной муниципалитет',
           'Дополнительные муниципалитеты', 'Дата публикации', 'Дата события',
           'Источник', 'Прямая ссылка', 'Дополнительные ссылки', 'Заголовок',
           'Категория', 'Стадия события', 'Краткое фактическое резюме']
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']


def padded_rows(values: list[list], size: int | None = None) -> list[list[str]]:
    if not isinstance(values, list):
        raise StopProcessing('Google Sheets вернул некорректные значения')
    result = []
    for row in values:
        if not isinstance(row, list) or len(row) > 13:
            raise StopProcessing('Неожиданная структура строк Google Sheets')
        result.append([str(v).replace('\r\n', '\n') for v in row] + [''] * (13 - len(row)))
    if size is not None:
        if len(result) > size:
            raise StopProcessing('Google Sheets вернул больше строк, чем запрошено')
        result.extend([[''] * 13 for _ in range(size - len(result))])
    return result


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
                                 'Запрос не повторяется; если выполнялось удаление, проверьте таблицу.') from None
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

    def check_access(self):
        if self.sheet_id is not None:
            return
        data = self._request('GET', self.base_url,
            params={'fields': 'spreadsheetId,sheets(properties(sheetId,title,sheetType,gridProperties(rowCount,columnCount)))'})
        try:
            matches = [s['properties'] for s in data['sheets'] if s['properties']['title'] == 'Новости']
            if len(matches) != 1:
                raise ValueError
            sheet = matches[0]
            if sheet['sheetType'] != 'GRID' or sheet['gridProperties']['columnCount'] < 13:
                raise ValueError
            self.sheet_id = int(sheet['sheetId'])
            self.grid_rows = int(sheet['gridProperties']['rowCount'])
            if self.grid_rows < 5:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            self.sheet_id = None
            raise StopProcessing('Не подтверждена структура листа «Новости» в Google Sheets API') from None

    def _values(self, first_row: int, last_row: int) -> list[list]:
        if first_row > last_row:
            return []
        target = quote(f"'Новости'!A{first_row}:M{last_row}", safe='')
        data = self._request('GET', f'{self.base_url}/values/{target}',
                             params={'valueRenderOption': 'FORMATTED_VALUE', 'majorDimension': 'ROWS'})
        return data.get('values', [])

    def read_rows(self) -> list[list[str]]:
        self.check_access()
        rows = padded_rows(self._values(5, self.grid_rows))
        if not rows or [v.strip() for v in rows[0]] != HEADERS:
            raise StopProcessing('Не подтверждены заголовки A5:M5 листа «Новости»')
        rows = rows[1:]
        while rows and not any(rows[-1]):
            rows.pop()
        return rows

    def _window(self, first: int, end: int) -> list[list[str]]:
        """Data indexes, end exclusive; only the target and adjacent rows."""
        return padded_rows(self._values(first + 6, end + 5), end - first)

    @staticmethod
    def _expected_window(rows: list[list[str]], first: int, end: int) -> list[list[str]]:
        return padded_rows(rows[first:end], end - first)

    def delete_row(self, index: int, expected: list[list[str]]):
        if index < 0 or index >= len(expected):
            raise StopProcessing('Недопустимый номер строки для удаления')
        self.check_access()
        first = max(0, index - 1)
        end = min(index + 3, self.grid_rows - 5)
        if self._window(first, end) != self._expected_window(expected, first, end):
            raise StopProcessing(f'Строка {index + 6} или соседние строки изменились; удаление отменено')
        # Data index 0 is sheet row 6, i.e. zero-based API index 5.
        result = self._request('POST', f'{self.base_url}:batchUpdate', json={
            'requests': [{'deleteDimension': {'range': {
                'sheetId': self.sheet_id, 'dimension': 'ROWS',
                'startIndex': index + 5, 'endIndex': index + 6}}}]})
        if not isinstance(result.get('replies'), list) or len(result['replies']) != 1:
            raise StopProcessing('Неожиданное подтверждение удаления Google Sheets; запрос не повторяется')
        self.grid_rows -= 1
        wanted = expected[:index] + expected[index + 1:]
        end = min(end, self.grid_rows - 5)
        if self._window(first, end) != self._expected_window(wanted, first, end):
            raise StopProcessing('После удаления не подтверждён ожидаемый сдвиг строк; '
                                 'обработка остановлена, повторного удаления не будет')

    def _text_windows(self, indexes: list[int]) -> list[list[list[str]]]:
        windows = [(max(0, index - 1), min(index + 2, self.grid_rows - 5))
                   for index in indexes]
        ranges = [f"'Новости'!A{first + 6}:M{end + 5}" for first, end in windows]
        response = self._request('GET', f'{self.base_url}/values:batchGet', params={
            'ranges': ranges, 'valueRenderOption': 'FORMATTED_VALUE', 'majorDimension': 'ROWS'})
        values = response.get('valueRanges')
        if not isinstance(values, list) or len(values) != len(indexes):
            raise StopProcessing('Не подтверждены диапазоны перед/после очистки M')
        result = []
        for value, (first, end) in zip(values, windows):
            if not isinstance(value, dict):
                raise StopProcessing('Некорректный диапазон Google Sheets при очистке M')
            result.append(padded_rows(value.get('values', []), end - first))
        return result

    def update_texts(self, changes: list[tuple[int, str]], expected: list[list[str]]):
        """Write only M in a bounded batch, preserving all other cell values."""
        if not changes:
            return
        if len(changes) > 100 or len({index for index, _ in changes}) != len(changes):
            raise StopProcessing('Некорректная порция очистки M')
        if any(index < 0 or index >= len(expected) or not isinstance(text, str)
               for index, text in changes):
            raise StopProcessing('Некорректная строка для очистки M')
        self.check_access()
        indexes = [index for index, _ in changes]
        def snapshots(rows):
            return [self._expected_window(rows, max(0, index - 1),
                    min(index + 2, self.grid_rows - 5)) for index in indexes]
        if self._text_windows(indexes) != snapshots(expected):
            raise StopProcessing('Строки изменились перед очисткой M; запись отменена')
        response = self._request('POST', f'{self.base_url}/values:batchUpdate', json={
            'valueInputOption': 'RAW',
            'data': [{'range': f"'Новости'!M{index + 6}", 'values': [[text]]}
                     for index, text in changes]})
        if response.get('totalUpdatedCells') != len(changes):
            raise StopProcessing('Не подтверждено число очищенных ячеек M; запрос не повторяется')
        wanted = [row[:] for row in expected]
        for index, text in changes:
            wanted[index][12] = text
        if self._text_windows(indexes) != snapshots(wanted):
            raise StopProcessing('Не подтверждён текст после очистки M; обработка остановлена')

    def close(self):
        self.session.close()
