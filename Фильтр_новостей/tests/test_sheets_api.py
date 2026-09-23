from pathlib import Path
import sys

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from classifier import StopProcessing
from sheet import NewsSheet, HEADERS, padded_rows


def row(text):
    result = [''] * 13
    result[1], result[7], result[12] = '08.09.2026', f'https://example.com/{text}', text
    return result


def metadata(count):
    return {'sheets': [{'properties': {'title': 'Новости', 'sheetId': 0,
            'sheetType': 'GRID', 'gridProperties': {'rowCount': count, 'columnCount': 25}}}]}


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status
    def json(self):
        return self.data


class Session:
    def __init__(self, responses):
        self.responses, self.calls = responses, []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
    def close(self):
        pass


def make_sheet(responses):
    session = Session(responses)
    return NewsSheet('test-id', Path('unused'), session=session, min_interval=0), session


def test_missing_google_key_is_clear_and_does_not_launch_browser(tmp_path):
    with pytest.raises(StopProcessing, match='GOOGLE_SETUP.md'):
        NewsSheet('id', tmp_path / 'missing.json')


def test_read_preserves_physical_empty_rows_and_checks_headers():
    sheet, session = make_sheet([Response(metadata(100)),
        Response({'values': [HEADERS, row('one'), [], row('three'), []]})])
    assert sheet.read_rows() == [row('one'), ['']*13, row('three')]
    assert sheet.sheet_id == 0
    assert all(call[0] == 'GET' for call in session.calls)
    assert session.calls[-1][2]['params']['valueRenderOption'] == 'FORMATTED_VALUE'


@pytest.mark.parametrize('index', [0, 5092])
def test_delete_uses_exact_sheet_and_zero_based_row_and_bounded_reads(index):
    rows = [row(str(i)) for i in range(index + 4)]
    first, end = max(0, index-1), index+3
    wanted = rows[:index] + rows[index+1:]
    sheet, session = make_sheet([Response(metadata(len(rows)+10)),
        Response({'values': rows[first:end]}), Response({'replies': [{}]}),
        Response({'values': wanted[first:end]})])
    sheet.delete_row(index, rows)
    writes = [call for call in session.calls if call[0] == 'POST']
    assert len(writes) == 1
    assert writes[0][2]['json'] == {'requests': [{'deleteDimension': {'range': {
        'sheetId': 0, 'dimension': 'ROWS', 'startIndex': index+5, 'endIndex': index+6}}}]}
    assert len(session.calls) == 4  # metadata + before + delete + after
    assert all(call[2]['allow_redirects'] is False for call in session.calls)


def test_changed_row_blocks_write():
    rows = [row('original'), row('next')]
    sheet, session = make_sheet([Response(metadata(10)), Response({'values': [row('changed')]})])
    with pytest.raises(StopProcessing, match='изменились'):
        sheet.delete_row(0, rows)
    assert not any(call[0] == 'POST' for call in session.calls)


@pytest.mark.parametrize('response', [Response({}, 429), requests.Timeout('sensitive details')])
def test_failed_write_is_never_retried(response):
    rows = [row('remove'), row('next')]
    sheet, session = make_sheet([Response(metadata(10)), Response({'values': rows}), response])
    with pytest.raises(StopProcessing) as error:
        sheet.delete_row(0, rows)
    assert 'sensitive details' not in str(error.value)
    assert sum(call[0] == 'POST' for call in session.calls) == 1


def test_confirmation_mismatch_never_repeats_delete():
    rows = [row('remove'), row('next')]
    sheet, session = make_sheet([Response(metadata(10)), Response({'values': rows}),
        Response({'replies': [{}]}), Response({'values': rows})])
    with pytest.raises(StopProcessing, match='сдвиг'):
        sheet.delete_row(0, rows)
    assert sum(call[0] == 'POST' for call in session.calls) == 1


def test_delete_last_physical_row_and_trimmed_empty_response():
    rows = [row('keep'), row('last')]
    sheet, session = make_sheet([Response(metadata(7)), Response({'values': rows}),
        Response({'replies': [{}]}), Response({'values': [rows[0]]})])
    sheet.delete_row(1, rows)
    assert sheet.grid_rows == 6
    assert session.calls[-1][1].endswith('A6%3AM6')


def test_forbidden_access_does_not_call_values_or_write():
    sheet, session = make_sheet([Response({}, 403)])
    with pytest.raises(StopProcessing, match='права редактора'):
        sheet.read_rows()
    assert len(session.calls) == 1


def windows(rows, indexes, grid_rows=10):
    return {'valueRanges': [{'values': rows[max(0, i-1):min(i+2, grid_rows-5)]}
                            for i in indexes]}


def test_cleanup_updates_only_m_as_literal_text_and_verifies_neighbors():
    before = [row('🔥 =1+2'), row('keep'), row('news 🎉')]
    after = [r[:] for r in before]
    after[0][12], after[2][12] = '=1+2', 'news'
    sheet, session = make_sheet([
        Response(metadata(10)), Response(windows(before, [0, 2])),
        Response({'totalUpdatedCells': 2}), Response(windows(after, [0, 2]))])
    sheet.update_texts([(0, '=1+2'), (2, 'news')], before)
    writes = [call for call in session.calls if call[0] == 'POST']
    assert len(writes) == 1
    assert writes[0][1].endswith('/values:batchUpdate')
    assert writes[0][2]['json'] == {'valueInputOption': 'RAW', 'data': [
        {'range': "'Новости'!M6", 'values': [['=1+2']]},
        {'range': "'Новости'!M8", 'values': [['news']]}]}
    assert session.calls[1][2]['params']['ranges'] == ["'Новости'!A6:M7", "'Новости'!A7:M9"]
    assert before[0][12] == '🔥 =1+2'  # caller updates snapshot only after success


def test_cleanup_changed_neighbor_cancels_write():
    before = [row('🔥 news'), row('next')]
    actual = [before[0], row('changed')]
    sheet, session = make_sheet([Response(metadata(10)), Response(windows(actual, [0]))])
    with pytest.raises(StopProcessing, match='изменились'):
        sheet.update_texts([(0, 'news')], before)
    assert not any(call[0] == 'POST' for call in session.calls)


@pytest.mark.parametrize('failure', [Response({}, 429), requests.Timeout('secret')])
def test_cleanup_write_failure_is_not_replayed(failure):
    before = [row('🔥 news')]
    sheet, session = make_sheet([Response(metadata(10)), Response(windows(before, [0])), failure])
    with pytest.raises(StopProcessing) as error:
        sheet.update_texts([(0, 'news')], before)
    assert 'secret' not in str(error.value)
    assert sum(call[0] == 'POST' for call in session.calls) == 1


def test_cleanup_mismatched_verification_stops():
    before = [row('🔥 news')]
    sheet, session = make_sheet([Response(metadata(10)), Response(windows(before, [0])),
        Response({'totalUpdatedCells': 1}), Response(windows(before, [0]))])
    with pytest.raises(StopProcessing, match='после очистки'):
        sheet.update_texts([(0, 'news')], before)
    assert sum(call[0] == 'POST' for call in session.calls) == 1
