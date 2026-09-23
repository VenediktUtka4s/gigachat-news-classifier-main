from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
from contextlib import ExitStack
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import certifi

from api import DEFAULT_MODEL, StopProcessing
from classifier import GigaChatClient, process
from sheet import NewsSheet

ROOT = Path(__file__).resolve().parent
MOSCOW = ZoneInfo('Europe/Moscow')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Второй проход: заполнение C/D/F/K/L/N/O/P/R через GigaChat Ultra; период по B')
    parser.add_argument('--from-date', type=date.fromisoformat, help='Начало периода по дате сбора B, YYYY-MM-DD')
    parser.add_argument('--to-date', type=date.fromisoformat, help='Конец периода включительно; по умолчанию один день')
    parser.add_argument('--dry-run', action='store_true', help='Проанализировать и показать значения без записи в таблицу')
    parser.add_argument('--overwrite', action='store_true', help='Пересчитать заполненные разрешённые поля; пустой результат ничего не стирает')
    parser.add_argument('--limit', type=int, help='Не более N новостей с текстом; снизу вверх')
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--scope', choices=['GIGACHAT_API_PERS', 'GIGACHAT_API_B2B', 'GIGACHAT_API_CORP'], default='GIGACHAT_API_PERS')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--list-models', action='store_true', help='Показать доступные модели, не читать таблицу')
    mode.add_argument('--check-sheet', action='store_true', help='Прочитать структуру, справочники и источники без GigaChat и записи')
    parser.add_argument('--api-key-file', type=Path, default=ROOT / 'api_key.txt')
    parser.add_argument('--criteria-file', type=Path, default=ROOT / 'request.txt')
    parser.add_argument('--ca-bundle', help='Доверенные сертификаты PEM для GigaChat')
    parser.add_argument('--spreadsheet-id', help='ID или ссылка; по умолчанию table.txt')
    parser.add_argument('--google-credentials', type=Path, default=ROOT / 'google_credentials.json')
    args = parser.parse_args(argv)
    if args.to_date and not args.from_date:
        parser.error('Для --to-date укажите --from-date')
    args.from_date = args.from_date or datetime.now(MOSCOW).date()
    args.to_date = args.to_date or args.from_date
    if args.from_date > args.to_date:
        parser.error('--from-date не может быть позже --to-date')
    if args.limit is not None and args.limit < 1:
        parser.error('--limit должен быть больше нуля')
    return args


def target_id(value):
    value = value.strip()
    match = re.search(r'/spreadsheets/d/([\w-]+)', value)
    if match:
        return match[1]
    if re.fullmatch(r'[\w-]+', value):
        return value
    raise StopProcessing('Некорректная ссылка таблицы')


def ca_bundle(args):
    configured = args.ca_bundle or os.environ.get('REQUESTS_CA_BUNDLE') or os.environ.get('CURL_CA_BUNDLE')
    if configured:
        return configured
    path = ROOT / '.data' / 'gigachat-ca.pem'
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(Path(certifi.where()).read_bytes() + b'\n' + (ROOT / 'russian_trusted_root_ca.pem').read_bytes())
    return str(path)


def acquire_locks(stack, target):
    folders = [ROOT / '.data']
    # Respect the existing filter's lock without changing that program.
    sibling = ROOT.parent / 'Фильтр_новостей'
    if sibling.is_dir():
        folders.append(sibling / '.data')
    for folder in sorted(folders):
        folder.mkdir(exist_ok=True)
        lock = stack.enter_context((folder / f'{target}.lock').open('a'))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StopProcessing('Фильтр или классификатор для этой таблицы уже запущен') from None


