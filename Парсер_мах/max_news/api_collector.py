from __future__ import annotations

import base64
import random
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import Post, Source
from .protocol import MaxProtocol, ProtocolError
from .text import first_sentence

MOSCOW = ZoneInfo('Europe/Moscow')


def date_bounds(start: date, end: date) -> tuple[int, int]:
    if start > end:
        raise ValueError('Начальная дата позже конечной')
    return (int(datetime.combine(start, time.min, MOSCOW).timestamp() * 1000),
            int(datetime.combine(end + timedelta(days=1), time.min, MOSCOW).timestamp() * 1000))


def message_to_post(source: Source, message: dict) -> Post | None:
    if message.get('type') in {'CONTROL', 'SYSTEM'} or message.get('status') == 'REMOVED':
        return None
    mid, timestamp = message.get('id'), message.get('time')
    if not isinstance(mid, int) or not isinstance(timestamp, int):
        raise ProtocolError('У публикации нет корректного идентификатора или даты')
    published = datetime.fromtimestamp(timestamp / 1000, MOSCOW)
    text = message.get('text') or ''
    link = message.get('link') or {}
    if isinstance(link, dict) and link.get('type') == 'FORWARD':
        original = link.get('message') or {}
        original_text = original.get('text') or ''
        if original_text and original_text != text:
            text = f'{text}\n\n{original_text}' if text else original_text
    if not isinstance(text, str):
        raise ProtocolError('MAX вернул неожиданный формат текста')
    encoded = base64.urlsafe_b64encode(mid.to_bytes(8, 'big', signed=False)).decode().rstrip('=')
    return Post(source.name, source.url, published.date(), published.time().replace(tzinfo=None),
                f'{source.url.rstrip("/")}/{encoded}', first_sentence(text) or 'Без текста', text)


class ApiCollector:
    def __init__(self, protocol: MaxProtocol, delay_min=1.0, delay_max=2.0):
        self.protocol = protocol
        if delay_min < 0 or delay_max < delay_min:
            raise ValueError('Некорректные задержки')
        self.delay_min, self.delay_max = delay_min, delay_max

    def batches(self, source: Source, start: date, end: date):
        lower, upper = date_bounds(start, end)
        result = self.protocol.request(89, {'link': source.url})
        chat = result.get('chat')
        if not isinstance(chat, dict) or chat.get('type') != 'CHANNEL' or not isinstance(chat.get('id'), int):
            raise ProtocolError('Ссылка не разрешилась в канал MAX')
        cursor = upper - 1
        seen: set[int] = set()
        page_size = 100
        for _ in range(10000):
            data = self.protocol.request(49, {'chatId': chat['id'], 'from': cursor,
                'forward': 0, 'backward': page_size, 'getMessages': True})
            messages = data.get('messages')
            if not isinstance(messages, list):
                raise ProtocolError('В ответе истории отсутствует список сообщений')
            if not messages:
                return
            times = []
            batch = []
            for message in messages:
                if not isinstance(message, dict) or not isinstance(message.get('time'), int):
                    raise ProtocolError('Некорректный элемент истории MAX')
                timestamp = message['time']
                times.append(timestamp)
                if timestamp > cursor:
                    raise ProtocolError('MAX вернул историю вне запрошенной границы')
                mid = message.get('id')
                if not isinstance(mid, int):
                    raise ProtocolError('В истории отсутствует идентификатор сообщения')
                if mid in seen:
                    continue
                seen.add(mid)
                if lower <= timestamp < upper:
                    post = message_to_post(source, message)
                    if post:
                        batch.append(post)
            if batch:
                yield sorted(batch, key=lambda p: (p.publication_date, p.publication_time))
            oldest = min(times)
            if oldest < lower:
                return
            if oldest == cursor:
                # Inclusive overlap preserves messages sharing a timestamp. A full
                # single-timestamp page cannot establish completeness: fail loudly.
                if len(messages) >= page_size:
                    raise ProtocolError('Слишком много сообщений с одинаковым временем; полнота не подтверждена')
                cursor -= 1
            else:
                cursor = oldest
            self.protocol.page.wait_for_timeout(1000 * random.uniform(self.delay_min, self.delay_max))
        raise ProtocolError('Достигнут лимит страниц; период собран не полностью')
