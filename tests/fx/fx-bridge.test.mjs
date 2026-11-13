import test from 'node:test';
import assert from 'node:assert/strict';
import {createSocket} from 'node:dgram';
import {connect as tcpConnect} from 'node:net';
import {randomBytes} from 'node:crypto';
import {startBridge, parseWireMessage, toWireMessage} from '../../tools/fx-bridge/bridge.mjs';
import {FrameParser, frame, acceptKey, originAllowed, MAX_CLIENTS} from '../../tools/fx-bridge/ws-server.mjs';
import {encodeMessage, encodeBundle, decodePacket} from '../../tools/fx-bridge/osc-codec.mjs';
import {oscSession, oscControl, freshPatch, CATALOG_BY_TYPE} from './runtime.mjs';

const once = (emitter, event) => new Promise(resolve => emitter.once(event, resolve));
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate, ms = 3000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {if (predicate()) return; await wait(10);}
  assert.fail('timed out waiting for the condition');
}

/** A stand-in for TouchDesigner: one UDP socket that listens and can send to the bridge. */
async function fakeTouchDesigner() {
  const socket = createSocket('udp4');
  socket.bind(0, '127.0.0.1');
  await once(socket, 'listening');
  const received = [];
  socket.on('message', packet => received.push(...decodePacket(new Uint8Array(packet))));
  return {socket, received, port: socket.address().port, send: (packet, port) => socket.send(packet, port, '127.0.0.1'), close: () => new Promise(resolve => socket.close(resolve))};
}

async function rig(options = {}) {
  const td = await fakeTouchDesigner();
  const logs = [];
  const bridge = await startBridge({wsPort: 0, oscPort: 0, tdPort: td.port, log: text => logs.push(text), ...options});
  const open = async (init) => {
    const ws = new WebSocket(`ws://127.0.0.1:${bridge.wsPort}`, init);
    const messages = [];
    ws.onmessage = event => messages.push(JSON.parse(event.data));
    await new Promise((resolve, reject) => {ws.onopen = resolve; ws.onerror = () => reject(Error('connection refused'));});
    return {ws, messages};
  };
  return {td, bridge, logs, open, async close() {await bridge.close(); await td.close();}};
}

test('the workbench side reaches TouchDesigner as OSC, and TouchDesigner replies come back as JSON', async () => {
  const {td, bridge, open, close} = await rig();
  try {
    const {ws, messages} = await open();
    await until(() => messages.length >= 1);
    assert.equal(messages[0].bridge, 'fx-td');
    assert.equal(messages[0].version, 1);
    assert.equal(messages[0].touchdesigner.port, td.port);

    ws.send(JSON.stringify({address: '/fx/node/fx-1/intensity', args: [0.25]}));
    ws.send(JSON.stringify({address: '/fx/selected', args: ['fx-1']}));
    ws.send(JSON.stringify({address: '/fx/transport/play', args: [1, true, null]}));
    await until(() => td.received.length === 3);
    assert.deepEqual(td.received, [
      {address: '/fx/node/fx-1/intensity', args: [0.25]},
      {address: '/fx/selected', args: ['fx-1']},
      {address: '/fx/transport/play', args: [1, true, null]},
    ]);

    td.send(encodeMessage('/fx/selected/knob/0', [0.5]), bridge.oscPort);
    td.send(encodeBundle([{address: '/fx/select/next', args: []}, {address: '/fx/transport/loop', args: [false]}]), bridge.oscPort);
    await until(() => messages.length >= 4);
    assert.deepEqual(messages.slice(1), [
      {address: '/fx/selected/knob/0', args: [0.5]},
      {address: '/fx/select/next', args: []},
      {address: '/fx/transport/loop', args: [false]},
    ]);
    assert.deepEqual({to: bridge.stats.toTouchDesigner, from: bridge.stats.fromTouchDesigner}, {to: 3, from: 3});
    ws.close();
  } finally {await close();}
});

test('every connected workbench hears TouchDesigner', async () => {
  const {td, bridge, open, close} = await rig();
  try {
    const a = await open(), b = await open();
    await until(() => bridge.stats.clients === 2);
    td.send(encodeMessage('/fx/sync', []), bridge.oscPort);
    await until(() => a.messages.length === 2 && b.messages.length === 2);
    assert.equal(a.messages[1].address, '/fx/sync');
    a.ws.close(); b.ws.close();
  } finally {await close();}
});

