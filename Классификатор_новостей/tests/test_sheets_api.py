import copy
from datetime import date, timedelta

import pytest

from api import StopProcessing
from sheet import HEADERS, NewsSheet
from test_classifier import CATALOG, row


def rule(values):
    return {'condition': {'type': 'ONE_OF_LIST', 'values': [{'userEnteredValue': v} for v in values]}, 'strict': True}


class FakeApiSheet(NewsSheet):
    def __init__(self):
        self.catalog, self.grid_rows, self.sheet_id = CATALOG, 100, 123
        self.base_url = 'https://example.invalid/sheet'
        self.current = row()
        self.formula = None
        self.writes = []
        self.bad_ack, self.bad_readback = False, False
        self.rules = {10: rule(CATALOG.categories), 11: rule(CATALOG.stages)}
    def read_catalog(self, validation=None):
        return CATALOG
    def cells(self, ranges):
        cells = {('Новости', 5, c): {'formattedValue': v} for c, v in enumerate(HEADERS)}
        for c, value in enumerate(self.current):
            cell = {'formattedValue': value}
            if c in self.rules:
                cell['dataValidation'] = self.rules[c]
            if c == self.formula:
                cell['userEnteredValue'] = {'formulaValue': '=some_formula()'}
            cells[('Новости', 6, c)] = cell
        return cells
    def values(self, ranges, render='FORMATTED_VALUE'):
        if self.bad_readback:
            return [[row('Changed after write')]]
        return [[self.current[:]]]
    def _request(self, method, url, **kwargs):
        assert method == 'POST'
        self.writes.append(copy.deepcopy(kwargs['json']))
        requests = kwargs['json']['requests']
        for request in requests:
            update = request['updateCells']
            col = update['range']['startColumnIndex']
            cell = update['rows'][0]['values'][0]
            if col == 5:
                assert update['fields'] == 'userEnteredValue,userEnteredFormat.numberFormat'
                serial = cell['userEnteredValue']['numberValue']
                self.current[col] = (date(1899, 12, 30) + timedelta(days=serial)).strftime('%d.%m.%Y')
            else:
                assert update['fields'] == 'userEnteredValue'
                self.current[col] = cell['userEnteredValue']['stringValue']
        return {'replies': [] if self.bad_ack else [{} for _ in requests]}


def test_exact_cells_preserve_other_fields_and_literal_strings():
    sheet = FakeApiSheet()
    expected = sheet.current[:]
    sheet.update_fields(0, expected, {2: 'Воркута', 10: 'Другое', 11: 'Анонс', 13: '=1+1'})
    assert len(sheet.writes) == 1
    for c in range(18):
        if c not in (2, 10, 11, 13):
            assert sheet.current[c] == expected[c]
    assert sheet.current[13] == '=1+1'
    assert all(r['updateCells']['range']['startRowIndex'] == 5 for r in sheet.writes[0]['requests'])


def test_event_date_is_numeric_date_with_no_text_apostrophe():
    sheet = FakeApiSheet()
    sheet.update_fields(0, sheet.current[:], {5: '22.08.2026'})
    update = sheet.writes[0]['requests'][0]['updateCells']
    cell = update['rows'][0]['values'][0]
    assert cell['userEnteredValue'] == {'numberValue': 46256}
    assert cell['userEnteredFormat'] == {'numberFormat': {'type': 'DATE', 'pattern': 'dd.mm.yyyy'}}
    assert sheet.current[5] == '22.08.2026'


@pytest.mark.parametrize('value', ["'22.08.2026", '31.02.2026', '2026-08-22', '=TODAY()'])
def test_invalid_dates_never_reach_write_api(value):
    sheet = FakeApiSheet()
    with pytest.raises(StopProcessing):
        sheet.update_fields(0, sheet.current[:], {5: value})
    assert not sheet.writes


@pytest.mark.parametrize('changes', [{12: 'replace text'}, {16: 'INN'}, {18: 'duplicate'},
                                   {2: 'Москва'}, {10: 'Новая категория'}, {11: 'Другое'},
                                   {3: 'Воркута; Москва'}, {13: ''}])
def test_disallowed_writes_never_reach_api(changes):
    sheet = FakeApiSheet()
    with pytest.raises(StopProcessing):
        sheet.update_fields(0, sheet.current[:], changes)
    assert not sheet.writes


def test_concurrent_row_edit_stops_before_write():
    sheet = FakeApiSheet()
    expected = sheet.current[:]
    sheet.current[12] = 'Другая новость'
    with pytest.raises(StopProcessing, match='изменились'):
        sheet.update_fields(0, expected, {2: 'Воркута'})
    assert not sheet.writes


def test_formula_target_is_preserved_even_in_overwrite():
    sheet = FakeApiSheet(); sheet.formula = 13
    with pytest.raises(StopProcessing, match='формула'):
        sheet.update_fields(0, sheet.current[:], {13: 'Новые факты'})
    assert not sheet.writes


def test_per_cell_validation_is_enforced():
    sheet = FakeApiSheet(); sheet.rules[10] = rule(['Образование'])
    with pytest.raises(StopProcessing):
        sheet.update_fields(0, sheet.current[:], {10: 'Другое'})
    assert not sheet.writes


@pytest.mark.parametrize('attribute', ['bad_ack', 'bad_readback'])
def test_ambiguous_write_never_retried(attribute):
    sheet = FakeApiSheet(); setattr(sheet, attribute, True)
    with pytest.raises(StopProcessing):
        sheet.update_fields(0, sheet.current[:], {2: 'Воркута'})
    assert len(sheet.writes) == 1
