from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import certifi

from classifier import DEFAULT_MODEL, GigaChatClient, StopProcessing, build_prompt, process
from sheet import NewsSheet

ROOT = Path(__file__).resolve().parent
MOSCOW = ZoneInfo('Europe/Moscow')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='GigaChat → отбор новостей по ДАТЕ СБОРА (B), текст из M')
    parser.add_argument('--from-date', type=date.fromisoformat, help='Начало периода включительно, YYYY-MM-DD')
    parser.add_argument('--to-date', type=date.fromisoformat, help='Конец периода включительно; без него — один день')
    parser.add_argument('--dry-run', action='store_true', help='Показать очистку и решения модели, не изменяя таблицу')
    parser.add_argument('--limit', type=int, help='Обработать не более N строк выбранного периода, снизу вверх')
    parser.add_argument('--model', default=DEFAULT_MODEL, help=f'Имя модели API (по умолчанию {DEFAULT_MODEL})')
    parser.add_argument('--scope', choices=['GIGACHAT_API_PERS', 'GIGACHAT_API_B2B', 'GIGACHAT_API_CORP'],
                        default='GIGACHAT_API_PERS')
    parser.add_argument('--list-models', action='store_true', help='Проверить ключ и вывести модели, не читать таблицу')
    parser.add_argument('--api-key-file', type=Path, default=ROOT / 'api_key.txt')
    parser.add_argument('--criteria-file', type=Path, default=ROOT / 'request.txt')
    parser.add_argument('--ca-bundle', help='Доверенные сертификаты в PEM; по умолчанию системная настройка requests')
    parser.add_argument('--spreadsheet-id', help='ID или ссылка; по умолчанию table.txt')
    parser.add_argument('--google-credentials', type=Path, default=ROOT / 'google_credentials.json',
                        help='JSON-ключ сервисного аккаунта Google')
    parser.add_argument('--check-sheet', action='store_true', help='Проверить чтение таблицы через API, без модели и удаления')
    args = parser.parse_args(argv)
    if args.to_date and not args.from_date:
        parser.error('Для --to-date нужно указать --from-date')
    args.from_date = args.from_date or datetime.now(MOSCOW).date()
    args.to_date = args.to_date or args.from_date
    if args.from_date > args.to_date:
        parser.error('--from-date не может быть позже --to-date')
    if args.limit is not None and args.limit < 1:
        parser.error('--limit должен быть больше нуля')
    return args


def target_id(value: str) -> str:
    value = value.strip()
    match = re.search(r'/spreadsheets/d/([\w-]+)', value)
    if match:
        return match[1]
    if re.fullmatch(r'[\w-]+', value):
        return value
    raise StopProcessing('Некорректная ссылка таблицы')


