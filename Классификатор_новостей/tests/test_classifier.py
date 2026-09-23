import json
from datetime import date
from pathlib import Path

import pytest

from api import StopProcessing
from classifier import (Catalog, FIELDS, GigaChatClient, InvalidDecision, parse_response,
                        plan_changes, process, source_municipality, validate_decision)
from main import parse_args

CATALOG = Catalog(**{k: tuple(v) for k, v in json.loads((Path(__file__).parent / 'catalog.json').read_text()).items()})
DAY = date(2026, 9, 3)


def decision(**kwargs):
    return dict(main_municipality='Воркута', category='ЖКХ и благоустройство', event_stage='Работы начаты',
                additional_municipalities=[], event_date='2026-09-02', key_facts=['Заменят 2 км сетей'],
                people=[], organizations=[], object_or_locality=None, **{}) | kwargs


def row(text='Вчера в Воркуте началась замена 2 км сетей.'):
    result = [''] * 18
    result[1], result[4], result[6], result[7], result[12] = '03.09.2026', '03.09.2026', 'Местные новости', 'https://max.ru/source/post', text
    return result


def payload(result, finish='stop'):
    return {'choices': [{'finish_reason': finish, 'message': {'role': 'assistant', 'content': json.dumps(result, ensure_ascii=False)}}]}


@pytest.mark.parametrize('field,value', [
    ('main_municipality', 'Сыктывдинский'), ('main_municipality', 'Москва'),
    ('main_municipality', ['Воркута']), ('category', 'Ремонт'), ('category', None),
    ('category', 'Другое '), ('event_stage', 'Другое'), ('event_stage', 'Не определено'),
    ('event_stage', ['Анонс']), ('additional_municipalities', ['Эжва']),
    ('additional_municipalities', ['Воркута']), ('additional_municipalities', ['Ухта', 'Ухта']),
    ('event_date', '2026-02-30'), ('event_date', '20260902'), ('event_date', '02.09.2026'),
    ('event_date', 123), ('people', 'Иван'), ('people', ['']), ('people', [1]),
    ('organizations', None), ('object_or_locality', []), ('object_or_locality', ''),
])
def test_rejects_invented_or_malformed_values(field, value):
    with pytest.raises(InvalidDecision):
        validate_decision(decision(**{field: value}), CATALOG)


def test_exact_json_keys_and_finish_status():
    assert parse_response(payload(decision()), CATALOG) == decision()
    for data in (decision(extra='x'), {k: v for k, v in decision().items() if k != 'people'}):
        with pytest.raises(InvalidDecision):
            parse_response(payload(data), CATALOG)
    for finish in ('blacklist', 'length', 'function_call', 'error'):
        with pytest.raises(InvalidDecision) as error:
            parse_response(payload(decision(), finish), CATALOG)
        assert error.value.details['finish_reason'] == finish
    p = payload(decision())
    p['choices'][0]['message']['content'] = '{"category":"Другое","category":"Другое"}'
    with pytest.raises(InvalidDecision):
        parse_response(p, CATALOG)


def test_fallback_and_nullable_stage_do_not_invent_announcement():
    changes = plan_changes(row(), decision(main_municipality=None, event_stage=None), 'Республика Коми', CATALOG)
    assert changes[2] == 'Республика Коми'
    assert 11 not in changes
    assert changes[5] == '02.09.2026'
    assert set(changes) <= set(FIELDS.values())


def test_unknown_stage_defaults_to_other_when_allowed():
    catalog = Catalog(CATALOG.municipalities, CATALOG.categories, (*CATALOG.stages, 'Другое'))
    assert validate_decision(decision(event_stage='Другое'), catalog)['event_stage'] == 'Другое'
    changes = plan_changes(row(), decision(event_stage=None), 'Республика Коми', catalog)
    assert changes[11] == 'Другое'
    existing = row()
    existing[11] = 'Работы начаты'
    assert 11 not in plan_changes(existing, decision(event_stage=None), 'Воркута', catalog)
    assert plan_changes(existing, decision(event_stage=None), 'Воркута', catalog, overwrite=True)[11] == 'Другое'


def test_default_stage_is_not_counted_as_unresolved():
    sheet = FakeSheet([row()])
    sheet.catalog = Catalog(CATALOG.municipalities, CATALOG.categories, (*CATALOG.stages, 'Другое'))
    events = []
    counts = process(sheet, FakeModel(decision(event_stage=None)), DAY, DAY, True, events.append)
    assert counts['unresolved_stage'] == 0
    assert events[-1]['changes']['L'] == 'Другое'


def test_preserve_filled_fields_and_overwrite_does_not_erase_unknowns():
    existing = row()
    existing[2], existing[10], existing[14] = 'Ухта', 'Другое', 'Сохранить'
    d = decision(additional_municipalities=['Ухта'])
    changes = plan_changes(existing, d, 'Воркута', CATALOG)
    assert not {2, 3, 10, 14} & set(changes)
    changes = plan_changes(existing, d, 'Воркута', CATALOG, overwrite=True)
    assert changes[2] == 'Воркута' and changes[3] == 'Ухта'
    assert 14 not in changes


