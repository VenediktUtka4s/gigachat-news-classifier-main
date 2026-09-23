from __future__ import annotations

import re
from datetime import date, datetime

from api import StopProcessing
from classifier import Catalog, FIELDS
from sheets_transport import NewsSheet as Transport


HEADERS = ['Сборщик', 'Дата сбора', 'Основной муниципалитет',
           'Дополнительные муниципалитеты', 'Дата публикации', 'Дата события',
           'Источник', 'Прямая ссылка', 'Дополнительные ссылки', 'Заголовок',
           'Категория', 'Стадия события', 'Краткое фактическое резюме',
           'Ключевые цифры и факты', 'Упомянутые лица', 'Организации',
           'ИНН из источника', 'Объект/населённый пункт']


def padded(values, size=None):
    if not isinstance(values, list) or any(not isinstance(row, list) or len(row) > 18 for row in values):
        raise StopProcessing('Некорректные строки Google Sheets')
    rows = [[str(v) if v is not None else '' for v in row] + [''] * (18 - len(row)) for row in values]
    if size is not None:
        if len(rows) > size:
            raise StopProcessing('Google Sheets вернул лишние строки')
        rows.extend([[''] * 18 for _ in range(size - len(rows))])
    return rows


class NewsSheet(Transport):
    def check_access(self):
        data = self._request('GET', self.base_url, params={'fields':
            'sheets(properties(sheetId,title,sheetType,gridProperties(rowCount,columnCount)))'})
        try:
            self.tabs = {s['properties']['title']: s['properties'] for s in data['sheets']}
            for name, width, height in [('Новости', 18, 6), ('Источники', 8, 6), ('Справочники', 4, 24)]:
                tab = self.tabs[name]
                if (tab['sheetType'] != 'GRID' or tab['gridProperties']['columnCount'] < width
                        or tab['gridProperties']['rowCount'] < height):
                    raise ValueError
            self.sheet_id = self.tabs['Новости']['sheetId']
            self.grid_rows = self.tabs['Новости']['gridProperties']['rowCount']
        except (KeyError, TypeError, ValueError):
            raise StopProcessing('Не подтверждены листы и размеры таблицы') from None

    def values(self, ranges, render='FORMATTED_VALUE'):
        data = self._request('GET', f'{self.base_url}/values:batchGet', params={
            'ranges': ranges, 'valueRenderOption': render, 'majorDimension': 'ROWS'})
        blocks = data.get('valueRanges')
        if not isinstance(blocks, list) or len(blocks) != len(ranges):
            raise StopProcessing('Не подтверждены прочитанные диапазоны Google Sheets')
        return [b.get('values', []) for b in blocks]

    def cells(self, ranges):
        data = self._request('GET', self.base_url, params={
            'ranges': ranges, 'includeGridData': 'true', 'fields':
            'sheets(properties(title),data(startRow,startColumn,rowData(values(formattedValue,userEnteredValue,dataValidation))))'})
        result = {}
        for sheet in data.get('sheets', []):
            for block in sheet.get('data', []):
                for r, row in enumerate(block.get('rowData', []), block.get('startRow', 0) + 1):
                    for c, cell in enumerate(row.get('values', []), block.get('startColumn', 0)):
                        result[(sheet['properties']['title'], r, c)] = cell
        return result

    def rule_reference(self, rule):
        condition = rule.get('condition', {})
        kind, entries = condition.get('type'), condition.get('values', [])
        if kind == 'ONE_OF_LIST':
            return None
        elif kind == 'ONE_OF_RANGE' and len(entries) == 1:
            ref = entries[0].get('userEnteredValue', '').removeprefix('=')
            # Only bounded, existing, single-column references are accepted.
            match = re.fullmatch(r"(?:'([^']+)'|([^'!]+))!\$?([A-Z]+)\$?(\d+):\$?([A-Z]+)\$?(\d+)", ref)
            if not match:
                raise StopProcessing('Неподдерживаемый диапазон выпадающего списка')
            name = match[1] or match[2]
            first, last = int(match[4]), int(match[6])
            if (name not in self.tabs or match[3] != match[5] or first < 1 or last < first
                    or last > self.tabs[name]['gridProperties']['rowCount']):
                raise StopProcessing('Некорректные границы справочника')
            return ref
        else:
            raise StopProcessing('Не поддерживается правило проверки данных; запись отменена')

    def allowed_values(self, rule):
        ref = self.rule_reference(rule)
        if ref is None:
            values = [v['userEnteredValue'] for v in rule['condition']['values']]
        else:
            cached = getattr(self, '_reference_values', {})
            rows = cached[ref] if ref in cached else self.values([ref])[0]
            values = [str(row[0]) for row in rows if row and row[0] != '']
        if not values or any(not isinstance(v, str) or not v.strip() for v in values):
            raise StopProcessing('Пустой выпадающий список')
        return tuple(dict.fromkeys(values))

    def read_catalog(self, validation=None):
        last = self.tabs['Справочники']['gridProperties']['rowCount']
        if validation is None:
            validation = self.cells(["'Новости'!K6:L6"])
        try:
            rules = [validation[('Новости', 6, col)]['dataValidation'] for col in (10, 11)]
        except KeyError:
            raise StopProcessing('Не найдены выпадающие списки K6 и L6') from None
        refs = list(dict.fromkeys(ref for rule in rules if (ref := self.rule_reference(rule)) is not None))
        blocks = self.values(["'Справочники'!A3:D3", f"'Справочники'!A4:A{last}", *refs])
        headers, municipalities = blocks[:2]
        # Renew this cache on every catalog read, including immediately before each write.
        self._reference_values = dict(zip(refs, blocks[2:]))
        if not headers or headers[0][:4] != ['Муниципалитеты', 'Категории', 'Платформы', 'Стадии']:
            raise StopProcessing('Изменились заголовки справочников A3:D3')
        categories, stages = [self.allowed_values(rule) for rule in rules]
        return Catalog(tuple(row[0] for row in municipalities if row and row[0] != ''), categories, stages)

    def inspect(self):
        self.check_access()
        header, source_header = self.values(["'Новости'!A5:R5", "'Источники'!C5:H5"])
        if not header or padded(header)[0] != HEADERS:
            raise StopProcessing('Изменились заголовки A5:R5 листа «Новости»')
        if not source_header or source_header[0] != [
                'Муниципалитет', 'Орган/владелец', 'Тип источника', 'Платформа', 'Название', 'Основная ссылка']:
            raise StopProcessing('Изменились заголовки реестра источников C5:H5')
        self.catalog = self.read_catalog()
        last = self.tabs['Источники']['gridProperties']['rowCount']
        values = self.values([f"'Источники'!C6:H{last}"])[0]
        self.sources = []
        for row in values:
            row = row + [''] * (6 - len(row))
            if row[4]:
                self.sources.append({'municipality': row[0], 'name': row[4], 'url': row[5]})
        return self.catalog

    def read_rows(self):
        if not hasattr(self, 'catalog'):
            self.inspect()
        rows = padded(self.values([f"'Новости'!A5:R{self.grid_rows}"])[0])
        if not rows or rows[0] != HEADERS:
            raise StopProcessing('Изменились заголовки A5:R5')
        rows = rows[1:]
        while rows and not any(rows[-1]):
            rows.pop()
        return rows

    def update_fields(self, index, expected, changes):
        if (not changes or not isinstance(index, int) or index < 0 or index + 6 > self.grid_rows
                or len(expected) != 18 or not set(changes).issubset(FIELDS.values())
                or any(not isinstance(v, str) or not v.strip() or len(v) > 45000 for v in changes.values())):
            raise StopProcessing('Недопустимые ячейки или значения для записи')
        row_number = index + 6
        target = f"'Новости'!A{row_number}:R{row_number}"
        current = self.cells(["'Новости'!A5:R5", "'Новости'!K6:L6", target])
        if self.read_catalog(current) != self.catalog:
            raise StopProcessing('Справочники изменились во время анализа; перезапустите классификатор')
        header = [current.get(('Новости', 5, c), {}).get('formattedValue', '') for c in range(18)]
        actual = [current.get(('Новости', row_number, c), {}).get('formattedValue', '') for c in range(18)]
        if header != HEADERS or actual != expected:
            raise StopProcessing(f'Строка {row_number} или заголовки изменились; запись отменена')
        for col, value in changes.items():
            cell = current.get(('Новости', row_number, col), {})
            if 'formulaValue' in cell.get('userEnteredValue', {}):
                raise StopProcessing(f'В {chr(65 + col)}{row_number} формула; запись отменена')
            rule = cell.get('dataValidation')
            if col in (10, 11) and not rule:
                raise StopProcessing('В целевой ячейке отсутствует выпадающий список K/L')
            if col == 2 and value not in self.catalog.municipalities:
                raise StopProcessing('Муниципалитет отсутствует в справочнике')
            if col == 3 and any(v not in self.catalog.municipalities for v in value.split('; ')):
                raise StopProcessing('Дополнительный муниципалитет отсутствует в справочнике')
            if col == 10 and value not in self.catalog.categories or col == 11 and value not in self.catalog.stages:
                raise StopProcessing('Категория или стадия отсутствует в справочнике')
            if rule:
                kind = rule.get('condition', {}).get('type')
                if kind == 'DATE_IS_VALID' and col == 5:
                    continue
                if value not in self.allowed_values(rule):
                    raise StopProcessing(f'Значение не разрешено списком {chr(65 + col)}{row_number}')
        requests = []
        for col, value in sorted(changes.items()):
            cell_data = {'userEnteredValue': {'stringValue': value}}
            fields = 'userEnteredValue'
            if col == 5:
                try:
                    if not re.fullmatch(r'\d{2}\.\d{2}\.\d{4}', value):
                        raise ValueError
                    event_date = datetime.strptime(value, '%d.%m.%Y').date()
                except ValueError:
                    raise StopProcessing('Некорректная дата события для записи в F') from None
                cell_data = {
                    'userEnteredValue': {'numberValue': (event_date - date(1899, 12, 30)).days},
                    'userEnteredFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'dd.mm.yyyy'}},
                }
                fields += ',userEnteredFormat.numberFormat'
            requests.append({'updateCells': {
                'range': {'sheetId': self.sheet_id, 'startRowIndex': row_number - 1,
                          'endRowIndex': row_number, 'startColumnIndex': col, 'endColumnIndex': col + 1},
                'rows': [{'values': [cell_data]}],
                'fields': fields}})
        # Exact cells only. Dates are numeric; other fields remain literal strings.
        response = self._request('POST', f'{self.base_url}:batchUpdate', json={'requests': requests})
        if not isinstance(response.get('replies'), list) or len(response['replies']) != len(changes):
            raise StopProcessing('Нет подтверждения записи; запрос не повторяется')
        wanted = expected[:]
        for col, value in changes.items():
            wanted[col] = value
        actual = padded(self.values([target])[0], 1)[0]
        if actual != wanted:
            raise StopProcessing('Не подтверждено содержимое строки после записи; обработка остановлена')