test('bad input on either side is dropped, counted and never stops the bridge', async () => {
  const {td, bridge, open, logs, close} = await rig();
  try {
    const {ws, messages} = await open();
    for (const bad of ['nope', '[]', '{"address":"fx/x"}', '{"address":"/fx/x y"}', '{"address":"/fx/x","args":[{"a":1}]}', '{"address":"/fx/x","args":"1"}', `{"address":"/fx/${'a'.repeat(600)}"}`]) ws.send(bad);
    td.send(Buffer.from([1, 2, 3]), bridge.oscPort);
    td.send(Buffer.from('/x\0\0,f\0\0\0\0'), bridge.oscPort);
    td.send(encodeMessage('/blob', [Uint8Array.from([1])]), bridge.oscPort);
    await until(() => bridge.stats.dropped === 10);
    ws.send(JSON.stringify({address: '/fx/still/works', args: [1]}));
    await until(() => td.received.length === 1);
    assert.equal(messages.length, 1, 'only the greeting reached the workbench');
    assert.ok(logs.some(line => line.includes('dropped OSC packet')));
    ws.close();
  } finally {await close();}
});

test('wire messages are validated strictly', () => {
  assert.deepEqual(parseWireMessage('{"address":"/a","args":[1,"x",true,null]}'), {address: '/a', args: [1, 'x', true, null]});
  assert.deepEqual(parseWireMessage('{"address":"/a"}'), {address: '/a', args: []});
  assert.throws(() => parseWireMessage('{"address":"/a","args":[1e999]}'), /finite|Not JSON/);
  assert.throws(() => parseWireMessage(`{"address":"/a","args":["${'s'.repeat(2000)}"]}`), /short strings/);
  assert.throws(() => parseWireMessage('{"address":"/a","args":["a\\u0000b"]}'), /short strings/);
  assert.equal(toWireMessage({address: '/a', args: [Infinity, 1]}), '{"address":"/a","args":[null,1]}');
  assert.equal(toWireMessage({address: '/a', args: [Uint8Array.of(1)]}), null);
});

test('browsers are only accepted from loopback pages or from origins that were named', async () => {
  assert.equal(originAllowed(undefined), true, 'non-browser clients send no Origin');
  assert.equal(originAllowed('http://localhost:8080'), true);
  assert.equal(originAllowed('https://127.0.0.1'), true);
  assert.equal(originAllowed('http://[::1]:3000'), true);
  assert.equal(originAllowed('https://evil.example'), false);
  assert.equal(originAllowed('null'), false);
  assert.equal(originAllowed('http://localhost.evil.example'), false);
  assert.equal(originAllowed('https://studio.example', ['https://studio.example']), true);
  assert.equal(originAllowed('https://studio.example:444', ['https://studio.example']), false);

  const {bridge, close} = await rig({allowedOrigins: ['https://studio.example']});
  try {
    const attempt = origin => new Promise(resolve => {
      const socket = tcpConnect(bridge.wsPort, '127.0.0.1');
      let text = '';
      socket.on('data', chunk => {text += chunk; if (text.includes('\r\n')) {resolve(text.split('\r\n')[0]); socket.destroy();}});
      socket.on('connect', () => socket.write(`GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: ${randomBytes(16).toString('base64')}\r\n${origin ? `Origin: ${origin}\r\n` : ''}\r\n`));
      setTimeout(() => socket.destroy(), 500);
    });
    assert.match(await attempt('https://evil.example'), /403/);
    assert.match(await attempt('null'), /403/);
    assert.match(await attempt('https://studio.example'), /101/);
    assert.match(await attempt('http://localhost:5173'), /101/);
    assert.match(await attempt(null), /101/);
  } finally {await close();}
});

