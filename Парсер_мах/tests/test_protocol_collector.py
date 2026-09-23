from datetime import date
from types import SimpleNamespace

import lz4.block
import msgpack
import pytest

from main import parse_args, spreadsheet_id
from max_news.api_collector import ApiCollector, date_bounds, message_to_post
from max_news.models import Source
from max_news.protocol import decode_frame, encode_frame, ProtocolError

SOURCE = Source('Имя из таблицы', 'https://max.ru/example')


def test_binary_compressed_ids_are_exact():
    identifier = 117206699013512305
    data = msgpack.packb({'id': msgpack.ExtType(1, msgpack.packb(identifier))})
    compressed = lz4.block.compress(data, store_size=False)
    header = bytes([10, 1, 255, 255, 0, 49, 2]) + len(compressed).to_bytes(3, 'big')
    assert decode_frame(header + compressed) == (1, 65535, 49, {'id': identifier})
    assert decode_frame(encode_frame(65535, 49, {'id': identifier}))[3]['id'] == identifier
    with pytest.raises(ValueError):
        decode_frame(header + compressed[:-1])


def test_moscow_day_and_forward_text():
    lower, upper = date_bounds(date(2026, 9, 8), date(2026, 9, 8))
    assert upper - lower == 86400000
    p = message_to_post(SOURCE, {'id':117206699013512305,'time':lower,'type':'CHANNEL','link':{'type':'FORWARD','message':{'text':'Полный текст.\nВторая строка'}}})
    assert p.publication_date == date(2026, 9, 8)
    assert p.publication_time.hour == 0
    assert p.text == 'Полный текст.\nВторая строка'
    assert p.direct_url == 'https://max.ru/example/AaBm3yv_HHE'
    assert p.source == SOURCE.name


class FakeProtocol:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []
        self.page = SimpleNamespace(wait_for_timeout=lambda _: None)
    def request(self, op, payload):
        self.calls.append((op,payload))
        if op == 89:
            return {'chat':{'id':-123,'type':'CHANNEL'}}
        return {'messages':next(self.pages)}


def test_history_overlap_and_period_boundaries():
    start = date(2026,9,8)
    lower, upper = date_bounds(start,start)
    def m(i,t): return {'id':i,'time':t,'type':'CHANNEL','text':str(i)}
    protocol = FakeProtocol([[m(3,upper-1),m(2,lower+1000)], [m(2,lower+1000),m(1,lower),m(0,lower-1)]])
    posts = [p for batch in ApiCollector(protocol,0,0).batches(SOURCE,start,start) for p in batch]
    assert len(posts) == 3
    assert {p.text for p in posts} == {'1','2','3'}
    assert protocol.calls[2][1]['from'] == lower+1000


def test_malformed_response_not_silent_success():
    protocol = FakeProtocol([[{'id':1}]])
    with pytest.raises(ProtocolError):
        list(ApiCollector(protocol,0,0).batches(SOURCE,date(2026,9,8),date(2026,9,8)))


def test_cli_date_selection_and_sheet_url():
    args = parse_args(['--dry-run','--from-date','2026-09-05','--to-date','2026-09-08'])
    assert not args.write_sheet
    assert args.from_date == date(2026,9,5)
    assert spreadsheet_id('https://docs.google.com/spreadsheets/d/abc-123/edit?gid=1') == 'abc-123'
    with pytest.raises(SystemExit):
        parse_args(['--from-date','2026-09-09','--to-date','2026-09-08'])


def test_forward_keeps_comment_and_original():
    timestamp = date_bounds(date(2026,9,8),date(2026,9,8))[0]
    post = message_to_post(SOURCE, {'id':1,'time':timestamp,'type':'CHANNEL','text':'Комментарий',
        'link':{'type':'FORWARD','message':{'text':'Текст оригинала'}}})
    assert post.text == 'Комментарий\n\nТекст оригинала'
    assert post.title == 'Комментарий'
