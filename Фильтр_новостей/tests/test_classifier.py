import json
import sys
import time
from datetime import date
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from classifier import (Decision, GigaChatClient, StopProcessing, clean_text,
                        parse_decision, prepare_news_text, process, selected_rows)
from main import parse_args
from sheet import HEADERS, padded_rows


DAY = date(2026, 9, 9)


def row(text, collected='09.09.2026', url='link'):
    values = [''] * 13
    values[1], values[7], values[12] = collected, url, text
    return values


class FakeSheet:
    def __init__(self, rows):
        self.rows = [r[:] for r in rows]
        self.deleted = []
        self.updated = []

    def read_rows(self):
        return [r[:] for r in self.rows]

    def delete_row(self, index, expected):
        assert self.rows == expected
        self.deleted.append(index + 6)
        self.rows.pop(index)
        while self.rows and not any(self.rows[-1]):
            self.rows.pop()

    def update_texts(self, changes, expected):
        assert self.rows == expected
        self.updated.append(changes[:])
        for index, text in changes:
            self.rows[index][12] = text


class FakeModel:
    def __init__(self):
        self.calls = []

    def classify(self, text):
        self.calls.append(text)
        if text == 'error':
            raise StopProcessing('API unavailable')
        return Decision(text == 'keep')


def test_bottom_up_deletes_consecutive_rows_without_skipping_and_uses_collection_date():
    rows = [row('drop'), row('drop'), row('keep'), row('old', '08.09.2026'), row('drop')]
    rows[0][4] = '01.01.2000'  # Publication date must not influence the filter.
    sheet, model, events = FakeSheet(rows), FakeModel(), []
    counts = process(sheet, model, DAY, DAY, False, events.append)
    assert sheet.deleted == [10, 7, 6]
    assert [r[12] for r in sheet.rows] == ['keep', 'old']
    assert model.calls == ['drop', 'keep', 'drop', 'drop']
    assert counts['deleted'] == 3


def test_interior_blank_rows_do_not_shift_index_mapping():
    sheet = FakeSheet([row('drop'), [''] * 13, row('drop')])
    process(sheet, FakeModel(), DAY, DAY, False, lambda e: None)
    assert sheet.deleted == [8, 6]
    assert sheet.rows == []


def test_empty_text_only_selected_dates_deleted_without_api():
    sheet, model = FakeSheet([row(' \u2063\u200b\ufeff\n'), row('', '08.09.2026')]), FakeModel()
    process(sheet, model, DAY, DAY, False, lambda e: None)
    assert model.calls == []
    assert sheet.deleted == [6]
    assert len(sheet.rows) == 1


def test_api_error_stops_preserves_current_and_all_remaining_rows():
    sheet, model = FakeSheet([row('drop'), row('error'), row('drop')]), FakeModel()
    with pytest.raises(StopProcessing):
        process(sheet, model, DAY, DAY, False, lambda e: None)
    assert sheet.deleted == [8]
    assert [r[12] for r in sheet.rows] == ['drop', 'error']
    assert model.calls == ['drop', 'error']


def test_delete_error_stops_without_classifying_next_row():
    sheet, model = FakeSheet([row('keep'), row('drop')]), FakeModel()
    def fail(*args):
        raise StopProcessing('changed')
    sheet.delete_row = fail
    with pytest.raises(StopProcessing):
        process(sheet, model, DAY, DAY, False, lambda e: None)
    assert model.calls == ['drop']
    assert len(sheet.rows) == 2


def test_dry_run_and_limit_never_delete():
    sheet, model = FakeSheet([row('drop'), row('drop'), row('keep')]), FakeModel()
    result = process(sheet, model, DAY, DAY, True, lambda e: None, limit=2)
    assert sheet.deleted == []
    assert model.calls == ['keep', 'drop']
    assert result == {'selected': 2, 'kept': 1, 'deleted': 0, 'would_delete': 1, 'manual_review': 0}


def test_invalid_dates_fail_before_any_classification_or_deletion():
    sheet, model = FakeSheet([row('drop', ''), row('drop')]), FakeModel()
    with pytest.raises(StopProcessing, match='Строка 6'):
        process(sheet, model, DAY, DAY, False, lambda e: None)
    assert model.calls == [] and sheet.deleted == []


def test_date_range_inclusive_unsorted_and_one_day_default():
    rows = [row('x', '10.09.2026'), row('x', '08.09.2026'), row('x', '2026-09-09')]
    assert selected_rows(rows, date(2026, 9, 8), DAY) == [2, 1]
    args = parse_args(['--from-date', '2026-09-08'])
    assert args.from_date == args.to_date == date(2026, 9, 8)
    with pytest.raises(SystemExit):
        parse_args(['--from-date', '2026-09-10', '--to-date', '2026-09-09'])
    with pytest.raises(SystemExit):
        parse_args(['--to-date', '2026-09-09'])


