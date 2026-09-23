from copy import deepcopy
from dataclasses import replace
from datetime import date
from pathlib import Path
from urllib.parse import unquote

import pytest
import requests

from max_news.google_sheets import ApiSheetWriter, SheetWriteError
from max_news.models import Post
from max_news.sheet_plan import plan_posts, text_key

DAY = date(2026, 9, 11)
POST = Post('Канал', 'https://max.ru/a', DAY, None,
            'https://max.ru/a/one', 'Заголовок', 'Новость.\nПодробности.')
HEADERS = ['Сборщик', 'Дата сбора'] + [''] * 19


class Response:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return deepcopy(self.data)


class FakeSession:
    """Exercise HTTP payloads and read-back, without network or credentials."""
    def __init__(self, rows=(), grid_rows=100):
        self.rows = deepcopy(list(rows))
        self.grid_rows = grid_rows
        self.calls = []
        self.writes = []
        self.after_write = None
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert kwargs['allow_redirects'] is False
        if method == 'GET':
            if '/values/' not in url:
                return Response({'sheets': [{'properties': {'title': 'Новости',
                    'sheetId': 9, 'sheetType': 'GRID', 'gridProperties': {
                        'rowCount': self.grid_rows, 'columnCount': 21}}}]})
            if 'Источники' in unquote(url):
                assert kwargs['params']['valueRenderOption'] == 'FORMATTED_VALUE'
                return Response({'values': [
                    ['Название', 'Основная ссылка', 'Платформа', 'Активность'],
                    ['Канал', 'https://max.ru/a', 'MAX', 'Да'],
                    ['Другой', 'https://max.ru/b', 'MAX', 'Нет']]})
            assert kwargs['params']['valueRenderOption'] == 'FORMULA'
            return Response({'values': [HEADERS] + self.rows})
        assert method == 'POST' and url.endswith(':batchUpdate')
        body = kwargs['json']['requests']
        self.writes.append(deepcopy(body))
        updated = deepcopy(self.rows)
        for operation in body:
            if 'appendDimension' in operation:
                self.grid_rows += operation['appendDimension']['length']
                continue
            change = operation['updateCells']
            index = change['start']['rowIndex'] - 5
            col = change['start']['columnIndex']
            assert change['start']['rowIndex'] < self.grid_rows
            assert col in (0, 1, 4, 6, 7, 9, 12)
            while len(updated) <= index:
                updated.append([''] * 21)
            cell = change['rows'][0]['values'][0]
            if col in (1, 4):
                value = cell['userEnteredValue']['numberValue']
                assert cell['userEnteredFormat']['numberFormat'] == {
                    'type': 'DATE', 'pattern': 'dd.MM.yyyy'}
            else:
                value = cell['userEnteredValue']['stringValue']
                assert change['fields'] == 'userEnteredValue'
            updated[index][col] = value
        self.rows = updated
        if self.after_write:
            self.after_write(self)
        return Response({'replies': [{} for _ in body]})

    def close(self):
        self.closed = True


def writer(session, **kwargs):
    return ApiSheetWriter('test', Path('unused'), session=session, min_interval=0, **kwargs)


def row(**values):
    result = [''] * 21
    for key, value in values.items():
        result[ord(key) - ord('A')] = value
    return result


def test_text_duplicate_in_sheet_preserves_original_without_url():
    original = row(G='Другой канал', M='  Новость.\u00a0\n Подробности.  ')
    session = FakeSession([original])
    sheet = writer(session)
    assert sheet.append([POST], DAY) == 0
    assert sheet.text_duplicates == 1
    assert session.rows == [original]
    assert session.writes == []


@pytest.mark.parametrize('chunk_size', [1, 100])
def test_cross_channel_duplicates_in_and_between_batches(chunk_size):
    session = FakeSession()
    sheet = writer(session, chunk_size=chunk_size)
    copied = replace(POST, direct_url='https://max.ru/b/two', source='Другой канал',
                     text='Новость.   Подробности.')
    assert sheet.append([POST, copied, POST], DAY) == 1
    assert len(session.rows) == 1
    assert session.rows[0][7] == POST.direct_url
    assert sheet.append([copied], DAY) == 0


def test_repairs_known_url_before_new_copy_and_preserves_manual_values():
    original = row(H=POST.direct_url, J='Ручной заголовок', C='Муниципалитет', U='=IF(TRUE,"",1)')
    session = FakeSession([original])
    sheet = writer(session)
    copied = replace(POST, direct_url='https://max.ru/b/two')
    assert sheet.append([copied, POST], DAY) == 0
    assert session.rows[0][12] == POST.text
    assert session.rows[0][9] == original[9]
    assert session.rows[0][2] == original[2]
    assert session.rows[0][20] == original[20]
    assert sheet.text_duplicates == 1
    assert sheet.append([POST], DAY) == 0
    assert len(session.writes) == 1


