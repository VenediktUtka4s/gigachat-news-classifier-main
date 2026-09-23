from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urlsplit

from api import GigaChatClient as Transport, StopProcessing


FIELDS = {
    'main_municipality': 2, 'additional_municipalities': 3,
    'event_date': 5, 'category': 10, 'event_stage': 11,
    'key_facts': 13, 'people': 14, 'organizations': 15, 'object_or_locality': 17,
}
LIST_FIELDS = {'additional_municipalities', 'key_facts', 'people', 'organizations'}


class InvalidDecision(StopProcessing):
    """A row-level model error; never write this response."""


@dataclass(frozen=True)
class Catalog:
    municipalities: tuple[str, ...]
    categories: tuple[str, ...]
    stages: tuple[str, ...]

    def __post_init__(self):
        for values in (self.municipalities, self.categories, self.stages):
            if not values or len(set(values)) != len(values) or any(not v.strip() for v in values):
                raise StopProcessing('Пустой или некорректный справочник таблицы')
        if 'Республика Коми' not in self.municipalities or 'Другое' not in self.categories:
            raise StopProcessing('В справочниках нет «Республика Коми» или категории «Другое»')

    def as_dict(self):
        return {'municipalities': list(self.municipalities),
                'categories': list(self.categories), 'stages': list(self.stages)}

    def schema(self):
        nullable = lambda values: {'type': ['string', 'null'], 'enum': [*values, None]}
        properties = {
            'main_municipality': nullable(self.municipalities),
            'category': {'type': 'string', 'enum': list(self.categories)},
            'event_stage': nullable(self.stages),
            'additional_municipalities': {'type': 'array', 'items': {
                'type': 'string', 'enum': list(self.municipalities)}},
            'event_date': {'type': ['string', 'null'], 'description': 'YYYY-MM-DD или null'},
            'object_or_locality': {'type': ['string', 'null']},
        }
        for key in ('key_facts', 'people', 'organizations'):
            properties[key] = {'type': 'array', 'items': {'type': 'string'}}
        return {'type': 'object', 'properties': properties,
                'required': list(properties), 'additionalProperties': False}


def parse_date(value: str) -> date:
    for fmt in ('%d.%m.%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError('Ожидается дата DD.MM.YYYY или YYYY-MM-DD')


def validate_decision(result, catalog: Catalog) -> dict:
    if not isinstance(result, dict) or set(result) != set(FIELDS):
        raise InvalidDecision('Неверный набор полей JSON')
    for key, values, nullable in (
        ('main_municipality', catalog.municipalities, True),
        ('category', catalog.categories, False), ('event_stage', catalog.stages, True),
    ):
        value = result[key]
        if value is None and nullable:
            continue
        if not isinstance(value, str) or value not in values:
            raise InvalidDecision(f'{key}: требуется точное значение из справочника')
    for key in LIST_FIELDS:
        values = result[key]
        if (not isinstance(values, list) or len(values) > 50
                or any(not isinstance(v, str) or not v.strip() or len(v) > 3000 for v in values)):
            raise InvalidDecision(f'{key}: требуется список непустых строк (до 50)')
        if len(set(values)) != len(values):
            raise InvalidDecision(f'{key}: повторяющиеся элементы')
    additional = result['additional_municipalities']
    if any(v not in catalog.municipalities for v in additional):
        raise InvalidDecision('additional_municipalities: значение отсутствует в справочнике')
    if result['main_municipality'] in additional:
        raise InvalidDecision('Основной муниципалитет повторён в дополнительных')
    value = result['event_date']
    if value is not None:
        try:
            if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError
            date.fromisoformat(value)
        except ValueError:
            raise InvalidDecision('event_date: нужна существующая дата YYYY-MM-DD или null') from None
    value = result['object_or_locality']
    if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 3000):
        raise InvalidDecision('object_or_locality: нужна непустая строка или null')
    return result


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def parse_response(payload, catalog):
    try:
        choices = payload['choices']
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError
        choice = choices[0]
        reason = choice.get('finish_reason')
        if reason != 'stop':
            reason = reason if reason in ('blacklist', 'length', 'error', 'function_call') else 'unknown'
            raise InvalidDecision(f'GigaChat: finish_reason={reason}', details={'finish_reason': reason})
        message = choice['message']
        if message.get('role') != 'assistant' or message.get('function_call'):
            raise ValueError
        result = json.loads(message['content'], object_pairs_hook=_unique_pairs)
    except (KeyError, TypeError, ValueError, IndexError):
        raise InvalidDecision('GigaChat вернул некорректный JSON-ответ') from None
    return validate_decision(result, catalog)


