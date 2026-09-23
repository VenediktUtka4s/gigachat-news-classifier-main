"""Authenticated Sheets API reads and atomic, verified news writes."""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from .models import Post
from .sheet_plan import FIRST_DATA_ROW, SheetWriteError, plan_posts
from .sources import parse_max_sources

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']


class ApiSheetWriter:
    def __init__(self, spreadsheet_id: str, credentials_path: Path, *, session=None,
                 chunk_size: int = 100, min_interval: float = 1.1):
        if chunk_size < 1 or min_interval < 0:
            raise ValueError('Некорректный размер порции или интервал Google API')
        self.base_url = f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}'
        self.chunk_size, self.min_interval = chunk_size, min_interval
        self._last_request = None
        self.sheet_id = None
        self.grid_rows = None
        self.text_duplicates = 0
        if session is not None:
            self.session = session
            return
        if not credentials_path.is_file():
            raise SheetWriteError('Нет JSON-ключа Google. Сохраните google_credentials.json '
                                  'рядом с main.py или укажите --google-credentials. '
                                  'Инструкция: GOOGLE_SETUP.md.')
        try:
            credentials = service_account.Credentials.from_service_account_file(
                str(credentials_path), scopes=SCOPES)
        except (ValueError, KeyError, TypeError, GoogleAuthError):
            raise SheetWriteError('Некорректный ключ сервисного аккаунта Google') from None
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
            raise SheetWriteError('Ошибка соединения или авторизации Google Sheets. '
                'Запрос не повторяется автоматически; повторите тот же период, '
                'чтобы заново сверить таблицу.') from None
        if response.status_code != 200:
            hints = {401: 'проверьте ключ сервисного аккаунта',
                     403: 'включите Sheets API и предоставьте сервисному аккаунту права редактора',
                     404: 'проверьте ссылку таблицы и доступ сервисного аккаунта',
                     429: 'исчерпана квота Google Sheets API; повторите запуск позже'}
            raise SheetWriteError(f'Google Sheets: HTTP {response.status_code}; '
                                  + hints.get(response.status_code, 'запрос остановлен'))
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            return result
        except ValueError:
            raise SheetWriteError('Google Sheets вернул некорректный JSON') from None

    def _metadata(self):
        data = self._request('GET', self.base_url, params={
            'fields': 'sheets(properties(sheetId,title,sheetType,gridProperties(rowCount,columnCount)))'})
        try:
            sheet, = [item['properties'] for item in data['sheets']
                      if item['properties']['title'] == 'Новости']
            if sheet['sheetType'] != 'GRID' or sheet['gridProperties']['columnCount'] < 21:
                raise ValueError
            self.sheet_id = int(sheet['sheetId'])
            self.grid_rows = int(sheet['gridProperties']['rowCount'])
            if self.grid_rows < 5:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise SheetWriteError('Нужен лист «Новости» с заголовками в строке 5 '
                                  'и столбцами A:U') from None

    def _values(self, target: str, render: str = 'FORMULA') -> list[list]:
        data = self._request('GET', f'{self.base_url}/values/{quote(target, safe="")}',
            params={'valueRenderOption': render, 'dateTimeRenderOption': 'SERIAL_NUMBER',
                    'majorDimension': 'ROWS'})
        rows = data.get('values', [])
        if not isinstance(rows, list) or any(not isinstance(row, list) for row in rows):
            raise SheetWriteError('Некорректные строки Google Sheets')
        return rows

    def load_sources(self):
        return parse_max_sources(self._values("'Источники'!A5:M", 'FORMATTED_VALUE'))

    def read_rows(self) -> list[list]:
        rows = self._values("'Новости'!A5:U")
        if not rows or rows[0][:2] != ['Сборщик', 'Дата сбора']:
            raise SheetWriteError('Не подтверждены заголовки A5:B5 листа «Новости»')
        return [row + [''] * max(0, 21 - len(row)) for row in rows[1:]]

    def check_access(self) -> list[list]:
        self._metadata()
        return self.read_rows()

    def append(self, posts: list[Post], collected_on: date) -> int:
        confirmed = 0
        for offset in range(0, len(posts), self.chunk_size):
            # Fresh reads include news from earlier channels and earlier runs.
            self._metadata()
            before = self.read_rows()
            plan = plan_posts(before, posts[offset:offset + self.chunk_size], collected_on)
            self.text_duplicates += plan.text_duplicates
            if plan.text_duplicates:
                print(f'  пропущено дублей по тексту: {plan.text_duplicates}', flush=True)
            if not plan.changes:
                continue
            # Detect row shifts and edits while planning. Sheets has no CAS;
            # concurrent writers must still be avoided.
            if self.read_rows() != before:
                raise SheetWriteError('Таблица изменилась перед записью; повторите запуск')
            requests_body = []
            last_row = max(row for row, _ in plan.changes)
            if last_row > self.grid_rows:
                requests_body.append({'appendDimension': {'sheetId': self.sheet_id,
                    'dimension': 'ROWS', 'length': last_row - self.grid_rows}})
            for row, changes in plan.changes:
                for col, value in changes.items():
                    cell = {'userEnteredValue': {'stringValue': value}}
                    fields = 'userEnteredValue'
                    if col in (1, 4):
                        cell = {'userEnteredValue': {'numberValue': value},
                                'userEnteredFormat': {'numberFormat': {
                                    'type': 'DATE', 'pattern': 'dd.MM.yyyy'}}}
                        fields += ',userEnteredFormat.numberFormat'
                    requests_body.append({'updateCells': {
                        'start': {'sheetId': self.sheet_id, 'rowIndex': row - 1, 'columnIndex': col},
                        'rows': [{'values': [cell]}], 'fields': fields}})
            # Identities and values commit atomically. stringValue prevents text
            # from becoming a formula. Never replay a write after a timeout.
            response = self._request('POST', f'{self.base_url}:batchUpdate',
                                     json={'requests': requests_body})
            if not isinstance(response.get('replies'), list) or len(response['replies']) != len(requests_body):
                raise SheetWriteError('Не подтверждён ответ записи; повторите период для сверки таблицы')
            actual = self.read_rows()
            mismatches = [f'{chr(65 + col)}{row}' for row, changes in plan.changes
                          for col, value in changes.items()
                          if row - FIRST_DATA_ROW >= len(actual)
                          or actual[row - FIRST_DATA_ROW][col] != value]
            if mismatches:
                raise SheetWriteError('Не подтверждены ячейки: ' + ', '.join(mismatches[:8])
                                      + '. Повторите период для сверки таблицы.')
            confirmed += plan.new_count
            print(f'  таблица API: проверено строк {len(plan.changes)}, новых {plan.new_count}', flush=True)
        return confirmed

    def close(self):
        self.session.close()