def main(argv=None):
    args = parse_args(argv)
    with ExitStack() as stack:
        if args.list_models:
            client = GigaChatClient(args.api_key_file, '', args.model, args.scope, ca_bundle(args))
            stack.callback(client.session.close)
            print('\n'.join(client.models()))
            return 0
        target = target_id(args.spreadsheet_id or (ROOT / 'table.txt').read_text(encoding='utf-8-sig'))
        acquire_locks(stack, target)
        sheet = NewsSheet(target, args.google_credentials)
        stack.callback(sheet.close)
        catalog = sheet.inspect()
        if args.check_sheet:
            print('Google Sheets API: структура подтверждена; запись не выполнялась.')
            print(json.dumps(catalog.as_dict(), ensure_ascii=False, indent=2))
            print(f'Источников в реестре: {len(sheet.sources)}')
            return 0
        prompt = args.criteria_file.read_text(encoding='utf-8-sig').strip()
        if not prompt:
            raise StopProcessing('Пустой файл промпта')
        prompt += '\n\nАКТУАЛЬНЫЕ СПРАВОЧНИКИ (автоматически прочитаны из таблицы):\n'
        prompt += json.dumps(catalog.as_dict(), ensure_ascii=False, indent=2)
        client = GigaChatClient(args.api_key_file, prompt, args.model, args.scope, ca_bundle(args), catalog=catalog)
        stack.callback(client.session.close)
        if args.model not in client.models():
            raise StopProcessing('Модель недоступна; проверьте --list-models')
        folder = ROOT / '.data'
        stamp = datetime.now(MOSCOW).strftime('%Y%m%d-%H%M%S')
        log_path = folder / f'run-{stamp}-{uuid4().hex[:8]}.jsonl'
        log = stack.enter_context(log_path.open('x', encoding='utf-8'))
        def emit(event):
            log.write(json.dumps({'time': datetime.now(MOSCOW).isoformat(), **event}, ensure_ascii=False) + '\n')
            log.flush()
            os.fsync(log.fileno())
            if 'row' in event:
                print(f"Строка {event['row']}: {event['action']}", flush=True)
                if event.get('changes'):
                    print(json.dumps(event['changes'], ensure_ascii=False, indent=2), flush=True)
                if event.get('error'):
                    print(event['error'], flush=True)
            elif event['action'] in ('format_retry', 'classification_retry'):
                print(f"Повтор {event['retry']}: {event['error']}", flush=True)
        client.on_retry = emit
        print(f'Дата сбора B: {args.from_date} — {args.to_date}; модель: {args.model}', flush=True)
        print('Режим: ' + ('ПРОСМОТР БЕЗ ЗАПИСИ' if args.dry_run else 'запись C/D/F/K/L/N/O/P/R') +
              ('; пересчёт заполненных' if args.overwrite else '; только пустые ячейки'), flush=True)
        print(f'Журнал: {log_path}', flush=True)
        emit({'action': 'start', 'spreadsheet_id': target, 'model': args.model,
              'from_date': str(args.from_date), 'to_date': str(args.to_date),
              'dry_run': args.dry_run, 'overwrite': args.overwrite,
              'catalog': catalog.as_dict(), 'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest()})
        try:
            counts = process(sheet, client, args.from_date, args.to_date, args.dry_run, emit, args.limit, args.overwrite)
            emit({'action': 'complete', **counts})
            print('Итог: ' + json.dumps(counts, ensure_ascii=False), flush=True)
            # A partial run with rejected model answers is distinguishable in automation.
            return 2 if counts['errors'] or counts['unresolved_stage'] else 0
        except BaseException as error:
            emit({'action': 'stopped', 'error_type': type(error).__name__,
                  **({'error': str(error), 'details': error.details} if isinstance(error, StopProcessing) else {})})
            raise


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nОстановлено. Подтверждённые записи сохранены; проверьте журнал незавершённой записи.')
        sys.exit(130)
    except StopProcessing as error:
        print(f'ОСТАНОВЛЕНО: {error}', file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print(f'ОСТАНОВЛЕНО: {type(error).__name__}. Проверьте файлы настроек и доступ к API.', file=sys.stderr)
        sys.exit(1)
