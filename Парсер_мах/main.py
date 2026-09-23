from __future__ import annotations

import argparse
import os
import re
from datetime import date, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from max_news.api_collector import ApiCollector, MOSCOW
from max_news.auth import ensure_max_ready
from max_news.protocol import MaxProtocol, SOCKET_HOOK
from max_news.google_sheets import ApiSheetWriter, SheetWriteError

ROOT = Path(__file__).resolve().parent


def spreadsheet_id(value: str) -> str:
    match = re.search(r'/spreadsheets/d/([\w-]+)', value)
    if match:
        return match.group(1)
    if re.fullmatch(r'[\w-]+', value.strip()):
        return value.strip()
    raise ValueError('Некорректная ссылка Google Таблицы')


def parse_args(argv=None):
    today = datetime.now(MOSCOW).date()
    parser = argparse.ArgumentParser(description='MAX → Google Таблица: клиентский протокол, без локального архива')
    parser.add_argument('--from-date', type=date.fromisoformat, default=today)
    parser.add_argument('--to-date', type=date.fromisoformat, default=today)
    parser.add_argument('--spreadsheet-id', help='ID или ссылка; по умолчанию — table.txt')
    parser.add_argument('--google-credentials', type=Path, default=ROOT / 'google_credentials.json',
                        help='JSON-ключ сервисного аккаунта Google')
    parser.add_argument('--check-sheet', action='store_true',
                        help='Проверить чтение таблицы через API без запуска MAX и записи')
    parser.add_argument('--source', action='append', help='Точное название из листа «Источники»')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--headless', dest='headed', action='store_false')
    mode.add_argument('--headed', dest='headed', action='store_true')
    parser.set_defaults(headed=True)
    write = parser.add_mutually_exclusive_group()
    write.add_argument('--dry-run', dest='write_sheet', action='store_false', help='Проверка сбора без записи')
    write.add_argument('--write-sheet', dest='write_sheet', action='store_true', help='Запись через Google Sheets API (по умолчанию)')
    parser.set_defaults(write_sheet=True)
    parser.add_argument('--delay-min', type=float, default=1.0, help='Минимальная пауза между запросами истории')
    parser.add_argument('--delay-max', type=float, default=2.0)
    args = parser.parse_args(argv)
    if args.from_date > args.to_date:
        parser.error('--from-date не может быть позже --to-date')
    if args.delay_min < 0 or args.delay_max < args.delay_min:
        parser.error('Некорректный диапазон задержек')
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    target = spreadsheet_id(args.spreadsheet_id or (ROOT / 'table.txt').read_text().strip())
    sheet = ApiSheetWriter(target, args.google_credentials)
    try:
        rows = sheet.check_access()
        sources = sheet.load_sources()
        if args.check_sheet:
            print(f'Google Sheets API: чтение доступно; строк новостей: {len(rows)}; '
                  f'активных источников MAX: {len(sources)}. Запись не выполнялась.', flush=True)
            return 0
        if args.source:
            requested = set(args.source)
            sources = [source for source in sources if source.name in requested]
            missing = requested - {source.name for source in sources}
            if missing:
                raise ValueError(f'Источники не найдены в таблице: {", ".join(sorted(missing))}')
        return collect(args, sources, sheet)
    finally:
        sheet.close()


def collect(args, sources, sheet) -> int:
    print(f'Источников MAX: {len(sources)}. Период: {args.from_date} — {args.to_date}', flush=True)
    print('Режим: ' + ('запись в таблицу' if args.write_sheet else 'проверка без записи'), flush=True)
    collected = written = completed = 0
    failures = []
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            ROOT / '.data' / 'max-profile', headless=not args.headed,
            viewport={'width': 1440, 'height': 1000}, locale='ru-RU', timezone_id='Europe/Moscow')
        try:
            context.add_init_script(SOCKET_HOOK)
            page = context.pages[0] if context.pages else context.new_page()
            for old in context.pages[1:]:
                old.close()
            ensure_max_ready(page, headless=not args.headed)
            page.wait_for_function('window.__maxNewsProtocol?.ready()', timeout=30000)
            collector = ApiCollector(MaxProtocol(page), args.delay_min, args.delay_max)
            writer = sheet if args.write_sheet else None
            for index, source in enumerate(sources, 1):
                print(f'[{index}/{len(sources)}] {source.name}', flush=True)
                count = 0
                try:
                    for batch in collector.batches(source, args.from_date, args.to_date):
                        count += len(batch)
                        collected += len(batch)
                        if writer:
                            # A write error stops the run; a retry re-reads MAX and
                            # the sheet instead of trusting an in-memory checkpoint.
                            written += writer.append(batch, datetime.now(MOSCOW).date())
                        print(f'  получено: {count}; подтверждено новых строк всего: {written}', flush=True)
                    completed += 1
                    print(f'  период проверен полностью; публикаций: {count}', flush=True)
                except SheetWriteError:
                    raise
                except Exception as error:
                    failures.append(source.name)
                    print(f'  НЕ ЗАВЕРШЁН: {error}', flush=True)
                    if page.is_closed():
                        break
            print(f'Проверено каналов: {completed}/{len(sources)}; публикаций: {collected}; добавлено: {written}; дублей по тексту: {sheet.text_duplicates}; ошибок: {len(failures)}', flush=True)
            if failures:
                print('Повторите тот же период для незавершённых источников: ' + '; '.join(failures), flush=True)
            return 1 if failures else 0
        finally:
            context.close()


if __name__ == '__main__':
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / '.playwright-browsers'))
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\nОстановлено. Подтверждённые строки остались в таблице. Повторите тот же период.')
        raise SystemExit(130)
    except Exception as error:
        print(f'Ошибка: {error}\nПовторите тот же период: данные будут получены заново, существующие ссылки проверены.')
        raise SystemExit(1)
