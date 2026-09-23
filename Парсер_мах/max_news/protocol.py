"""Minimal read-only MAX protocol over the browser's authenticated WebSocket.

Wire reference: PronikFire/Max-API-Guide and Komet's MaxWebFraming.
No account token or received messages are written to disk.
"""
from __future__ import annotations

import base64
import struct

import lz4.block
import msgpack


MAX_BODY = 32 * 1024 * 1024


def _ext(code: int, value: bytes):
    if code == 1:
        return msgpack.unpackb(value, raw=False, strict_map_key=False, ext_hook=_ext)
    return msgpack.ExtType(code, value)


def encode_frame(seq: int, opcode: int, payload: dict) -> bytes:
    body = msgpack.packb(payload, use_bin_type=True)
    return struct.pack('>BBHHB', 10, 0, seq, opcode, 0) + len(body).to_bytes(3, 'big') + body


def decode_frame(raw: bytes) -> tuple[int, int, int, dict]:
    if len(raw) < 10 or raw[0] != 10:
        raise ValueError('Неизвестный формат ответа MAX')
    _, cmd, seq, opcode, compression = struct.unpack('>BBHHB', raw[:7])
    length = int.from_bytes(raw[7:10], 'big')
    if length > MAX_BODY or len(raw) != length + 10:
        raise ValueError('Неверная длина ответа MAX')
    body = raw[10:]
    if compression and body:
        body = lz4.block.decompress(body, uncompressed_size=min(MAX_BODY, length * (compression + 1) * 16))
    payload = msgpack.unpackb(body, raw=False, strict_map_key=False, ext_hook=_ext) if body else {}
    if not isinstance(payload, dict):
        raise ValueError('MAX вернул неожиданный тип ответа')
    return cmd, seq, opcode, payload


# Install before MAX starts. Reserve sequence numbers and consume only our replies;
# the web app continues to own login, reconnects and heartbeat. Use binary frames
# so 64-bit message IDs never pass through JavaScript Number/JSON.
SOCKET_HOOK = r"""
(() => {
  const Native = window.WebSocket;
  const sockets = [];
  const pending = new Map();
  let sequence = 65535;
  const to64 = bytes => {
    let s = '';
    for (let i=0; i<bytes.length; i+=8192)
      s += String.fromCharCode(...bytes.subarray(i,i+8192));
    return btoa(s);
  };
  window.WebSocket = class extends Native {
    constructor(...args) {
      super(...args);
      if (!String(args[0]).includes('/websocket')) return;
      sockets.push(this);
      this.addEventListener('message', event => {
        if (!(event.data instanceof ArrayBuffer)) return;
        const bytes = new Uint8Array(event.data);
        if (bytes.length < 10) return;
        const seq = (bytes[2]<<8)|bytes[3];
        const item = pending.get(seq);
        if (!item || item.socket !== this || bytes[1] === 0) return;
        event.stopImmediatePropagation();
        clearTimeout(item.timer);
        pending.delete(seq);
        item.resolve(to64(bytes));
      });
      this.addEventListener('close', () => {
        for (const [seq, item] of pending) if (item.socket === this) {
          clearTimeout(item.timer); pending.delete(seq);
          item.reject(new Error('Соединение MAX закрыто; повторите запуск'));
        }
      });
    }
    send(data) {
      if (data instanceof ArrayBuffer || ArrayBuffer.isView(data)) {
        const b = data instanceof ArrayBuffer ? new Uint8Array(data) : new Uint8Array(data.buffer,data.byteOffset,data.byteLength);
        if (b.length>=4 && pending.has((b[2]<<8)|b[3]))
          throw new Error('Конфликт последовательности MAX; повторите запуск');
      }
      return super.send(data);
    }
  };
  window.__maxNewsProtocol = {
    ready: () => sockets.some(s => s.readyState === 1),
    next: () => { if (sequence < 32768) throw new Error('Лимит запросов сессии; повторите запуск'); return sequence--; },
    request: encoded => new Promise((resolve,reject) => {
      const socket = sockets.filter(s=>s.readyState===1).at(-1);
      if (!socket) return reject(new Error('Нет активного соединения MAX'));
      const bytes = Uint8Array.from(atob(encoded), c=>c.charCodeAt(0));
      const seq = (bytes[2]<<8)|bytes[3];
      const timer=setTimeout(()=>{pending.delete(seq);reject(new Error('MAX не ответил за 30 секунд'));},30000);
      pending.set(seq,{socket,resolve,reject,timer});
      try { Native.prototype.send.call(socket,bytes); }
      catch(error) {clearTimeout(timer);pending.delete(seq);reject(error);}
    })
  };
})();
"""


class ProtocolError(RuntimeError):
    pass


class MaxProtocol:
    def __init__(self, page):
        self.page = page

    def request(self, opcode: int, payload: dict) -> dict:
        if opcode not in {49, 89}:
            raise ValueError('Разрешены только чтение истории и проверка ссылки')
        seq = self.page.evaluate('window.__maxNewsProtocol.next()')
        frame = encode_frame(seq, opcode, payload)
        response = self.page.evaluate('(data) => window.__maxNewsProtocol.request(data)', base64.b64encode(frame).decode())
        cmd, returned_seq, returned_opcode, result = decode_frame(base64.b64decode(response))
        if returned_seq != seq or returned_opcode != opcode:
            raise ProtocolError('Ответ MAX не соответствует запросу')
        if cmd != 1:
            code = str(result.get('error', 'неизвестная ошибка'))
            raise ProtocolError(f'MAX: операция {opcode}, {code}')
        return result