def test_fallback_deduplicates_additional_municipality():
    changes = plan_changes(row(), decision(main_municipality=None, additional_municipalities=['Воркута', 'Ухта']), 'Воркута', CATALOG)
    assert changes[3] == 'Ухта'


def test_source_registry_disambiguation():
    sources = [dict(name='Новости', municipality='Воркута', url='https://max.ru/vorkuta'),
               dict(name='Новости', municipality='Ухта', url='https://max.ru/ukhta')]
    assert source_municipality('Новости', 'https://max.ru/ukhta/post', sources, CATALOG) == 'Ухта'
    assert source_municipality('Новости', 'https://max.ru/ukhta_fake/post', sources, CATALOG) == 'Республика Коми'
    assert source_municipality('Неизвестно', 'https://max.ru/vorkuta/post', sources, CATALOG) == 'Воркута'
    assert source_municipality('НовостиВоркуты', '', [], CATALOG) == 'Республика Коми'


class FakeSheet:
    catalog = CATALOG
    sources = []
    def __init__(self, rows):
        self.rows, self.writes = rows, []
    def read_rows(self):
        return [r[:] for r in self.rows]
    def update_fields(self, index, expected, changes):
        assert self.rows[index] == expected
        self.writes.append((index, changes))
        for col, value in changes.items():
            self.rows[index][col] = value


class FakeModel:
    def __init__(self, result=None):
        self.result, self.calls = result or decision(), []
    def classify_news(self, news):
        self.calls.append(news)
        if news['text'] == 'blocked':
            raise InvalidDecision('blacklist', details={'finish_reason': 'blacklist'})
        return self.result


@pytest.mark.parametrize('dry_run', [True, False])
def test_period_limit_empty_rows_and_only_allowed_writes(dry_run):
    outside = row(); outside[1] = '02.09.2026'
    rows = [row(), outside, row(), row('')]
    sheet, model, events = FakeSheet(rows), FakeModel(), []
    counts = process(sheet, model, DAY, DAY, dry_run, events.append, limit=1)
    assert counts['analyzed'] == 1 and len(model.calls) == 1
    assert model.calls[0]['publication_date'] == '2026-09-03'
    assert set(model.calls[0]) == {'text', 'publication_date'}
    assert len(sheet.writes) == (0 if dry_run else 1)
    if not dry_run:
        assert sheet.writes[0][0] == 2
    assert len(sheet.rows) == 4 and sheet.rows[0] == row() and sheet.rows[1] == outside
    assert sheet.rows[2][12] == row()[12]


def test_blacklist_preserves_row_and_continues():
    sheet, model, events = FakeSheet([row(), row('blocked')]), FakeModel(), []
    counts = process(sheet, model, DAY, DAY, False, events.append)
    assert counts['errors'] == 1 and counts['updated'] == 1
    assert sheet.rows[1] == row('blocked')


def test_unknown_publication_date_and_stage():
    r = row(); r[4] = ''
    sheet, model = FakeSheet([r]), FakeModel(decision(event_stage=None))
    counts = process(sheet, model, DAY, DAY, True, lambda e: None)
    assert model.calls[0]['publication_date'] is None and counts['unresolved_stage'] == 1


def test_invalid_collection_date_stops_before_model():
    r = row(); r[1] = 'не дата'
    model = FakeModel()
    with pytest.raises(StopProcessing):
        process(FakeSheet([r]), model, DAY, DAY, True, lambda e: None)
    assert not model.calls


def test_model_retries_format_and_uses_live_enum_schema(tmp_path):
    key = tmp_path / 'key'; key.write_text('dummy')
    client = GigaChatClient(key, 'prompt', catalog=CATALOG)
    calls = []
    client._authorization = lambda: {}
    def request(method, url, **kwargs):
        calls.append(kwargs['json'])
        return payload(decision(category='Ошибка') if len(calls) == 1 else decision())
    client._json_request = request
    assert client.classify_news({'text': 'news'}) == decision()
    assert len(calls) == 2
    assert calls[0]['response_format']['schema']['properties']['category']['enum'] == list(CATALOG.categories)
    assert 'Предыдущий ответ' in calls[1]['messages'][0]['content']
    assert [m['role'] for m in calls[1]['messages']] == ['system', 'user']
    client.session.close()


@pytest.mark.parametrize('argv', [['--limit', '0'], ['--to-date', '2026-09-03'],
                                 ['--from-date', '2026-09-04', '--to-date', '2026-09-03']])
def test_invalid_cli_arguments(argv):
    with pytest.raises(SystemExit):
        parse_args(argv)
