/**
 * A small RFC 6455 WebSocket server for the FX bridge: text frames only, no
 * extensions, no subprotocols and no dependencies. It exists so the bridge runs with
 * the Node that ships on the machine and nothing else installed.
 *
 * Client frames must be masked. Frames, messages and the client count are bounded.
 */
import {createServer} from 'node:http';
import {createHash} from 'node:crypto';

const GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
export const MAX_MESSAGE_BYTES = 64 * 1024;
export const MAX_CLIENTS = 8;
const MAX_BACKLOG_BYTES = 1024 * 1024;

export function acceptKey(key) {return createHash('sha1').update(key + GUID).digest('base64');}

/** Build one unmasked server frame. */
export function frame(opcode, payload = Buffer.alloc(0)) {
  const length = payload.length;
  let head;
  if (length < 126) head = Buffer.from([0x80 | opcode, length]);
  else if (length < 65536) {head = Buffer.alloc(4); head[0] = 0x80 | opcode; head[1] = 126; head.writeUInt16BE(length, 2);}
  else {head = Buffer.alloc(10); head[0] = 0x80 | opcode; head[1] = 127; head.writeBigUInt64BE(BigInt(length), 2);}
  return Buffer.concat([head, payload]);
}

/** Incremental parser for client frames. `push(chunk)` returns decoded events or throws {code, reason}. */
export class FrameParser {
  constructor(maxBytes = MAX_MESSAGE_BYTES) {this.buffer = Buffer.alloc(0); this.maxBytes = maxBytes; this.fragments = []; this.fragmentBytes = 0; this.fragmentOpcode = 0;}
  push(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    const events = [];
    for (;;) {
      const buffer = this.buffer;
      if (buffer.length < 2) break;
      const fin = Boolean(buffer[0] & 0x80), reserved = buffer[0] & 0x70, opcode = buffer[0] & 0x0F, masked = Boolean(buffer[1] & 0x80);
      let length = buffer[1] & 0x7F, offset = 2;
      if (reserved) throw {code: 1002, reason: 'Reserved bits are not supported.'};
      if (!masked) throw {code: 1002, reason: 'Client frames must be masked.'};
      if (length === 126) {if (buffer.length < 4) break; length = buffer.readUInt16BE(2); offset = 4;}
      else if (length === 127) {
        if (buffer.length < 10) break;
        const big = buffer.readBigUInt64BE(2);
        if (big > BigInt(this.maxBytes)) throw {code: 1009, reason: 'Message too large.'};
        length = Number(big); offset = 10;
      }
      if (length > this.maxBytes) throw {code: 1009, reason: 'Message too large.'};
      if (buffer.length < offset + 4 + length) break;
      const mask = buffer.subarray(offset, offset + 4);
      const payload = Buffer.from(buffer.subarray(offset + 4, offset + 4 + length));
      for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i & 3];
      this.buffer = buffer.subarray(offset + 4 + length);
      if (opcode >= 8) {
        if (!fin || length > 125) throw {code: 1002, reason: 'Control frames must be short and unfragmented.'};
        events.push({opcode, payload});
        continue;
      }
      if (opcode === 0) {
        if (!this.fragments.length) throw {code: 1002, reason: 'Unexpected continuation frame.'};
      } else {
        if (this.fragments.length) throw {code: 1002, reason: 'Interleaved data frames are not supported.'};
        this.fragmentOpcode = opcode;
      }
      this.fragments.push(payload); this.fragmentBytes += payload.length;
      if (this.fragmentBytes > this.maxBytes) throw {code: 1009, reason: 'Message too large.'};
      if (fin) {
        events.push({opcode: this.fragmentOpcode, payload: Buffer.concat(this.fragments)});
        this.fragments = []; this.fragmentBytes = 0;
      }
    }
    return events;
  }
}

const LOOPBACK = new Set(['localhost', '127.0.0.1', '[::1]']);
/** Browsers always send Origin; non-browser clients (TouchDesigner, scripts) send none. */
export function originAllowed(origin, allowed = []) {
  if (origin === undefined) return true;
  if (allowed.includes(origin)) return true;
  try {
    const url = new URL(origin);
    return (url.protocol === 'http:' || url.protocol === 'https:') && LOOPBACK.has(url.hostname);
  } catch {return false;}
}

/**
 * Listen for WebSocket clients.
 * options: {host, port, allowedOrigins, onMessage(text, client), onOpen(client), onClose(client)}
 * Returns {server, port, clients, broadcast(text) -> sent count, close()}.
 */
export async function listen({host = '127.0.0.1', port = 0, allowedOrigins = [], onMessage = () => {}, onOpen = () => {}, onClose = () => {}} = {}) {
  const clients = new Set();
  const server = createServer((request, response) => {
    response.writeHead(426, {'Content-Type': 'text/plain', Upgrade: 'websocket'});
    response.end('FX bridge: connect with a WebSocket.\n');
  });
  server.on('upgrade', (request, socket, head) => {
    const refuse = (status, text) => {socket.end(`HTTP/1.1 ${status} ${text}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`);};
    const key = request.headers['sec-websocket-key'];
    if (request.method !== 'GET' || String(request.headers.upgrade).toLowerCase() !== 'websocket' || !key || request.headers['sec-websocket-version'] !== '13') return refuse(400, 'Bad Request');
    if (!originAllowed(request.headers.origin, allowedOrigins)) return refuse(403, 'Forbidden');
    if (clients.size >= MAX_CLIENTS) return refuse(503, 'Service Unavailable');
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${acceptKey(key)}\r\n\r\n`);
    socket.setNoDelay(true);
    const parser = new FrameParser();
    const client = {socket, origin: request.headers.origin ?? null, closed: false,
      send(text) {
        if (client.closed || socket.writableLength > MAX_BACKLOG_BYTES) return false;  // a stalled client loses frames rather than growing memory
        socket.write(frame(1, Buffer.from(text, 'utf8')));
        return true;
      },
      close(code = 1000, reason = '') {
        if (client.closed) return;
        client.closed = true;
        const body = Buffer.alloc(2 + Buffer.byteLength(reason)); body.writeUInt16BE(code); body.write(reason, 2);
        socket.end(frame(8, body));
      }};
    clients.add(client);
    onOpen(client);
    const receive = chunk => {
      if (client.closed) return;
      let events;
      try {events = parser.push(chunk);} catch (error) {client.close(error.code ?? 1002, error.reason ?? 'Protocol error'); return;}
      for (const event of events) {
        if (event.opcode === 1) {
          let text;
          try {text = new TextDecoder('utf-8', {fatal: true}).decode(event.payload);} catch {client.close(1007, 'Invalid UTF-8'); return;}
          onMessage(text, client);
        } else if (event.opcode === 8) {client.close(1000); return;}
        else if (event.opcode === 9) socket.write(frame(10, event.payload));
        else if (event.opcode === 2) {client.close(1003, 'Text only'); return;}
      }
    };
    socket.on('data', receive);
    if (head?.length) receive(head);  // bytes that arrived in the same packet as the handshake
    const gone = () => {if (clients.delete(client)) {client.closed = true; onClose(client);}};
    socket.on('close', gone); socket.on('error', gone);
  });
  await new Promise((resolve, reject) => {server.once('error', reject); server.listen(port, host, resolve);});
  return {
    server, clients, port: server.address().port,
    broadcast(text) {let sent = 0; for (const client of clients) if (client.send(text)) sent++; return sent;},
    close() {
      for (const client of clients) {client.close(1001, 'Bridge stopping'); client.socket.destroy();}
      return new Promise(resolve => server.close(() => resolve()));
    },
  };
}