def main(argv=None) -> int:
    args = parse_args(argv)
    sheet = None
    if not args.list_models:
        target = target_id(args.spreadsheet_id or (ROOT / 'table.txt').read_text())
        sheet = NewsSheet(target, args.google_credentials)
        if args.check_sheet:
            try:
                rows = sheet.read_rows()
                print(f'Google Sheets API: лист «Новости» доступен для чтения; строк данных: {len(rows)}. '
                      'Удаление не выполнялось.')
                return 0
            finally:
                sheet.close()
    prompt = build_prompt(args.criteria_file.read_text(encoding='utf-8-sig'))
    ca_bundle = args.ca_bundle or os.environ.get('REQUESTS_CA_BUNDLE') or os.environ.get('CURL_CA_BUNDLE')
    if not ca_bundle:
        # Trust the official Russian CA only inside this application's API client.
        folder = ROOT / '.data'
        folder.mkdir(exist_ok=True)
        ca_path = folder / 'gigachat-ca.pem'
        ca_path.write_bytes(Path(certifi.where()).read_bytes() + b'\n' +
                            (ROOT / 'russian_trusted_root_ca.pem').read_bytes())
        ca_bundle = str(ca_path)
    client = GigaChatClient(args.api_key_file, prompt, args.model, args.scope, ca_bundle)
    if args.list_models:
        print('\n'.join(client.models()))
        return 0
    target = target_id(args.spreadsheet_id or (ROOT / 'table.txt').read_text())
    folder = ROOT / '.data'
    folder.mkdir(exist_ok=True)
    with (folder / f'{target}.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StopProcessing('Классификатор для этой таблицы уже запущен') from None
        stamp = datetime.now(MOSCOW).strftime('%Y%m%d-%H%M%S')
        log_path = folder / f'run-{stamp}-{uuid4().hex[:8]}.jsonl'
        print(f'Дата сбора (B): {args.from_date} — {args.to_date}; модель: {args.model}', flush=True)
        print('Режим: ' + ('проверка без изменений таблицы' if args.dry_run else 'Очистка M и УДАЛЕНИЕ неполезных строк'), flush=True)
        print(f'Журнал: {log_path}', flush=True)
        with log_path.open('x', encoding='utf-8') as log:
            def emit(event):
                record = {'time': datetime.now(MOSCOW).isoformat(), **event}
                log.write(json.dumps(record, ensure_ascii=False) + '\n')
                log.flush()
                os.fsync(log.fileno())
                if event.get('action') == 'classification_retry':
                    print(f"GigaChat: повтор {event['retry']}/{event['max_retries']} через "
                          f"{event['delay_seconds']} с после {event['details']['network_error']}", flush=True)
                elif event.get('action') == 'manual_review':
                    print(f"Строка {event['row']}: manual_review — blacklist; оставлена для ручной проверки — {event['url']}", flush=True)
                elif event.get('action') == 'classification_error':
                    print(f"Строка {event['row']}: classification_error — {event['error']} — {event['url']}", flush=True)
                elif event.get('reason') == 'empty_text':
                    print(f"Строка {event['row']}: {event['action']} — пустой текст после очистки; без GigaChat", flush=True)
                elif event.get('action') in {'clean_text_pending', 'text_cleaned', 'would_clean_text'}:
                    print(f"Строка {event['row']}: {event['action']} — очистка смайликов и пробелов в M", flush=True)
                elif 'row' in event and 'decision' in event:
                    print(f"Строка {event['row']}: {event['action']} — {'полезная' if event['decision']['useful'] else 'неполезная'}", flush=True)
                elif 'row' in event:
                    print(f"Строка {event['row']}: {event['action']} — {event['url']}", flush=True)
            emit({'action': 'start', 'spreadsheet_id': target, 'model': args.model,
                  'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                  'from_date': str(args.from_date), 'to_date': str(args.to_date), 'dry_run': args.dry_run})
            try:
                sheet.check_access()
                if args.model not in client.models():
                    raise StopProcessing('Выбранная модель отсутствует в списке доступных; используйте --list-models')
                client.on_retry = emit
                counts = process(sheet, client, args.from_date, args.to_date, args.dry_run, emit, args.limit)
                emit({'action': 'complete', **counts})
                print(f"Готово: выбрано {counts['selected']}, оставлено {counts['kept']}, "
                      f"удалено {counts['deleted']}, к удалению в dry-run {counts['would_delete']}, "
                      f"нужна ручная проверка {counts['manual_review']}")
                return 0
            except BaseException as error:
                emit({'action': 'stopped', 'error_type': type(error).__name__,
                      **({'error': str(error), 'details': error.details}
                         if isinstance(error, StopProcessing) else {})})
                raise
            finally:
                sheet.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nОстановлено пользователем. Уже подтверждённые удаления сохранены.')
        sys.exit(130)
    except StopProcessing as error:
        print(f'ОСТАНОВЛЕНО: {error}', file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        # Do not dump arbitrary HTTP exceptions, payloads, credentials or tracebacks.
        print(f'ОСТАНОВЛЕНО: {type(error).__name__}. Проверьте файлы и доступ к API; '
              'дальнейшие строки не обрабатываются.', file=sys.stderr)
        sys.exit(1)