test('plain HTTP requests and malformed upgrades are refused', async () => {
  const {bridge, close} = await rig();
  try {
    const plain = await fetch(`http://127.0.0.1:${bridge.wsPort}/`);
    assert.equal(plain.status, 426);
    const raw = request => new Promise(resolve => {
      const socket = tcpConnect(bridge.wsPort, '127.0.0.1');
      let text = '';
      socket.on('data', chunk => {text += chunk; if (text.includes('\r\n')) {resolve(text.split('\r\n')[0]); socket.destroy();}});
      socket.on('connect', () => socket.write(request));
      setTimeout(() => socket.destroy(), 500);
    });
    assert.match(await raw('GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 8\r\nSec-WebSocket-Key: abc\r\n\r\n'), /400/);
    assert.match(await raw('GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n\r\n'), /400/);
  } finally {await close();}
});

test('the number of simultaneous workbenches is bounded', async () => {
  const {bridge, open, close} = await rig();
  try {
    const sockets = [];
    for (let i = 0; i < MAX_CLIENTS; i++) sockets.push(await open());
    await assert.rejects(open(), /refused/);
    sockets[0].ws.close();
    await until(() => bridge.stats.clients === MAX_CLIENTS - 1);
    sockets.push(await open());
    for (const {ws} of sockets) ws.close();
  } finally {await close();}
});

function masked(opcode, payload, {fin = true} = {}) {
  const body = Buffer.from(payload), mask = randomBytes(4);
  const head = body.length < 126 ? Buffer.from([(fin ? 0x80 : 0) | opcode, 0x80 | body.length])
    : body.length < 65536 ? Buffer.from([(fin ? 0x80 : 0) | opcode, 0x80 | 126, body.length >> 8, body.length & 255])
    : (() => {const h = Buffer.alloc(10); h[0] = (fin ? 0x80 : 0) | opcode; h[1] = 0x80 | 127; h.writeBigUInt64BE(BigInt(body.length), 2); return h;})();
  return Buffer.concat([head, mask, Buffer.from(body.map((byte, i) => byte ^ mask[i & 3]))]);
}

test('the frame parser handles fragments, split chunks, control frames and every length form', () => {
  const parser = new FrameParser(200000);
  const medium = 'm'.repeat(300), large = 'L'.repeat(70000);
  const bytes = Buffer.concat([masked(1, 'hi'), masked(1, 'he', {fin: false}), masked(9, 'p'), masked(0, 'llo'), masked(1, medium), masked(1, large), masked(8, '')]);
  const events = [];
  for (let i = 0; i < bytes.length; i += 7) events.push(...parser.push(bytes.subarray(i, i + 7)));
  assert.deepEqual(events.map(e => [e.opcode, e.payload.length]), [[1, 2], [9, 1], [1, 5], [1, 300], [1, 70000], [8, 0]]);
  assert.equal(events[2].payload.toString(), 'hello');
});

test('the frame parser refuses protocol violations and oversized messages', () => {
  const code = bytes => {try {new FrameParser(1000).push(bytes); return null;} catch (error) {return error.code;}};
  assert.equal(code(Buffer.from([0x81, 0x01, 0x41])), 1002, 'unmasked');
  assert.equal(code(Buffer.from([0xC1, 0x80, 0, 0, 0, 0])), 1002, 'reserved bit');
  assert.equal(code(masked(0, 'x')), 1002, 'stray continuation');
  assert.equal(code(Buffer.concat([masked(1, 'a', {fin: false}), masked(1, 'b')])), 1002, 'interleaved data');
  assert.equal(code(masked(9, 'x'.repeat(126))), 1002, 'long ping');
  assert.equal(code(masked(1, 'x'.repeat(1001))), 1009);
  assert.equal(code(Buffer.concat([masked(1, 'x'.repeat(600), {fin: false}), masked(0, 'x'.repeat(600))])), 1009, 'fragments add up');
  const hugeClaim = Buffer.alloc(10); hugeClaim[0] = 0x81; hugeClaim[1] = 0xFF; hugeClaim.writeBigUInt64BE(2n ** 40n, 2);
  assert.equal(code(hugeClaim), 1009, 'a 1 TB length claim is refused before any buffering');
});