def payload(result, finish='stop'):
    return {'choices': [{'finish_reason': finish, 'message': {
        'role': 'assistant', 'content': json.dumps(result)}}]}


@pytest.mark.parametrize('result,finish', [
    ({'useful': 'false'}, 'stop'),
    ({'useful': 0}, 'stop'),
    ({'useful': None}, 'stop'),
    ({}, 'stop'),
    (False, 'stop'),
    ({'useful': False}, 'length'),
    ({'useful': False}, 'blacklist'),
    ({'useful': False, 'extra': 1}, 'stop'),
])
def test_rejects_uncertain_model_output(result, finish):
    with pytest.raises(StopProcessing):
        parse_decision(payload(result, finish))


def test_valid_model_output():
    result = {'useful': True}
    assert parse_decision(payload(result)).useful is True
    assert clean_text('\u2063 news \ufeff') == 'news'


@pytest.mark.parametrize('finish,description', [
    ('length', 'лимит длины'),
    ('blacklist', 'тематическими ограничениями'),
    ('function_call', 'вызов функции'),
    ('error', 'ошибке формирования'),
    ('new_status', 'неожиданный статус'),
])
def test_finish_reason_is_preserved_without_accepting_a_decision(finish, description):
    with pytest.raises(StopProcessing) as error:
        parse_decision(payload({'useful': False}, finish))
    assert error.value.details == {'finish_reason': finish}
    assert description in str(error.value)
    assert f'finish_reason={finish}' in str(error.value)


@pytest.mark.parametrize('dry_run', [False, True])
def test_model_block_preserves_row_and_continues_without_skipping(dry_run):
    class BlockedModel:
        def classify(self, text):
            if text == 'blocked':
                return parse_decision(payload({'useful': False}, 'blacklist'))
            return Decision(False)

    sheet, events = FakeSheet([row('drop'), row('blocked'), row('drop')]), []
    counts = process(sheet, BlockedModel(), DAY, DAY, dry_run, events.append)
    assert sheet.deleted == ([] if dry_run else [8, 6])
    assert [r[12] for r in sheet.rows] == (['drop', 'blocked', 'drop'] if dry_run else ['blocked'])
    reviews = [e for e in events if e['action'] == 'manual_review']
    assert len(reviews) == 1
    assert reviews[0]['row'] == 7
    assert reviews[0]['details'] == {'finish_reason': 'blacklist'}
    assert 'decision' not in reviews[0]
    assert counts['manual_review'] == 1 and counts['kept'] == 0


@pytest.mark.parametrize('finish', ['length', 'error', 'function_call'])
def test_non_blacklist_status_still_stops_processing(finish):
    class FailedModel:
        def classify(self, text):
            return parse_decision(payload({'useful': False}, finish))

    sheet, events = FakeSheet([row('drop'), row('failed')]), []
    with pytest.raises(StopProcessing):
        process(sheet, FailedModel(), DAY, DAY, False, events.append)
    assert sheet.deleted == []
    assert len(events) == 1 and events[0]['action'] == 'classification_error'


def test_only_column_m_is_sent_to_classifier():
    news = row('keep', url='https://max.ru/source/post')
    news[9] = 'Заголовок из другого столбца'
    model = FakeModel()
    process(FakeSheet([news]), model, DAY, DAY, False, lambda e: None)
    assert model.calls == ['keep']


def test_api_values_keep_multiline_text_and_interior_empty_rows():
    rows = padded_rows([row('line 1\nline 2'), [], row('end')])
    assert len(rows) == 3 and rows[0][12] == 'line 1\nline 2' and rows[1] == [''] * 13
    with pytest.raises(StopProcessing):
        padded_rows('invalid')


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


class Session:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_auth_refresh_and_independent_messages(tmp_path):
    key = tmp_path / 'key.txt'
    key.write_text('test-secret')
    answer = payload({'useful': False})
    session = Session([Response({'access_token': 'token', 'expires_at': (time.time()+1800)*1000}),
                       Response(answer), Response(answer)])
    client = GigaChatClient(key, 'rules', session=session)
    client.classify('news')
    client.classify('more news')
    assert len(session.calls) == 3
    assert session.calls[0][1]['data']['scope'] == 'GIGACHAT_API_PERS'
    messages = session.calls[-1][1]['json']['messages']
    assert messages == [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'more news'}]