def test_populated_formula_zero_false_and_manual_text_not_overwritten():
    original = row(H=POST.direct_url, A=False, B=0, J='=""', M='Ручной текст')
    session = FakeSession([original])
    assert writer(session).append([POST], DAY) == 0
    for col in (0, 1, 7, 9, 12):
        assert session.rows[0][col] == original[col]


def test_empty_texts_are_distinct_and_punctuation_is_significant():
    posts = [replace(POST, direct_url=f'url{i}', text=text) for i, text in enumerate(
        ['', ' \n ', 'Новость!', 'Новость?', 'новость!', 'Новость! #канал'])]
    plan = plan_posts([], posts, DAY)
    assert plan.new_count == len(posts)
    assert plan.text_duplicates == 0
    assert text_key('е\u0308лка\n растёт') == text_key('ёлка растёт')


def test_appends_below_occupied_u_and_grows_grid_atomically():
    existing = row(U='=""')
    session = FakeSession([existing], grid_rows=6)
    assert writer(session).append([POST], DAY) == 1
    assert session.grid_rows == 7
    assert session.rows[0] == existing
    assert session.rows[1][7] == POST.direct_url
    assert 'appendDimension' in session.writes[0][0]
    assert len(session.writes) == 1


def test_literal_formula_like_text_and_typed_dates():
    session = FakeSession()
    post = replace(POST, text='=IMPORTXML("url", "query")', title='+1')
    assert writer(session).append([post], DAY) == 1
    assert session.rows[0][12] == post.text
    assert session.rows[0][9] == '+1'
    assert session.rows[0][1] == (DAY - date(1899, 12, 30)).days


def test_changed_snapshot_stops_before_write(monkeypatch):
    session = FakeSession()
    sheet = writer(session)
    snapshots = iter([[], [row(C='Человек добавил строку')]])
    monkeypatch.setattr(sheet, 'read_rows', lambda: next(snapshots))
    with pytest.raises(SheetWriteError, match='изменилась'):
        sheet.append([POST], DAY)
    assert session.writes == []


def test_ambiguous_committed_write_is_not_replayed_and_restart_deduplicates():
    session = FakeSession()
    def timeout(_):
        raise requests.Timeout('sensitive transport details')
    session.after_write = timeout
    with pytest.raises(SheetWriteError, match='не повторяется') as error:
        writer(session).append([POST], DAY)
    assert 'sensitive' not in str(error.value)
    assert len(session.writes) == 1
    session.after_write = None
    assert writer(session).append([POST], DAY) == 0
    assert len(session.writes) == 1


def test_readback_mismatch_stops_next_chunk():
    session = FakeSession()
    session.after_write = lambda s: s.rows[0].__setitem__(12, 'Изменено')
    with pytest.raises(SheetWriteError, match='M6'):
        writer(session, chunk_size=1).append([POST, replace(POST, direct_url='other', text='Иное')], DAY)
    assert len(session.writes) == 1


@pytest.mark.parametrize('status', [401, 403, 404, 429, 500])
def test_http_failure_does_not_retry(status):
    session = FakeSession()
    def fail(method, url, **kwargs):
        session.calls.append(method)
        response = Response({})
        response.status_code = status
        return response
    session.request = fail
    with pytest.raises(SheetWriteError, match=f'HTTP {status}'):
        writer(session).check_access()
    assert session.calls == ['GET']


def test_api_sources_and_sheet_access():
    session = FakeSession()
    sheet = writer(session)
    assert sheet.check_access() == []
    sources = sheet.load_sources()
    assert len(sources) == 1 and sources[0].name == 'Канал'
    assert all(url.startswith('https://sheets.googleapis.com/') for _, url, _ in session.calls)


def test_missing_credentials_actionable(tmp_path):
    with pytest.raises(SheetWriteError, match='--google-credentials'):
        ApiSheetWriter('test', tmp_path / 'missing.json')


def test_check_sheet_cli_does_not_start_max_or_write(monkeypatch):
    import main
    session = FakeSession()
    monkeypatch.setattr(main, 'ApiSheetWriter', lambda *args: writer(session))
    monkeypatch.setattr(main, 'sync_playwright', lambda: pytest.fail('MAX must not open'))
    assert main.main(['--spreadsheet-id', 'test', '--check-sheet']) == 0
    assert session.writes == [] and session.closed


def test_duplicate_matches_after_classifier_removes_emojis():
    session = FakeSession([row(M='Открыта школа!', H='other-source')])
    post = replace(POST, text='🔥 Открыта школа! 🎉')
    sheet = writer(session)
    assert sheet.append([post], DAY) == 0
    assert sheet.text_duplicates == 1 and not session.writes
    assert text_key('1️⃣ этап ©2026') == text_key('1 этап ©2026')
    assert text_key('ремонт🚧дороги') == text_key('ремонт дороги')
    assert text_key('👨‍👩‍👧‍👦 🇷🇺 👍🏽 ❤️') == ''