test('the server frame encoder and accept key follow RFC 6455', () => {
  assert.equal(acceptKey('dGhlIHNhbXBsZSBub25jZQ=='), 's3pPLMBiTxaQ9kYGzzhZRbK+xOo=');  // RFC 6455 section 1.3
  assert.deepEqual([...frame(1, Buffer.from('Hi'))], [0x81, 2, 0x48, 0x69]);
  assert.deepEqual([...frame(1, Buffer.alloc(200)).subarray(0, 4)], [0x81, 126, 0, 200]);
  assert.deepEqual([...frame(1, Buffer.alloc(70000)).subarray(0, 10)], [0x81, 127, 0, 0, 0, 0, 0, 1, 17, 112]);
});

test('an oversized or non-text frame closes only that client', async () => {
  const {bridge, td, open, close} = await rig();
  try {
    const good = await open();
    const bad = await new Promise((resolve, reject) => {
      const socket = tcpConnect(bridge.wsPort, '127.0.0.1');
      let text = Buffer.alloc(0);
      socket.on('data', chunk => {text = Buffer.concat([text, chunk]);});
      socket.on('close', () => resolve(text));
      socket.on('connect', () => {
        socket.write(`GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: ${randomBytes(16).toString('base64')}\r\n\r\n`);
        setTimeout(() => socket.write(masked(1, 'x'.repeat(70000))), 50);
      });
      setTimeout(() => {socket.destroy(); reject(Error('server did not close'));}, 2000);
    });
    assert.ok(bad.includes(Buffer.from([0x88])), 'a close frame was sent');
    assert.ok(bad.includes(Buffer.from([0x03, 0xF1])), 'close code 1009');
    good.ws.send(JSON.stringify({address: '/fx/ok', args: [1]}));
    await until(() => td.received.length === 1);
    good.ws.close();
  } finally {await close();}
});

test('the page-side session and the bridge close the loop with a TouchDesigner stand-in', async () => {
  const {td, bridge, close} = await rig();
  try {
    const patch = freshPatch('delay');
    const node = patch.nodes.find(n => CATALOG_BY_TYPE[n.type]);
    node.solo = false; node.bypass = false;
    const values = new Map(), applied = [];
    const view = {
      nodes: [node], selectedId: node.id, duration: 12, time: 0,
      current: (id, path) => values.has(`${id}|${path}`) ? values.get(`${id}|${path}`) : path === 'bypass' || path === 'solo' ? Boolean(node[path]) : path.startsWith('params.') ? node.params[path.slice(7)] : node[path],
      global: () => false,
    };
    const host = {getView: () => view, apply(action) {applied.push(action); values.set(`${action.nodeId}|${action.path}`, action.value);}, gesture() {}, select() {}, selectNode() {}};
    const session = new oscSession.OscSession(host, {});
    session.connect(`ws://127.0.0.1:${bridge.wsPort}`);
    try {
      // Opening the link sends the full state to TouchDesigner.
      await until(() => td.received.some(m => m.address === '/fx/selected'));
      assert.ok(td.received.some(m => m.address === oscControl.nodeAddress(node.id, 'intensity')));
      // TouchDesigner moves a knob; the rack follows and the new value is reported back.
      td.received.length = 0;
      td.send(encodeMessage('/fx/selected/knob/0', [0.5]), bridge.oscPort);
      await until(() => applied.length === 1);
      assert.deepEqual(applied[0], {nodeId: node.id, path: 'intensity', value: 0.5, binding: 'osc'});
      await until(() => td.received.some(m => m.address === oscControl.nodeAddress(node.id, 'intensity') && m.args[0] === 0.5));
      // TouchDesigner asks for everything again.
      td.received.length = 0;
      td.send(encodeMessage('/fx/sync', []), bridge.oscPort);
      await until(() => td.received.some(m => m.address === '/fx/count'));
    } finally {session.disconnect();}
    await until(() => bridge.stats.clients === 0);
  } finally {await close();}
});

test('a frame sent in the same packet as the handshake is not lost', async () => {
  const {bridge, td, close} = await rig();
  try {
    const socket = tcpConnect(bridge.wsPort, '127.0.0.1');
    await once(socket, 'connect');
    const handshake = `GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: ${randomBytes(16).toString('base64')}\r\n\r\n`;
    socket.write(Buffer.concat([Buffer.from(handshake), masked(1, JSON.stringify({address: '/fx/early', args: [1]}))]));
    await until(() => td.received.length === 1);
    assert.equal(td.received[0].address, '/fx/early');
    socket.destroy();
  } finally {await close();}
});