@pytest.mark.parametrize('response', [Response({}, 429), requests.ConnectionError('test-secret')])
def test_http_failure_no_retry_and_no_secret_in_error(tmp_path, response):
    key = tmp_path / 'key.txt'
    key.write_text('test-secret')
    session = Session([response])
    with pytest.raises(StopProcessing) as error:
        GigaChatClient(key, 'rules', session=session).classify('news')
    assert len(session.calls) == 1
    assert 'test-secret' not in str(error.value)


@pytest.mark.parametrize('failure', [
    requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout,
    requests.exceptions.ProxyError, requests.exceptions.ConnectionError,
    requests.exceptions.SSLError, requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError, requests.exceptions.Timeout,
    requests.exceptions.RequestException,
])
@pytest.mark.parametrize('authorized', [False, True])
def test_network_diagnostics_identify_type_and_stage_without_secrets(tmp_path, failure, authorized):
    key = tmp_path / 'key.txt'
    key.write_text('test-secret')
    session = Session([failure('test-secret payload')])
    client = GigaChatClient(key, 'rules', session=session)
    if authorized:
        client._token, client._expires = 'test-secret-token', time.time() + 1800
    with pytest.raises(StopProcessing) as error:
        client._classify_once('news')
    assert error.value.details == {
        'network_error': failure.__name__,
        'operation': 'классификация новости' if authorized else 'получение токена',
    }
    assert len(session.calls) == 1
    assert 'test-secret' not in str(error.value)
    assert 'test-secret' not in json.dumps(error.value.details)


@pytest.mark.parametrize('failures', [1, 3, 4])
def test_classification_retries_same_news_with_bounded_backoff(tmp_path, monkeypatch, failures):
    key = tmp_path / 'key.txt'
    key.write_text('test-secret')
    session = Session([requests.ConnectionError('test-secret') for _ in range(failures)]
                      + [Response(payload({'useful': False}))])
    events, pauses = [], []
    monkeypatch.setattr('classifier.time.sleep', pauses.append)
    client = GigaChatClient(key, 'rules', session=session, on_retry=events.append)
    client._token, client._expires = 'token', time.time() + 1800
    sheet = FakeSheet([row('news')])
    if failures == 4:
        with pytest.raises(StopProcessing, match='исчерпаны 3 повтора') as error:
            process(sheet, client, DAY, DAY, False, events.append)
        assert error.value.details['attempts'] == 4
        assert sheet.deleted == [] and len(sheet.rows) == 1
    else:
        process(sheet, client, DAY, DAY, False, events.append)
        assert sheet.deleted == [6] and sheet.rows == []
    assert pauses == [2, 5, 10][:min(failures, 3)]
    assert len(session.calls) == min(failures + 1, 4)
    assert all(call[1]['json'] == session.calls[0][1]['json'] for call in session.calls)
    retries = [e for e in events if e['action'] == 'classification_retry']
    assert [e['retry'] for e in retries] == list(range(1, min(failures, 3) + 1))
    assert 'test-secret' not in json.dumps(events)


@pytest.mark.parametrize('response', [
    Response({}, 429), Response({}, 500),
    Response(payload({'useful': False}, 'blacklist')),
    Response(payload({'useful': False}, 'length')),
    Response(payload({'useful': 'false'})),
    requests.exceptions.SSLError('test-secret'),
])
def test_non_transient_classification_errors_are_not_retried(tmp_path, monkeypatch, response):
    key = tmp_path / 'key.txt'
    key.write_text('test-secret')
    session, pauses, events = Session([response]), [], []
    monkeypatch.setattr('classifier.time.sleep', pauses.append)
    client = GigaChatClient(key, 'rules', session=session, on_retry=events.append)
    client._token, client._expires = 'token', time.time() + 1800
    with pytest.raises(StopProcessing):
        client.classify('news')
    assert len(session.calls) == 1 and pauses == [] and events == []


def test_all_empty_texts_are_removed_before_first_model_call_with_correct_shifts():
    sheet = FakeSheet([row(''), row('drop'), row(' \u200b\n'), row('keep'), row('drop')])
    class Model(FakeModel):
        def classify(self, text):
            assert all(clean_text(r[12]) for r in sheet.rows)
            return super().classify(text)
    model, events = Model(), []
    counts = process(sheet, model, DAY, DAY, False, events.append)
    assert model.calls == ['drop', 'keep', 'drop']
    assert sheet.deleted == [8, 6, 8, 6]
    assert [r[12] for r in sheet.rows] == ['keep']
    assert counts['selected'] == 5 and counts['deleted'] == 4
    assert [e['reason'] for e in events[:4]] == ['empty_text'] * 4


def test_preclean_keeps_original_limit_and_dates():
    sheet = FakeSheet([row(''), row('', '08.09.2026'), row('keep'), row('')])
    model = FakeModel()
    counts = process(sheet, model, DAY, DAY, False, lambda e: None, limit=2)
    assert sheet.deleted == [9]
    assert model.calls == ['keep']
    assert counts['selected'] == 2
    assert [r[12] for r in sheet.rows] == ['', '', 'keep']