class GigaChatClient(Transport):
    def __init__(self, *args, catalog: Catalog | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.catalog = catalog
        self.correction = None

    def _classify_once(self, text):
        prompt = self.prompt
        if self.correction:
            prompt += ('\n\nПредыдущий ответ не прошёл проверку: ' + self.correction +
                       '. Выполни извлечение заново, соблюдая схему и справочники. '
                       'Для отсутствующих списков возвращай [], а не [""].')
        # GigaChat accepts one system message at the start of the conversation.
        messages = [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': text}]
        payload = self._json_request('POST', 'https://api.giga.chat/v1/chat/completions',
            headers=self._authorization(), json={
                'model': self.model, 'messages': messages, 'temperature': 0.1,
                'max_tokens': 2500, 'stream': False,
                'response_format': {'type': 'json_schema', 'schema': self.catalog.schema(), 'strict': True}})
        return parse_response(payload, self.catalog)

    def classify_news(self, news):
        text = json.dumps(news, ensure_ascii=False)
        self.correction = None
        for attempt in range(3):
            try:
                return super().classify(text)
            except InvalidDecision as error:
                if error.details.get('finish_reason') == 'blacklist' or attempt == 2:
                    raise
                self.correction = str(error)
                if self.on_retry:
                    self.on_retry({'action': 'format_retry', 'retry': attempt + 1, 'error': str(error)})


def source_municipality(source: str, news_url: str, sources, catalog: Catalog):
    """Registry data only. A URL can disambiguate equal source names."""
    candidates = [s for s in sources if s['name'].strip().casefold() == source.strip().casefold()]
    def belongs(base):
        a, b = urlsplit(base), urlsplit(news_url)
        return (a.scheme in ('http', 'https') and a.netloc.casefold() == b.netloc.casefold()
                and bool(a.path.strip('/'))
                and (b.path == a.path.rstrip('/') or b.path.startswith(a.path.rstrip('/') + '/')))
    by_url = [s for s in (candidates or sources) if belongs(s['url'])]
    if by_url:
        candidates = by_url
    values = {s['municipality'] for s in candidates}
    if len(values) == 1 and next(iter(values)) in catalog.municipalities:
        return next(iter(values))
    return 'Республика Коми'


def plan_changes(row, result, fallback, catalog, overwrite=False):
    validate_decision(result, catalog)
    result = dict(result)
    result['main_municipality'] = result['main_municipality'] or fallback
    if result['event_stage'] is None and 'Другое' in catalog.stages:
        result['event_stage'] = 'Другое'
    validate_decision({**result, 'additional_municipalities': []}, catalog)
    # Existing manually entered C is preserved in fill-empty mode.
    effective_main = row[2] if row[2] and not overwrite else result['main_municipality']
    result['additional_municipalities'] = [v for v in result['additional_municipalities'] if v != effective_main]
    changes = {}
    for key, col in FIELDS.items():
        value = result[key]
        if value is None or value == [] or (row[col] and not overwrite):
            continue
        if key in LIST_FIELDS:
            value = ('; ' if key == 'additional_municipalities' else '\n').join(value)
        elif key == 'event_date':
            value = date.fromisoformat(value).strftime('%d.%m.%Y')
        if value != row[col]:
            changes[col] = value
    return changes


def process(sheet, client, start, end, dry_run, emit, limit=None, overwrite=False):
    rows = sheet.read_rows()
    catalog, sources = sheet.catalog, sheet.sources
    indexes = []
    for index, row in enumerate(rows):
        if not any(row):
            continue
        try:
            collected = parse_date(row[1])
        except ValueError:
            raise StopProcessing(f'Строка {index + 6}: некорректная дата сбора B') from None
        if start <= collected <= end:
            indexes.append(index)
    indexes.reverse()
    counts = dict(selected=len(indexes), analyzed=0, updated=0, would_update=0, skipped=0, errors=0, unresolved_stage=0)
    for index in indexes:
        row = rows[index]
        record = {'row': index + 6, 'url': row[7], 'dry_run': dry_run}
        if not row[12].strip() or (not overwrite and all(row[col] for col in FIELDS.values())):
            counts['skipped'] += 1
            emit({**record, 'action': 'skipped', 'reason': 'empty_text' if not row[12].strip() else 'filled'})
            continue
        if limit is not None and counts['analyzed'] >= limit:
            break
        fallback = source_municipality(row[6], row[7], sources, catalog)
        try:
            published = parse_date(row[4]).isoformat()
        except ValueError:
            published = None
        # Source attribution is handled in code. Passing the author's name to the
        # model caused it to invent a mention in people, despite prompt rules.
        news = {'text': row[12], 'publication_date': published}
        counts['analyzed'] += 1
        try:
            result = client.classify_news(news)
            changes = plan_changes(row, result, fallback, catalog, overwrite)
        except InvalidDecision as error:
            counts['errors'] += 1
            emit({**record, 'action': 'manual_review', 'error': str(error), 'details': error.details})
            continue
        if result['event_stage'] is None and not row[11] and 11 not in changes:
            counts['unresolved_stage'] += 1
        record.update(decision=result, fallback_municipality=fallback,
                      changes={chr(65 + col): value for col, value in changes.items()})
        if not changes:
            emit({**record, 'action': 'no_changes'})
        elif dry_run:
            emit({**record, 'action': 'would_update'})
            counts['would_update'] += 1
        else:
            emit({**record, 'action': 'write_pending'})
            sheet.update_fields(index, row, changes)
            emit({**record, 'action': 'updated'})
            counts['updated'] += 1
    return counts