def test_preclean_dry_run_preserves_indices_and_reports_empty_first():
    original = [row(''), row('drop'), row(' \u2063'), row('keep')]
    sheet, model, events = FakeSheet(original), FakeModel(), []
    counts = process(sheet, model, DAY, DAY, True, events.append)
    assert sheet.rows == original and sheet.deleted == []
    assert model.calls == ['keep', 'drop']
    assert [e['row'] for e in events] == [8, 6, 9, 7]
    assert counts['would_delete'] == 3


def test_preclean_deletion_error_stops_before_model():
    sheet, model = FakeSheet([row(''), row('keep')]), FakeModel()
    def fail(*args):
        raise StopProcessing('changed')
    sheet.delete_row = fail
    with pytest.raises(StopProcessing, match='changed'):
        process(sheet, model, DAY, DAY, False, lambda e: None)
    assert model.calls == []


@pytest.mark.parametrize('original,expected', [
    ('🔥 Открыта школа! 🎉', 'Открыта школа!'),
    ('ремонт🚧дороги', 'ремонт дороги'),
    ('👨‍👩‍👧‍👦 🇷🇺 👍🏽 ❤️\n', ''),
    ('1️⃣ Этап №2: 10 млн ₽, +5%, -3 °C. ©2026', '1 Этап №2: 10 млн ₽, +5%, -3 °C. ©2026'),
    ('😀\ufe0f\u200b', ''),
    ('✅ Первый пункт\n\n➡️ Второй пункт', 'Первый пункт\n\nВторой пункт'),
    ('=1+2 🔥', '=1+2'),
    ('https://example.ru/a?q=1#x 🙂', 'https://example.ru/a?q=1#x'),
])
def test_prepare_text_removes_whole_emojis_preserves_words_numbers_and_paragraphs(original, expected):
    assert prepare_news_text(original) == expected
    assert prepare_news_text(expected) == expected


def test_cleans_m_before_model_and_deletes_emoji_only_first():
    original = [row('🔥'), row('keep 🎉'), row('drop 🚧')]
    original[1][9] = 'Заголовок 🎉'
    sheet = FakeSheet(original)
    class Model(FakeModel):
        def classify(self, text):
            assert sheet.updated == [[(1, 'drop'), (0, 'keep')]]
            assert sheet.rows[0][9] == 'Заголовок 🎉'
            return super().classify(text)
    model, events = Model(), []
    counts = process(sheet, model, DAY, DAY, False, events.append)
    assert model.calls == ['drop', 'keep']
    assert sheet.deleted == [6, 7]
    assert sheet.rows[0][12] == 'keep'
    assert counts['deleted'] == 2
    assert events[0]['reason'] == 'empty_text'
    assert [e['row'] for e in events if e['action'] == 'text_cleaned'] == [7, 6]


def test_emoji_cleanup_respects_dates_limit_and_dry_run():
    original = [row('🔥 keep'), row('keep 🎉', '08.09.2026'), row('keep ✅'), row('🔥')]
    sheet, model = FakeSheet(original), FakeModel()
    counts = process(sheet, model, DAY, DAY, True, lambda e: None, limit=2)
    assert model.calls == ['keep'] and counts['would_delete'] == 1
    assert sheet.rows == original and sheet.updated == [] and sheet.deleted == []
    sheet, model = FakeSheet(original), FakeModel()
    process(sheet, model, DAY, DAY, False, lambda e: None, limit=2)
    assert [r[12] for r in sheet.rows] == ['🔥 keep', 'keep 🎉', 'keep']


def test_cleanup_failure_prevents_model_calls():
    sheet, model = FakeSheet([row('keep 🔥')]), FakeModel()
    def fail(*args):
        raise StopProcessing('write failed')
    sheet.update_texts = fail
    with pytest.raises(StopProcessing, match='write failed'):
        process(sheet, model, DAY, DAY, False, lambda e: None)
    assert model.calls == []


def test_cleaned_retry_does_not_rewrite_m():
    sheet, model = FakeSheet([row('keep 🔥')]), FakeModel()
    process(sheet, model, DAY, DAY, False, lambda e: None)
    process(sheet, model, DAY, DAY, False, lambda e: None)
    assert len(sheet.updated) == 1


def test_cleanup_batches_before_classification():
    sheet, model = FakeSheet([row('keep 🔥') for _ in range(205)]), FakeModel()
    process(sheet, model, DAY, DAY, False, lambda e: None)
    assert [len(batch) for batch in sheet.updated] == [100, 100, 5]
    assert len(model.calls) == 205
